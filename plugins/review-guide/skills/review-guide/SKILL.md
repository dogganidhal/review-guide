---
name: review-guide
description: Builds a guided review page for one or more GitHub pull requests. Stacked PRs are reviewed as one change (their final state), with a guide an Opus agent writes for the change (its own structure, pictures and reading order, every claim linked to its code), a browsable file tree with syntax-highlighted diffs, and findings from reviewers a director agent picks for the stack, each checked by another agent that tried to disprove it. Each comment is routed to the PR that wrote its line, and the reviewer posts from a local page through their own gh login.
when_to_use: The user asks for help reviewing PRs, wants a review guide or walkthrough, shares PR links or a Slack thread asking for a review, or wants to post review comments on several PRs at once.
argument-hint: [quick|balanced|deep] <PR URLs, owner/repo#N, or a Slack thread link>
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*), Read
model: sonnet # reads Slack threads for PR links and turns the runner's status and finding lines into a report for the reviewer
effort: medium
metadata:
  category: engineering
---

# Review guide

The work happens in scripts and agents that `rerun.py` runs headless, the same way the page's Rerun button does. You start them and relay what they report; you do not write the guide yourself.

## 1. Collect the PRs

Take PR references from the arguments: `https://github.com/<owner>/<repo>/pull/<n>` or `<owner>/<repo>#<n>`. If the user gave a Slack thread, read it with the Slack tools and pull out the PR links. All PRs must be in one repository; for several repositories, run the skill once per repository.

Take the effort from the arguments too: `quick` (blockers only, the fastest), `balanced` (the default) or `deep` (reads callers and tests, slower). Words like "quick look" or "go deep" count.

## 2. Send them to the review server

Before sending, check whether the repository could hold patient or personal data in fixtures or test data. If it could, say so and ask the user to confirm.

Run from the user's clone of the repository when the current directory is one; the fetch runs there and never touches the working tree. Otherwise it uses a blobless clone under `~/.cache/review-guide/repos/`, authenticated through `gh`.

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/serve.py" open --wait --effort <effort> [--focus "<focus>"] <pr-ref>...
```

This reuses the one review-guide server, or starts it, and adds a round to the review of these PRs. The same PRs again add a round to the same review, carrying the user's drafts over. It opens the review in the browser and prints:
- `review <url>` and `inbox <url>`;
- `status <text>` lines as the round moves (fetching, writing the guide and reviewing, checking findings);
- `done <stack>: <summary>` per stack;
- `warning: <text>` for stack problems, such as a PR built on an older version of the one below it;
- one `finding #<pr> <severity> <axis> <status>: <title>` line per finding.

Run it in the background and wait for it to exit; relay the status lines only if the user asks.

The page opens on the file tree and diffs right after the fetch. Each finished part lands on it on its own: the guide, the reviewers' findings, and one fact check per finding. At `balanced` and `deep` a director agent first picks the stack's reviewers (1 to 8, each with its own charge and model), while the guide author writes the guide; `quick` keeps the structured guide and the three built-in reviewers (correctness, standards, spec).

On a non-zero exit, the last line names `runner.log` and `agents.log` in the round directory. `agents.log` has one line per agent with its reply, turns, duration, cost and denied calls. Read them and report which step failed.

## 3. Report

Tell the user, in a few lines:

- the review URL, and that nothing is posted until they pick verdicts and press "Post" in the "Your review" tab;
- per stack: the one or two findings that matter most, and how many go to each PR;
- findings the fact check disproved or could not confirm, any skipped axis (from the `done` lines), and the warnings;
- that every review lives in the inbox at the `inbox` URL. "Finish review" files a review there. The server keeps running until they press "Stop server" in the inbox.

Never post a review yourself. The reviewer posts from the page.

## Notes

- Data leaves the machine only through `gh` (GitHub) and the model.
- The page warns, and the server refuses to post, when a PR receives new commits after the guide was built. The page's "Rerun the guide" button, with an effort picker, adds a round the same way this skill does, and the open page moves to it.
- Everything is saved under `~/.cache/review-guide/reviews/`, one folder per review and one per round: the guides, the findings, what was posted, and the page state (drafts, verdicts, viewed files). Earlier rounds stay readable from the round picker.
