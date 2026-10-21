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
   ├─▶ guide-writer (one per stack) ──▶ build.py --partial ──▶ page reloads with the guide
   │                                         │
   │                                         └─▶ comment-verifier ──▶ build.py ──▶ page reloads with verdicts
   │
serve.py: local page, posts reviews through the user's gh login
```

| Step | Kind | Output |
| --- | --- | --- |
| `fetch.py` | script | `run.json`, per stack `target.diff`, `tip/`, `attribution.json`, `brief.md`, `repo-guidance/`; per PR `pr.diff`, `hunks.json`, `existing-comments.json` |
| `guide-writer` | agent (sonnet, medium effort) | `<stack>/guide.json` |
| `build.py --check` | script | validation errors the writer must fix |
| `comment-verifier` | agent (sonnet, high effort) | `<stack>/verify.json` |
| `build.py [--partial]` | script | `guide.json` (merged page data) and `index.html` |
| `serve.py` | script | serves the page on `127.0.0.1` and posts reviews |

The contract between the agents and the scripts is `plugins/review-guide/skills/review-guide/references/guide-schema.md`. Change the schema doc, `build.py` validation and the prompts together.

### Work directory

`~/.cache/review-guide/<owner>-<repo>-<prs>-<hash of head SHAs>/`. A rerun on unchanged heads reuses it. `posted.json` records reviews posted from the page, so a PR cannot be posted twice.

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

Both prompts are self-contained. Agent-to-agent replies are one line (`<stack> ok: N steps, M comments`). Text that a person reads follows plain-writing rules: no praise, no "Consider…", no headings or bold in comments, identifiers in backticks.

### guide-writer

- Reads `brief.md`, `repo-guidance/` and existing comments in one or two calls, then decides.
- Budget: about 8 tool calls, hard cap 15. Reads in parallel. Does not search broadly, because the brief already lists callers. Never opens third-party code (jars, `~/.m2`, `node_modules`, `site-packages`). A finding that depends on library behavior becomes a `question` naming the behavior to confirm.
- Produces:
  - `summary` (300 characters at most) and 2 to 4 `key_facts`, the things a newcomer must know first;
  - the map: layers, 5 to 12 components, each with a plain-words `role` and its code `label`, plus edges labeled with the operation;
  - 1 to 3 `flows`: ordered hops from an entry point to where data lands, plus the failure path when the stack is about failures;
  - a role for every changed file (`read`, `skim`, `skip`; `read` when unsure);
  - 2 to 6 steps (8 at most), ordered so each needs only earlier ones; each step has a 10 to 60 line anchor, the components it covers, and the test that covers it, or `null`;
  - comments, at most 5 per PR, with severity `blocker`, `should`, `question` or `nit` (2 nits per stack at most), a title under 12 words, a body as posted, and `path:line` evidence.
- Review lenses: correctness, contracts, risk (auth, PHI in logs, secrets), over-engineering (the ponytail lens), stack shape (a lower PR unsafe to merge alone, churn across PRs), tests, and the description against the diff.
- Verdict per PR: `COMMENT` on the viewer's own PR, `REQUEST_CHANGES` only with a blocker routed to that PR, and `APPROVE` only with no blocker, should or open question.

### comment-verifier

- Tries to disprove each drafted comment against the code: `verified` (it traced the mechanism, and every claim matches), `refuted` (the code contradicts it), or `unverified` (the claim depends on library internals, prod config or runtime).
- Budget: about 10 tool calls, hard cap 15, with the same rules about third-party code.
- May move a comment to a better line the stack wrote. May add up to 3 `missed` blocker or should findings.

## Validation (`build.py --check`)

- Length limits:

  | Field | Limit (characters) |
  | --- | --- |
  | summary | 300 |
  | step title | 70 |
  | step `why` | 220 |
  | file `why` | 90 |
  | comment title | 90 |
  | comment body | 700 |
  | `verdict.why` | 160 |
  | `review_body` | 400 |
  | labels | 40 |
  | node role | 30 |
  | fact title | 60 |
  | fact text | 200 |
  | test | 140 |

- Every changed file has a role, and no other file does.
- Anchors exist at the tip and span 60 lines at most.
- Inline comments sit on lines the stack wrote. A range must cover consecutive lines from one PR. A line error lists nearby valid lines.
- Map nodes reference declared layers. `new` and `changed` nodes name the changed file that holds them. Edges and flow hops reference node ids.
- Verdict rules are enforced per PR, including `COMMENT` on the viewer's own PRs.

`--partial` renders a stack without a guide as a file tree and diffs only. It renders comments as "being checked" until `verify.json` exists.

## Page

Single self-contained HTML file. Figtree from Google Fonts and highlight.js 11.9.0 from cdnjs are the only external loads.

### Layout

- **Top bar:** stack picker (when there are several), the PR chain as chips with a color dot per PR, the tabs "Guide" and "Your review (N)", and the theme toggle. Clicking a PR chip dims everything the other PRs wrote.
- **Left sidebar:** file tree with collapsible folders, compressed single-child folders, a search box, read/skim/skip filter chips, a viewed count, a role marker per file, and a drafted-comment count. The PR dots sit pinned to the right edge, over a fade, so they stay visible while the tree scrolls sideways. Neither scroll direction shows a scrollbar.
- **Guide view:**
  - title, summary, and meta chips (size, CI, links to the stack and each PR), plus warnings in a collapsible box;
  - "Know this first";
  - "How a call moves through it": one tab per flow, each drawn as a sequence diagram. Components are columns in call order with role above label. Arrows are numbered top to bottom, and blue arrows are what the stack added. Clicking a box opens its file;
  - "All components by layer": the layered map, collapsed by default. Nodes are ordered by barycenter to reduce crossings, edges are spread along node borders, and labels are placed where they do not collide;
  - "Read in this order": collapsible steps showing title, one sentence, a decide badge, and a comment count. Opening a step shows the highlighted excerpt, the test line ("none covers this" in red), the comments, a "Reviewed" checkbox, and a link to the whole file.
- **File view:** the full stack diff with old and new line numbers, a color strip per line for the PR that wrote it, drafted comments under their lines, a "Viewed" checkbox, and prev/next buttons. Clicking a line number adds a comment there, routed to the owning PR.
- **Your review:** one card per PR with the suggested verdict, the comments routed to it, "Add all N suggestions checked in the code", an editable summary, the review type (only "Comment" on the viewer's own PR), "Post on #N", and "Copy as markdown".

### Comment states

| State | Look | Actions |
| --- | --- | --- |
| Suggested | dashed card, ✦ avatar, "Suggested comment", severity, verification status, target PR and line | "Add to my review", "Dismiss", "Read the comment and the proof" |
| In your review | solid card with blue left border, the viewer's initial, editor open, type picker | "Remove from my review" |
| Your comment | same as above, labeled "Your comment", defaults to type "comment" | "Delete" |
| Dismissed | one dashed line | "Undo" |

Nothing is pre-added to the review. Severity prefixes ("Should fix: ", "Nit: ", "Blocker: ") are prepended at post time, and the editor shows the prefix that will be used. Verification wording on the page is "checked in the code", "could not confirm", "disproved" and "being checked".

### Live updates

The page polls `/api/state` every 3 seconds while a stack is pending or a comment is being checked. When `index.html` changes on disk, it saves state and reloads. State lives in `localStorage` under `review-guide-v2-<run_id>`.

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
