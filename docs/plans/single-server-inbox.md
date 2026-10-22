# One server, an inbox of reviews, state on disk

## Outcome

- At most one review-guide server runs. Every `/review-guide` invocation sends its review to it.
- The server's root page is an inbox: open reviews, then finished ones. Each review has rounds: every invocation with the same PRs and every Rerun adds one. Earlier rounds can be opened read-only.
- Page state lives on disk under `~/.cache/review-guide/`, not in `localStorage`. It survives server restarts, port changes and browser changes.
- A new round carries over your drafts, verdict text, and viewed marks on files that did not change. It marks the lines changed since the previous round.

Not in this change:
- Matching findings across rounds ("raised in round 2 too").
- Importing the existing work directories.
- Running several repositories in one review. The data model allows it (refs carry the repository); fetching still refuses it with a clear message.

## Decisions (settled)

| Question | Decision |
| --- | --- |
| What is one review | The set of PR refs given to one invocation. The same set, in any order, is the same review; a different set is a new review, even when it overlaps. |
| Same PRs again | A new round of the same review. |
| Carry over | Drafts, verdict text, and viewed marks on unchanged files. Lines changed since the previous round get the "new" mark. |
| Lifetime | Runs until stopped. Port 7357 by default, or a free port when that one is taken. Recorded in `server.json`. |
| Close on a review page | Becomes "Finish review": marks the review done and returns to the inbox. The inbox has "Stop server". |
| Old runs | Start fresh. |

## Layout on disk

```
~/.cache/review-guide/
  server.json                    {port, pid, token}, mode 0600
  prefs.json                     {theme}
  timings.json                   unchanged
  repos/                         unchanged (blobless clones)
  reviews/<slug>-<hash>/         hash = sha1 of the sorted refs ("owner/repo#n")
    review.json                  {id, refs, cwd, status: open|done, created, updated, rounds: [{n, effort, focus, created}]}
    rounds/<n>/                  a work directory, as today (run.json, stacks, guide.json, index.html, progress.json, posted.json)
    rounds/<n>/ui.json           the page state that used to sit in localStorage
```

Each round gets its own work directory, so a rerun on unchanged heads no longer overwrites the previous round. Fetching takes seconds, and the clone cache is shared, so the per-head-set work directory cache goes away.

## Flow

1. The skill runs `serve.py open --effort <e> [--focus ...] <refs>` from the user's clone.
   - This finds the running server through `server.json` and pings it. When nothing answers, it starts one detached.
   - It then sends the refs to `POST /api/reviews`, with the token from `server.json` and its cwd.
   - It prints the review URL and opens it in the browser.
   - With `--wait`, it prints status lines until the round ends, then a summary per stack (counts and finding titles) for the skill's report.
2. The server finds or creates the review, adds round n, and starts `rerun.py rounds/<n> --refs ... --prev rounds/<n-1>` detached (new session).
   - Its output goes to `rounds/<n>/runner.log`.
3. `rerun.py` fetches into the round directory (`fetch.py --workdir`), then runs the agents as today.
   - It records its pid, a `status` line, and finally `finished` and `error` in `progress.json`.
   - A server restart therefore does not kill a running round. The page keeps showing its progress, because "running" means the pid is alive and the round is not finished.
4. Rerun on the page goes through the same path: `POST /api/r/<id>/rerun` adds a round.

## Server routes (`serve.py`)

| Route | What |
| --- | --- |
| `GET /` | Inbox page (`ui/inbox.html`, data injected). |
| `GET /r/<id>` | Latest round's page. While its fetch runs, a small page that says so and reloads itself. |
| `GET /r/<id>/<n>` | A given round. Read-only when it is not the latest: no posting, no rerun, no saves, and a banner linking to the latest round. |
| `GET /api/reviews` | Inbox data, polled while something runs. |
| `POST /api/reviews` | Create a review or add a round (from `serve.py open`). |
| `GET /api/r/<id>/<n>/state` | As today's `/api/state`, plus rounds. |
| `PUT /api/r/<id>/<n>/ui` | Save page state. Latest round only. |
| `POST /api/r/<id>/<n>/review` | Post a review; the guards are unchanged. |
| `POST /api/r/<id>/rerun` | Add a round. |
| `POST /api/r/<id>/finish`, `/reopen` | Set the review's status. |
| `GET /api/r/<id>/heads` | As today. |
| `POST /api/stop` | Stop the server. |
| `PUT /api/prefs` | Theme. |

