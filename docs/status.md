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
- **A brief, not a writer exploring.** The first writers ran on Opus and explored with 25 to 40 sequential tool calls, taking 36 to 48 minutes each. Opus turns took 64 to 85 s, against 8 to 27 s for Sonnet on the same files. The brief plus a 15-call cap brought a Sonnet writer to about 4 minutes.
- **Opus again, with a brief and a read budget.** The Sonnet writer stopped at the brief, used the same shape on every stack (a flow, a table, steps) and drew arrows nobody checked against the code. The writer is back on Opus at `balanced` and `deep`, but it starts from the brief, reads only the code its diagrams cite (about 12 reads at `balanced`, 25 at `deep`, sent in batches rather than one per turn) and runs alongside the reviewers, which take longer. `quick` stays on Sonnet. What would undo this: an open-ended "explore the code" in the prompt, or one read per turn.
- **Page before agents.** The page opens on the file tree and diffs right after fetch, and reloads as each stage lands. A reviewer waits for nothing.
- **Sequence diagrams first, layered map collapsed.** The layered graph read as a tangle, even to the author of the code. A per-flow sequence diagram with plain-words roles is easier to follow, and the full map stays available one click away.
- **Suggestions are opt-in.** Pre-ticked checkboxes did not read as "suggested feedback". Suggestions now look like draft review comments from a separate author (✦). The reviewer adds each one explicitly, and adding it opens the editor.
- **Hard text limits.** The first page carried too much prose. Limits enforced by `build.py` keep every field to a sentence or two.
- **Taken from the hand-made "Journey of a turn" guide:** "know this first" facts and a test line per step. Left out: chapters, quizzes and stamps, which suit a teaching walkthrough but slow down a review.

## Measured

Test stack: two stacked PRs in a Java and Spring service (a Redis event log writer and its reader), 31 files, +1,204 lines.

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

## Model-driven guide (in progress, 2026-10-24)

Plan in `docs/plans/model-driven-guide.md`. Built, not yet run end to end through the skill:

- `guide-author` writes `guide.html` freely at `balanced` and `deep`; `guidehtml.py` sanitizes it on the server and `ghParse` again in the page. Steps, Ask on any `data-ref` part, `data-at` links into the diff, guide fixes and sharing all work with it.
- `director` writes `plan.json` with 1 to 8 reviewers; the runner starts them with the plan's model and effort as soon as it passes `--check`. The page, the stages strip, sharing and the verifier work with any reviewer ids.
- `quick` keeps the structured guide and the three built-in reviewers.
- Measured by hand on a 3-PR backend stack: author 11 min and $3.69 (19 sections, 113 code links, about 2,500 words of prose); with the trimmed prompt 3 min and $1.54 (535 words, which undershot); director 40 s and $0.46 (four reviewers, no spec reviewer).

- The guide is a walk (what it does, where it runs, the journey stage by stage, what can go wrong, what to decide), with each stage's code as "Read now" steps under it and the findings on those lines inside the steps. At most 3 tables.
- Pictures are drawn by figure artists in parallel; a map and a timeline are required when they apply.
- The page takes each new build in place, keeping the reader's position, and the progress panel shows the guide, the reviewers and the fact check as three columns.
- Measured on the same stack, full round at `balanced` (2026-10-24): guide readable at about 4 min, pictures at about 10 min, round finished in 14 min 21 s, 1,490 words of prose, 1 table, 7 steps, 3 pictures.

Not tested yet: a round through `/review-guide` as installed, the author on a stack the prompts have never seen (a small PR, a frontend stack), and the reviewers' recall against the built-in three. Reviewers are still the slowest part at `balanced`.

## Next

In priority order:

1. Install the plugin locally (`claude plugin marketplace add ~/Sources/review-guide`, then `claude plugin install review-guide@review-guide`) and run the skill end to end on the test stack.
2. Run it on the 6-PR stack behind the hand-made guide and compare it with the hand-made guide. This shows whether large stacks need several writers, one per subsystem.
3. Speed: run writer and verifier in an overlapping way (verify each comment as it is drafted), and split structure (map, flows, roles; small and fast) from findings. Target: under 5 minutes in total.
4. Add a light mode for small PRs: comments only, no map or flows.
5. Measure recall on about 10 merged PRs that had human review comments, to check that `skim` and `skip` roles are safe.
6. Release to the team: the repository is `doctolib/review-guide` (CHEAT-981), with CODEOWNERS, release-please and CI from the prompts marketplace scaffolding. Remaining: register it in `doctolib/prompts` as an external plugin, then install it from the marketplace on a second machine.
7. "Risks to review" cards with a status (accepted limit, decided, not discussed), taken from the hand-made guide.
