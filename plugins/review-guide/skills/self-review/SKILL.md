---
name: self-review
description: Opens a guided review page on the work in progress in this session, the local branch stack and its uncommitted changes, with a guide written for the change and findings from reviewers picked for it, each checked by another agent that tried to disprove it. The user reviews it in the browser and presses "Send to the agent"; the feedback comes back to this session as file:line comments to act on. Nothing goes to GitHub.
when_to_use: The user, in the session implementing a change, wants to review that work before or alongside opening PRs, asks for a self-review or a review of "what you wrote", or wants to give the agent line-by-line feedback from the review page.
argument-hint: [quick|balanced|deep] [--branch <top branch>] [--base <ref>] [focus]
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/../review-guide/scripts/*), Read
---

# Self-review

The user reviews what this session wrote, on the same page as a PR review, and sends the feedback back here. You start the review, wait for the feedback, then act on it.

## 1. Start the review

Take the effort from the arguments (`quick`, `balanced` by default, `deep`), a `--branch` if the user named the top branch, a `--base` if the stack sits on a branch that is not theirs to review (someone else's unmerged PR, say), and any other words as a focus sentence.

Before starting, check whether the repository could hold patient or personal data in fixtures or test data. If it could, say so and ask the user to confirm. Code goes to the model only; nothing goes to GitHub.

Run this from the repository you are working in, in the background:

```bash
python3 "${CLAUDE_SKILL_DIR}/../review-guide/scripts/serve.py" open --self --wait-feedback --effort <effort> [--branch <branch>] [--base <ref>] [--focus "<focus>"]
```

It reviews the local branch stack: each local branch between the default branch (or `--base`) and the current one is a layer, and uncommitted changes are a top layer. Your index, working tree and branches are not touched. Running it again on the same branch adds a round to the same review: the user's drafts carry over, and what changed since the last round is marked "new".

When it prints `review <url>`, tell the user, in two lines:
- the URL;
- that they can pick and edit comments there, then press "Send to the agent" in "Your review".

Then end your turn. Do not poll the command; you are woken when it exits.

If it prints feedback right away, before any `review` line, that feedback was sent earlier and never delivered. Act on it as in step 2; no round was started.

## 2. Act on the feedback

When the command exits, its output ends with the feedback:
- one section per layer (a branch, or "Uncommitted changes"), with the user's verdict and summary;
- one `- path:line (severity): comment` line per comment, with `at:` quoting the line it was written on.

Treat it as review comments from the user on your work:
- **Comments:** address every comment. A line may have moved if you edited the file since; find it by the quoted text.
- **Questions:** answer each one in your reply. Change code only when the answer calls for it.
- **"looks good" layers:** no comment there means no change.
- **Checks:** run the project's checks for what you changed.
- **Commits:** do not commit, amend or push unless the user asked for that in this session.

End with a short list: each comment and what you did about it, and the checks you ran. Then say that `/review-guide:self-review` again builds the next round, with their drafts carried over and your changes marked.

If the output says the review was finished without feedback, say so and stop. On a non-zero exit, the last line names `runner.log` and `agents.log`; read them and report which step failed.
