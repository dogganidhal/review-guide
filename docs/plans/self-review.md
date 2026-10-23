# Self-review: review your own work in progress, and send the feedback to the agent

## Outcome

You run `/review-guide:self-review` from the Claude Code session that is implementing a change. You get the same review page, with the guide, three reviewers and the fact check, built from your local branches. You read it, pick and edit comments, and press "Send to the agent". The implementing agent wakes up with your feedback as file:line comments and works through it. Nothing goes to GitHub.

Not in this change:
- Posting a self-review to GitHub.
- Running a self-review round automatically after each agent change. You run it again, or press Rerun on the page.

## Decisions

| Question | Decision |
| --- | --- |
| Name | `/review-guide:self-review` (plugin skills are namespaced). |
| Source | By default, the local branch stack, so no PR or push is needed. You can pass PR links instead; delivery stays local. |
| Delivery | The skill starts a background command and ends its turn. "Send to the agent" ends the command with the feedback, and Claude Code wakes the agent with it. |
| Agents | The same guide writer, three reviewers and fact check, at the same effort levels. |
| Inbox | The same server and inbox. Self-reviews carry a "self" tag. The same branch run again adds a round, with drafts carried over and a "new" mark on what changed since. |

## Which changes get reviewed (default, no arguments)

Run from the repository the agent works in:

- **Top:** the current branch, or `HEAD` when detached.
- **Base:** the merge base of the top with the default branch (`origin/HEAD`, falling back to `main`, then `master`).
- **Layers:** every local branch whose tip lies between the base and the top, ordered from the base up, with branches at the same commit listed once. A stack `feat-a` → `feat-b` → `feat-c` gives three layers, the way three stacked PRs do. A branch on its own is one layer.
- **Uncommitted changes:** when the working tree has staged, unstaged or untracked changes, they become a top layer, "Uncommitted changes". The snapshot uses a temporary index and `git commit-tree`, so the real index, the working tree and the branches are never touched.
- **PRs:** for a branch that has an open PR, the PR's title, description, linked issues and CI are used when its head equals the local tip. Otherwise the layer is described from its commit messages. Tickets in branch names (`PROJ-123-...`) feed `spec.md` as they do today.

`/review-guide:self-review <PR links>` reviews those PRs as `/review-guide` does, but with local delivery.

## How it fits the existing pieces

**fetch.py: a local source**

- Refs of the form `self:<repo-root>@<branch>` build the layers above.
- Layers are numbered 1..k internally, so attribution, routing, hunks and the brief are unchanged.
- Each layer carries a `label`: the branch name, `#123 · branch` when it has a PR, or "Uncommitted changes".
- Fields with no local value: existing comments are empty when there is no PR, CI is "not pushed" when there is none, and `last_seen` is skipped (rounds give "Since the previous round").
- It writes `refs/review-guide/self/<n>` in the clone, the way PR fetches write `refs/review-guide/<n>` today. That keeps the snapshot commits alive for the agents' `git grep`.

**serve.py**

- `serve.py open --self [--branch B] [PR refs]` builds the ref and sends it with `mode: "self"`. With `--wait-feedback`, it waits until feedback is sent or the review is finished, then prints the feedback.
- `POST /api/r/<id>/<n>/feedback` (token, latest round only) writes `feedback.json` and marks the layers sent in `posted.json`, so carry-over leaves them out the way it leaves out posted PRs.
- In a self-review, the heads check compares local branch tips (`git rev-parse` in the clone) instead of asking GitHub. Uncommitted changes are not watched.
- `/api/r/.../review` (GitHub posting) is refused for self-reviews.

**Page (`template.html`, `DATA.mode === "self"`)**

- **Labels:** layer labels replace `#n` everywhere (chips, stack picker, comment targets, rows). GitHub links show only where a PR exists.
- **Title:** "Self-review of `<branch>`", or "Stack of 3 branches".
- **CI tile:** "Not pushed" in neutral when there is no PR; the other tiles are unchanged.
- **Your review:** one "Send to the agent" button for the whole stack instead of per-PR posting. Each layer keeps an optional verdict (looks good / needs changes) and its summary. "Approve all clean" goes away. The confirmation says nothing leaves the machine.
- **After sending:** the tab shows "Sent to the agent at 14:32" with what was sent. Further sends in the same round are blocked; a new round is how the next pass happens.

**Inbox:** a "self" tag on the row, and the branch name in place of the PR numbers.

## What the agent receives

The background command prints, then exits:

```
review-guide feedback for feat-b (round 2), sent 14:32

## feat-a: looks good

## feat-b: needs changes
Summary: <your summary>

- src/main/java/.../Foo.java:42 (should fix): <your comment, as edited>
- src/main/java/.../Bar.java:10-14 (question): <...>
- general (nit): <...>

## Uncommitted changes: needs changes
- ...
```

- Line numbers are the reviewed snapshot's lines in the working tree. The skill tells the agent that a line may have moved if it edited the file since.
- The skill then tells the agent to:
  - treat the feedback as review comments from the user;
  - address each one, and answer each question in its reply;
  - run the project's checks;
  - never commit or push unless the user asked for that;
  - finish by saying another `/review-guide:self-review` builds round N+1 with the drafts carried over.
- **Finish without sending:** the command prints "finished without feedback" and exits 0.

## Skill (`skills/self-review/SKILL.md`)

1. Read the effort and focus from the arguments, and any PR links.
2. Run `serve.py open --self --wait-feedback ...` in the background.
3. Report the review URL, then end the turn.
4. When the command exits, act on the feedback as above.

- **Allowed tools:** the scripts and Read.
- **Data note:** the same as `/review-guide`. Code goes to the model only; nothing goes to GitHub.

## Tests

- **`fetch.py`, local source:** a scratch repo with `main`, `feat-a` and `feat-b` stacked, plus an untracked file. Check that:
  - the layers are `feat-a`, `feat-b` and "Uncommitted changes", in that order;
  - attribution puts the untracked file's lines on the uncommitted layer;
  - the real index and `git status` are unchanged afterwards.
- **Server:**
  - a self review id is stable for the same branch;
  - `/feedback` writes the file and marks the layers sent;
  - `/review` is refused on a self-review;
  - a second `/feedback` in the same round is refused;
  - carry-over leaves out sent layers.
- **`serve.py open --wait-feedback`:** it prints the feedback once `/feedback` lands.

## Also done in this session (already committed, 78faab5)

The guide's first screen now opens with four tiles in place of the grey chips:
- **Time to review:** estimated from the file roles and the number of findings.
- **Size:** additions in green, deletions in red, with a bar.
- **CI:** green, amber when pending, red with the failing checks.
- **Reviewers found:** colored by the worst severity.

## Risks

- **Branch heuristics.** Local branches between the base and the top may include ones you don't think of as layers, such as an old branch at a commit in the middle. The page lists the layers, and the next round can name the top branch explicitly with `--branch`.
- **Line drift.** Comments name snapshot lines, and the agent may have edited the file since. The feedback says so, and quotes the line's text next to its number so the agent can find it again.
- **The waiting command's lifetime.** If the Claude Code session ends while waiting, the feedback still sits in `feedback.json` in the round folder. The next `/review-guide:self-review` prints any feedback that was never collected, before starting a round.
