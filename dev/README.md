# dev tools

Not shipped with the plugin. Two checks for the pages (`check_page.mjs`, `check_diagrams.mjs`, Playwright) and the replay and compare pair below.

## Replay and compare

Writes the guides of the saved reviews again with the current guide writer, then shows the old and the new guide side by side for you to vote on.

```bash
python3 dev/replay.py --dry-run          # list what would run
python3 dev/replay.py                    # run it (about 5 minutes per stack, 3 stacks at once)
python3 dev/compare.py                   # prints the URL; open it and vote
```

### What it sends to the model

The same source code as the original runs. Each stack gets one guide-writer run (Opus, `balanced` effort, through `claude -p` with the plugin's own prompt and tool limits), reading the saved brief and the repository checkout recorded in the review. Findings, verifications and the fetch are reused, not run again. Replay prints this once at start.

### `replay.py`

- Reads `~/.cache/review-guide` (`--source`) and never writes there.
- Copies each review into `~/.cache/review-guide-replay` (`--home`). A review that is already copied is not copied again unless `--fresh`; a stack with a good result in `replay.json` is not run again, so an interrupted run resumes.
- The older `doctolib-*` work directories in the source root become one-round reviews. Only the stacks of the plan are kept (#3564-3565, #3569, #3564-3565-3571 twice); other runs, runs without a guide and older runs of the same PRs are listed and skipped.
- The new round is round N+1: a copy of the latest built round's work directory without each stack's `guide.json`. The guide writer runs on it, then `build.py --check` and `build.py`.
- `--only TEXT` replays the reviews whose id contains TEXT. `--jobs N` is the number of writers at once across all reviews (3). `--dry-run` lists and writes nothing. `--fresh` starts over.
- The old rounds are rebuilt in the copy with today's `build.py`, so both sides show today's page and only the guide differs. A run from before the reviewers existed gets "skipped" reviewer files so its page does not wait on them.

### Reading `replay.json`

`<home>/replay.json` has `reviews` (copy source, old round, new round) and one `stacks["<review id>/<stack id>"]` per stack:

| Field | Meaning |
| --- | --- |
| `ok`, `check` | `build.py --check --only guide` passed on the new guide; otherwise its error text. A failed stack still gets a page (files and diffs) |
| `seconds`, `calls`, `cost`, `attempts` | what the writer took, summed over attempts (one retry, as in `rerun.py`); `cost` is in dollars as reported by the CLI |
| `model` | the model of the init event, to confirm which model ran |
| `reply` | the last line of the writer's reply |
| `note`, `uncited`, `arrows` | the `--check` note, `N of M arrows cite no line`, as numbers. The plan wants under 20% |
| `old`, `new` | `seconds`, `calls` and `sections` of each guide. `sections` is the type of each section, `diagram:<kind>` for diagrams. The old numbers come from the first run's `progress.json` and are `null` when it had none; old `flow` and `map` sections count as `diagram:sequence` and `diagram:structure` |

### `compare.py`

`--home DIR` (the replay home) and `--port N` (default: a free one). Binds 127.0.0.1 only. For each stack it shows the real page of round N and round N+1 in two frames, read-only, scrolled together (untick "sync scroll" to scroll them apart). Below them: the vote buttons, a note, and the numbers of that stack from `replay.json`.

Keys: `1` old better, `2` new better, `3` same, arrows (or `j` and `k`) move between stacks. Pressing the same vote again clears it. The note is saved when you leave the field or move on.

Votes are written to `<home>/compare.json` as `{"<review id>/<stack id>": {"vote": "old|new|same|null", "note": "...", "at": <unix time>}}`. There is no browser storage.

The replay home also works with the normal server, which lists both rounds of each review: `python3 plugins/review-guide/skills/review-guide/scripts/serve.py --home ~/.cache/review-guide-replay --port 0`.
