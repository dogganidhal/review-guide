"""Questions asked from the review page: threads on disk, the answering agent, and the actions an answer proposes.

serve.py routes to `Asker`. A thread is `rounds/<n>/threads/<thread>.json`; its turns run one at a time in a background
thread through `claude -p --agent review-guide:answerer`, and the file is rewritten as the stream arrives, so a poll
of the thread shows the answer growing. Nothing here calls GitHub or writes outside the round's directory.

`REVIEW_GUIDE_CLAUDE` names the command to run instead of `claude` (the tests swap in a stub that replays a stream).
"""

import json
import os
import pathlib
import re
import secrets
import subprocess
import tempfile
import threading
import time

from build import LIMITS, MAX_ANCHOR_LINES, SEVERITIES, GuideCheck, Stack, check_section, check_text
from rerun import DENY, PLUGIN, describe

MAX_RUNNING = 3
TURN_TIMEOUT = 900
SAVE_EVERY = 0.15
KINDS = ("line", "section", "fact", "step", "node", "finding", "item", "free")
BUSY = ("queued", "running")
THREAD = re.compile(r"[\w-]{1,40}")
ACTION = re.compile(r"```action[ \t]*\r?\n(.*?)\r?\n?```[ \t]*\Z", re.S)
FENCE_START = re.compile(r"`{1,3}(?:a(?:c(?:t(?:i(?:on?)?)?)?)?)?\Z")  # the start of an action fence, still arriving


