#!/usr/bin/env python3
"""Dev only: a stand-in for `claude -p` that replays a stream shaped like the live CLI's (init, tool lines, then the whole
answer at the end), so the page's question threads can be tried and checked without a model.

  REVIEW_GUIDE_CLAUDE=dev/stub_claude.py python3 plugins/review-guide/skills/review-guide/scripts/serve.py --home <home> --port 0

Environment: STUB_DELAY seconds between events (default 0.4); STUB_REPLIES a JSON file {"text in the question": "reply"},
the first match wins and "" is the default. A reply may carry a fenced `action` block; FINDING in it becomes the id of the
finding the thread is about. A question starting with "boom" fails once, so Retry can be tried.
"""
import json, os, pathlib, random, re, sys, tempfile, time

argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1]
question = prompt.split("\n")[0].removeprefix("Question: ")
resumed = "--resume" in argv
sid = argv[argv.index("--resume") + 1] if resumed else "stub-%06d" % random.randrange(10**6)
tmp = pathlib.Path(tempfile.gettempdir())

# A follow-up carries no target, so what the first turn saw is kept next to the session id.
memo = tmp / f"rg-stub-{sid}.json"
seen = json.loads(memo.read_text()) if memo.exists() else {}
if (m := re.search(r'"id": "([^"]+)", "axis"', prompt)):
    seen["finding"] = m.group(1)
if (m := re.search(r'"path": "([^"]+)", "line": (\d+)', prompt)):
    seen["at"] = f"{m.group(1)}:{m.group(2)}"
memo.write_text(json.dumps(seen))

replies = json.loads(pathlib.Path(os.environ["STUB_REPLIES"]).read_text()) if os.environ.get("STUB_REPLIES") else {}
default = f"Yes, it holds. The code is at `{seen.get('at', 'A.java:3')}`. Could not confirm how production is configured."
reply = next((v for k, v in replies.items() if k and k in question), replies.get("", default)).replace("FINDING", seen.get("finding", "x"))

delay = float(os.environ.get("STUB_DELAY", "0.4"))
out = lambda o: (print(json.dumps(o), flush=True), time.sleep(delay))
out({"type": "system", "subtype": "init", "session_id": sid})
if question.startswith("boom") and not (tmp / f"rg-stub-boom-{sid}").exists():
    (tmp / f"rg-stub-boom-{sid}").touch()
    print("claude: something broke", file=sys.stderr)
    sys.exit(1)
for name, inp in (("Read", {"file_path": "/w/tip/FrameCursor.java"}), ("Grep", {"pattern": "compareEntryIds"})):
    out({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}})
out({"type": "stream_event", "event": {"type": "message_start"}})
for word in reply.split(" "):
    print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": word + " "}}}), flush=True)
out({"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}})
out({"type": "result", "subtype": "success", "is_error": False, "result": reply, "session_id": sid})
