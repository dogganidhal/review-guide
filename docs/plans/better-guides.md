# Better guides: the right diagram for each change, a stronger writer, and a page you can find your way around

## Outcome

The guide becomes the main reason to open the page. It shows how this particular change works, using the kind of picture that fits it. Every arrow points at the code that makes it happen. The page uses the whole screen, and you always know where you are in the review and what is left.

What is wrong today:
- **The page can draw two diagrams, and the writer uses one.** A `flow` always renders as a straight sequence diagram. A `map` is the old layered graph. Across the 11 guides saved on this machine, `flow` appears in 6, `map` in none, and the other 5 have no diagram. A state machine, a branching pipeline or a data model has no shape, so the writer falls back to a table.
- **The writer runs on Sonnet with a small budget.** At balanced and quick it reads the brief and stops. Nothing ties its diagrams to the code, so an arrow can be wrong and nobody checks it.
- **The page is hard to navigate.** The top bar holds the back link, the PR chips (two lines for three PRs), the tabs, the theme toggle and Finish. The same PR list appears again in the guide. Once you open a file, you lose your place in the guide. Nothing shows how far along you are.
- **The guide uses about half the width.** `.wrap` caps it at 1060 px, so diagrams scroll sideways next to an empty half of the screen.

Not in this change:
- A critic agent that reviews the guide. Opus plus grounded arrows comes first; a critic can follow if the replay shows it is needed.
- Splitting large stacks across several writers.
- Matching findings across rounds.

## Decisions (settled)

| Question | Decision |
| --- | --- |
| How diagrams are drawn | Our own SVG renderers in `template.html`, no library. One layered layout engine serves the graph kinds, and the sequence renderer is extended. |
| Which model writes the guide | Opus at balanced and deep, with a larger budget for reading code around the entry points. Quick stays on Sonnet. |
| Navigation | A one-line top bar with a compact stack switcher, four stages with progress (Understand, Read, Findings, Send), and a left rail that switches between the guide's outline and the files. File views get a breadcrumb, and going back returns to the exact place. |
| How we judge it | Replay the saved stacks: build a new guide next to each existing one and compare them side by side. The prompt gets tuned on the ones that lose. |

## Milestone 1: the page frame (no contract change)

This can be shown on the guides that already exist.

**Width**
- Prose (summary, fact text, step text) stays at about 80 characters a line for reading.
- Tiles, diagrams, tables, steps, finding cards and code use the full content width, up to about 1600 px.
- "Know this first" goes to two columns when there is room.

**Top bar, one line**
- Left: "← Reviews", then the review's title (the PR title, "Stack of 3 PRs", or the self-review branch).
- The stack switcher: one chip with a dot per PR and "3 PRs ▾" (or "3 layers ▾"). Its popover lists each PR with its title, CI state, a GitHub link and "Show only what it wrote". This replaces the chain of chips and the dropdown used above five PRs.
- Middle: the stages, each with its progress: `Understand · Read 2/6 · Findings 3/7 · Send (4)`.
- Right: a "⋯" menu with the round picker, Rerun, the theme and Finish review. Finish moves out of the menu and becomes a primary button once every PR is posted, or once the feedback is sent.
- The PR list in the guide stays where it is, so the details are still one scroll away. The top bar only carries the switcher.

**Stages**
- **Understand:** the overview (title, summary, tiles, warnings, "Since the previous round", "Know this first"), then the diagram and table sections.
- **Read:** the reading-order steps.
- **Findings:** the reviewers' findings by axis.
- **Send:** "Your review", the current second tab.
- The first three are one scrolling document; a stage link scrolls to its part and marks the stage you are in. Send stays its own view.
- Progress counts:
  - Read: steps marked reviewed.
  - Findings: findings added or dismissed, out of all of them.
  - Send: comments in your review.

**Left rail: Outline | Files**
- **Outline:**
  - Overview, then each section by title.
  - The steps, with ✓ for the reviewed ones.
  - The findings, by axis, with the count still waiting for a decision.
  - Send.
  - The part on screen is highlighted as you scroll.
- **Files:** the current tree, unchanged.
- **Default:** the rail opens on Outline, and on Files once a file is open. The choice is saved with the page state.

**Keeping your place**
- Opening a file from a step, a finding or a diagram records where you came from.
- The file view shows a breadcrumb: `Guide › Read in this order › 3. The resume cursor › Cursor.java`.
- "← Back" returns to that spot, at the same scroll position.
- A file opened from a step gets "← 2. …" and "4. … →", to move along the reading order without going back to the guide.
- A file opened from a finding gets the previous and next finding.
- Files opened from the tree keep the current Prev/Next file buttons.

