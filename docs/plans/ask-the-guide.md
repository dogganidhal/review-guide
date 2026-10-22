# M5: ask the agent from the page

## Outcome

While reviewing, you can ask a question about anything on the page and get an answer in place, from an agent that reads the code. The question can be about a diff line or a selection, a guide section, a diagram node or arrow, a reading step, or one of the suggested findings. The answer streams in under what you asked about, cites the code it rests on, and can turn into something you use: a draft comment, a corrected finding, or a fix to the guide.

Not in this change:
- Asking about several reviews at once, or about code outside the reviewed repository.
- Voice, or questions sent from GitHub.
- Answers that change code. In a self-review, code changes still go through the feedback sent to the implementing session.

## Decisions (settled)

| Question | Decision |
| --- | --- |
| Where questions and answers live | An inline thread under the thing you asked about, plus a "Questions" list in the rail that jumps to each thread. |
| What an answer can do | Answering is the default and changes nothing. Three actions, each one a button you press: "Turn into comment" drafts a review comment; on a finding, "Re-judge" updates its status with a note; on the guide, "Fix the guide" proposes an edit that you accept or reject. |
| Model | Sonnet answers by default, streaming. "Dig deeper" reruns the question on Opus with a larger reading budget. |
| Self-review | The page's own agent answers, so the implementing session is not disturbed. "Ask the implementer" adds the question to the feedback you send, for questions only the author can answer. |

## The experience

**Asking**
- **Diff:** select lines in a file view (or click a line number and pick "Ask"). A small bar appears: "Comment" (as today) and "Ask". "Ask" opens a box under the selection.
- **Guide:** each section, key fact and step has an "Ask" affordance on hover, at its top right. In a diagram, clicking a node or arrow opens its popover, which has "Ask about this" next to "Open the code".
- **Finding:** each suggested comment card gets "Ask" next to "Add to my review" and "Dismiss". Typical questions: "is this real?", "what would break?", "show me the caller".
- **Anywhere:** `?` opens a free question box in the rail. It is attached to whatever is selected or on screen, and you can remove the attachment.
- The box shows the attachment as a chip ("FrameCursor.java:21-30", "Step 3: The resume cursor", "Finding: pool full answers 503"). Suggested starters fit the target: "Why this way?", "What calls this?", "What breaks if…", "Is this tested?".

**Answering**
- The thread shows "Reading FrameCursor.java…", "git grep compareEntryIds…" while the agent works, the same live line as the progress panel. Then the answer streams in.
- Answers are short: a direct answer first, then the evidence. Every `path:line` is a link that opens the file view at that line, with the breadcrumb back to the thread. Code it quotes is shown highlighted.
- When the answer is a guess (library internals, production config), it says so in one line, the way the verifier marks "could not confirm".
- Follow-ups continue the same thread, with the agent keeping the context.
- "Dig deeper" reruns the last question on Opus and replaces the answer, keeping the first one folded.

**Actions on an answer**
- **Turn into comment:** drafts a review comment from the exchange, on the line the question was about (or the line the answer points to, when it is a line the stack wrote). It appears as one of your comments with the editor open. Nothing is posted.
- **Re-judge** (finding threads): when the conversation settles whether a finding holds, the agent proposes a status (checked in the code, or disproved) with a one-line note. You accept it, and the finding card updates with "re-judged after your question".
- **Fix the guide** (guide threads): the agent proposes a replacement for that section, fact or step, checked by `build.py`. You see the before and after and accept or reject it. An accepted fix is marked on the guide ("corrected after a question").
- **Ask the implementer** (self-review only): adds the question to "Your review", so it goes to the implementing session with the rest of the feedback.

**Finding them again**
- **Rail:** the Outline gets a "Questions (n)" group listing each thread by its first question, with a dot while one is being answered.
- **Saved:** threads are saved with the round and reloaded with the page.
- **New rounds:** threads carry over like drafts. A thread whose anchor moved follows it by text; one whose anchor disappeared moves to a "Questions from earlier rounds" list.

## How it works

**Server (`serve.py`)**
- **Asking:** `POST /api/r/<id>/<n>/ask` with `{thread, target, question, deep}` starts or continues a thread and returns at once. `target` describes what was asked about: `{kind: line|section|fact|step|node|finding|free, path, start, end, ref}`.
- **Storage:** threads live in `rounds/<n>/threads/<thread>.json`: the target, the messages, the agent's session id, the status, and any proposed action.
- **Following an answer:** `GET /api/r/<id>/<n>/threads/<thread>` returns the thread. While an answer is in flight, the page polls it every 500 ms; the server rewrites the file as the stream arrives. This reuses the polling design of the progress panel, so the page needs no new transport. Server-sent events would be smoother, and can replace polling later.
- **Actions:**
  - `POST .../threads/<thread>/apply` accepts a proposed action.
  - The server applies a re-judge to a stored override (`rejudged.json`), which `build.py` merges like a verifier result.
  - It applies a guide fix as `guide-fixes.json`, which `build.py` merges after checking it.
  - A draft comment goes into the page state like any custom comment.
