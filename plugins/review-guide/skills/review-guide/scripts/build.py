#!/usr/bin/env python3
"""Check the agents' guide files against the fetched data, then render the review page.

Usage: build.py WORKDIR [--check] [--stack ID] [--partial]

  --check     validate only, print every problem, exit 1 if any (the guide writer runs this)
  --stack ID  limit to one stack
  --partial   render a stack with no guide yet as its file tree and diffs (the page opens before the agents finish)

Without --check, writes WORKDIR/index.html and WORKDIR/guide.json (the data serve.py reads).
Every comment is written against the stack's tip; this script routes it to the PR that wrote the line.
"""

import argparse
import hashlib
import json
import pathlib
import sys

from fetch import parse_diff

SEVERITIES = ("blocker", "should", "question", "nit")
EVENTS = ("COMMENT", "APPROVE", "REQUEST_CHANGES")
ROLES = ("read", "skim", "skip")
KINDS = ("decide", "read")
STATUSES = ("verified", "unverified", "refuted")
NODE_STATES = ("new", "changed", "existing", "external")
LIMITS = {
    "summary": 300, "step_title": 70, "step_why": 220, "file_why": 90, "comment_title": 90,
    "comment_body": 700, "verdict_why": 160, "review_body": 400, "label": 40,
    "fact_title": 60, "fact_text": 200, "test": 140,
}
MAX_FACTS = 4
MAX_STEPS, MAX_ANCHOR_LINES, MAX_NODES, MAX_FLOWS, MAX_FLOW_STEPS, MAX_COMMENTS_PER_PR = 8, 60, 14, 3, 10, 5
TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "ui" / "template.html"


class Stack:
    """One stack's fetched inputs, and the routing of a tip line to the PR that wrote it."""

    def __init__(self, workdir: pathlib.Path, run: dict, stack: dict):
        self.id, self.prs, self.base, self.tip = stack["id"], stack["prs"], stack["base"], stack["tip"]
        self.dir = workdir / stack["id"]
        self.files = {f["path"]: f for f in stack["files"]}
        self.attr = json.loads((self.dir / "attribution.json").read_text())
        self.target = parse_diff((self.dir / "target.diff").read_text())
        self.hunks = {n: json.loads((workdir / str(n) / "hunks.json").read_text()) for n in self.prs}
        self.pr_info = {p["n"]: p for p in run["prs"] if p["n"] in self.prs}

    def tip_lines(self, path) -> list[str] | None:
        f = self.dir / "tip" / path
        return f.read_text().splitlines() if path in self.files and f.is_file() else None

    def owner(self, path: str, line) -> tuple[int, int] | None:
        v = self.attr.get(path, {}).get(str(line))
        if not v or v[1] is None or v[1] not in self.hunks[v[0]].get(path, {}).get("commentable", []):
            return None
        return v[0], v[1]

    def route(self, path, line, start=None) -> tuple[int, int, int | None] | str:
        """(pr, line in that PR, start line in that PR) for a tip location, or the reason it cannot be posted."""
        if path not in self.files:
            return f"path {path!r} is not changed in this stack"
        own = self.owner(path, line)
        if own is None:
            written = sorted(int(k) for k in self.attr.get(path, {}))
            near = sorted(written, key=lambda k: abs(k - line) if isinstance(line, int) else k)[:4]
            return (f"line {line} of {path} was not written by this stack; use a line it wrote (nearby: {sorted(near)}) "
                    "or a general comment with path null and pr set")
        if start is None:
            return own[0], own[1], None
        span = [self.owner(path, i) for i in range(start, line + 1)] if isinstance(start, int) and start < line else []
        if not span or any(s is None or s[0] != own[0] for s in span) or [s[1] for s in span] != list(range(span[0][1], span[0][1] + len(span))):
            return f"start_line {start}..line {line} of {path} must be consecutive lines written by one PR"
        return own[0], own[1], span[0][1]


