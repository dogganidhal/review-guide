# Guide files

The guide is written per **stack**: a chain of PRs where each one's base branch is the previous one's head. A PR on its own is a stack of one. The stack is reviewed as one change, the **target**: the diff from the bottom PR's base to the top PR's head. Every comment is placed on a target line, and `build.py` routes it to the PR that wrote that line.

## Inputs (written by `fetch.py`, read-only)

| File | Content |
| --- | --- |
| `W/run.json` | `repo`, `viewer` (who posts), `effort` (`quick`, `balanced` or `deep`; the prompt's `Effort:` line says the same), `checkout` (a local clone holding the whole repository at each stack's tip, for `git -C <checkout> grep` and `show`), `warnings`, `prs` and `stacks`. Each PR has `n`, `title`, `body`, `author`, `sha`, `review_sha` and `stack`. Each stack has `id`, `prs` (bottom first), `base`, `tip` and `files`, and each file has `path`, `old`, `status`, `adds`, `dels` and `prs` (the PRs that touch it). A PR also has `last_review` (the viewer's latest review: `state`, `sha`, `at`, or `null`). A stack the viewer reviewed before has `since`: `pr`, `state`, `sha`, `at`, and `files` (path to the tip lines added after that review); `state` is `ROUND` when it is the previous round of the guide instead. `mode` is `pr`, or `self` for a self-review of local work: then `source` holds the repository `root` and `branch`, each PR is a layer of the local branch stack (`n` is its position, not a GitHub number) with a `label` (the branch, `#123 · branch` when it has a PR, or "Uncommitted changes"), `pr_number` and `pushed`, and the feedback goes to the implementing agent, not GitHub. |
| `W/<stack>/brief.md` | Start here. PR descriptions, warnings, the changed-file table, the whole target diff with tip line numbers and the PR that wrote each added line, where each declared class or function is used, and what the stack removed that the repo still references (plus functions left without a caller). |
| `W/<stack>/spec.md` | The Jira tickets the PR titles and branches name (when Jira credentials are set) and the GitHub issues the PRs close. Says so when none was found. |
| `W/<stack>/target.diff` | The raw diff, base to tip. |
| `W/<stack>/tip/<path>` | Each changed file at the tip. All line numbers below refer to these files. |
| `W/<stack>/attribution.json` | Path to a map from tip line to `[pr, line in that PR]`, for each line the stack wrote. |
| `W/<stack>/repo-guidance/` | The repo's own review rules at the tip. Any of them may be missing. |
| `W/<n>/pr.diff` | One PR's own diff. Read it only to judge whether that PR is safe to merge before the PRs above it. |
| `W/<n>/existing-comments.json` | Review comments already on the PR. |

## `W/<stack>/plan.json` (director)

At `balanced` and `deep`, the director picks the stack's reviewers before they start. At `quick` there is no plan, and the reviewers are `correctness`, `standards` and `spec`.

```json
{
  "focus": "Where the risk sits and why these reviewers. Up to 400 characters.",
  "reviewers": [
    { "id": "retries-and-limits", "title": "Retries and rate limits", "hint": "timeouts, double charges, API outages",
      "charge": "What to hunt for, where, and what to leave to the others (40 to 1500 characters).",
      "paths": ["src/billing/**"], "model": "opus", "effort": "high" }
  ]
}
```

Rules `build.py --check --only plan` enforces: 1 to 8 reviewers; `id` is 2 to 28 lowercase letters, digits and dashes, starts with a letter, is unique and is not `guide`, `plan`, `check` or `verify`; `title` up to 40 characters, `hint` up to 90; `model` is `opus`, `sonnet` or `haiku`; `effort` is `low`, `medium` or `high`; `paths` is up to 12 globs or `null`. The runner starts one reviewer per entry with that model and effort, and the page groups findings by reviewer, in this order.

## `W/<stack>/guide/NN-<ref>.html` and `W/<stack>/files.json` (guide author)

At `balanced` and `deep` the guide is HTML the author writes freely: its own structure and pictures, within a length budget. It writes one file per section, `guide/NN-<ref>.html` with exactly one top-level `<section>`, in reading order by name, and the page shows each as it lands. `files.json` comes last and marks the guide complete. (A single `guide.html` holding every section is read the same way.) The page inserts the HTML into its own document after a sanitizer keeps only what is listed here, so anything else fails `--check`.

- **Length:** words of prose (outside tables, pictures, figure captions and code blocks) are capped at 1.5 per changed line of production code (tests, fixtures, docs and lockfiles do not count), at least 250 and at most 1,800. A table cell is at most 40 words. Both fail `--check`.
- **While writing:** `--check --only guide` checks the sections written so far (allowlist, code links, budget). Once `files.json` exists it also requires the overview first and at least 3 code links.
- **Top level:** only `<section>` elements. Each has a unique `data-ref` (lowercase letters, digits, `-` and `_`) and starts with an `<h2>`, which becomes its title in the outline. The first is `<section data-ref="overview">`.
- **Steps (reading the code as you go):** `<section data-step data-ref="…" data-at="path:start-end">` elements, each starting with an `<h3>`, placed right after the section whose code they show. `data-at` names at most 60 lines of a changed file at the tip; the page shows them under the step's text. `data-decide` marks a step where the reviewer makes a call. The page shows the steps after a section as its "Read now" (numbered across the whole guide, tracked as reviewed), and a finding on a step's lines inside that step instead of in the findings list. Steps with no section before them form a "Read in this order" group, as older guides did.
- **Tables:** at most 3 in the guide, `--check` counts them.
- **Tags:** `h2 h3 h4 p ul ol li code pre em strong table thead tbody tr th td figure figcaption section details summary blockquote span div br`, and inline `svg` with `g path rect circle ellipse line polyline polygon text tspan title desc`. No links, images, scripts, styles, forms, iframes, `defs`, `marker`, `use` or `foreignObject`.
- **Classes:** `callout` (a boxed point), `facts` (a grid of short facts, on a `ul`), `cols` (columns), `muted`, `tag-new`, `tag-changed`, `tag-removed`. No others.
- **Colors:** only `var(--ink)`, `var(--muted)`, `var(--line)`, `var(--card)`, `var(--accent)`, `var(--accent-soft)`, `var(--warn)`, `var(--warn-soft)`, `var(--good)`, `var(--good-soft)`, in `style` or in SVG `fill` and `stroke`. They follow the page's light and dark themes. `style` takes `fill`, `stroke`, `stroke-width`, `stroke-dasharray`, `opacity`, `color`, `background`, `text-anchor`, `font-weight`, `font-style` and `font-size` with those colors or plain numbers.
- **SVG:** a `viewBox`, no fixed width; the page sizes it to the column and scrolls it sideways on small screens. Geometry attributes take numbers, `transform` takes `translate`, `rotate`, `scale` and `matrix`. Arrowheads: `marker-end="url(#rg-arrow)"` (solid) or `url(#rg-arrow-dash)` (lighter); the page defines both. Put a `<title>` in a `<g>` to name it.
- **`data-at="path:line"`** (or `path:start-end`) on any element that stands for code: a `code` span, a table row or cell, a list item, a `g` in a picture. The path is a file the stack changed, or any file of the repository at the tip. The page opens the code there on click. `--check` fails when a reference is outside its file, and when the guide cites fewer than 3 lines.
- **`data-ref`** on any part a reviewer should be able to ask about on its own (a figure, a `g` in a picture, a table row, a list item). The page adds an Ask button, or for a picture part a popover, and keeps the threads next to it.
- **Size:** up to 300,000 characters in all, 60,000 per section.
- **Pictures** are drawn by figure artists, in parallel with the prose. The author writes a placeholder in a section, `<figure data-kind="<kind>" data-figure="<ref>"><figcaption>…</figcaption></figure>`, and a spec `W/<stack>/figures/<ref>.json`: `kind`, `question` (up to 200 characters), `show` (the artist's brief, up to 1,500), `caption` and `at` (the `path:line` references it is drawn from). `kind` is `map`, `timeline`, `sequence`, `lifecycle`, `decision`, `data` or `before-after`. The artist writes `figures/<ref>.svg`: one `<svg>` with a `viewBox`, under the same allowlist, at most 40,000 characters, labels that fit (`--check --only figure:<ref>`). The page shows the caption with a "being drawn" note until it lands. At most 6 pictures. Once the guide is complete, `--check` requires a `map` for a stack of 200 or more changed production lines, and a `timeline` when `timing.json` says so (`fetch.py` finds at least 3 added lines in source files that set a wait, timeout, expiry or retry; the brief lists them under "Time-dependent behavior").

`files.json` maps every changed file to `{"role": "read" | "skim" | "skip", "why": "…"}`, as `files` does in `guide.json` below.

## `W/<stack>/guide.json` (guide writer, `quick`)

The structured guide the `quick` effort writes, and every round from before the HTML guide. Findings live in the reviewers' files.

```json
{
  "summary": "What the stack does and where the risk sits. Two sentences at most.",
  "key_facts": [{ "title": "Nothing changes for customers yet.", "text": "Orders are written to the new table but nothing reads it until the flag is on." }],
  "components": {
    "layers": [{ "id": "api", "label": "api" }, { "id": "db", "label": "Database" }],
    "nodes": [
      { "id": "service", "label": "OrderService", "role": "places orders", "layer": "api", "state": "new", "path": "src/.../OrderService.java", "at": "src/.../OrderService.java:58" },
      { "id": "orders", "label": "orders table", "layer": "db", "state": "external", "path": null }
    ]
  },
  "sections": [
    { "type": "diagram", "kind": "sequence", "title": "Place an order", "participants": ["service", "orders"],
      "messages": [{ "from": "service", "to": "orders", "label": "INSERT order", "new": true, "at": "src/.../OrderService.java:68" }] },
    { "type": "risks", "title": "Where it can go wrong", "items": [
      { "title": "A failed payment leaves no order", "text": "The insert is rolled back, so support sees no trace of the attempt.", "status": "decided", "at": "src/.../OrderService.java:89" }] },
    { "type": "before_after", "title": "Notifications after this PR", "rows": [{ "aspect": "Order shipped", "before": "Email and SMS", "after": "Email only" }] },
    { "type": "table", "title": "What each PR does", "columns": ["PR", "Change", "Safe alone"], "rows": [["#1", "Writes orders", "yes"]] },
    { "type": "list", "title": "Rollout", "items": ["Merge after the old checkout flag is off."] },
    { "type": "steps", "title": "Read in this order", "steps": [{
        "id": "place", "kind": "read", "title": "Placing an order and failure handling",
        "why": "One sentence on what the reviewer should look for here.",
        "nodes": ["service"], "test": "OrderServiceIT#place_rollsBackOnPaymentFailure",
        "anchor": { "path": "src/.../OrderService.java", "start": 40, "end": 90 } }] }
  ],
  "files": { "src/.../OrderService.java": { "role": "read", "why": "Places orders and handles payment failures." } }
}
```

### Diagrams

A `diagram` section has a `kind`, picked by the question it answers. Each answers one question: at most 12 boxes (participants for `sequence`), or `--check` fails and asks for a split.

| `kind` | Answers | Boxes |
| --- | --- | --- |
| `sequence` | who calls whom, in order, and what happens on each outcome | `participants`: component ids |
| `flowchart` | how a request or job is decided and routed: branches, retries, pipelines | its own `nodes` |
| `states` | the lifecycle of one thing: an order, a task, a status column | its own `nodes` |
| `structure` | what depends on what after the change, and what moved or was removed | `nodes`: component ids, grouped by their `layer` |
| `data` | what a stored record, an event or a payload looks like, and what changed in it | its own `nodes`, each with `fields` |

```json
{ "type": "diagram", "kind": "sequence", "title": "GET /orders/{id}", "participants": ["api", "service", "store"], "messages": [
  { "from": "api", "to": "service", "label": "findOrder", "new": true, "at": "src/.../Api.java:392" },
  { "frame": "alt", "branches": [
    { "guard": "not the caller's", "messages": [{ "from": "service", "to": "api", "label": "throws 404", "at": "src/.../Service.java:289" }] },
    { "guard": "found", "messages": [
      { "frame": "loop", "branches": [{ "guard": "each line item", "messages": [{ "from": "service", "to": "store", "label": "load price", "at": "src/.../Service.java:301" }] }] },
      { "from": "store", "to": "service", "label": "prices", "return": true },
      { "note": "prices are cached for 5 min", "on": "service" }] }] }] }

{ "type": "diagram", "kind": "flowchart", "title": "Handling a webhook", "nodes": [
  { "id": "verify", "label": "verify signature", "at": "src/.../Webhook.java:47" }, { "id": "known", "label": "known event?", "shape": "decision" },
  { "id": "inbox", "label": "event inbox", "shape": "store" }, { "id": "done", "label": "200", "shape": "end" }],
  "edges": [{ "from": "verify", "to": "known", "at": "src/.../Webhook.java:52" }, { "from": "known", "to": "inbox", "label": "yes", "at": "src/.../Webhook.java:63" },
            { "from": "known", "to": "done", "label": "no, log and ack", "at": "src/.../Webhook.java:69" }] }

{ "type": "diagram", "kind": "states", "title": "Life of a payment", "nodes": [
  { "id": "pending", "label": "pending", "initial": true }, { "id": "authorized", "label": "authorized", "state": "new" }, { "id": "captured", "label": "captured", "terminal": true }],
  "edges": [{ "from": "pending", "to": "authorized", "label": "bank approves", "at": "src/.../Payment.java:61" },
            { "from": "authorized", "to": "captured", "label": "order ships", "guard": "within 7 days", "at": "src/.../Payment.java:82" }] }

{ "type": "diagram", "kind": "structure", "title": "Who writes and who reads orders", "nodes": ["service", "report", "legacy", "orders"],
  "edges": [{ "from": "service", "to": "orders", "label": "INSERT", "new": true, "at": "src/.../OrderService.java:68" }, { "from": "report", "to": "orders", "label": "SELECT", "at": "src/.../Report.java:47" }] }

{ "type": "diagram", "kind": "data", "title": "The order_created event", "nodes": [
  { "id": "event", "label": "order_created", "state": "changed", "at": "src/.../OrderEvent.java:35", "fields": [
    { "name": "order_id", "type": "UUID" }, { "name": "currency", "type": "ISO 4217", "change": "added" }] }], "edges": [] }
```

- **Node ids** are unique across the whole guide (components and every diagram's own nodes), so a step's `nodes` can point at a node of any diagram.
- **`state`** of a node is `new`, `changed`, `existing` (the default), `external` or `removed`. A removed node is drawn struck through and dashed, and its edges too.
- **`sequence`:** `messages` is an ordered list of calls (`from`, `to`, `label`, optional `new`, `return: true` for a dashed reply), notes (`note`, `on`: a participant) and frames (`frame`: `alt`, `opt` or `loop`, with `branches`: `guard` and `messages`). `alt` has 2 to 4 branches, one per outcome; `opt` and `loop` exactly one. Frames nest 2 deep. Up to 24 calls and notes.
- **`flowchart`:** a node's `shape` is `step` (the default), `decision`, `store` or `end`. Edges may carry a `label` ("yes", "pool full"). A back edge (a retry) is fine; it is drawn around the side.
- **`states`:** exactly one node is `initial`; final states are `terminal`. Every transition has a `label`, the event, and may have a `guard`.
- **`structure`:** `nodes` lists component ids; the layers of `components` become the boxes around them. It replaces the old `map`.
- **`data`:** each node has 1 to 12 `fields` of `name`, optional `type` and optional `change` (`added`, `removed`, `changed`).
- **Edges** (`flowchart`, `states`, `structure`, `data`) join nodes of the same diagram: `from`, `to`, optional `label`, `guard`, `new`. Up to 20 per diagram. `direction` is `TB` or `LR`; left out, the page picks (states and data read left to right when that fits).
- **`at`: grounding.** Any component, node, call, note, edge or risk that stands for code may carry `"at": "path:line"` (or `path:start-end`), a line at the tip. The path is a file the stack changed, or any file of the repository at the tip. The page opens the code there on click, and draws an arrow without `at` lighter, as a guess. Cite the line that makes the call for every arrow you can. `--check` prints how many arrows (calls and edges; returns do not count) cite no line, and fails when more than half do.

Rules `build.py --check --only guide` enforces:

- **Brevity:** `summary` up to 300 characters; section `title` 70; step `title` 70; step `why` 220; file `why` 90; table cells and before/after cells 160; list items 220; labels, guards, field names 40; field `type` 30; notes 90; node `role` 30; fact `title` 60 and `text` 200; risk `title` 70 and `text` 220; `test` 140.
- **`key_facts`:** up to 4.
- **`sections`:** 1 to 8, each with a `type` of `diagram`, `risks`, `before_after`, `table`, `list` or `steps`, and a `title`. (`html` sections are what `build.py` makes of `guide.html`; a guide fix on one keeps its `type` and replaces its `html`, under the rules above.)
  - `diagram`: see above.
  - `risks`: 1 to 6 `items` of `title`, `text` (one sentence), `status` (`open`, `decided` or `accepted`, an accepted limit) and optional `at`.
  - `before_after`: 1 to 30 `rows` of `aspect`, `before`, `after`.
  - `table`: 2 to 6 `columns`, 1 to 30 `rows`, each a list of one text cell per column.
  - `list`: 1 to 8 `items`.
  - `steps`: 1 to 8 steps. `kind` is `decide` or `read`. The anchor spans at most 60 lines of a changed file at the tip. `nodes` reference node ids of `components` or any diagram. `test` is the covering test or `null`.
- **`components`:** the code units `sequence` participants and `structure` nodes draw from; leave it out when neither is used. Up to 20 nodes, each in a declared layer. A `new` or `changed` node needs the changed file that holds it as its `path`; a `removed` one names the changed or deleted file, or `null`.
- **`files`:** one entry per changed file in the stack, no more. `role` is `read`, `skim` or `skip`.

Guides written before the diagram kinds used `flow` and `map` sections. `--check` rejects them; a plain build of an earlier round still renders a `flow` as a `sequence` and a `map` as a `structure` of every component.

## `W/<stack>/review-<reviewer>.json` (reviewers)

One file per reviewer of the stack: the ids in `plan.json`, or `correctness`, `standards` and `spec` without one.

```json
{
  "skipped": null,
  "findings": [
    {
      "id": "truncated-log",
      "severity": "question",
      "title": "Truncated log stays readable after a connection failure",
      "path": "src/.../EventLogWriter.java",
      "line": 81,
      "start_line": null,
      "body": "The comment exactly as it is posted.",
      "evidence": ["src/.../EventLogWriter.java:79-83"]
    }
  ]
}
```

Rules `build.py --check --only <reviewer>` enforces:

- `severity` is `blocker`, `should`, `question` or `nit`. `title` up to 90 characters, `body` up to 700. `evidence` lists `path:line` references.
- An inline finding's `line` (and `start_line` through `line`) must be lines the stack wrote, meaning they appear in `attribution.json`. A range must come from one PR. A finding that cannot sit on such a line uses `path: null` and names its PR with `"pr": 42`. That should be rare: the page shows such findings in the review summary, with links to their `evidence`, and an inline comment on the change that caused the problem reads better.
- At most 3 findings per PR per reviewer.
- `skipped` is `null`, or one sentence on why the charge could not be reviewed (for example, no spec).

The page suggests a verdict per PR from the findings the reviewer keeps: request changes with a blocker, comment with a should-fix or question, approve otherwise.

## `W/<stack>/verify/<reviewer>-<id>.json` (verifiers)

One verifier per finding, in parallel. Each writes one file:

```json
{ "status": "verified", "note": "79-83 skip the DEL after a connection failure.", "line": 81 }
```

- `status` is `verified`, `unverified`, `refuted` or `duplicate`, and `note` is one line.
- `line` is optional. Set it only to move the finding to a better line the stack wrote.
- `duplicate` means an earlier finding (by reviewer order, that of `plan.json` or correctness, standards, spec, then file order) makes the same point; the note names its key. The page hides duplicates. A finding is never a duplicate of a later one, so each point keeps one finding.
- A finding without a file is shown as still being checked.
