---
name: review-guide
description: Builds a guided review page for one or more GitHub pull requests. Stacked PRs are reviewed as one change (their final state), with a guide shaped to the change (flows, before and after, tables, a reading order), a browsable file tree with syntax-highlighted diffs, and findings from three reviewers (correctness, standards, spec) that a fourth agent tried to disprove. Each comment is routed to the PR that wrote its line, and the reviewer posts from a local page through their own gh login.
when_to_use: The user asks for help reviewing PRs, wants a review guide or walkthrough, shares PR links or a Slack thread asking for a review, or wants to post review comments on several PRs at once.
argument-hint: [quick|balanced|deep] <PR URLs, owner/repo#N, or a Slack thread link>
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*), Read, Agent
---

# Review guide

The work happens in three scripts and two agents. You coordinate and relay; you do not write the guide yourself.

## 1. Collect the PRs

Take PR references from the arguments: `https://github.com/<owner>/<repo>/pull/<n>` or `<owner>/<repo>#<n>`. If the user gave a Slack thread, read it with the Slack tools and pull out the PR links. All PRs must be in one repository; for several repositories, run the skill once per repository.

Take the effort from the arguments too: `quick` (blockers only, the fastest), `balanced` (the default) or `deep` (reads callers and tests, slower). Words like "quick look" or "go deep" count.

## 2. Fetch

Run from the user's clone of the repository when the current directory is one. Otherwise `fetch.py` keeps a blobless clone under `~/.cache/review-guide/repos/`, authenticated through `gh`.

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/fetch.py" --effort <effort> <pr-ref>...
```

The last line is the work directory `W`. Earlier lines list the stacks (`stack 3564-3565: #3564 -> #3565`) and warnings, such as a PR built on an older version of the one below it. Relay the warnings to the user at the end.

## 3. Open the page now

Build the page without guides and start the server, so the reviewer can browse the file tree and diffs while the agents work. The page reloads itself each time a later build lands.

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/build.py" <W> --partial
python3 "${CLAUDE_SKILL_DIR}/scripts/serve.py" <W>    # in the background; open the URL it prints
```

## 4. Write the guide and review, in parallel

For each stack, spawn four agents in a single message, for all stacks at once:

- one `review-guide:guide-writer`;
- three `review-guide:reviewer`, one per axis: `correctness`, `standards`, `spec`.

Each prompt is:

```
Work directory: <W>
Stack: <stack id>
Effort: <effort>
Axis: <axis>          (reviewers only)
```

For `deep`, pass `model: "opus"` to each Agent call; `quick` and `balanced` keep the agents' own model. Add one sentence if the user stated a focus (for example "focus on the migration").

The writer replies `<id> ok: …`, each reviewer `<id> <axis> ok: …`, or `failed: <reason>`. As each one finishes, rerun `build.py <W> --partial` so its part shows up. Rerun a failed agent once. If a reviewer fails twice, write `{"skipped": "This reviewer failed.", "findings": []}` to `<W>/<stack>/review-<axis>.json` so the page stops waiting, and report it.

## 5. Verify the findings

When the three reviewers of a stack are done, spawn a `review-guide:comment-verifier` for it with the same prompt, without `Axis`; do not wait for other stacks. When a verifier finishes, rerun `build.py <W> --partial`. When all are done, run `build.py <W>` once without `--partial` to confirm every stack is complete.

## 6. Report

Tell the user, in a few lines:

- the URL, and that nothing is posted until they pick verdicts and press "Post" in the "Your review" tab;
- per stack: the one or two findings that matter most, and how many go to each PR;
- findings the verifier refuted or could not verify, and any axis that was skipped;
- that the server runs until they press Close on the page, or `pkill -f "review-guide.*serve.py"`.

Never post a review yourself. The reviewer posts from the page.

## Notes

- Data leaves the machine only through `gh` (GitHub) and the model. Before fetching, check whether the repository could hold patient or personal data in fixtures or test data. If it could, say so and ask the user to confirm.
- The page warns, and the server refuses, when a PR receives new commits after the guide was built. The page has a "Rerun the guide" button with an effort picker: `serve.py` runs `rerun.py`, which fetches again and runs the same agents through `claude -p`, and the open page moves to the new guide. Rerunning the skill does the same.
- The work directory holds a cache per PR set and head SHA under `~/.cache/review-guide/`. A rerun on unchanged heads reuses its files; delete the directory to start clean.