def skeleton(st: Stack) -> dict:
    """What the page shows before the guide writer finishes: the tree and diffs, no map, steps or comments."""
    return {"pending": True, "summary": "", "map": {"layers": [], "nodes": [], "edges": []}, "flows": [], "steps": [],
            "files": {p: {"role": "pending", "why": ""} for p in st.files},
            "prs": {str(n): {"verdict": {"event": "COMMENT", "why": ""}, "review_body": ""} for n in st.prs}}


def check_text(err, label: str, value, limit: int) -> None:
    if not isinstance(value, str) or not value.strip():
        err(f"{label} is empty")
    elif len(value) > limit:
        err(f"{label} is {len(value)} characters; the limit is {limit}")


def check_stack(st: Stack, viewer: str) -> tuple[dict | None, list[str]]:
    errors: list[str] = []
    err = lambda m: errors.append(f"stack {st.id}: {m}")  # noqa: E731
    try:
        guide = json.loads((st.dir / "guide.json").read_text())
    except FileNotFoundError:
        return None, [f"stack {st.id}: guide.json is missing"]
    except json.JSONDecodeError as e:
        return None, [f"stack {st.id}: guide.json is not valid JSON ({e})"]

    check_text(err, "summary", guide.get("summary"), LIMITS["summary"])
    facts = guide.get("key_facts") or []
    if len(facts) > MAX_FACTS:
        err(f"key_facts: at most {MAX_FACTS}, got {len(facts)}")
    for i, f in enumerate(facts):
        check_text(err, f"key_facts[{i}].title", (f or {}).get("title"), LIMITS["fact_title"])
        check_text(err, f"key_facts[{i}].text", (f or {}).get("text"), LIMITS["fact_text"])

    files = guide.get("files") or {}
    if missing := sorted(set(st.files) - set(files)):
        err(f"files: every changed file needs a role; missing {missing}")
    if extra := sorted(set(files) - set(st.files)):
        err(f"files: not changed in this stack: {extra}")
    for path, f in files.items():
        if (f or {}).get("role") not in ROLES:
            err(f"files[{path}].role must be one of {ROLES}")
        check_text(err, f"files[{path}].why", (f or {}).get("why"), LIMITS["file_why"])

    node_ids = check_map(guide, st, err)

    steps = guide.get("steps") or []
    if not 1 <= len(steps) <= MAX_STEPS:
        err(f"steps: expected 1 to {MAX_STEPS}, got {len(steps)}")
    step_ids, comment_ids, routed = set(), set(), {n: [] for n in st.prs}
    for s in steps:
        sid = s.get("id")
        label = f"step {sid!r}"
        if not isinstance(sid, str) or not sid or sid in step_ids:
            err(f"{label}: id missing or duplicated")
        step_ids.add(sid)
        if s.get("kind") not in KINDS:
            err(f"{label}: kind must be one of {KINDS}")
        check_text(err, f"{label}.title", s.get("title"), LIMITS["step_title"])
        check_text(err, f"{label}.why", s.get("why"), LIMITS["step_why"])
        if "test" in s and s["test"] is not None:
            check_text(err, f"{label}.test", s["test"], LIMITS["test"])
        if bad := [n for n in s.get("nodes") or [] if n not in node_ids]:
            err(f"{label}: nodes not in the map: {bad}")
        anchor = s.get("anchor") or {}
        lines = st.tip_lines(anchor.get("path"))
        start, end = anchor.get("start"), anchor.get("end")
        if lines is None:
            err(f"{label}: anchor.path {anchor.get('path')!r} is not a changed text file at the tip")
        elif not (isinstance(start, int) and isinstance(end, int) and 1 <= start <= end <= len(lines)):
            err(f"{label}: anchor {start}..{end} is outside {anchor['path']} (1..{len(lines)})")
        elif end - start + 1 > MAX_ANCHOR_LINES:
            err(f"{label}: anchor spans {end - start + 1} lines; the limit is {MAX_ANCHOR_LINES}")
        for c in s.get("comments") or []:
            cid = c.get("id")
            clabel = f"comment {cid!r}"
            if not isinstance(cid, str) or not cid or cid in comment_ids:
                err(f"{clabel}: id missing or duplicated")
            comment_ids.add(cid)
            if c.get("severity") not in SEVERITIES:
                err(f"{clabel}: severity must be one of {SEVERITIES}")
            check_text(err, f"{clabel}.title", c.get("title"), LIMITS["comment_title"])
            check_text(err, f"{clabel}.body", c.get("body"), LIMITS["comment_body"])
            if not isinstance(c.get("evidence"), list) or not c["evidence"]:
                err(f"{clabel}: evidence must list the path:line references the claim rests on")
            if c.get("path") is None:
                if c.get("pr") not in st.prs:
                    err(f"{clabel}: a general comment needs pr set to one of {st.prs}")
                else:
                    routed[c["pr"]].append(c.get("severity"))
                continue
            r = st.route(c["path"], c.get("line"), c.get("start_line"))
            if isinstance(r, str):
                err(f"{clabel}: {r}")
            else:
                routed[r[0]].append(c.get("severity"))

    prs = guide.get("prs") or {}
    for n in st.prs:
        p = prs.get(str(n)) or {}
        verdict = p.get("verdict") or {}
        event = verdict.get("event")
        if event not in EVENTS:
            err(f"prs[{n}].verdict.event must be one of {EVENTS}")
        check_text(err, f"prs[{n}].verdict.why", verdict.get("why"), LIMITS["verdict_why"])
        check_text(err, f"prs[{n}].review_body", p.get("review_body"), LIMITS["review_body"])
        if st.pr_info[n]["author"] == viewer and event != "COMMENT":
            err(f"prs[{n}].verdict.event must be COMMENT: GitHub refuses approve and request-changes on your own PR")
        if event == "REQUEST_CHANGES" and "blocker" not in routed[n]:
            err(f"prs[{n}]: REQUEST_CHANGES needs a blocker comment routed to #{n}")
        if event == "APPROVE" and {"blocker", "should"} & set(routed[n]):
            err(f"prs[{n}]: APPROVE with a blocker or should comment routed to #{n}")
        if len(routed[n]) > MAX_COMMENTS_PER_PR:
            err(f"#{n} receives {len(routed[n])} comments; the limit is {MAX_COMMENTS_PER_PR}")
    return guide, errors


