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

## `W/<stack>/guide.json` (guide writer)

The explanation. Findings live in the reviewers' files.

```json
{
  "summary": "What the stack does and where the risk sits. Two sentences at most.",
  "key_facts": [{ "title": "Nothing changes for patients yet.", "text": "The stream is written but nothing reads it until the switch is on." }],
  "components": {
    "layers": [{ "id": "web", "label": "web-server" }, { "id": "redis", "label": "Redis" }],
    "nodes": [
      { "id": "writer", "label": "RedisAgUiEventLog", "role": "the event log writer", "layer": "web", "state": "new", "path": "src/.../RedisAgUiEventLog.java", "at": "src/.../RedisAgUiEventLog.java:58" },
      { "id": "stream", "label": "run stream", "layer": "redis", "state": "external", "path": null }
    ]
  },
  "sections": [
    { "type": "diagram", "kind": "sequence", "title": "Write a frame", "participants": ["writer", "stream"],
      "messages": [{ "from": "writer", "to": "stream", "label": "XADD", "new": true, "at": "src/.../RedisAgUiEventLog.java:68" }] },
    { "type": "risks", "title": "Where it can go wrong", "items": [
      { "title": "A failed write deletes the log", "text": "Readers see no log rather than one with a hole.", "status": "decided", "at": "src/.../RedisAgUiEventLog.java:89" }] },
    { "type": "before_after", "title": "Alerts after this PR", "rows": [{ "aspect": "Review created", "before": "SMS to the HCP on duty", "after": "Push only" }] },
    { "type": "table", "title": "What each PR does", "columns": ["PR", "Change", "Safe alone"], "rows": [["#3564", "Writes frames", "yes"]] },
    { "type": "list", "title": "Rollout", "items": ["Merge after the monolith switch is off."] },
    { "type": "steps", "title": "Read in this order", "steps": [{
        "id": "append", "kind": "read", "title": "Append and failure handling",
        "why": "One sentence on what the reviewer should look for here.",
        "nodes": ["writer"], "test": "RedisAgUiEventLogIT#append_expiresTheKey",
        "anchor": { "path": "src/.../RedisAgUiEventLog.java", "start": 40, "end": 90 } }] }
  ],
  "files": { "src/.../RedisAgUiEventLog.java": { "role": "read", "why": "Appends frames and handles write failures." } }
}
```

### Diagrams

A `diagram` section has a `kind`, picked by the question it answers. Each answers one question: at most 12 boxes (participants for `sequence`), or `--check` fails and asks for a split.

| `kind` | Answers | Boxes |
| --- | --- | --- |
| `sequence` | who calls whom, in order, and what happens on each outcome | `participants`: component ids |
| `flowchart` | how a request or job is decided and routed: branches, retries, pipelines | its own `nodes` |
| `states` | the lifecycle of one thing: a turn, a task, a status column | its own `nodes` |
| `structure` | what depends on what after the change, and what moved or was removed | `nodes`: component ids, grouped by their `layer` |
| `data` | what a stored record, an event or a payload looks like, and what changed in it | its own `nodes`, each with `fields` |