- The token rule is unchanged: every write needs it.
- The page gets `TOKEN`, `BASE` (`/api/r/<id>/<n>`), `UI` (saved state), `ROUNDS` and `THEME` injected the way `TOKEN` is today.

## Page changes (`template.html`)

- `state` comes from the injected `UI` (with defaults). `save()` sends a debounced `PUT`, and a `keepalive` flush on `pagehide`. Theme goes through `PUT /api/prefs`. All `localStorage` use is removed.
- Top bar:
  - the brand links to the inbox;
  - a round picker ("Round 3 of 3"), with each round's date, effort and posted reviews;
  - Close becomes "Finish review", keeping the same quiet, normal and done styling.
- "Since" box: a round-based `since` reads "Changed since the previous round (date)". The existing GitHub-review wording stays.
- Comments that could not be carried over get a box in "Your review" that shows their text.

## Carry-over (server, on the first page load of round n)

The server builds round n's `ui.json` from round n−1's `ui.json`, plus both rounds' `guide.json`:

- **Verdict text:** carried per PR, unless round n−1 posted on that PR. The verdict choice itself is not carried.
- **Your comments and accepted findings:** both become your comments in round n.
  - A line comment moves to the `+` line in the same file with the same text, nearest the old line number. `pr` and `pl` come from the new row.
  - A general comment stays general, on the same PR.
  - Comments on PRs that round n−1 already posted are not carried.
  - A comment whose line is gone goes into `lost` with its text.
- **Viewed:** kept for files not in round n's `since.files`.
- **Stack and filter:** kept. The view resets to the guide.

## Since the previous round (`fetch.py --prev P`)

- The previous round's `review_sha` per PR feeds `last_seen` the way GitHub reviews do today: the highest PR in the chain that has one.
- It is used only when some head moved. Otherwise the GitHub-review `since` stays.
- `since` gets `state: "ROUND"`. The brief titles that section "Changed since the previous round" and says to review the whole stack. The reviewer prompts key on the existing "Since the viewer's last review" header, so they do not narrow to the delta.

## Skill and docs

- **SKILL.md:** steps 2 to 4 become one `serve.py open --wait` call. The report reads its summary. Close becomes "Finish review". It gives the inbox URL.
- **docs/spec.md:** new sections on the inbox, rounds and persistence. Plus `CLAUDE.md` checks (the fixture is served with `serve.py --home`) and `docs/status.md`.

## Tests (`test_scripts.py`)

- Lay out a home with one review and two rounds on disk (the fixture work directory copied twice, with heads moved in round 2). Start `serve.py --home H --port 0`, then check:
  - the inbox lists the review;
  - `/r/<id>` serves round 2, and `/r/<id>/1` is read-only (the ui PUT is refused);
  - carry-over moves a custom comment to its new line, puts a vanished one in `lost`, and keeps viewed only on unchanged files;
  - ui PUT followed by a server restart returns the same state;
  - `finish` moves the review to done;
  - the token guards still hold;
  - `/api/stop` exits.
- `fetch.py` unit: a `--prev`-style `last_seen` with `ROUND` state picks the right sha.

## Risks

- **Detached runners outlive a stopped server.** That is intended. Stop server does not kill them; the next server picks them up through their pids.
- **Port 7357 taken by something else.** We fall back to a free port and write it to `server.json`. Bookmarked URLs break then, but the state does not.
- **Reused pids.** A pid could be reused after a reboot. The check also requires `progress.json` to have been modified less than 15 minutes ago, or the pid's command line to name `rerun.py`.