def check_map(guide: dict, st: Stack, err) -> set[str]:
    m = guide.get("map") or {}
    layers = {l.get("id") for l in m.get("layers") or []}
    for l in m.get("layers") or []:
        check_text(err, f"map layer {l.get('id')!r} label", l.get("label"), LIMITS["label"])
    nodes = m.get("nodes") or []
    if not 1 <= len(nodes) <= MAX_NODES:
        err(f"map: expected 1 to {MAX_NODES} nodes, got {len(nodes)}")
    ids = set()
    for node in nodes:
        nid = node.get("id")
        if not isinstance(nid, str) or not nid or nid in ids:
            err(f"map node {nid!r}: id missing or duplicated")
        ids.add(nid)
        check_text(err, f"map node {nid!r} label", node.get("label"), LIMITS["label"])
        if node.get("role") is not None:
            check_text(err, f"map node {nid!r} role", node["role"], 30)
        if node.get("layer") not in layers:
            err(f"map node {nid!r}: layer {node.get('layer')!r} is not declared")
        if node.get("state") not in NODE_STATES:
            err(f"map node {nid!r}: state must be one of {NODE_STATES}")
        if node.get("state") in ("new", "changed") and node.get("path") not in st.files:
            err(f"map node {nid!r}: a {node['state']} node needs path set to the changed file that holds it")
    for e in m.get("edges") or []:
        if e.get("from") not in ids or e.get("to") not in ids:
            err(f"map edge {e.get('from')!r}->{e.get('to')!r}: both ends must be node ids")
        if e.get("label"):
            check_text(err, f"map edge {e.get('from')}->{e.get('to')} label", e["label"], LIMITS["label"])
    flows = guide.get("flows") or []
    if len(flows) > MAX_FLOWS:
        err(f"flows: at most {MAX_FLOWS}, got {len(flows)}")
    for f in flows:
        check_text(err, "flow title", f.get("title"), LIMITS["step_title"])
        steps = f.get("steps") or []
        if not 1 <= len(steps) <= MAX_FLOW_STEPS:
            err(f"flow {f.get('title')!r}: expected 1 to {MAX_FLOW_STEPS} steps, got {len(steps)}")
        for s in steps:
            if s.get("from") not in ids or s.get("to") not in ids:
                err(f"flow {f.get('title')!r}: step {s.get('from')!r}->{s.get('to')!r} must reference node ids")
            check_text(err, f"flow {f.get('title')!r} step label", s.get("label"), LIMITS["label"])
    return ids