**Spec and tests**
- `docs/spec.md` "Layout" is rewritten.
- `test_scripts.py --keep` still produces a home for UI work.
- A Playwright check (dev only, under `dev/`) loads the fixture and checks that:
  - the page throws no errors;
  - the top bar is one line at 1440 px;
  - Back restores the scroll position.

## Milestone 2: diagrams and the new guide contract

**Diagram kinds**

Each kind is a `diagram` section with a `kind`, chosen by the question it answers.

| Kind | Answers | Drawn as |
| --- | --- | --- |
| `sequence` | Who calls whom, in what order, and what happens on each outcome | Participants as columns. Calls are solid and returns dashed, with self-calls. `alt`, `opt` and `loop` frames carry a guard (for example "no frame within 30 s", "pool full"), and notes sit on a participant. Today's `flow` becomes this, and a table like "How a GET ends" can become `alt` branches on the one diagram. |
| `flowchart` | How a request or job is decided and routed: branches, fan-out, retries, pipelines | A layered top-to-bottom graph with step, decision (diamond), store and end nodes. Edges are labeled ("yes", "pool full"). |
| `states` | The lifecycle of a thing: a turn, a task, a record status | States left to right from the initial one, transitions labeled with event and guard, terminal states double-bordered, back edges routed around the side. |
| `structure` | What depends on what after the change, and what moved or was removed | Components grouped in boxes by module or layer, with dependency edges. States are new, changed, existing, external and removed (struck through and dashed), so one picture shows a refactor or a removal. It replaces `map`. |
| `data` | What a stored record, an event or an API payload looks like, and what changed in it | Entity cards listing their fields. Added, removed and changed fields are marked, and relations connect the cards. |

**Grounded in the code**
- Every node and every arrow that stands for code can carry `at: "path:line"`, a line at the tip.
- `build.py` checks that the file exists in the stack, or in the checkout for code outside it, and that the line is within it.
- On the page:
  - Clicking a node or an arrow opens the file view at that line, with the breadcrumb back to the diagram.
  - Hovering highlights the node's edges.
  - A step still lights up its nodes, in whichever diagram holds them.
- The writer is told to cite a line for every arrow it can. An arrow with no line gets a lighter look, so a guessed hop shows as one.

**One layout engine for flowchart, states, structure and data**
1. **Ranks:** longest path from the sources. Cycles are broken by a depth-first search, and the back edges are drawn reversed.
2. **Order within a rank:** barycenter sweeps. This comes from today's `mapView`, which already does it.
3. **Positions:** nodes of different sizes; data cards are tall.
4. **Edges:** spread over ports on the node edges, curved or orthogonal. Labels are placed where they do not collide, also from `mapView`.
5. **Direction and groups:** top to bottom or left to right, with clusters (the module boxes in `structure`).

The old map read as a tangle for two reasons: it held every component at once, and it ordered nodes only by layer. Diagrams are now scoped to one question, with at most 12 nodes. A diagram over 12 nodes fails validation, and the writer is told to split it.

**Fit and size**
- A diagram fills the content width and scales down to a minimum readable text size.
- Past that, it scrolls inside its frame and gets an "Expand" button that opens it full screen.

**Risks section**
- A `risks` section lists 1 to 6 cards. Each card has a title, one sentence, a `status` (`open`, `decided`, `accepted limit`) and an `at` reference.
- This is item 7 of the "Next" list in `docs/status.md`, taken from the hand-made guide.
- The summary already names where the risk sits. This gives each risk a status the reviewer can see at a glance.

**Contract changes, together**
- **`references/guide-schema.md`:**
  - `components` stays as the list of code units, referenced by `sequence` participants and `structure` nodes.
  - `flowchart`, `states` and `data` declare their own nodes.
  - Node ids are unique across the guide, so steps can point at any of them.
  - `flow` and `map` are removed. Pages built for earlier rounds are already saved as HTML and keep working.
- **`build.py`:** checks for each kind (node and edge ids, frame nesting, `at` references, size limits) and rendering.
- **`test_scripts.py`:** a fixture guide with one diagram of each kind, the validation errors, and the layout check (no overlapping nodes; every edge ends on its nodes) run through the Playwright check from M1.
- **`template.html`:** the renderers.
- **`docs/spec.md`:** updated.