- **Concurrency:** at most 3 answers run at a time per server; others wait in order. A thread runs one answer at a time.
- **Guards:** the token as for every write; read-only for earlier rounds; nothing is posted to GitHub.

**The answering agent (`agents/answerer.md`)**
- **Inputs:** the question, the target with its text (the lines, the section JSON, the finding with its evidence and verification note), the stack's `brief.md` path, `run.json`'s checkout and tip, and the thread so far.
- **Tools:** read only. It may use Read, Grep and Glob in the work directory and the checkout, and `git -C <checkout> show|grep|log`. It writes nothing; its proposed action comes back as a fenced JSON block at the end of its reply, which the server parses and validates.
- **Budget:**
  - Sonnet: about 8 tool calls;
  - "Dig deeper" on Opus: about 20.
- **Style:** a direct answer first, short, with every claim cited as `path:line`. It says "could not confirm" when it cannot.
- **Command:** the same `claude -p` shape as the review agents (`--agent`, `--plugin-dir`, dontAsk, `--strict-mcp-config`, empty `--setting-sources`), plus `--include-partial-messages`. Text arrives as `stream_event` lines whose `event.delta.type` is `text_delta`, so the answer streams word by word; tool calls feed the live line as today.
- **Read-only, enforced:** `--tools "Read,Grep,Glob,Bash"` with Bash allowed only for `git -C <checkout> show|grep|log`, and `--disallowedTools "mcp__*"` plus Write and Edit. The docs warn that Bash patterns are a convenience, not a boundary; if that is not enough, Bash is dropped and the agent reads blobs from the work directory only.
- **Follow-ups:** the first turn's `system/init` event carries the `session_id`, stored on the thread. A follow-up runs `claude -p --resume <session_id>` with the same `--agent`, `--plugin-dir`, `--add-dir` and tool flags (the docs say plugin dir and add-dir are not restored on resume, so they are passed every time). The answerer drops `--no-session-persistence`, which the review agents use, since resume needs the transcript. Turns of one thread are serialized: two resumes of one session at once interleave its transcript.
- **Later, if starts feel slow:** one long-lived `--input-format stream-json` process per open review. The CLI's stdin line format is not documented, so this waits for a test.

**Page (`template.html`)**
- The Ask affordances, the thread component (question, live line, streamed answer, links, actions) and the rail group.
- A thread stays open while you scroll away, and its answer keeps arriving.
- Threads use the same `el()` helpers and tokens as finding cards. The agent's messages carry the ✦ mark used for suggestions, so its text never reads as the reviewer's own.

## Data handling

- Questions and the code they point at go to the model, as the review agents' input already does. Nothing new leaves the machine, and nothing goes to GitHub unless you post a comment.
- Threads are stored under `~/.cache/review-guide/`, next to the round they belong to.

## Tests

- **`test_scripts.py`:**
  - the ask endpoint with a stub agent (an env var swaps the `claude` command for a script that replays a recorded stream);
  - thread storage and resume;
  - read-only rounds;
  - the parsed and validated actions (a bad re-judge or guide fix is refused);
  - carry-over of threads to a new round.
- **Page check (`dev/check_page.mjs`):** ask from a line, a section and a finding against the stub; the thread renders, links open the file at the line, and Back returns to the thread.
- **Live:** ten real questions on the #3564→#3571 stack, of each kind, with answer time and whether the answer was right.

## Risks

- **Wrong answers that sound sure.** The citation rule, the "could not confirm" rule and links on every claim make a bad answer checkable in one click. Re-judging and guide fixes are proposals you accept, never automatic.
- **Latency.** A cold `claude -p` start is several seconds before any reading. The live line shows progress at once. The session resume keeps follow-ups warm. If starts still feel slow, a long-lived process per open review (stream-json input) is the next step.
- **Cost.** Answers are bounded by the tool budget and the concurrency cap; Opus runs only on "Dig deeper".

## Order

M5 comes after M2 (diagram nodes and their popovers) and M1 (rail, breadcrumb), which are merged or in progress. It does not depend on M3 or M4.

1. Server, agent and stub, with tests.
2. Threads on findings and diff lines.
3. Guide, step and diagram targets, and the rail group.
4. The three actions.
5. The live trial on a real stack.

## State

- Phase: plan, waiting for review.
- Settled: the four decisions above, answered on 2026-10-22. Resume, partial streaming and tool restriction were checked against the Claude Code docs (headless, CLI reference, sessions, permissions).