def read(path: pathlib.Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write(path: pathlib.Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def load_round(rdir: pathlib.Path) -> tuple[dict, dict]:
    data, run = read(rdir / "guide.json"), read(rdir / "run.json")
    if not data or not run:
        raise ValueError("this round has no guide yet")
    return data, run


def need(label: str, value, limit: int) -> None:
    errs: list[str] = []
    check_text(errs.append, label, value, limit)
    if errs:
        raise ValueError(errs[0])


# ---- targets: what a question is about ----------------------------------------------------------------------------

def steps_of(st: dict) -> list[dict]:
    return [x for sec in st["sections"] if sec["type"] == "steps" for x in sec["steps"]]


def source_step(s: dict, sid: str) -> dict:
    """A rendered step in the shape guide.json's author wrote it, which is also the shape a guide fix replaces it with."""
    return {"id": s["id"].removeprefix(sid + "-"), "kind": s["kind"], "title": s["title"], "why": s["why"], "nodes": s["nodes"],
            "test": s.get("test") or None, "anchor": {"path": s["path"], "start": s["start"], "end": s["end"]}}


def source_section(sec: dict, sid: str) -> dict:
    return {**sec, "steps": [source_step(s, sid) for s in sec["steps"]]} if sec["type"] == "steps" else sec


def index(ref, items: list, what: str) -> int:
    if not isinstance(ref, int) or isinstance(ref, bool) or not 0 <= ref < len(items):
        raise ValueError(f"target.ref must be a {what} index from 0 to {len(items) - 1}")
    return ref


ROWS = {"risks": "items", "list": "items", "table": "rows", "before_after": "rows"}


def row_label(sec: dict, row) -> str:
    """What a row is called in a thread's chip: a risk's title, a table row's first cell, a list item's text."""
    first = row["title"] if sec["type"] == "risks" else row["aspect"] if sec["type"] == "before_after" else row[0] if sec["type"] == "table" else row
    return str(first)[:80]


def resolve(data: dict, rdir: pathlib.Path, t) -> tuple[dict, dict | None, str]:
    """Check a target against the round's guide.json and fetch what it points at: (the target as stored, its stack in
    guide.json, the text the agent is given). Raises ValueError for a target that is not in this round."""
    if not isinstance(t, dict) or t.get("kind") not in KINDS:
        raise ValueError(f"target.kind must be one of {KINDS}")
    kind, ref, path = t["kind"], t.get("ref"), t.get("path")
    has = {"line": lambda s: any(f["path"] == path for f in s["files"]),
           "finding": lambda s: any(c["id"] == ref for c in s["comments"]),
           "step": lambda s: any(x["id"] == ref for x in steps_of(s))}.get(kind)
    stacks = [s for s in data["stacks"] if s["id"] == t["stack"]] if t.get("stack") else [s for s in data["stacks"] if not has or has(s)]
    st = stacks[0] if stacks and (len(stacks) == 1 or has) else None
    if kind == "free":
        return {"kind": "free", "stack": st and st["id"], "label": "Free question"}, st, ""
    if st is None:
        raise ValueError("target.stack is required when a review has several stacks" if len(stacks) > 1 else "target is not in this round")
    sid, out = st["id"], {"kind": kind, "stack": st["id"]}
    if kind == "line":
        start = t.get("start")
        end = t.get("end", start)
        f = rdir / sid / "tip" / str(path)
        if not any(x["path"] == path for x in st["files"]) or not f.is_file():
            raise ValueError(f"target.path {path!r} is not a changed text file in this stack")
        lines = f.read_text().splitlines()
        if not (isinstance(start, int) and isinstance(end, int) and 1 <= start <= end <= len(lines)):
            raise ValueError(f"target lines {start}..{end} are outside {path} (1..{len(lines)})")
        if end - start + 1 > MAX_ANCHOR_LINES:
            raise ValueError(f"target spans {end - start + 1} lines; the limit is {MAX_ANCHOR_LINES}")
        picked = lines[start - 1:end]
        out |= {"path": path, "start": start, "end": end, "label": f"{pathlib.PurePath(path).name}:{start}" + (f"-{end}" if end > start else ""),
                "text": "\n".join(picked)}
        return out, st, "\n".join(f"{start + i}: {x}" for i, x in enumerate(picked))
    if kind == "section":
        i = index(ref, st["sections"], "section")
        sec = st["sections"][i]
        return out | {"ref": i, "label": sec["title"]}, st, json.dumps(source_section(sec, sid), ensure_ascii=False)
    if kind == "item":
        i = index(ref, st["sections"], "section")
        sec = st["sections"][i]
        if sec["type"] not in ROWS:
            raise ValueError("target.ref is a section without rows")
        j = index(t.get("item"), sec[ROWS[sec["type"]]], "row")
        row = sec[ROWS[sec["type"]]][j]
        body = {"section": sec["title"], "type": sec["type"], **({"columns": sec["columns"]} if sec["type"] == "table" else {}), "row": row}
        return out | {"ref": i, "item": j, "label": row_label(sec, row), "section_title": sec["title"]}, st, json.dumps(body, ensure_ascii=False)
    if kind == "fact":
        i = index(ref, st["key_facts"], "key fact")
        return out | {"ref": i, "label": st["key_facts"][i]["title"]}, st, json.dumps(st["key_facts"][i], ensure_ascii=False)
    if kind == "step":
        s = next((x for x in steps_of(st) if x["id"] == ref), None)
        if s is None:
            raise ValueError(f"no step {ref!r} in stack {sid}")
        return out | {"ref": ref, "label": s["title"]}, st, json.dumps(source_step(s, sid), ensure_ascii=False)
    if kind == "finding":
        c = next((c for c in st["comments"] if c["id"] == ref), None)
        if c is None:
            raise ValueError(f"no finding {ref!r} in stack {sid}")
        keys = ("id", "axis", "severity", "title", "body", "evidence", "path", "line", "start_line", "pr", "status", "note")
        return out | {"ref": ref, "label": c["title"]}, st, json.dumps({k: c.get(k) for k in keys}, ensure_ascii=False)
    return resolve_node(st, out, ref, t.get("section"))


def diagram_parts(sec: dict, comps: dict) -> tuple[dict, list]:
    """The boxes (by id) and arrows of one diagram section. A sequence's arrows are its messages, frames flattened."""
    edges: list = []

    def walk(items) -> None:
        for m in items or []:
            if m.get("frame"):
                for b in m.get("branches") or []:
                    walk(b.get("messages"))
            elif "from" in m:
                edges.append({k: m[k] for k in ("from", "to", "label", "at", "return", "new") if k in m})

    if sec.get("kind") == "sequence":
        walk(sec.get("messages"))
        ids = sec.get("participants") or []
    else:
        edges, ids = sec.get("edges") or [], sec.get("nodes") or []
    nodes = {}
    for n in ids:
        n = (comps.get(n) or {"id": n, "label": n}) if isinstance(n, str) else n
        nodes[n["id"]] = n
    return nodes, edges


def resolve_node(st: dict, out: dict, ref, hint) -> tuple[dict, dict, str]:
    """A box or arrow (`a>b`) of a diagram, or a component no diagram draws. The diagram holding it is stored as `section`;
    `hint` (the diagram the reviewer clicked in) is tried first, since component ids repeat across diagrams. Edges live in
    the diagrams, not in `components`, and a flowchart's boxes are its own, so both are looked up there."""
    sid, comps = st["id"], {n["id"]: n for n in (st.get("components") or {}).get("nodes", [])}
    parts = sorted(((i, *diagram_parts(s, comps)) for i, s in enumerate(st["sections"]) if s.get("type") == "diagram"), key=lambda p: p[0] != hint)
    if isinstance(ref, str) and ">" in ref:
        a, b = ref.split(">", 1)
        for i, nodes, edges in parts:
            if a in nodes and b in nodes and (e := next((e for e in edges if e["from"] == a and e["to"] == b), None)):
                return out | {"ref": ref, "section": i, "label": f"{nodes[a]['label']} to {nodes[b]['label']}"}, st, json.dumps(e, ensure_ascii=False)
        raise ValueError(f"no arrow {ref!r} in the diagrams of stack {sid}")
    for i, nodes, edges in parts:
        if ref in nodes:
            near = [e for e in edges if ref in (e["from"], e["to"])]
            return out | {"ref": ref, "section": i, "label": nodes[ref]["label"]}, st, json.dumps({"node": nodes[ref], "edges": near}, ensure_ascii=False)
    if ref not in comps:
        raise ValueError(f"no box {ref!r} in the diagrams of stack {sid}")
    return out | {"ref": ref, "label": comps[ref]["label"]}, st, json.dumps({"node": comps[ref]}, ensure_ascii=False)


def move_target(t: dict, dst: pathlib.Path, new: dict) -> dict | None:
    """A thread's target in a new round: a line block moves to the same text nearest its old place, a section or fact to the
    one with the same title, the rest stay when they still exist. None when the target is gone."""
    t = dict(t)
    if t.get("stack") not in {s["id"] for s in new["stacks"]}:
        t.pop("stack", None)
    if t["kind"] == "line":
        st = next((s for s in new["stacks"] if s["id"] == t.get("stack") or not t.get("stack") and any(f["path"] == t["path"] for f in s["files"])), None)
        f = st and dst / st["id"] / "tip" / t["path"]
        if not f or not f.is_file() or not any(x["path"] == t["path"] for x in st["files"]):
            return None
        old, lines = t["text"].split("\n"), f.read_text().splitlines()
        at = [i for i in range(len(lines) - len(old) + 1) if lines[i:i + len(old)] == old]
        if not at:
            return None
        i = min(at, key=lambda i: abs(i + 1 - t["start"]))
        t |= {"start": i + 1, "end": i + len(old)}
    elif t["kind"] in ("section", "fact"):
        st = next((s for s in new["stacks"] if s["id"] == t.get("stack")), None)
        items = st and (st["sections"] if t["kind"] == "section" else st["key_facts"])
        i = next((i for i, x in enumerate(items or []) if x["title"] == t["label"]), None)
        if i is None:
            return None
        t["ref"] = i
    elif t["kind"] == "item":
        st = next((s for s in new["stacks"] if s["id"] == t.get("stack")), None)
        i = next((i for i, x in enumerate(st["sections"] if st else []) if x["title"] == t.get("section_title") and x["type"] in ROWS), None)
        if i is None:
            return None
        sec = st["sections"][i]
        j = next((j for j, r in enumerate(sec[ROWS[sec["type"]]]) if row_label(sec, r) == t["label"]), None)
        if j is None:
            return None
        t |= {"ref": i, "item": j}
    try:
        return resolve(new, dst, t)[0]
    except ValueError:
        return None


def carry_threads(src: pathlib.Path, dst: pathlib.Path, new: dict) -> list[dict]:
    """Copy the earlier round's threads into a new round, each with its target moved. Threads whose target is gone are
    returned (for the page's `lost_threads`). Session ids are kept, so a follow-up resumes the old session. An unapplied
    proposal is dropped: it was checked against the old diff."""
    lost = []
    for f in sorted((src / "threads").glob("*.json")):
        t = read(f)
        if not t:
            continue
        moved = move_target(t["target"], dst, new)
        if moved is None:
            lost.append(t)
            continue
        if t["status"] in BUSY:
            t |= {"status": "failed", "error": "The answer was interrupted; ask again.", "live": ""}
        if t.get("proposal") and not t["proposal"].get("applied"):
            t["proposal"] = None
        (dst / "threads").mkdir(exist_ok=True)
        write(dst / "threads" / f.name, {**t, "target": moved})
    return lost


# ---- the answer's proposed action ---------------------------------------------------------------------------------

def parse_action(text: str) -> tuple[str, object, str | None]:
    """(the text to show, the proposed action or None, why it could not be read). The action is a last fenced block."""
    m = ACTION.search(text)
    if not m:
        return text.strip(), None, None
    shown = text[:m.start()].strip()
    try:
        act = json.loads(m.group(1))
    except ValueError:
        return shown, None, "the action block is not valid JSON"
    return shown, act, None


def validate(rdir: pathlib.Path, run: dict, data: dict, target: dict, act) -> dict:
    """An action as the thread stores it, or ValueError with the reason it is refused."""
    if not isinstance(act, dict):
        raise ValueError("the action is not an object")
    typ, kind, sid = act.get("type"), target["kind"], target.get("stack")
    if typ == "rejudge":
        if kind != "finding":
            raise ValueError("a re-judge belongs on a finding thread")
        if act.get("finding") != target["ref"]:
            raise ValueError("a re-judge names the finding the thread is about")
        if act.get("status") not in ("verified", "refuted"):
            raise ValueError("a re-judge status is verified or refuted")
        need("note", act.get("note"), LIMITS["item"])
        return {"type": "rejudge", "finding": target["ref"], "status": act["status"], "note": act["note"]}
    if typ == "comment":
        path, line, start = act.get("path"), act.get("line"), act.get("start_line")
        sd = next((s for s in run["stacks"] if (s["id"] == sid if sid else any(f["path"] == path for f in s["files"]))), None)
        if sd is None or not isinstance(line, int) or start is not None and not isinstance(start, int):
            raise ValueError("a comment needs a path in this stack and a numeric line")
        need("body", act.get("body"), LIMITS["comment_body"])
        sev = act.get("severity") or "comment"
        if sev not in (*SEVERITIES, "comment"):
            raise ValueError(f"severity must be one of {(*SEVERITIES, 'comment')}")
        r = Stack(rdir, run, sd).route(path, line, start)
        if isinstance(r, str):
            raise ValueError(r)
        return {"type": "comment", "stack": sd["id"], "path": path, "line": line, "start_line": start, "body": act["body"], "severity": sev,
                "pr": r[0], "pl": r[1], "ps": r[2]}
    if typ == "guide_fix":
        if kind not in ("section", "fact", "step"):
            raise ValueError("a guide fix belongs on a section, key fact or step thread")
        ref, rep = act.get("ref") or {}, act.get("replacement")
        if not isinstance(rep, dict) or ref.get("kind", kind) != kind:
            raise ValueError("a guide fix names the thread's target kind and gives a replacement object")
        gs = next(s for s in data["stacks"] if s["id"] == sid)
        st = Stack(rdir, run, next(s for s in run["stacks"] if s["id"] == sid))
        errs: list[str] = []
        skip = target["ref"] if kind == "section" else None

        def check(sec: dict, other: set[str]) -> None:
            # Ids from the components and the other sections' own diagram nodes, so the replacement may reuse its own.
            g = GuideCheck(st, errs.append, strict=False)
            g.ids = {n["id"] for n in (gs.get("components") or {}).get("nodes", [])}
            g.ids |= {n["id"] for i, s in enumerate(gs["sections"]) if i != skip and s.get("type") == "diagram"
                      and s.get("kind") in ("flowchart", "states", "data") for n in s.get("nodes") or []}
            step_nodes: list = []
            check_section(sec, "replacement", g, set(other), step_nodes)
            for slabel, ns in step_nodes:
                if bad := [n for n in ns if n not in g.ids]:
                    errs.append(f"{slabel}: nodes not declared in components or any diagram: {bad}")
        if kind == "fact":
            need("title", rep.get("title"), LIMITS["fact_title"])
            need("text", rep.get("text"), LIMITS["fact_text"])
            canon = {"kind": kind, "stack": sid, "index": target["ref"]}
        elif kind == "section":
            cur = gs["sections"][target["ref"]]
            if rep.get("type") != cur["type"]:
                raise ValueError(f"the replacement stays a {cur['type']} section")
            other = {s["id"].removeprefix(sid + "-") for i, sec in enumerate(gs["sections"]) if i != target["ref"] and sec["type"] == "steps" for s in sec["steps"]}
            check(rep, other)
            canon = {"kind": kind, "stack": sid, "index": target["ref"]}
        else:
            raw = target["ref"].removeprefix(sid + "-")
            rep = {**rep, "id": raw}
            other = {s["id"].removeprefix(sid + "-") for s in steps_of(gs)} - {raw}
            check({"type": "steps", "title": "x", "steps": [rep]}, other)
            canon = {"kind": kind, "stack": sid, "id": raw}
        if errs:
            raise ValueError("; ".join(errs[:3]))
        return {"type": "guide_fix", "ref": canon, "replacement": rep}
    raise ValueError("the action type is comment, rejudge or guide_fix")


# ---- running the answering agent ----------------------------------------------------------------------------------

def turn_prompt(rdir: pathlib.Path, run: dict, t: dict, text: str, question: str, deep: bool, full: bool) -> str:
    """One turn's prompt. The first turn (or the first in a new round) carries the target and where things are; later turns
    only the question, since the session remembers the rest."""
    head = [f"Question: {question}"] + (["Depth: deep"] if deep else [])
    if not full:
        return "\n".join(head)
    tg = t["target"]
    stacks = [s for s in run["stacks"] if s["id"] == tg["stack"]] or run["stacks"]
    where = [f"Target: {tg['kind']} {tg['label']}" + (f" ({tg['path']})" if tg["kind"] == "line" else "") if tg["kind"] != "free" else "Target: none, a general question"]
    return "\n".join([*head, *where, *([text] if text else []), f"Work directory: {rdir}",
                      *[f"Stack {s['id']}: {rdir / s['id']} (brief.md there), tip {s['tip']}" for s in stacks], f"Checkout: {run.get('checkout')}"])


def claude_cmd(rdir: pathlib.Path, run: dict, prompt: str, deep: bool, session: str | None) -> list[str]:
    checkout = run.get("checkout")
    allow = ["Read", "Grep", "Glob", *([f"Bash(git -C {checkout} {sub}:*)" for sub in ("show", "grep", "log")] if checkout else [])]
    return [os.environ.get("REVIEW_GUIDE_CLAUDE") or "claude", "-p", prompt, *(["--resume", session] if session else []),
            "--agent", "review-guide:answerer", "--plugin-dir", str(PLUGIN), "--add-dir", str(PLUGIN), "--add-dir", str(rdir),
            *(["--add-dir", checkout] if checkout else []),
            "--setting-sources", "", "--strict-mcp-config", "--permission-mode", "dontAsk",
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--tools", "Read,Grep,Glob,Bash", *(["--model", "opus", "--effort", "high"] if deep else ["--model", "sonnet"]),
            "--allowedTools", *allow, "--disallowedTools", "Write", "Edit", "mcp__*", *DENY]


class Asker:
    """The threads of one server: the file shape, the concurrency cap and the queue."""

    def __init__(self):
        self.lock = threading.RLock()
        self.sem = threading.Semaphore(MAX_RUNNING)  # waiters wake in the order they arrived
        self.active: set[pathlib.Path] = set()

    @staticmethod
    def path(rdir: pathlib.Path, tid: str) -> pathlib.Path:
        return rdir / "threads" / f"{tid}.json"

    def load(self, rdir: pathlib.Path, tid: str) -> dict | None:
        t = read(self.path(rdir, tid))
        if t and t["status"] in BUSY and self.path(rdir, tid) not in self.active:  # a server that died mid-answer
            t |= {"status": "failed", "error": "The answer was interrupted; ask again.", "live": ""}
        return t

    def save(self, rdir: pathlib.Path, t: dict) -> None:
        with self.lock:
            write(self.path(rdir, t["id"]), t)

    def threads(self, rdir: pathlib.Path) -> list[dict]:
        rows = []
        for f in (rdir / "threads").glob("*.json"):
            if t := self.load(rdir, f.stem):
                rows.append({"id": t["id"], "target": t["target"], "question": t["messages"][0]["text"], "status": t["status"],
                             "created": t["created"], "updated": int(f.stat().st_mtime)})
        return sorted(rows, key=lambda r: (r["created"], r["id"]))

    def start(self, rdir: pathlib.Path, body: dict) -> tuple[int, dict]:
        """Record the question and queue the answer. Returns (status, payload); ValueError for a bad request."""
        question = body.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question is empty")
        if len(question) > 2000:
            raise ValueError("question is longer than 2000 characters")
        deep = bool(body.get("deep"))
        data, _ = load_round(rdir)
        with self.lock:
            if (tid := body.get("thread")) is not None:
                if not isinstance(tid, str) or not THREAD.fullmatch(tid):
                    raise ValueError("bad thread id")
                if (t := self.load(rdir, tid)) is None:
                    return 404, {"error": f"no thread {tid}"}
                if t["status"] in BUSY:
                    return 409, {"error": "this thread is still answering"}
            else:
                tid = secrets.token_hex(4)
                t = {"id": tid, "target": resolve(data, rdir, body.get("target"))[0], "created": int(time.time()), "session_id": None,
                     "messages": []}
            t["messages"].append({"role": "user", "text": question.strip(), "at": int(time.time()), **({"deep": True} if deep else {})})
            t |= {"status": "queued", "live": "", "proposal": None}
            t.pop("error", None)
            t.pop("proposal_error", None)
            (rdir / "threads").mkdir(exist_ok=True)
            self.active.add(self.path(rdir, tid))
            self.save(rdir, t)
        threading.Thread(target=self.run, args=(rdir, tid, deep), daemon=True).start()
        return 202, {"thread": tid}

    def run(self, rdir: pathlib.Path, tid: str, deep: bool) -> None:
        try:
            with self.sem:
                self.turn(rdir, tid, deep)
        except Exception as e:  # noqa: BLE001 - whatever went wrong is the thread's error, not the server's
            self.fail(rdir, tid, f"{type(e).__name__}: {e}"[:300])
        finally:
            self.active.discard(self.path(rdir, tid))

    def fail(self, rdir: pathlib.Path, tid: str, error: str) -> None:
        with self.lock:
            t = read(self.path(rdir, tid))
            if t["messages"][-1]["role"] == "agent" and not t["messages"][-1]["text"].strip():
                t["messages"].pop()
            self.save(rdir, t | {"status": "failed", "error": error, "live": ""})

    def turn(self, rdir: pathlib.Path, tid: str, deep: bool) -> None:
        t = self.load(rdir, tid)
        data, run = load_round(rdir)
        _, _, text = resolve(data, rdir, t["target"])
        full = not t["session_id"] or t.get("ctx") != rdir.name
        prompt = turn_prompt(rdir, run, t, text, t["messages"][-1]["text"], deep, full)
        msg = {"role": "agent", "text": "", "at": int(time.time()), "model": "opus" if deep else "sonnet", **({"deep": True} if deep else {})}
        t |= {"status": "running", "ctx": rdir.name}
        t["messages"].append(msg)
        self.save(rdir, t)
        # Sessions are stored per working directory, so every round of a review shares one.
        with tempfile.TemporaryFile("w+") as errf:
            proc = subprocess.Popen(claude_cmd(rdir, run, prompt, deep, t["session_id"]), cwd=rdir.parents[1], stdout=subprocess.PIPE, stderr=errf,
                                    stdin=subprocess.DEVNULL, text=True)
            killer = threading.Timer(TURN_TIMEOUT, proc.kill)
            killer.daemon = True
            killer.start()
            buf, result, last, dirty = "", None, 0.0, False
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("parent_tool_use_id"):
                    continue
                kind = ev.get("type")
                if kind == "system" and ev.get("subtype") == "init" and not t["session_id"]:
                    t["session_id"], dirty = ev.get("session_id"), True
                elif kind == "stream_event":
                    e = ev.get("event") or {}
                    if e.get("type") == "message_start":
                        buf = ""
                    elif (e.get("delta") or {}).get("type") == "text_delta":
                        buf += e["delta"]["text"]
                        msg["text"], dirty = FENCE_START.sub("", buf.split("```action")[0]), True
                elif kind == "assistant":
                    for c in (ev.get("message") or {}).get("content") or []:
                        if c.get("type") == "tool_use":
                            t["live"], dirty = describe(c.get("name", ""), c.get("input") or {}), True
                elif kind == "result":
                    result = ev
                if dirty and time.monotonic() - last >= SAVE_EVERY:
                    self.save(rdir, t)
                    last, dirty = time.monotonic(), False
            code = proc.wait()
            killer.cancel()
            errf.seek(0)
            stderr = [x.strip() for x in errf.read().splitlines() if x.strip()]
        if code != 0 or result is None or result.get("is_error"):
            if not msg["text"].strip():
                t["messages"].pop()
            self.save(rdir, t)
            return self.fail(rdir, tid, (stderr[-1] if stderr else (result or {}).get("result") or f"claude exited with {code}")[:300])
        shown, act, why = parse_action(result.get("result") or buf)
        msg["text"] = shown
        if act is not None:
            try:
                t["proposal"] = {**validate(rdir, run, data, t["target"], act), "applied": False}
            except Exception as e:  # noqa: BLE001 - a malformed action is stored as the reason, never raised
                why = str(e) or type(e).__name__
        if why:
            t["proposal_error"] = why
        t["session_id"] = t["session_id"] or result.get("session_id")
        self.save(rdir, t | {"status": "idle", "live": ""})

    def apply(self, rdir: pathlib.Path, tid: str) -> tuple[int, dict]:
        """Accept a thread's proposal. A comment goes back to the page, which holds the comments; a re-judge and a guide fix are
        stored in the stack's directory for build.py to merge."""
        with self.lock:
            t = self.load(rdir, tid)
            if t is None:
                return 404, {"error": f"no thread {tid}"}
            p = t.get("proposal")
            if t["status"] in BUSY or not p:
                return 409, {"error": "this thread has nothing to apply" if not p else "this thread is still answering"}
            if not p["applied"]:
                at = int(time.time())
                if p["type"] == "rejudge":
                    f = rdir / t["target"]["stack"] / "rejudged.json"
                    write(f, (read(f) or {}) | {t["target"]["ref"].removeprefix(t["target"]["stack"] + "-"): {"status": p["status"], "note": p["note"], "thread": tid}})
                elif p["type"] == "guide_fix":
                    f = rdir / p["ref"]["stack"] / "guide-fixes.json"
                    write(f, [*(read(f) or []), {"ref": p["ref"], "replacement": p["replacement"], "thread": tid, "at": at}])
                p["applied"] = True
                self.save(rdir, t)
            return 200, {"applied": True, "proposal": p}
