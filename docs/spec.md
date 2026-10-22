# review-guide specification

The plugin turns one or more GitHub pull requests into a local review page. The reviewer reads a short guide instead of every diff, adds the suggested comments they agree with, and posts one GitHub review per PR from the page. This document records what the product must do and why. `docs/status.md` covers what is built, what was measured, and what comes next.

## Goals

1. **Compact and trustworthy.** The page shows titles and one-sentence explanations by default, with details one click away. Each drafted comment carries the evidence it rests on and the result of a second agent's attempt to disprove it.
2. **Context before code.** A newcomer to the change must understand the moving parts before reading a diff. The page opens with key facts, then how a call travels through the components, then the reading order.
3. **Stacks are one change.** A stack of PRs is reviewed as its final state. A file the stack edits three times appears once, in final form. Each comment is still posted on the PR that wrote the line.
4. **Fast enough to use.** The page is usable seconds after the command starts. The guide and the verified comments fill in on their own. The full run should take under 5 minutes; today it takes about 7.5 (see `docs/status.md`).
5. **The reviewer decides.** Nothing is posted without an explicit click. The agents never post.
6. **Portable.** The plugin works in any GitHub repository with `git`, `gh` and `python3`. It does not depend on other plugins (caveman, ponytail, humanizer) being installed. Their ideas are written into the agent prompts.

## Pipeline

```
serve.py open <refs> ──▶ the one server: finds or creates the review, adds a round, starts rerun.py detached
   │
fetch.py ──▶ page opens (tree + diffs)
   │
   ├─▶ guide-writer ─────────────────┐   (per stack, all four in parallel;
   ├─▶ reviewer: correctness ───┐    │    build.py --partial after each, page reloads)
   ├─▶ reviewer: standards ─────┤    │
   ├─▶ reviewer: spec ──────────┤    │
   │                            └─▶ comment-verifier, one per finding (6 at a time) ──▶ build.py ──▶ page reloads
   │
rerun.py fetches, then runs every agent above headless (claude -p), for the skill and for the page's Rerun button alike
serve.py: the inbox, every review's pages, saved page state, live progress, posting through the user's gh login, rounds
```

| Step | Kind | Output |
| --- | --- | --- |
| `fetch.py` | script | `run.json`, per stack `target.diff`, `tip/`, `attribution.json`, `brief.md`, `spec.md`, `repo-guidance/`; per PR `pr.diff`, `hunks.json`, `existing-comments.json` |
| `guide-writer` | agent (sonnet, medium effort) | `<stack>/guide.json`: the explanation |
| `reviewer` ×3 | agent (sonnet, high effort) | `<stack>/review-<axis>.json`: findings on one axis |
| `build.py --check --only X` | script | validation errors each agent must fix in its own file |
| `comment-verifier` ×findings | agent (sonnet, high effort) | `<stack>/verify/<axis>-<id>.json`: one finding's status, duplicates marked |
| `build.py [--partial]` | script | `guide.json` (merged page data) and `index.html` |
| `serve.py` | script | the one server on `127.0.0.1`: inbox, review pages, page state, posting, rounds; `serve.py open` sends PRs to it |
| `rerun.py` | script | one round: `fetch.py` into the round directory (with `--refs`), then the agents headless through `claude -p` (stream-json); writes `progress.json`, `agents.log` and timings |

### Effort

One level for the whole run, chosen in the skill's arguments (`quick`, `balanced` by default, `deep`) or on the page when rerunning. `fetch.py --effort` stores it in `run.json`, and each agent prompt carries an `Effort:` line.

| Level | Writer | Reviewers | Verifier | Model |
| --- | --- | --- | --- | --- |
| `quick` | brief and spec only | brief and spec only; `blocker` and `should` | `unverified` rather than reading far | agents' own, low effort |
| `balanced` | brief, spec, code where the diff is unclear | reads what each candidate finding needs, and the consumers of each risky change | reads the code behind each finding | agents' own (sonnet) |
| `deep` | also the code around entry points | also tests and config around each change; follows each changed contract to every reader | callers, config and tests for every finding | opus, high effort |

