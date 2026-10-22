# Status

Last updated 2026-10-22. Read `docs/spec.md` first.

## Built

- `fetch.py`: stack detection, lagging-child handling, line attribution, brief generation. Takes about 8 s on a 31-file stack.
- `guide-writer` and `comment-verifier` prompts, with budgets and the brief-first workflow.
- `build.py`: validation, partial rendering, merging of verifier output, comment routing.
- `serve.py`: one server per machine with an inbox of reviews, rounds, page state saved on disk with carry-over between rounds, token and posting guards. `serve.py open` is how the skills send PRs (or, with `--self`, the local branch stack) to it.
- `/review-guide:self-review`: reviews the local branch stack and uncommitted changes from the implementing session, and sends the feedback back to it instead of GitHub.
- `ui/template.html`: everything in the "Page" section of the spec.
- `test_scripts.py`: an offline test on a scratch two-PR stack in git. It covers attribution, routing, validation errors, rendering, and the server: inbox, rounds, carry-over, saved state across a restart, and the token and line guards. `--keep DIR` saves a server home with the fixture as a two-round review, so `serve.py --home DIR --port 0` can be used for UI work without agents.
- `claude plugin validate --strict` passes on the marketplace and the plugin.

## How the design got here

These are the decisions that a future change could undo by accident.

- **One stack, one guide.** The first version wrote one guide per PR. In a stack that showed the same file several times with no view of the target. The target diff plus line attribution fixed that while still posting per PR.
- **Sonnet with a brief, not Opus exploring.** The first writers ran on Opus and explored with 25 to 40 sequential tool calls, taking 36 to 48 minutes each. Opus turns took 64 to 85 s, against 8 to 27 s for Sonnet on the same files. The brief plus a 15-call cap brought the writer to about 4 minutes.
- **Page before agents.** The page opens on the file tree and diffs right after fetch, and reloads as each stage lands. A reviewer waits for nothing.
- **Sequence diagrams first, layered map collapsed.** The layered graph read as a tangle, even to the author of the code. A per-flow sequence diagram with plain-words roles is easier to follow, and the full map stays available one click away.
- **Suggestions are opt-in.** Pre-ticked checkboxes did not read as "suggested feedback". Suggestions now look like draft review comments from a separate author (✦). The reviewer adds each one explicitly, and adding it opens the editor.
- **Hard text limits.** The first page carried too much prose. Limits enforced by `build.py` keep every field to a sentence or two.
- **Taken from the hand-made "Journey of a turn" guide (ACT-57):** "know this first" facts and a test line per step. Left out: chapters, quizzes and stamps, which suit a teaching walkthrough but slow down a review.

## Measured

Test stack: doctolib/on-demand-care-chat #3564 (ACT-69, Redis event log writer) and #3565 (ACT-70, reader), 31 files, +1,204 lines.

| Run | Writer | Verifier | Total |
| --- | --- | --- | --- |
| Per PR, Opus, before the brief | 46 to 48 min each | 1 to 4 min | about 50 min |
| Stack, Sonnet, brief, 15-call cap | 6 min 51 s, 15 calls | 2 min 1 s, 10 calls | about 9 min |
| Same, prompt tightened to about 8 calls | 3 min 53 s, 8 calls | 3 min 33 s, 16 calls | about 7.5 min |

Two runs of the writer on the same stack produced 7 and 8 comments, with about 3 topics in common. Treat comments as a sample of real issues, not a complete review.

## Not tested yet

- The skill as installed (`/review-guide:review-guide`), including the `review-guide:guide-writer` agent names. All runs so far used general agents reading the prompt from a file.
- A real post to GitHub, especially multi-line comments and comments on a lower PR's `review_sha`.
- The "whole stack on GitHub" link, which uses compare-view file anchors.
- Repositories outside Java and Spring, stacks of more than 2 PRs, PRs under 150 lines, frontend and infra changes.

## Next

In priority order:

1. Install the plugin locally (`claude plugin marketplace add ~/Sources/review-guide`, then `claude plugin install review-guide@review-guide`) and run the skill end to end on the test stack.
2. Run it on the ACT-57 stack (6 PRs) and compare it with the hand-made guide. This shows whether large stacks need several writers, one per subsystem.
3. Speed: run writer and verifier in an overlapping way (verify each comment as it is drafted), and split structure (map, flows, roles; small and fast) from findings. Target: under 5 minutes in total.
4. Add a light mode for small PRs: comments only, no map or flows.
5. Measure recall on about 10 merged PRs that had human review comments, to check that `skim` and `skip` roles are safe.
6. Before sharing with the team: add a LICENSE, then choose a home. `doctolib/prompts` treats a new plugin as a significant change, which needs a discussion issue and 48 hours of feedback.
7. "Risks to review" cards with a status (accepted limit, decided, not discussed), taken from the ACT-57 guide.
