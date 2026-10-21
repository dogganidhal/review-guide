# Guide files

The guide is written per **stack**: a chain of PRs where each one's base branch is the previous one's head. A PR on its own is a stack of one. The stack is reviewed as one change, the **target**: the diff from the bottom PR's base to the top PR's head. Every comment is placed on a target line, and `build.py` routes it to the PR that wrote that line.

## Inputs (written by `fetch.py`, read-only)

| File | Content |
| --- | --- |
| `W/run.json` | `repo`, `viewer` (who posts), `checkout` (local clone or `null`), `warnings`, `prs` and `stacks`. Each PR has `n`, `title`, `body`, `author`, `sha`, `review_sha` and `stack`. Each stack has `id`, `prs` (bottom first), `base`, `tip` and `files`, and each file has `path`, `old`, `status`, `adds`, `dels` and `prs` (the PRs that touch it). |
| `W/<stack>/brief.md` | Start here. PR descriptions, warnings, the changed-file table, the whole target diff with tip line numbers and the PR that wrote each added line, and where each declared class or function is used. |
| `W/<stack>/target.diff` | The raw diff, base to tip. |
| `W/<stack>/tip/<path>` | Each changed file at the tip. All line numbers below refer to these files. |
| `W/<stack>/attribution.json` | Path to a map from tip line to `[pr, line in that PR]`, for each line the stack wrote. |
| `W/<stack>/repo-guidance/` | The repo's own review rules at the tip. Any of them may be missing. |
| `W/<n>/pr.diff` | One PR's own diff. Read it only to judge whether that PR is safe to merge before the PRs above it. |
| `W/<n>/existing-comments.json` | Review comments already on the PR. |

## `W/<stack>/guide.json` (guide writer)

```json
{
  "summary": "What the stack does and where the risk sits. Two sentences at most.",
  "key_facts": [{ "title": "Nothing changes for patients yet.", "text": "The stream is written but nothing reads it until the switch is on." }],
  "prs": {
    "3564": { "verdict": { "event": "COMMENT", "why": "What stands between this PR and approval." }, "review_body": "Posted with the review." }
  },
  "map": {
    "layers": [{ "id": "web", "label": "web-server" }, { "id": "redis", "label": "Redis" }],
    "nodes": [
      { "id": "writer", "label": "RedisAgUiEventLog", "role": "the event log writer", "layer": "web", "state": "new", "path": "src/.../RedisAgUiEventLog.java" },
      { "id": "stream", "label": "run stream", "layer": "redis", "state": "external", "path": null }
    ],
    "edges": [{ "from": "writer", "to": "stream", "label": "XADD + EXPIRE", "new": true }]
  },
  "flows": [
    { "title": "Write a frame", "steps": [{ "from": "writer", "to": "stream", "label": "XADD", "new": true }] }
  ],
  "files": { "src/.../RedisAgUiEventLog.java": { "role": "read", "why": "Appends frames and handles write failures." } },
  "steps": [
    {
      "id": "append",
      "kind": "read",
      "title": "Append and failure handling",
      "why": "One sentence on what the reviewer should look for here.",
      "nodes": ["writer"],
      "test": "RedisAgUiEventLogIT#append_expiresTheKeyAfterTheEndMarker",
      "anchor": { "path": "src/.../RedisAgUiEventLog.java", "start": 40, "end": 90 },
      "comments": [
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
  ]
}
```

Rules `build.py --check` enforces:

- **Brevity:** `summary` up to 300 characters; step `title` 70; step `why` 220; file `why` 90; comment `title` 90; comment `body` 700; `verdict.why` 160; `review_body` 400; node and edge labels 40.
- **`prs`:** one entry per PR in the stack. `event` is `COMMENT`, `APPROVE` or `REQUEST_CHANGES`, and must be `COMMENT` when `viewer` is the author. `REQUEST_CHANGES` needs a `blocker` routed to that PR, and `APPROVE` allows no `blocker` or `should` routed to it.
- **`map`:** up to 14 nodes. `role` (optional, up to 30 characters) names what the node is for in plain words; the sequence diagrams show it above the class name. `state` is `new`, `changed`, `existing` or `external`, and a `new` or `changed` node needs the changed file that holds it as its `path`. Edges and flow steps reference node ids. There are up to 3 flows of up to 10 steps each.
- **`files`:** one entry per changed file in the stack, no more. `role` is `read`, `skim` or `skip`.
- **`key_facts`:** up to 4; `title` up to 60 characters, `text` up to 200.
- **`steps[].test`:** the test that covers the step's behavior (up to 140 characters), or `null` when no test does.
- **`steps`:** 1 to 8. `kind` is `decide` or `read`. The anchor spans at most 60 lines of a changed file at the tip. `nodes` reference map ids.
- **Comments:**
  - `severity` is `blocker`, `should`, `question` or `nit`. At most 5 comments are routed to any one PR.
  - An inline comment's `line` (and `start_line` through `line`) must be lines the stack wrote, meaning they appear in `attribution.json`. A range must come from one PR.
  - A comment that cannot sit on such a line uses `path: null` and names its PR with `"pr": 3564`.
  - `evidence` lists the `path:line` references the claim rests on.

## `W/<stack>/verify.json` (verifier)

```json
{
  "comments": { "truncated-log": { "status": "verified", "note": "79-83 skip the DEL after a connection failure.", "line": 81 } },
  "missed": [{ "severity": "should", "title": "…", "path": "…", "line": 88, "body": "…", "evidence": ["…:85-88"] }]
}
```

- One entry per drafted comment id. `status` is `verified`, `unverified` or `refuted`, and `note` is one line.
- `line` is optional. Set it only to move the comment to a better line the stack wrote.
- `missed` holds at most three `blocker` or `should` findings, placed on lines the stack wrote.