def merge_verification(st: Stack, guide: dict) -> list[str]:
    """Attach the verifier's status to each comment, and add its missed findings as a last step."""
    warnings = []
    path = st.dir / "verify.json"
    if not path.is_file():
        for s in guide["steps"]:
            for c in s.get("comments") or []:
                c["status"], c["note"] = "checking", "The verifier is still checking this comment."
        return warnings
    report = json.loads(path.read_text())
    results = report.get("comments") or {}
    for s in guide["steps"]:
        for c in s.get("comments") or []:
            r = results.get(c["id"]) or {}
            c["status"] = r.get("status") if r.get("status") in STATUSES else "unverified"
            c["note"] = r.get("note") or ("" if r else "The verifier did not check this comment.")
            if c.get("path") and r.get("line") and r["line"] != c.get("line"):
                if isinstance(st.route(c["path"], r["line"]), tuple):
                    c["line"] = r["line"]
                    c.pop("start_line", None)
                else:
                    warnings.append(f"stack {st.id}: verifier moved {c['id']} to {r['line']}, a line the stack did not write; kept {c.get('line')}")
    missed = []
    for i, m in enumerate(report.get("missed") or []):
        if m.get("severity") not in SEVERITIES or not isinstance(st.route(m.get("path"), m.get("line")), tuple):
            warnings.append(f"stack {st.id}: dropped a verifier finding at {m.get('path')}:{m.get('line')} (bad severity or line)")
            continue
        missed.append({**m, "id": f"verifier-{i + 1}", "title": (m.get("title") or m["body"])[: LIMITS["comment_title"]],
                       "status": "unverified", "note": "Raised by the verifier; nobody checked it a second time."})
    if missed:
        first = missed[0]
        total = len(st.tip_lines(first["path"]) or [first["line"]])
        guide["steps"].append({
            "id": "verifier", "kind": "read", "title": "Raised while checking the drafted comments",
            "why": "The verifier found these while reading the code behind the other comments. Read the evidence before ticking.",
            "anchor": {"path": first["path"], "start": max(1, first["line"] - 8), "end": min(total, first["line"] + 8)},
            "comments": missed,
        })
    return warnings


def diff_anchor(path: str) -> str:
    return "diff-" + hashlib.sha256(path.encode()).hexdigest()


