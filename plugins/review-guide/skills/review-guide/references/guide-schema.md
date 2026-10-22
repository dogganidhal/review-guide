# Guide files

The guide is written per **stack**: a chain of PRs where each one's base branch is the previous one's head. A PR on its own is a stack of one. The stack is reviewed as one change, the **target**: the diff from the bottom PR's base to the top PR's head. Every comment is placed on a target line, and `build.py` routes it to the PR that wrote that line.

## Inputs (written by `fetch.py`, read-only)

| File | Content |
| --- | --- |
| `W/run.json` | `repo`, `viewer` (who posts), `effort` (`quick`, `balanced` or `deep`; the prompt's `Effort:` line says the same), `checkout` (a local clone holding the whole repository at each stack's tip, for `git -C <checkout> grep` and `show`), `warnings`, `prs` and `stacks`. Each PR has `n`, `title`, `body`, `author`, `sha`, `review_sha` and `stack`. Each stack has `id`, `prs` (bottom first), `base`, `tip` and `files`, and each file has `path`, `old`, `status`, `adds`, `dels` and `prs` (the PRs that touch it). A PR also has `last_review` (the viewer's latest review: `state`, `sha`, `at`, or `null`). A stack the viewer reviewed before has `since`: `pr`, `state`, `sha`, `at`, and `files` (path to the tip lines added after that review). |
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
      { "id": "writer", "label": "RedisAgUiEventLog", "role": "the event log writer", "layer": "web", "state": "new", "path": "src/.../RedisAgUiEventLog.java" },
      { "id": "stream", "label": "run stream", "layer": "redis", "state": "external", "path": null }
    ],
    "edges": [{ "from": "writer", "to": "stream", "label": "XADD + EXPIRE", "new": true }]
  },
  "sections": [
    { "type": "flow", "title": "Write a frame", "steps": [{ "from": "writer", "to": "stream", "label": "XADD", "new": true }] },
    { "type": "map", "title": "Components by layer" },
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

Rules `build.py --check --only guide` enforces:

- **Brevity:** `summary` up to 300 characters; section `title` 70; step `title` 70; step `why` 220; file `why` 90; table cells and before/after cells 160; list items 220; labels 40; node `role` 30; fact `title` 60 and `text` 200; `test` 140.
- **`key_facts`:** up to 4.
- **`sections`:** 1 to 5, each with a `type` of `flow`, `map`, `before_after`, `table`, `list` or `steps`, and a `title`.
  - `flow`: 1 to 10 hops, each `from` and `to` a component id, with a `label`.
  - `map`: draws `components`; no other field.
  - `before_after`: 1 to 30 `rows` of `aspect`, `before`, `after`.
  - `table`: 2 to 6 `columns`, 1 to 30 `rows`, each a list of one text cell per column.
  - `list`: 1 to 8 `items`.
  - `steps`: 1 to 8 steps. `kind` is `decide` or `read`. The anchor spans at most 60 lines of a changed file at the tip. `nodes` reference component ids. `test` is the covering test or `null`.
- **`components`:** needed by `flow` and `map`, optional otherwise. Up to 14 nodes. `state` is `new`, `changed`, `existing` or `external`, and a `new` or `changed` node needs the changed file that holds it as its `path`. Edges reference node ids.
- **`files`:** one entry per changed file in the stack, no more. `role` is `read`, `skim` or `skip`.

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
- An inline finding's `line` (and `start_line` through `line`) must be lines the stack wrote, meaning they appear in `attribution.json`. A range must come from one PR. A finding that cannot sit on such a line uses `path: null` and names its PR with `"pr": 3564`.
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