## Milestone 3: the writer

**Model and budget**
- **Effort per agent in `rerun.py`:**
  - The guide writer runs on Opus at balanced and deep, and on Sonnet at quick.
  - The reviewers and the verifier keep their current settings.
  - `MODELS` becomes per agent.
- **Budget:**
  - quick: the brief and spec;
  - balanced: about 12 extra reads, around the entry points and the code the arrows cite;
  - deep: about 25.
- **Time:** the writer runs alongside the reviewers, which take 30 s to 30 min, and the page shows the diffs meanwhile. An extra 1 to 3 minutes for the guide does not hold up anything else.

**Prompt (`agents/guide-writer.md`), rewritten around the reader**
1. **Questions first:** list the questions a reviewer new to this code will have, about what it does, how it runs, what can break and what they must decide, before writing anything. Each section answers one of them and says which in its title.
2. **Choosing a shape:** a table maps each kind of question to a shape, with a short example of each from a real stack. Among them:
   - outcomes of a request, a `sequence` with `alt` frames;
   - a status column, a `states` diagram;
   - a removal, a `structure` diagram with removed parts plus `before_after`;
   - a payload or migration, a `data` diagram.
3. **No default shape:** the "a new feature: flow, then steps" nudge goes away.
4. **Ground it:** read the code each arrow claims and cite its line; leave out a hop that cannot be cited, or mark it as inferred.
5. **Check what the reader takes away:** reread the summary and the key facts against the diagrams. A fact the diagrams contradict is fixed before writing.
6. **Kept from today:** the writing rules, key facts, steps with anchors and tests, and file roles.

**Grounding check**
- `build.py --check --only guide` reports how many arrows have no `at`.
- Past half of the arrows, it fails and asks the writer to cite them or drop them, so an uncited diagram cannot pass as checked.

## Milestone 4: replay and compare (dev tooling, not shipped)

- **`dev/replay.py`:**
  - copies every saved review into a scratch home (`~/.cache/review-guide-replay`);
  - for each, adds a round that reuses the saved fetch, findings and verifications;
  - runs only the new guide writer on it, then builds.
  - The reviews in `~/.cache/review-guide` are not touched.
- **The saved stacks:**
  - the 8 stacks saved from earlier runs, kept in the author's local cache (not in this repository).
  - The writers are run again only for these 8 stacks; the source code goes to the model as it did the first time.
- **`dev/compare.html`:** served by the replay server, it shows round 1 (the old guide) and round 2 (the new one) side by side for each stack, with "old better", "new better" and "same" and a note. The answers go to `compare.json` in the scratch home.
- **You** go through the 8 pairs. The prompt is tuned on the stacks where the new guide loses, and those are replayed again.
- **Done when:** the new guide wins or ties on every stack, each guide uses at least one diagram kind other than `sequence` where one fits, and fewer than 20% of arrows lack a line.

## Order and checks

M1 and M2 can be built in parallel; they touch different parts of `template.html` (frame and navigation versus section rendering). M3 needs M2's contract. M4 needs M3.

Checks for every milestone:
```bash
python3 -I plugins/review-guide/skills/review-guide/scripts/test_scripts.py
claude plugin validate ./ --strict
claude plugin validate ./plugins/review-guide --strict
```
Then the Playwright check on the fixture home, and screenshots of a real review at 1440 and 1920 px for review.

## Risks

- **Layout quality is the hard part.** That is why the old map was hidden. Scope keeps each diagram small, and M2 is judged on screenshots of real stacks, not only on the fixture.
- **Opus time and cost per guide.** About 1 to 3 more minutes, hidden behind the reviewers. The replay measures the real numbers, and quick stays on Sonnet.
- **Size of `template.html`.** It is about 107 KB and grows with four renderers. It stays one file because the page is a single self-contained file, but the diagram code sits in one block.
- **Wrong diagrams.** Citing a line for every arrow, plus the build check, makes a guess visible. Verifying diagrams the way findings are verified is left out until the replay shows guesses get through.

## State

- Phase: done.
- M1 to M3 merged and installed. M4 tooling is in `dev/` (`replay.py`, `compare.py`).
- Result: on 11 saved stacks the new guide won every vote (2026-10-22). The one note, oversized diagrams on cleanup PRs, led to a prompt rule that sizes the guide to the change; the four cleanup stacks were replayed and judged better.
- Left open: a writer takes 1 to 5 minutes on Opus against 0.5 to 4 before; it runs beside the reviewers.