Agents stop on coverage, not on a call count: a reviewer is done when its axis is checked against every changed file that holds behavior, the verifier when every finding has a status it can defend. A 40-call cap stays as a guard. The output limits do not change with the level.

### Reviews, rounds and the inbox

One server runs per machine, on port 7357 (a free port when that one is taken). `/api/ping` returns a hash of the scripts and pages the server runs; when `serve.py open` finds a server whose hash differs from the installed code, it stops that server and starts one on the update, on the same port. Rounds are detached, so a running one survives the restart. It records its port, pid and token in `~/.cache/review-guide/server.json` (mode 0600) and holds a lock, so a second server exits and prints the first one's URL. The skill runs `serve.py open --wait <refs>`. That reuses the running server or starts one detached, then sends the refs, the effort, the focus and the current directory to `POST /api/reviews`. It opens the review in the browser and prints the round's status lines and findings for the skill's report.

- **Review:** the set of PR refs given to one invocation, normalized to `owner/repo#n` and sorted. The same set in any order or form is the same review; a different set is a new one, even when it overlaps. Refs carry their repository, so a review can name several repositories later; for now the server refuses that with a clear message.
- **Round:** each invocation on the same set, and each Rerun on the page, adds a round. A round is its own work directory, so earlier rounds stay as they were. Only one round of a review runs at a time.
- **Inbox:** `GET /` lists the open reviews, most recently touched first, then the finished ones. Each row shows the repository, the title or "Stack of N PRs", the PR numbers (green when posted), rounds, findings, how many PRs are posted, and the newest round's state while it runs or when it failed. It has a theme toggle and "Stop server". Stopping does not stop running rounds; the next server picks them up.
- **Review page:** `/r/<id>` is the latest round with a page; `/r/<id>/<n>` is a given round. The brand in the top bar goes back to the inbox. A round picker appears once two rounds have pages. An earlier round is read-only: a banner says so and links to the latest round, and the server refuses its saves and posts. While the first round is still fetching, the review URL shows a small page with the runner's status that reloads itself.
- **Finish review:** replaces Close. It marks the review done and goes back to the inbox, after a confirmation when some PRs have no review. A finished review shows "Reopen" instead. Its styling follows the old Close button: faint until a review is posted, primary ("Done · Finish review") once every PR has one.

### Self-review (`/review-guide:self-review`)

The same review, run from the session that is implementing the change, with the feedback going back to that session instead of GitHub. The skill runs `serve.py open --self --wait-feedback` in the background and ends its turn. When the user sends feedback from the page, the command exits and prints it, and Claude Code wakes the agent with it.

