---
name: review-guide
description: Builds a guided review page for one or more GitHub pull requests. Stacked PRs are reviewed as one change (their final state), with a guide shaped to the change (flows, before and after, tables, a reading order), a browsable file tree with syntax-highlighted diffs, and findings from three reviewers (correctness, standards, spec) that a fourth agent tried to disprove. Each comment is routed to the PR that wrote its line, and the reviewer posts from a local page through their own gh login.
when_to_use: The user asks for help reviewing PRs, wants a review guide or walkthrough, shares PR links or a Slack thread asking for a review, or wants to post review comments on several PRs at once.
argument-hint: [quick|balanced|deep] <PR URLs, owner/repo#N, or a Slack thread link>
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*), Read
---

# Review guide

The work happens in scripts and agents that `rerun.py` runs headless, the same way the page's Rerun button does. You start them and relay what they report; you do not write the guide yourself.

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

## 4. Run the agents

Start the runner in the background on the work directory, with the effort and, when the user stated one, a focus sentence:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/rerun.py" <W> --no-fetch --effort <effort> [--focus "<focus>"]
```

Per stack it runs the guide writer and three reviewers (correctness, standards, spec) in parallel, then one verifier per finding, at most six at a time. `deep` runs them on Opus. The page shows a live strip of each stage, the tool calls of the agents still working, and "3 of 7 checked" for the fact check. Each finished part lands on the page on its own.

The runner prints `status <text>` as stages start, `done <stack>: <summary>` per stack, and exits 0 when every guide is written. Relay the status lines only if the user asks; wait for the exit. On a non-zero exit, read `<W>/agents.log` (one line per agent with its reply, turns, duration, cost and denied calls) and report which agent failed.

## 6. Report

Tell the user, in a few lines:

- the URL, and that nothing is posted until they pick verdicts and press "Post" in the "Your review" tab;
- per stack: the one or two findings that matter most, and how many go to each PR;
- findings the verifiers refuted or could not verify, and any axis that was skipped (the `done` lines give the counts);
- that the server runs until they press Close on the page, or `pkill -f "review-guide.*serve.py"`.

Never post a review yourself. The reviewer posts from the page.

## Notes

- Data leaves the machine only through `gh` (GitHub) and the model. Before fetching, check whether the repository could hold patient or personal data in fixtures or test data. If it could, say so and ask the user to confirm.
- The page warns, and the server refuses, when a PR receives new commits after the guide was built. The page has a "Rerun the guide" button with an effort picker: `serve.py` runs `rerun.py`, which fetches again and runs the same agents through `claude -p`, and the open page moves to the new guide. Rerunning the skill does the same.
- The work directory holds a cache per PR set and head SHA under `~/.cache/review-guide/`. A rerun on unchanged heads reuses its files; delete the directory to start clean.