def render_stack(st: Stack, repo: str, guide: dict) -> dict:
    compare = f"https://github.com/{repo}/compare/{st.base}...{st.tip}"

    def located(path: str, line: int) -> dict:
        own = st.owner(path, line)
        return {"pr": own[0], "pl": own[1]} if own else {}

    def line_rows(path: str, start: int, end: int) -> list[dict]:
        text = st.tip_lines(path)
        added = set(st.target.get(path, {}).get("added", []))
        return [{"n": i, "t": text[i - 1], "k": "+" if i in added else " ", **located(path, i)} for i in range(start, end + 1)]

    comments, steps = [], []
    for s in guide["steps"]:
        a = s["anchor"]
        sid = f"{st.id}-{s['id']}"
        for c in s.get("comments") or []:
            out = {k: c.get(k) for k in ("severity", "title", "body", "evidence", "path", "line", "start_line", "status", "note")}
            out |= {"id": f"{sid}-{c['id']}", "step": sid}
            if c.get("path"):
                pr, pl, ps = st.route(c["path"], c["line"], c.get("start_line"))
                info = st.pr_info[pr]
                out |= {"pr": pr, "pl": pl, "ps": ps,
                        "link": f"https://github.com/{repo}/blob/{info['review_sha']}/{c['path']}#L{ps or pl}" + (f"-L{pl}" if ps else "")}
            else:
                out |= {"pr": c["pr"], "pl": None, "ps": None, "link": None}
            comments.append(out)
        steps.append({
            "id": sid, "kind": s["kind"], "title": s["title"], "why": s["why"], "nodes": s.get("nodes") or [],
            "test": s.get("test", False),
            "path": a["path"], "start": a["start"], "end": a["end"],
            "lines": line_rows(a["path"], a["start"], a["end"]),
            "link": f"{compare}#{diff_anchor(a['path'])}R{a['start']}-R{a['end']}",
        })

    files = []
    for path, f in st.files.items():
        hunks = [[{**line, **(located(path, line["n"]) if line["k"] == "+" else {})} for line in hunk] for hunk in st.target.get(path, {}).get("hunks", [])]
        g = guide["files"][path]
        files.append({**f, "role": g["role"], "why": g["why"], "hunks": hunks,
                      "link": f"{compare}#{diff_anchor(path)}",
                      "pr_links": {n: f"{st.pr_info[n]['url']}/files#{diff_anchor(path)}" for n in f["prs"]}})

    prs = []
    for n in st.prs:
        info, g = st.pr_info[n], guide["prs"][str(n)]
        prs.append({**{k: info[k] for k in ("n", "url", "title", "author", "sha", "review_sha", "draft", "checks")},
                    "verdict": g["verdict"], "review_body": g["review_body"]})
    return {"id": st.id, "pending": bool(guide.get("pending")), "base": st.base, "tip": st.tip, "compare": compare, "summary": guide["summary"],
            "key_facts": guide.get("key_facts") or [],
            "map": guide["map"], "flows": guide.get("flows") or [], "prs": prs, "steps": steps, "files": files, "comments": comments}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir", type=pathlib.Path)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--stack")
    ap.add_argument("--partial", action="store_true", help="render stacks without a guide as file tree and diffs only")
    args = ap.parse_args()
    run = json.loads((args.workdir / "run.json").read_text())
    stacks = [Stack(args.workdir, run, s) for s in run["stacks"] if args.stack in (None, s["id"])]
    if not stacks:
        sys.exit(f"no stack {args.stack!r}; stacks are {[s['id'] for s in run['stacks']]}")

    errors, guides = [], {}
    for st in stacks:
        if args.partial and not args.check and not (st.dir / "guide.json").is_file():
            guides[st.id] = skeleton(st)
            continue
        guide, errs = check_stack(st, run["viewer"])
        errors += errs
        guides[st.id] = guide
    if errors:
        print("\n".join(errors))
        sys.exit(1)
    if args.check:
        print("ok")
        return

    warnings = []
    for st in stacks:
        if not guides[st.id].get("pending"):
            warnings += merge_verification(st, guides[st.id])
    data = {"repo": run["repo"], "viewer": run["viewer"], "run_id": run["run_id"], "warnings": run.get("warnings") or [],
            "stacks": [render_stack(st, run["repo"], guides[st.id]) for st in stacks]}
    (args.workdir / "guide.json").write_text(json.dumps(data, indent=1))
    embedded = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    (args.workdir / "index.html").write_text(TEMPLATE.read_text().replace("/*DATA*/null", embedded))
    for w in warnings:
        print("warning:", w)
    for s in data["stacks"]:
        by_pr = {p["n"]: sum(c["pr"] == p["n"] for c in s["comments"]) for p in s["prs"]}
        by_status = {k: sum(c["status"] == k for c in s["comments"]) for k in STATUSES}
        print(f"stack {s['id']}: {len(s['steps'])} steps, comments per PR {by_pr}, {by_status}")
    print(args.workdir / "index.html")


if __name__ == "__main__":
    main()