- **Source:** the local branch stack of the repository the session works in.
  - Layers are every local branch whose tip lies between the merge base with the default branch (or `--base <ref>`, for a stack on someone else's unmerged branch; the ref becomes `self:<root>@<base>..<branch>`) and the current branch (or `--branch`), bottom first, plus "Uncommitted changes" when the working tree differs. The uncommitted layer is a snapshot commit built in a throwaway index with fixed dates, so the real index, working tree and branches are untouched, and an unchanged tree gives the same commit. The newest snapshot is kept at `refs/review-guide/self-snapshot`.
  - Layers are numbered 1..k and handled like PRs by attribution, routing and the agents. The brief says it is a self-review, and that `#N` is a layer, not a GitHub PR.
  - A layer whose branch has an open PR takes the PR's title, description and linked issues. It takes the PR's CI only when the PR head equals the local tip; otherwise it is described from its commit messages.
- **Review id:** `self:<repo root>@<branch>`. The same branch run again is a new round of the same review, with drafts carried over and "Since the previous round" marking what the agent changed. Layers are matched across rounds by branch name.
- **Page** (`mode: self`):
  - layer labels (branch, or `#123 · branch` with a PR) replace PR numbers everywhere;
  - the title is "Self-review of <branch>";
  - the CI tile says "Not pushed" when no layer has a matching PR;
  - GitHub links are shown only where a pushed PR exists;
  - the heads check compares local branch tips.
- **Your review:** one row per layer with "Looks good" or "Needs changes", a summary for the agent, and the layer's findings, then one "Send to the agent" button. It sends once per round (`POST /api/r/<id>/<n>/feedback`, which writes `feedback.json` and marks the layers sent); the next pass is a new round. The server refuses GitHub posting on a self-review.
- **What the agent gets:** per layer, the verdict and summary, then one `- path:line (severity): comment` line per comment with `at:` quoting the line it was written on, since the agent may have edited the file since. The skill tells the agent to address every comment, answer questions, run the checks, and not commit or push unless asked.
- **Edge cases:**
  - Finishing the review without sending ends the wait with a note.
  - A Rerun from the page moves the wait to the new round.
  - Feedback sent after the waiting session ended is printed by the next `--self` run, before any new round starts.
- **Inbox:** a "self" tag, the branch name as title, the layers as chips, and "feedback sent" or "no feedback sent yet".



The page asks the server for the PRs' current heads (one GraphQL call, cached a minute) when it opens and each time the tab becomes visible. When a head moved, both tabs show a banner: which PRs have new commits, that posting is blocked, an effort picker, and "Rerun the guide". The meta chips also carry "<effort> guide · Rerun ↻" to rerun on unchanged heads.

`POST /api/r/<id>/rerun` (same token as posting) adds a round and starts `rerun.py` for it, detached from the server, the same way `serve.py open` does. The open page keeps working on its round and shows the new round's progress until the new round has a page. It then moves to the new round, whose first load carries the page state over (below). The agents run with:
- `--permission-mode dontAsk`, and no user settings, hooks or MCP servers;
- writes limited to the work directory;
- an allowlist of the read commands their prompts name: `git -C <checkout> show|grep|log`, `gh api -H`, `gh search code` and `build.py`. `gh api` with a method, fields or input is denied.

Their output goes to `agents.log` in the work directory, and the runner's own to `runner.log`. Each agent is retried once. A failed reviewer marks its axis skipped, and a failed verifier leaves its finding `unverified` with a note.

### Saved page state and carry-over

Everything the page used to keep in `localStorage` is saved on disk next to its round, in `ui.json`: the stack, view, filters, viewed and reviewed marks, comment states and edits, your comments, verdicts and summaries, and open sections. The page sends it with `PUT /api/r/<id>/<n>/ui` 400 ms after the last change, and flushes it when the tab hides. A failed save shows in the top bar. The theme goes to `prefs.json` through `PUT /api/prefs`, and the server sets it on every page it serves. Nothing lives in the browser any more, so state survives server restarts, port changes and other browsers.

The first load of a new round builds its `ui.json` from the newest earlier round that has one:
- **Comments:** your comments and the findings you added to your review become your comments in the new round. A line comment moves to the added line in the same file with the same text, nearest its old line number, and takes the PR and line of the new diff. A comment whose line is gone is listed in "Your review" with its text, under "N comments from the previous round could not be placed", with a Dismiss button.
- **Verdicts:** verdict summaries carry over; the verdict choice does not.
- **Viewed marks:** these carry over for files that did not change since that round.
- **Posted PRs:** nothing is carried for a PR that round posted on. A posted review whose head did not move stays posted, so the same PR is not reviewed twice.

The contract between the agents and the scripts is `plugins/review-guide/skills/review-guide/references/guide-schema.md`. Change the schema doc, `build.py` validation and the prompts together.

### Work directory

`~/.cache/review-guide/reviews/<id>/rounds/<n>/`, where `<id>` is a slug of the repositories and PR numbers plus a hash of the refs. `reviews/<id>/review.json` holds the refs, the directory the review was started from (fetches run there, so a clone of the repository is used), the status (open or done) and the rounds. `posted.json` records reviews posted from the page, so a PR cannot be posted twice.

The stack's `since` says what the viewer last saw. When the heads moved since the previous round, `fetch.py --prev` diffs that round's review commits against the new tip (`state: ROUND`, "Since the previous round" on the page). Otherwise it uses the viewer's latest review on GitHub ("Since your last review"). The brief tells the reviewers to narrow to the delta only after a posted review; after a round they review the whole stack.

When the current directory is a clone of the reviewed repository, `fetch.py` uses it: it fetches PR heads into `refs/review-guide/<n>` and never checks out a branch or modifies the working tree. Otherwise it keeps a blobless bare clone under `~/.cache/review-guide/repos/`, authenticated through `gh auth git-credential`.

## Stacks

- **Detection:** PR B is stacked on PR A when B's base branch is A's head branch and B contains at least one of A's commits. Chains are ordered bottom first.
- **Target:** the diff from the merge base of the bottom PR's base and head, to the top PR's head.
- **Lagging child:** when B is built on an older version of A, A is reviewed at the commit B contains (`review_sha`), not at A's head. A's newer commits are left out of the target, and a warning names them. Comments for A are posted on `review_sha`; GitHub marks them outdated where newer commits changed the line. Rebasing B removes the warning.
- **Branching:** when two PRs build on the same parent, each one is reviewed as its own stack, with a warning.
- **Not handled:** a parent squash-merged while its child was not rebased. The child's diff then includes the parent's old commits.

### Line attribution

For every line the stack wrote, `attribution.json` maps its line number at the tip to `[pr, line in that PR's own diff]`. `git blame --porcelain base..tip` gives each line its origin commit and line number in that commit. Commits are owned by the PR that introduced them, in chain order. For a line owned by a lower PR, blaming that PR's `review_sha` finds the same `(commit, line)` origin and therefore the line's number there. Only lines inside the owning PR's `commentable` set (added or context lines in its diff) can take an inline comment.

On the test stack (#3564 to #3565), all 1,323 attributed lines mapped to a commentable line in their owning PR. `AgUiEventLogConfiguration.java` alternated owners nine times over 112 lines.

## The brief

`brief.md` exists so the agents read one file instead of exploring turn by turn. Exploration is what made the first runs take 45 minutes. It contains:

- each PR's title, branch and description (up to 2,500 characters), plus the stack warnings;
- the changed-file table with status, line counts and the PRs that touch each file;
- the whole target diff with 8 lines of context, tip line numbers, and `#N` on each added line naming the PR that wrote it;
- "where declared symbols are used": up to 40 classes and functions the stack declares (regex per language: Java, Kotlin, Scala, Python, TS/JS, Go, Ruby, Rust, C#, Swift, PHP), each with up to 6 `git grep -w` hits at the tip, marked when they fall inside the stack.

The brief is capped at 220 KB. When over the cap, the largest test files' diffs are dropped first and listed as "not shown". Lockfiles, snapshots, minified files, images, and files over 1,000 changed lines are always listed instead of shown.

## Agents

The prompts are self-contained. Agent-to-agent replies are one line. Text that a person reads follows plain-writing rules: no praise, no "Consider…", no headings or bold in comments, identifiers in backticks.

Explaining and finding are separate agents, and finding is split by axis, after the `/code-review` skill of mattpocock-skills: one context that both explains and hunts does both worse, and one axis can hide another (code that follows every standard but builds the wrong thing).

### guide-writer

- Reads `brief.md` and `spec.md`, and code only where the diff is unclear. Writes no findings.
- Produces `summary`, 2 to 4 `key_facts`, a role for every changed file, and 1 to 4 `sections` chosen for the change from six types: `flow` (a sequence diagram), `map` (components by layer), `before_after`, `table`, `list` and `steps` (a reading order with anchored excerpts). The prompt gives each type a "use when / not when" and common shapes (a removal gets before/after and what still depends on it, a long stack gets a per-PR table). `components` feed `flow` and `map` and are left out when neither is used.

### reviewer (one per axis)

- **correctness:** error paths, boundaries, retries, concurrency, timeouts; everything that depends on what changed (the brief lists removed names still in use and functions left without a caller; a name another system may read becomes a question naming who); stored data and mixed-version deploys; security and personal data; stack shape; whether a test would catch a break.
- **standards:** every documented rule in `repo-guidance/` and the convention docs they point to, each finding citing its rule; then a smell baseline (Fowler's code smells plus dead weight) as judgement calls that a repo rule overrides.
- **spec:** against `spec.md` (Jira tickets the PRs name, when credentials are set, and the issues they close), or the PR descriptions when there is none: missing or partial requirements, scope creep, requirements done wrong, description claims the code does not back. Skips with a reason when the spec is too thin.
- Each reads existing comments first and never repeats them, and in a new round reviews only what changed since. At most 3 findings per PR per axis; an empty list is a good result.

### comment-verifier

- One verifier per finding, in parallel, at most six at a time, so the fact check takes as long as the slowest single check and its progress is an honest count.
- Tries to disprove its finding: `verified` (it traced the mechanism, and every claim matches), `refuted` (the code, the spec or the description contradicts it; for standards, the rule does not say that), `unverified` (library internals, prod config, runtime). Marks its finding `duplicate` only when an earlier finding (axis order correctness, standards, spec, then file order) makes the same point; the earlier one always stays, so parallel verifiers never drop both copies of a point. The page hides duplicates.
- May move a finding to a better line the stack wrote.

## Validation (`build.py --check`)

- Length limits:

  | Field | Limit (characters) |
  | --- | --- |
  | summary | 300 |
  | step title | 70 |
  | step `why` | 220 |
  | file `why` | 90 |
  | section title | 70 |
  | table and before/after cell | 160 |
  | list item | 220 |
  | finding title | 90 |
  | finding body | 700 |
  | labels | 40 |
  | node role | 30 |
  | fact title | 60 |
  | fact text | 200 |
  | test | 140 |

- Every changed file has a role, and no other file does.
- Anchors exist at the tip and span 60 lines at most.
- Inline comments sit on lines the stack wrote. A range must cover consecutive lines from one PR. A line error lists nearby valid lines.
- 1 to 5 sections of a known type, each with its own shape rules (cells per row, items, hops). `flow` and `map` need `components`.
- Components reference declared layers. `new` and `changed` nodes name the changed file that holds them. Edges and flow hops reference node ids.
- At most 3 findings per PR per axis.
- `--only guide|<axis>` checks one agent's file, so agents running in parallel do not see each other's errors.

`--partial` renders a stack without a guide as a file tree and diffs only, and an axis with no file yet as "reviewing". It renders a finding as "being checked" until its `verify/<axis>-<id>.json` exists.

## Page

Single self-contained HTML file. Figtree from Google Fonts and highlight.js 11.9.0 from cdnjs are the only external loads.

### Layout

- **Top bar, one line:** "← Reviews" (back to the inbox), the review's title (the PR title, "Stack of N PRs", or the self-review branch), and for a stack a switcher chip with one dot per PR and "3 PRs ▾" ("3 layers" in a self-review). Its popover lists each PR with its tag, title, CI state and GitHub link; picking one shows only what it wrote and dims everything else, and the chip then names that PR. The stack picker (when there are several stacks) sits next to it. In the middle are the stages, each with its progress: `Understand · Read 2/6 · Findings 3/7 · Send (4)`. On the right, "Finish review" as a primary button once every PR is posted (or the feedback is sent), and a "⋯" menu with the round picker (from two rounds), Rerun, the theme and Finish review. A failed save shows as a bar under the top bar. Under 860 px the stages take a second row and the rail opens from a ☰ button.
- **Stages:** Understand, Read and Findings are anchors in the one scrolling guide: Understand is the overview, the facts and the sections that are not steps, Read is the steps sections, Findings is the reviewers' findings. A stage link scrolls to its part, and the stage on screen is marked from the scroll position. Send is its own view, "Your review". Counts: Read is the steps marked reviewed, Findings is the findings added or dismissed out of all axis findings that are not duplicates, Send is the comments in the review.
- **Scrolling:** no panel on the page shows a scrollbar; content still scrolls by wheel, trackpad and keyboard.
- **Width:** prose (summary, key facts, step text, finding titles and bodies) stays near 80 characters a line. Tiles, diagrams, tables, steps, finding cards and code use the full width of the content, up to 1600 px. "Know this first" is two columns from 1200 px.
- **Left rail:** a switch between Outline and Files, saved with the page state. It opens on Outline, and on Files once a file is open.
  - **Outline:** Overview, each section by title, the steps with a check once reviewed, the findings by axis with the count still waiting for a decision, and Your review. What is on screen is highlighted, and the rail follows it.
  - **Files:** file tree with collapsible folders, compressed single-child folders, a search box, read/skim/skip filter chips, a viewed count, a role marker per file, and a drafted-comment count. The PR dots sit pinned to the right edge, over a fade, so they stay visible while the tree scrolls sideways.
- **Guide view:**
  - title, summary, then four tiles that tell the reviewer what they are signing up for:
    - **Time to review:** an estimate from the writer's file roles: added lines in `read` files at 20 a minute, `skim` files at 100, `skip` files not at all, a quarter weight for deleted lines, and 1.5 minutes per finding, rounded to 5 minutes.
    - **Size:** additions in green and deletions in red, with a bar.
    - **CI:** green, amber when checks are pending, red with the failing check names.
    - **Reviewers found:** blockers in red, should-fixes in amber, otherwise "Nothing blocking" in green, with the questions and nits and how many were checked in the code.
  - then chips for the GitHub link and Rerun, a PR list with one row per PR (title, CI state, failing check names, GitHub link; collapsed above five PRs), plus warnings in a collapsible box;
  - "Know this first";
  - the writer's sections, in its order, each under its own title:
    - `flow`: a sequence diagram. Components are columns in call order with role above label. Arrows are numbered top to bottom, and blue arrows are what the stack added. Clicking a box opens its file;
    - `map`: the layered component map. Nodes are ordered by barycenter to reduce crossings, edges are spread along node borders, and labels are placed where they do not collide;
    - `before_after` and `table`: plain tables, backticked names as code;
    - `list`: a bullet list;
    - `steps`: collapsible steps showing title, one sentence, a decide badge, and a count of findings on the excerpt. Opening a step shows the highlighted excerpt with those findings under their lines, the test line ("none covers this" in red), a "Reviewed" checkbox, and a link to the whole file;
  - "What the reviewers found": one collapsible block per axis (correctness, standards, spec), showing "reviewing" while its reviewer works, "skipped" with the reason, or the findings, most severe first, each tagged with its axis and verifier status. Each finding card outside the diff shows the few diff lines it sits on, with the file name opening the file view. A finding with no line shows "In" and links to the code it cites (the file view for files the stack changed, GitHub otherwise), plus the lines of the first cited file the stack changed. Deleted files show their old line numbers. Posted general comments carry the same links.
- **File view:** the full stack diff with old and new line numbers, a color strip per line for the PR that wrote it, drafted comments under their lines, a "Viewed" checkbox, and walk buttons. Clicking a line number adds a comment there, routed to the owning PR.
  - **Keeping your place:** opening a file records where it came from (the guide and its scroll position, or Your review, and whether a step, a finding, a diagram or the tree). A breadcrumb shows the way, for example `Guide › Read in this order › 3. The resume cursor › FrameCursor.java`, each part a link, and "← Back" returns to the same scroll position.
  - **Walking:** a file opened from a step gets "← 2. …" and "4. … →" to move along the reading order (each opens at the step's first line) and a "Step reviewed" checkbox. A file opened from a finding gets the previous and next finding, with "3 of 7". A file opened from the tree gets Prev and Next file.
  - **Anchor:** when opened from a step, a finding or a cited line, the view scrolls to that line and outlines it.
- **Your review:** one compact row per PR waiting to be posted, then a collapsed "Posted (N)" list. A row shows the title, picked comment count with suggestions still waiting, a CI failure mark, and a Comment / Approve / Changes switch. No verdict is selected until the reviewer picks one (clicking it again clears it) or adds a comment, which brings the suggestion along; a PR without a verdict is not posted. ✦ marks the suggestion, recomputed from the live comments: request changes with a blocker, comment with a should-fix or question, approve otherwise (only Comment on the viewer's own PR). Opening a row shows the reason (the open findings by title, or "Nothing open"), the routed findings, the summary (empty, with a placeholder), "Post only #N" and "Copy as markdown". Above the list: "Approve all clean (N)" (not the viewer's PR, CI not failing, suggestion is approve), "Use the suggested verdicts", "Clear verdicts", "Add N suggestions checked in the code", and "Post N reviews", which asks once and posts each PR with a verdict in order, showing each result on its row.
- **Since your last review:** when the viewer already reviewed a PR of the stack, the guide shows when and how (the highest PR they reviewed), with "Review what changed". Files changed since carry a "new" mark and a "New N" filter, and the tip lines added since have a highlighted line number. Each row of "Your review" shows the viewer's last verdict on that PR.
- **Theme:** the toggle is saved on the server, for every page and run.

### Comment states

| State | Look | Actions |
| --- | --- | --- |
| Suggested | dashed card, ✦ avatar, "Suggested comment", severity, verification status, target PR and line | "Add to my review", "Dismiss", "Read the comment and the proof" |
| In your review | solid card with blue left border, the viewer's initial, editor open, type picker | "Remove from my review" |
| Your comment | same as above, labeled "Your comment", defaults to type "comment" | "Delete" |
| Dismissed | one dashed line | "Undo" |

Nothing is pre-added to the review. Severity prefixes ("Should fix: ", "Nit: ", "Blocker: ") are prepended at post time, and the editor shows the prefix that will be used. Verification wording on the page is "checked in the code", "could not confirm", "disproved" and "being checked".

### Live updates

The page polls `/api/r/<id>/<n>/state` every 3 seconds while a stack is pending, an agent is running, or a comment is being checked, and every 15 seconds otherwise. When the latest round's `index.html` changes, or a newer round gets a page, it flushes its state and loads the latest round. The inbox polls `/api/reviews` the same way.

### Progress strip

While agents work, both tabs show a run panel under the meta chips. Its header names the phase (writing the guide and reviewing, reviewing, checking the findings) with the time since the runner started, the effort and "usually about N min", and a thin bar for the share of stages finished. Below it, one row per stage (Guide, Correctness, Standards, Spec, Fact check): an icon for its state (waiting, running, done, skipped, failed), a line of detail, and how long that agent ran. A running row shows the tool-call count and the latest action ("reading NotificationService.java", "git grep findProfessionalOnDuty"); a finished reviewer shows its finding count or why it skipped; the fact check shows "3 of 7 checked". During a rerun's fetch, before any agent starts, the panel shows the runner's status line. The panel is painted in place from each `/api/state` reply, so it never reloads the page, and it disappears when every stage is finished.

- Stage states come from the files the agents write (`build.stages()`): a file means done, a reviewer file with `skipped` means skipped, and `progress.json` from the runner says which agents are running or failed and what each did last.
- Live actions come from `claude -p --output-format stream-json --verbose`: the runner counts each `tool_use` block in the `assistant` events and rewrites `progress.json` atomically.
- Timings: at the end of a run the runner appends its duration to `~/.cache/review-guide/timings.json` (last ten per effort level). The state endpoint returns the averages, shown as "usually about N min" in the strip and next to the effort picker.

### Design

The page follows the Doctolib brand.

- **Fonts:** Figtree for text, JetBrains Mono for code.
- **Light theme:** Snowbliss ground, Nightshift text, Oxylove accent, with Honeywell and Berryblush for warnings and errors.
- **Dark theme:** a neutral near-black ground (`#101010`, never navy), with Skyjoy as the accent.
- **PR colors:** brand colors only (Oxylove, Honeywell, Berryblush, Skyjoy, then the light tints).

The design is hardcoded in `ui/template.html`, so every user gets the same look.

## Posting (`serve.py`)

- Binds `127.0.0.1` only, on port 7357 unless `--port` is given or the port is taken.
- Every write (POST and PUT) must carry a per-process token embedded in the served pages and in `server.json`. Without it the server answers 403, so another site open in the browser cannot post as the user or change saved state.
- Only the latest round of a review with a page takes posts and saves.
- Before posting it checks:
  - the PR head still equals the SHA the guide was built on;
  - every inline line is in the PR's `commentable` set;
  - non-comment review types are refused on the viewer's own PR;
  - the PR has not been posted from this page already.
- It posts `POST repos/<repo>/pulls/<n>/reviews` through `gh api` with `commit_id = review_sha`. Multi-line comments send `start_line` and `start_side`.

## Data handling

Data leaves the machine through `gh` (GitHub) and the model only. The skill asks the user to confirm before fetching when the repository could hold patient or personal data in fixtures. Doctolib policy forbids publishing a page built from such a repository to any public destination.
