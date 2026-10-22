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
fetch.py ──▶ page opens (tree + diffs)
   │
   ├─▶ guide-writer ─────────────────┐   (per stack, all four in parallel;
   ├─▶ reviewer: correctness ───┐    │    build.py --partial after each, page reloads)
   ├─▶ reviewer: standards ─────┤    │
   ├─▶ reviewer: spec ──────────┤    │
   │                            └─▶ comment-verifier, one per finding (6 at a time) ──▶ build.py ──▶ page reloads
   │
rerun.py runs every agent above headless (claude -p), for the skill and for the page's Rerun button alike
serve.py: local page, live progress, posting through the user's gh login, reruns
```

| Step | Kind | Output |
| --- | --- | --- |
| `fetch.py` | script | `run.json`, per stack `target.diff`, `tip/`, `attribution.json`, `brief.md`, `spec.md`, `repo-guidance/`; per PR `pr.diff`, `hunks.json`, `existing-comments.json` |
| `guide-writer` | agent (sonnet, medium effort) | `<stack>/guide.json`: the explanation |
| `reviewer` ×3 | agent (sonnet, high effort) | `<stack>/review-<axis>.json`: findings on one axis |
| `build.py --check --only X` | script | validation errors each agent must fix in its own file |
| `comment-verifier` ×findings | agent (sonnet, high effort) | `<stack>/verify/<axis>-<id>.json`: one finding's status, duplicates marked |
| `build.py [--partial]` | script | `guide.json` (merged page data) and `index.html` |
| `serve.py` | script | serves the page on `127.0.0.1`, posts reviews, and starts reruns |
| `rerun.py` | script | runs the agents headless through `claude -p` (stream-json), writes `progress.json`, `agents.log` and timings; `--no-fetch` when the skill already fetched |

### Effort

One level for the whole run, chosen in the skill's arguments (`quick`, `balanced` by default, `deep`) or on the page when rerunning. `fetch.py --effort` stores it in `run.json`, and each agent prompt carries an `Effort:` line.

| Level | Writer | Reviewers | Verifier | Model |
| --- | --- | --- | --- | --- |
| `quick` | brief and spec only | brief and spec only; `blocker` and `should` | `unverified` rather than reading far | agents' own, low effort |
| `balanced` | brief, spec, code where the diff is unclear | reads what each candidate finding needs, and the consumers of each risky change | reads the code behind each finding | agents' own (sonnet) |
| `deep` | also the code around entry points | also tests and config around each change; follows each changed contract to every reader | callers, config and tests for every finding | opus, high effort |

Agents stop on coverage, not on a call count: a reviewer is done when its axis is checked against every changed file that holds behavior, the verifier when every finding has a status it can defend. A 40-call cap stays as a guard. The output limits do not change with the level.

### Rerun from the page

The page asks `serve.py` for the PRs' current heads (one GraphQL call, cached a minute) when it opens and each time the tab becomes visible. When a head moved, both tabs show a banner: which PRs have new commits, that posting is blocked, an effort picker, and "Rerun the guide". The meta chips also carry "<effort> guide · Rerun ↻" to rerun on unchanged heads.

`POST /api/rerun` (same token as posting, one rerun at a time) starts `rerun.py`, the same runner the skill uses. It fetches again, which lands in a new work directory when heads moved, and `serve.py` switches to that directory as soon as it holds a page. The open page reloads onto it and follows the agents as in a first run. Unchanged heads reuse the directory and replace its guides. The agents run with `--permission-mode dontAsk`, no user settings, hooks or MCP servers, writes limited to the work directory, and an allowlist of the read commands their prompts name (`git -C <checkout> show|grep|log`, `gh api -H`, `gh search code`, `build.py`); `gh api` with a method, fields or input is denied. Their output goes to `agents.log` in the work directory. Each agent is retried once. A failed reviewer marks its axis skipped, and a failed verifier leaves its finding `unverified` with a note.

The contract between the agents and the scripts is `plugins/review-guide/skills/review-guide/references/guide-schema.md`. Change the schema doc, `build.py` validation and the prompts together.

### Work directory

`~/.cache/review-guide/<owner>-<repo>-<prs>-<hash of head SHAs>/`. A rerun on unchanged heads reuses it. `posted.json` records reviews posted from the page, so a PR cannot be posted twice. The viewer's latest review on each PR comes from GitHub, so a new round knows what they last saw: `fetch.py` diffs that commit against the new tip and stores the changed lines as the stack's `since`. The brief tells the writer to focus on them.

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

- **Top bar:** stack picker (when there are several), the PR chain as chips with a color dot per PR (above five PRs, one dropdown listing them with their CI state), the tabs "Guide" and "Your review (N)", the theme toggle, and Close. Clicking a PR chip dims everything the other PRs wrote. Close stops the local server after a confirmation when some PRs have no review; it is faint until a review is posted and becomes the primary "Done · Close" once every PR has one.
- **Scrolling:** no panel on the page shows a scrollbar; content still scrolls by wheel, trackpad and keyboard.
- **Left sidebar:** file tree with collapsible folders, compressed single-child folders, a search box, read/skim/skip filter chips, a viewed count, a role marker per file, and a drafted-comment count. The PR dots sit pinned to the right edge, over a fade, so they stay visible while the tree scrolls sideways.
- **Guide view:**
  - title, summary, and meta chips (size, CI summary, link to the stack), a PR list with one row per PR (title, CI state, failing check names, GitHub link; collapsed above five PRs), plus warnings in a collapsible box;
  - "Know this first";
  - the writer's sections, in its order, each under its own title:
    - `flow`: a sequence diagram. Components are columns in call order with role above label. Arrows are numbered top to bottom, and blue arrows are what the stack added. Clicking a box opens its file;
    - `map`: the layered component map. Nodes are ordered by barycenter to reduce crossings, edges are spread along node borders, and labels are placed where they do not collide;
    - `before_after` and `table`: plain tables, backticked names as code;
    - `list`: a bullet list;
    - `steps`: collapsible steps showing title, one sentence, a decide badge, and a count of findings on the excerpt. Opening a step shows the highlighted excerpt with those findings under their lines, the test line ("none covers this" in red), a "Reviewed" checkbox, and a link to the whole file;
  - "What the reviewers found": one collapsible block per axis (correctness, standards, spec), showing "reviewing" while its reviewer works, "skipped" with the reason, or the findings, most severe first, each tagged with its axis and verifier status.
- **File view:** the full stack diff with old and new line numbers, a color strip per line for the PR that wrote it, drafted comments under their lines, a "Viewed" checkbox, and prev/next buttons. Clicking a line number adds a comment there, routed to the owning PR.
- **Your review:** one compact row per PR waiting to be posted, then a collapsed "Posted (N)" list. A row shows the title, picked comment count with suggestions still waiting, a CI failure mark, and a Comment / Approve / Changes switch. No verdict is selected until the reviewer picks one (clicking it again clears it) or adds a comment, which brings the suggestion along; a PR without a verdict is not posted. ✦ marks the suggestion, recomputed from the live comments: request changes with a blocker, comment with a should-fix or question, approve otherwise (only Comment on the viewer's own PR). Opening a row shows the reason (the open findings by title, or "Nothing open"), the routed findings, the summary (empty, with a placeholder), "Post only #N" and "Copy as markdown". Above the list: "Approve all clean (N)" (not the viewer's PR, CI not failing, suggestion is approve), "Use the suggested verdicts", "Clear verdicts", "Add N suggestions checked in the code", and "Post N reviews", which asks once and posts each PR with a verdict in order, showing each result on its row.
- **Since your last review:** when the viewer already reviewed a PR of the stack, the guide shows when and how (the highest PR they reviewed), with "Review what changed". Files changed since carry a "new" mark and a "New N" filter, and the tip lines added since have a highlighted line number. Each row of "Your review" shows the viewer's last verdict on that PR.
- **Theme:** the toggle is remembered across reloads and runs.

### Comment states

| State | Look | Actions |
| --- | --- | --- |
| Suggested | dashed card, ✦ avatar, "Suggested comment", severity, verification status, target PR and line | "Add to my review", "Dismiss", "Read the comment and the proof" |
| In your review | solid card with blue left border, the viewer's initial, editor open, type picker | "Remove from my review" |
| Your comment | same as above, labeled "Your comment", defaults to type "comment" | "Delete" |
| Dismissed | one dashed line | "Undo" |

Nothing is pre-added to the review. Severity prefixes ("Should fix: ", "Nit: ", "Blocker: ") are prepended at post time, and the editor shows the prefix that will be used. Verification wording on the page is "checked in the code", "could not confirm", "disproved" and "being checked".

### Live updates

The page polls `/api/state` every 3 seconds while a stack is pending, an agent is running, or a comment is being checked. When `index.html` changes on disk, it saves state and reloads.

### Progress strip

While agents work, both tabs show a run panel under the meta chips. Its header names the phase (writing the guide and reviewing, reviewing, checking the findings) with the time since the runner started, the effort and "usually about N min", and a thin bar for the share of stages finished. Below it, one row per stage (Guide, Correctness, Standards, Spec, Fact check): an icon for its state (waiting, running, done, skipped, failed), a line of detail, and how long that agent ran. A running row shows the tool-call count and the latest action ("reading NotificationService.java", "git grep findProfessionalOnDuty"); a finished reviewer shows its finding count or why it skipped; the fact check shows "3 of 7 checked". During a rerun's fetch, before any agent starts, the panel shows the runner's status line. The panel is painted in place from each `/api/state` reply, so it never reloads the page, and it disappears when every stage is finished.

- Stage states come from the files the agents write (`build.stages()`): a file means done, a reviewer file with `skipped` means skipped, and `progress.json` from the runner says which agents are running or failed and what each did last.
- Live actions come from `claude -p --output-format stream-json --verbose`: the runner counts each `tool_use` block in the `assistant` events and rewrites `progress.json` atomically.
- Timings: at the end of a run the runner appends its duration to `~/.cache/review-guide/timings.json` (last ten per effort level). `/api/state` returns the averages, shown as "usually about N min" in the strip and next to the effort picker. State lives in `localStorage` under `review-guide-v2-<run_id>`.

### Design

The page follows the Doctolib brand.

- **Fonts:** Figtree for text, JetBrains Mono for code.
- **Light theme:** Snowbliss ground, Nightshift text, Oxylove accent, with Honeywell and Berryblush for warnings and errors.
- **Dark theme:** a neutral near-black ground (`#101010`, never navy), with Skyjoy as the accent.
- **PR colors:** brand colors only (Oxylove, Honeywell, Berryblush, Skyjoy, then the light tints).

The design is hardcoded in `ui/template.html`, so every user gets the same look.

## Posting (`serve.py`)

- Binds `127.0.0.1` only, on a random port unless `--port` is given.
- Every POST must carry a per-process token embedded in the served page. Without it the server answers 403, so another site open in the browser cannot post as the user.
- Before posting it checks:
  - the PR head still equals the SHA the guide was built on;
  - every inline line is in the PR's `commentable` set;
  - non-comment review types are refused on the viewer's own PR;
  - the PR has not been posted from this page already.
- It posts `POST repos/<repo>/pulls/<n>/reviews` through `gh api` with `commit_id = review_sha`. Multi-line comments send `start_line` and `start_side`.

## Data handling

Data leaves the machine through `gh` (GitHub) and the model only. The skill asks the user to confirm before fetching when the repository could hold patient or personal data in fixtures. Doctolib policy forbids publishing a page built from such a repository to any public destination.