```json
{ "type": "diagram", "kind": "sequence", "title": "GET /turns/{id}", "participants": ["api", "service", "store"], "messages": [
  { "from": "api", "to": "service", "label": "readTurn", "new": true, "at": "src/.../Api.java:392" },
  { "frame": "alt", "branches": [
    { "guard": "no such turn", "messages": [{ "from": "service", "to": "api", "label": "throws 404", "at": "src/.../Service.java:289" }] },
    { "guard": "found", "messages": [
      { "frame": "loop", "branches": [{ "guard": "until end", "messages": [{ "from": "service", "to": "store", "label": "XREAD", "at": "src/.../Loop.java:47" }] }] },
      { "from": "store", "to": "service", "label": "entries", "return": true },
      { "note": "no terminal frame after a cut", "on": "api" }] }] }] }

{ "type": "diagram", "kind": "flowchart", "title": "The read loop", "nodes": [
  { "id": "read", "label": "XREAD", "at": "src/.../Loop.java:47" }, { "id": "any", "label": "entries?", "shape": "decision" },
  { "id": "save", "label": "the stream", "shape": "store" }, { "id": "done", "label": "ENDED", "shape": "end" }],
  "edges": [{ "from": "read", "to": "any", "at": "src/.../Loop.java:46" }, { "from": "any", "to": "done", "label": "yes", "at": "src/.../Loop.java:63" },
            { "from": "any", "to": "read", "label": "no, retry", "at": "src/.../Loop.java:69" }] }

{ "type": "diagram", "kind": "states", "title": "How a read ends", "nodes": [
  { "id": "wait", "label": "waiting", "initial": true }, { "id": "live", "label": "streaming", "state": "new" }, { "id": "ended", "label": "200, ended", "terminal": true }],
  "edges": [{ "from": "wait", "to": "live", "label": "first entry", "at": "src/.../Loop.java:61" },
            { "from": "live", "to": "ended", "label": "end entry", "guard": "end = 1", "at": "src/.../Loop.java:62" }] }

{ "type": "diagram", "kind": "structure", "title": "Who writes and who reads", "nodes": ["writer", "reader", "noop", "stream"],
  "edges": [{ "from": "writer", "to": "stream", "label": "XADD", "new": true, "at": "src/.../Writer.java:68" }, { "from": "reader", "to": "stream", "label": "XREAD", "at": "src/.../Loop.java:47" }] }

{ "type": "diagram", "kind": "data", "title": "A stream entry", "nodes": [
  { "id": "entry", "label": "stream entry", "state": "new", "at": "src/.../Writer.java:35", "fields": [
    { "name": "frame", "type": "JSON" }, { "name": "end", "type": "\"1\"", "change": "added" }] }], "edges": [] }
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
- **`sections`:** 1 to 8, each with a `type` of `diagram`, `risks`, `before_after`, `table`, `list` or `steps`, and a `title`.
  - `diagram`: see above.
  - `risks`: 1 to 6 `items` of `title`, `text` (one sentence), `status` (`open`, `decided` or `accepted`, an accepted limit) and optional `at`.
  - `before_after`: 1 to 30 `rows` of `aspect`, `before`, `after`.
  - `table`: 2 to 6 `columns`, 1 to 30 `rows`, each a list of one text cell per column.
  - `list`: 1 to 8 `items`.
  - `steps`: 1 to 8 steps. `kind` is `decide` or `read`. The anchor spans at most 60 lines of a changed file at the tip. `nodes` reference node ids of `components` or any diagram. `test` is the covering test or `null`.
- **`components`:** the code units `sequence` participants and `structure` nodes draw from; leave it out when neither is used. Up to 20 nodes, each in a declared layer. A `new` or `changed` node needs the changed file that holds it as its `path`; a `removed` one names the changed or deleted file, or `null`.
- **`files`:** one entry per changed file in the stack, no more. `role` is `read`, `skim` or `skip`.

Guides written before the diagram kinds used `flow` and `map` sections. `--check` rejects them; a plain build of an earlier round still renders a `flow` as a `sequence` and a `map` as a `structure` of every component.

## `W/<stack>/review-<axis>.json` (reviewers)

One file per axis: `correctness`, `standards`, `spec`.

```json
{
  "skipped": null,
  "findings": [
    {
      "id": "truncated-log",
      "severity": "question",
      "title": "Truncated log stays readable after a connection failure",
      "path": "src/.../RedisAgUiEventLog.java",
      "line": 81,
      "start_line": null,
      "body": "The comment exactly as it is posted.",
      "evidence": ["src/.../RedisAgUiEventLog.java:79-83"]
    }
  ]
}
```

Rules `build.py --check --only <axis>` enforces:

- `severity` is `blocker`, `should`, `question` or `nit`. `title` up to 90 characters, `body` up to 700. `evidence` lists `path:line` references.
- An inline finding's `line` (and `start_line` through `line`) must be lines the stack wrote, meaning they appear in `attribution.json`. A range must come from one PR. A finding that cannot sit on such a line uses `path: null` and names its PR with `"pr": 3564`. That should be rare: the page shows such findings in the review summary, with links to their `evidence`, and an inline comment on the change that caused the problem reads better.
- At most 3 findings per PR per axis.
- `skipped` is `null`, or one sentence on why the axis could not be reviewed (for example, no spec).

The page suggests a verdict per PR from the findings the reviewer keeps: request changes with a blocker, comment with a should-fix or question, approve otherwise.

## `W/<stack>/verify/<axis>-<id>.json` (verifiers)

One verifier per finding, in parallel. Each writes one file:

```json
{ "status": "verified", "note": "79-83 skip the DEL after a connection failure.", "line": 81 }
```

- `status` is `verified`, `unverified`, `refuted` or `duplicate`, and `note` is one line.
- `line` is optional. Set it only to move the finding to a better line the stack wrote.
- `duplicate` means an earlier finding (by axis order correctness, standards, spec, then file order) makes the same point; the note names its key. The page hides duplicates. A finding is never a duplicate of a later one, so each point keeps one finding.
- A finding without a file is shown as still being checked.
