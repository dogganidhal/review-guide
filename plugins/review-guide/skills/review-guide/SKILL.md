---
name: review-guide
description: Builds a guided review page for one or more GitHub pull requests. Stacked PRs are reviewed as one change (their final state), with an architecture map, data flows, a short reading order, a browsable file tree with syntax-highlighted diffs, and drafted comments that a second agent tried to disprove. Each comment is routed to the PR that wrote its line, and the reviewer posts from a local page through their own gh login.
when_to_use: The user asks for help reviewing PRs, wants a review guide or walkthrough, shares PR links or a Slack thread asking for a review, or wants to post review comments on several PRs at once.
argument-hint: <PR URLs, owner/repo#N, or a Slack thread link>
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*), Read, Agent
---

# Review guide

The work happens in three scripts and two agents. You coordinate and relay; you do not write the guide yourself.

## 1. Collect the PRs

Take PR references from the arguments: `https://github.com/<owner>/<repo>/pull/<n>` or `<owner>/<repo>#<n>`. If the user gave a Slack thread, read it with the Slack tools and pull out the PR links. All PRs must be in one repository; for several repositories, run the skill once per repository.

## 2. Fetch

Run from the user's clone of the repository when the current directory is one. Otherwise `fetch.py` keeps a blobless clone under `~/.cache/review-guide/repos/`, authenticated through `gh`.

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/fetch.py" <pr-ref>...
```

The last line is the work directory `W`. Earlier lines list the stacks (`stack 3564-3565: #3564 -> #3565`) and warnings, such as a PR built on an older version of the one below it. Relay the warnings to the user at the end.

## 3. Open the page now

Build the page without guides and start the server, so the reviewer can browse the file tree and diffs while the agents work. The page reloads itself each time a later build lands.

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/build.py" <W> --partial
python3 "${CLAUDE_SKILL_DIR}/scripts/serve.py" <W>    # in the background; open the URL it prints
```

## 4. Write the guides

Spawn one `review-guide:guide-writer` agent per stack, all in a single message so they run in parallel. Each prompt is:

```
Work directory: <W>
Stack: <stack id>
```

Add one sentence if the user stated a focus (for example "focus on the migration"). Each replies `<id> ok: …` or `<id> failed: <reason>`. As each one finishes, rerun `build.py <W> --partial` so its guide shows up, with its comments marked as still being checked. Rerun a failed writer once, then report it and continue with the others.

## 5. Verify the comments

As each guide lands, spawn a `review-guide:comment-verifier` for that stack with the same two-line prompt; do not wait for the other writers. When a verifier finishes, rerun `build.py <W> --partial`. When all are done, run `build.py <W>` once without `--partial` to confirm every stack is complete.

## 6. Report

Tell the user, in a few lines:

- the URL, and that nothing is posted until they press "Post on #N" for a PR in the "Your review" tab;
- per stack: the one or two points that matter most, and how many comments go to each PR;
- comments the verifier refuted or could not verify (they start unticked);
- that the server runs until they stop it, and how: `pkill -f "review-guide.*serve.py"`.

Never post a review yourself. The reviewer posts from the page.

## Notes

- Data leaves the machine only through `gh` (GitHub) and the model. Before fetching, check whether the repository could hold patient or personal data in fixtures or test data. If it could, say so and ask the user to confirm.
- The page warns, and the server refuses, when a PR receives new commits after the guide was built. Rerun the skill to rebuild.
- The work directory holds a cache per PR set and head SHA under `~/.cache/review-guide/`. A rerun on unchanged heads reuses its files; delete the directory to start clean.
