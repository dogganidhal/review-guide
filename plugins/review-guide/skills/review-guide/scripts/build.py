#!/usr/bin/env python3
"""Check the agents' guide files against the fetched data, then render the review page.

Usage: build.py WORKDIR [--check] [--stack ID] [--only guide|correctness|standards|spec] [--partial]

  --check     validate the agents' files that exist, print every problem, exit 1 if any (each agent runs this)
  --stack ID  limit to one stack
  --only F    with --check, validate one agent's file: guide.json or review-<axis>.json
  --partial   render a stack with no guide yet as its file tree and diffs (the page opens before the agents finish)

Without --check, writes WORKDIR/index.html and WORKDIR/guide.json (the data serve.py reads).
Every comment is written against the stack's tip; this script routes it to the PR that wrote the line.
"""

import argparse
import hashlib
import json
import pathlib
import sys
import time

from fetch import parse_diff

SEVERITIES = ("blocker", "should", "question", "nit")
AXES = ("correctness", "standards", "spec")
ROLES = ("read", "skim", "skip")
KINDS = ("decide", "read")
STATUSES = ("verified", "unverified", "refuted")
NODE_STATES = ("new", "changed", "existing", "external")
SECTION_TYPES = ("flow", "map", "before_after", "table", "list", "steps")
LIMITS = {
    "summary": 300, "step_title": 70, "step_why": 220, "file_why": 90, "comment_title": 90,
    "comment_body": 700, "label": 40, "fact_title": 60, "fact_text": 200, "test": 140,
    "section_title": 70, "cell": 160, "item": 220, "skipped": 200,
}
MAX_FACTS, MAX_SECTIONS, MAX_ROWS, MAX_COLUMNS, MAX_ITEMS = 4, 5, 30, 6, 8
MAX_STEPS, MAX_ANCHOR_LINES, MAX_NODES, MAX_FLOW_STEPS, MAX_PER_PR_AXIS = 8, 60, 14, 10, 3
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
        self.since = stack.get("since")

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
    return {"pending": True, "summary": "", "key_facts": [], "components": None, "sections": [],
            "files": {p: {"role": "pending", "why": ""} for p in st.files}}


def check_text(err, label: str, value, limit: int) -> None:
    if not isinstance(value, str) or not value.strip():
        err(f"{label} is empty")
    elif len(value) > limit:
        err(f"{label} is {len(value)} characters; the limit is {limit}")


def check_stack(st: Stack) -> tuple[dict | None, list[str]]:
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
    sections = guide.get("sections") or []
    if not 1 <= len(sections) <= MAX_SECTIONS:
        err(f"sections: expected 1 to {MAX_SECTIONS}, got {len(sections)}")
    step_ids = set()
    for i, sec in enumerate(sections):
        check_section(sec or {}, f"sections[{i}]", st, node_ids, step_ids, err)
    return guide, errors


def check_anchor(anchor: dict, label: str, st: Stack, err) -> None:
    lines = st.tip_lines(anchor.get("path"))
    start, end = anchor.get("start"), anchor.get("end")
    if lines is None:
        err(f"{label}: anchor.path {anchor.get('path')!r} is not a changed text file at the tip")
    elif not (isinstance(start, int) and isinstance(end, int) and 1 <= start <= end <= len(lines)):
        err(f"{label}: anchor {start}..{end} is outside {anchor['path']} (1..{len(lines)})")
    elif end - start + 1 > MAX_ANCHOR_LINES:
        err(f"{label}: anchor spans {end - start + 1} lines; the limit is {MAX_ANCHOR_LINES}")


def check_section(sec: dict, label: str, st: Stack, node_ids: set[str], step_ids: set[str], err) -> None:
    kind = sec.get("type")
    if kind not in SECTION_TYPES:
        return err(f"{label}.type must be one of {SECTION_TYPES}")
    label = f"{label} ({kind})"
    check_text(err, f"{label}.title", sec.get("title"), LIMITS["section_title"])
    if kind in ("flow", "map") and not node_ids:
        err(f"{label}: needs `components` with nodes")
    if kind == "flow":
        hops = sec.get("steps") or []
        if not 1 <= len(hops) <= MAX_FLOW_STEPS:
            err(f"{label}: expected 1 to {MAX_FLOW_STEPS} steps, got {len(hops)}")
        for h in hops:
            if h.get("from") not in node_ids or h.get("to") not in node_ids:
                err(f"{label}: step {h.get('from')!r}->{h.get('to')!r} must reference component ids")
            check_text(err, f"{label} step label", h.get("label"), LIMITS["label"])
    elif kind == "before_after":
        rows = sec.get("rows") or []
        if not 1 <= len(rows) <= MAX_ROWS:
            err(f"{label}: expected 1 to {MAX_ROWS} rows, got {len(rows)}")
        for j, r in enumerate(rows):
            for k in ("aspect", "before", "after"):
                check_text(err, f"{label}.rows[{j}].{k}", (r or {}).get(k), LIMITS["cell"])
    elif kind == "table":
        cols, rows = sec.get("columns") or [], sec.get("rows") or []
        if not 2 <= len(cols) <= MAX_COLUMNS:
            err(f"{label}: expected 2 to {MAX_COLUMNS} columns, got {len(cols)}")
        if not 1 <= len(rows) <= MAX_ROWS:
            err(f"{label}: expected 1 to {MAX_ROWS} rows, got {len(rows)}")
        for j, r in enumerate(rows):
            if not isinstance(r, list) or len(r) != len(cols):
                err(f"{label}.rows[{j}]: expected {len(cols)} cells")
                continue
            for c in r:
                if not isinstance(c, str) or len(c) > LIMITS["cell"]:
                    err(f"{label}.rows[{j}]: each cell is text of at most {LIMITS['cell']} characters")
    elif kind == "list":
        items = sec.get("items") or []
        if not 1 <= len(items) <= MAX_ITEMS:
            err(f"{label}: expected 1 to {MAX_ITEMS} items, got {len(items)}")
        for j, it in enumerate(items):
            check_text(err, f"{label}.items[{j}]", it, LIMITS["item"])
    elif kind == "steps":
        steps = sec.get("steps") or []
        if not 1 <= len(steps) <= MAX_STEPS:
            err(f"{label}: expected 1 to {MAX_STEPS} steps, got {len(steps)}")
        for s in steps:
            sid = s.get("id")
            slabel = f"step {sid!r}"
            if not isinstance(sid, str) or not sid or sid in step_ids:
                err(f"{slabel}: id missing or duplicated")
            step_ids.add(sid)
            if s.get("kind") not in KINDS:
                err(f"{slabel}: kind must be one of {KINDS}")
            check_text(err, f"{slabel}.title", s.get("title"), LIMITS["step_title"])
            check_text(err, f"{slabel}.why", s.get("why"), LIMITS["step_why"])
            if s.get("test") is not None:
                check_text(err, f"{slabel}.test", s["test"], LIMITS["test"])
            if bad := [n for n in s.get("nodes") or [] if n not in node_ids]:
                err(f"{slabel}: nodes not in components: {bad}")
            check_anchor(s.get("anchor") or {}, slabel, st, err)


def check_findings(st: Stack, axis: str) -> tuple[dict | None, list[str]]:
    """One reviewer's file: findings on lines the stack wrote, at most MAX_PER_PR_AXIS per PR."""
    errors: list[str] = []
    err = lambda m: errors.append(f"stack {st.id} {axis}: {m}")  # noqa: E731
    try:
        report = json.loads((st.dir / f"review-{axis}.json").read_text())
    except FileNotFoundError:
        return None, [f"stack {st.id}: review-{axis}.json is missing"]
    except json.JSONDecodeError as e:
        return None, [f"stack {st.id}: review-{axis}.json is not valid JSON ({e})"]
    if report.get("skipped") is not None:
        check_text(err, "skipped", report["skipped"], LIMITS["skipped"])
    ids, routed = set(), {n: 0 for n in st.prs}
    for c in report.get("findings") or []:
        cid = c.get("id")
        clabel = f"finding {cid!r}"
        if not isinstance(cid, str) or not cid or cid in ids:
            err(f"{clabel}: id missing or duplicated")
        ids.add(cid)
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
                routed[c["pr"]] += 1
            continue
        r = st.route(c["path"], c.get("line"), c.get("start_line"))
        if isinstance(r, str):
            err(f"{clabel}: {r}")
        else:
            routed[r[0]] += 1
    for n, count in routed.items():
        if count > MAX_PER_PR_AXIS:
            err(f"#{n} receives {count} findings; keep the {MAX_PER_PR_AXIS} that matter most")
    return report, errors


def check_map(guide: dict, st: Stack, err) -> set[str]:
    m = guide.get("components")
    if m is None:
        return set()
    layers = {l.get("id") for l in m.get("layers") or []}
    for l in m.get("layers") or []:
        check_text(err, f"components layer {l.get('id')!r} label", l.get("label"), LIMITS["label"])
    nodes = m.get("nodes") or []
    if not 1 <= len(nodes) <= MAX_NODES:
        err(f"components: expected 1 to {MAX_NODES} nodes, got {len(nodes)}")
    ids = set()
    for node in nodes:
        nid = node.get("id")
        if not isinstance(nid, str) or not nid or nid in ids:
            err(f"component {nid!r}: id missing or duplicated")
        ids.add(nid)
        check_text(err, f"component {nid!r} label", node.get("label"), LIMITS["label"])
        if node.get("role") is not None:
            check_text(err, f"component {nid!r} role", node["role"], 30)
        if node.get("layer") not in layers:
            err(f"component {nid!r}: layer {node.get('layer')!r} is not declared")
        if node.get("state") not in NODE_STATES:
            err(f"component {nid!r}: state must be one of {NODE_STATES}")
        if node.get("state") in ("new", "changed") and node.get("path") not in st.files:
            err(f"component {nid!r}: a {node['state']} node needs path set to the changed file that holds it")
    for e in m.get("edges") or []:
        if e.get("from") not in ids or e.get("to") not in ids:
            err(f"components edge {e.get('from')!r}->{e.get('to')!r}: both ends must be node ids")
        if e.get("label"):
            check_text(err, f"components edge {e.get('from')}->{e.get('to')} label", e["label"], LIMITS["label"])
    return ids


def verify_results(sdir: pathlib.Path) -> dict[str, dict]:
    """The verifiers' files, one per finding: `verify/<axis>-<id>.json`. A file still being written counts as missing."""
    out = {}
    for f in (sdir / "verify").glob("*.json"):
        try:
            out[f.stem] = json.loads(f.read_text())
        except ValueError:
            pass
    return out


def stages(w: pathlib.Path) -> list[dict]:
    """Per stack, where each agent stands: from the files the agents write, and progress.json when the runner wrote it.
    A stage's `secs` is how long its agent ran (or has run so far); the stack's `secs` counts from the runner's start."""
    run = json.loads((w / "run.json").read_text())
    try:
        progress = json.loads((w / "progress.json").read_text())
    except (OSError, ValueError):
        progress = {}
    agents = progress.get("agents") or {}
    now = time.time()
    t0 = progress.get("started") or run.get("fetched_at") or now
    out = []
    for s in run["stacks"]:
        d, sid = w / s["id"], s["id"]

        def stage(key: str, label: str, path: pathlib.Path) -> dict:
            a = agents.get(f"{sid}/{key}") or {}
            start = a.get("started") or t0
            if path.is_file():
                res = {"key": key, "label": label, "state": "done", "secs": round((a.get("ended") or path.stat().st_mtime) - start)}
                if key in AXES:
                    try:
                        report = json.loads(path.read_text())
                    except ValueError:
                        return {"key": key, "label": label, "state": "running", "secs": round(now - start)}
                    if report.get("skipped"):
                        res.update(state="skipped", note=report["skipped"])
                    else:
                        res["found"] = len(report.get("findings") or [])
                return res
            state = a.get("state") or ("running" if not agents else "waiting")
            secs = round((a.get("ended") or now) - start) if state != "waiting" else None
            return {"key": key, "label": label, "state": state, "secs": secs, "calls": a.get("calls", 0), "last": a.get("last", "")}

        row = [stage("guide", "Guide", d / "guide.json"), *(stage(a, a.capitalize(), d / f"review-{a}.json") for a in AXES)]
        total = None
        if all(x["state"] in ("done", "skipped", "failed") for x in row[1:]):
            total = sum(x.get("found", 0) for x in row[1:])
        checked = verify_results(d)
        verifiers = [a for k, a in agents.items() if k.startswith(f"{sid}/verify/")]
        running = [a for a in verifiers if a.get("state") == "running"]
        vstart = min((a["started"] for a in verifiers if a.get("started")), default=t0)
        if total is None:
            check = {"state": "waiting"}
        elif len(checked) >= total:
            last = max((f.stat().st_mtime for f in (d / "verify").glob("*.json")), default=vstart)
            check = {"state": "done", "secs": round(last - vstart)}
        else:
            latest = max(running, key=lambda a: a.get("started", 0), default={})
            check = {"state": "running", "secs": round(now - vstart), "calls": sum(a.get("calls", 0) for a in running), "last": latest.get("last", "")}
        row.append({"key": "check", "label": "Fact check", "checked": len(checked), "total": total, **check})
        out.append({"id": sid, "secs": round(now - t0), "stages": row})
    return out


def collect_findings(st: Stack, reports: dict[str, dict]) -> tuple[list[dict], dict[str, str], list[str]]:
    """Every reviewer's findings with the verifier's status. Duplicates the verifier names are dropped."""
    warnings, skipped = [], {}
    results = verify_results(st.dir)
    out = []
    for axis in AXES:
        report = reports.get(axis)
        if report is None:
            continue
        if report.get("skipped"):
            skipped[axis] = report["skipped"]
        for c in report.get("findings") or []:
            c = {**c, "axis": axis, "key": f"{axis}/{c['id']}"}
            r = results.get(f"{axis}-{c['id']}")
            if r is None:
                c["status"], c["note"] = "checking", "The verifier is still checking this comment."
                out.append(c)
                continue
            if r.get("status") == "duplicate":
                continue
            c["status"] = r.get("status") if r.get("status") in STATUSES else "unverified"
            c["note"] = r.get("note") or ("" if r else "The verifier did not check this comment.")
            if c.get("path") and r.get("line") and r["line"] != c.get("line"):
                if isinstance(st.route(c["path"], r["line"]), tuple):
                    c["line"] = r["line"]
                    c.pop("start_line", None)
                else:
                    warnings.append(f"stack {st.id}: verifier moved {c['key']} to {r['line']}, a line the stack did not write; kept {c.get('line')}")
            out.append(c)
    return out, skipped, warnings


def diff_anchor(path: str) -> str:
    return "diff-" + hashlib.sha256(path.encode()).hexdigest()


def render_stack(st: Stack, repo: str, guide: dict, findings: list[dict], skipped: dict[str, str], reviewing: list[str], local: bool = False) -> dict:
    # A self-review's commits may exist only on this machine: no GitHub link points at them.
    compare = None if local else f"https://github.com/{repo}/compare/{st.base}...{st.tip}"

    def located(path: str, line: int) -> dict:
        own = st.owner(path, line)
        return {"pr": own[0], "pl": own[1]} if own else {}

    def line_rows(path: str, start: int, end: int) -> list[dict]:
        text = st.tip_lines(path)
        added = set(st.target.get(path, {}).get("added", []))
        return [{"n": i, "t": text[i - 1], "k": "+" if i in added else " ", **located(path, i)} for i in range(start, end + 1)]

    comments = []
    for c in findings:
        out = {k: c.get(k) for k in ("severity", "title", "body", "evidence", "path", "line", "start_line", "status", "note", "axis")}
        out["id"] = f"{st.id}-{c['axis']}-{c['id']}"
        if c.get("path"):
            pr, pl, ps = st.route(c["path"], c["line"], c.get("start_line"))
            info = st.pr_info[pr]
            out |= {"pr": pr, "pl": pl, "ps": ps,
                    "link": None if local else f"https://github.com/{repo}/blob/{info['review_sha']}/{c['path']}#L{ps or pl}" + (f"-L{pl}" if ps else "")}
        else:
            out |= {"pr": c["pr"], "pl": None, "ps": None, "link": None}
        comments.append(out)

    sections = []
    for i, sec in enumerate(guide["sections"]):
        sec = dict(sec)
        if sec["type"] == "steps":
            steps = []
            for s in sec["steps"]:
                a = s["anchor"]
                steps.append({
                    "id": f"{st.id}-{s['id']}", "kind": s["kind"], "title": s["title"], "why": s["why"], "nodes": s.get("nodes") or [],
                    "test": s.get("test", False), "path": a["path"], "start": a["start"], "end": a["end"],
                    "lines": line_rows(a["path"], a["start"], a["end"]),
                    "link": compare and f"{compare}#{diff_anchor(a['path'])}R{a['start']}-R{a['end']}",
                })
            sec["steps"] = steps
        sections.append(sec)

    files = []
    for path, f in st.files.items():
        hunks = [[{**line, **(located(path, line["n"]) if line["k"] == "+" else {})} for line in hunk] for hunk in st.target.get(path, {}).get("hunks", [])]
        g = guide["files"][path]
        files.append({**f, "role": g["role"], "why": g["why"], "hunks": hunks,
                      "link": compare and f"{compare}#{diff_anchor(path)}",
                      "pr_links": {n: st.pr_info[n]["url"] and f"{st.pr_info[n]['url']}/files#{diff_anchor(path)}" for n in f["prs"]}})

    prs = [{**{k: st.pr_info[n][k] for k in ("n", "url", "title", "author", "sha", "review_sha", "draft", "checks")},
            **{k: st.pr_info[n].get(k) for k in ("last_review", "label", "pr_number", "pushed", "head")}} for n in st.prs]
    return {"id": st.id, "pending": bool(guide.get("pending")), "reviewing": reviewing, "skipped": skipped,
            "base": st.base, "tip": st.tip, "since": st.since, "compare": compare, "summary": guide["summary"],
            "key_facts": guide.get("key_facts") or [], "components": guide.get("components"), "sections": sections,
            "prs": prs, "files": files, "comments": comments}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir", type=pathlib.Path)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--stack")
    ap.add_argument("--only", choices=("guide", *AXES))
    ap.add_argument("--partial", action="store_true", help="render stacks without a guide as file tree and diffs only")
    args = ap.parse_args()
    run = json.loads((args.workdir / "run.json").read_text())
    stacks = [Stack(args.workdir, run, s) for s in run["stacks"] if args.stack in (None, s["id"])]
    if not stacks:
        sys.exit(f"no stack {args.stack!r}; stacks are {[s['id'] for s in run['stacks']]}")

    errors, guides, reports = [], {}, {}
    for st in stacks:
        reports[st.id] = {}
        for axis in AXES:
            if args.only in (None, axis) and (args.only == axis or (st.dir / f"review-{axis}.json").is_file()):
                reports[st.id][axis], errs = check_findings(st, axis)
                errors += errs
        # Reviewers check only their own file; a plain --check or --partial accepts a guide that is not written yet.
        if args.only not in (None, "guide") or args.only is None and (args.partial or args.check) and not (st.dir / "guide.json").is_file():
            guides[st.id] = skeleton(st)
            continue
        guide, errs = check_stack(st)
        errors += errs
        guides[st.id] = guide
    if errors:
        print("\n".join(errors))
        sys.exit(1)
    if args.check:
        print("ok")
        return

    warnings, rendered = [], []
    for st in stacks:
        findings, skipped, warns = collect_findings(st, reports[st.id])
        warnings += warns
        reviewing = [a for a in AXES if a not in reports[st.id]]
        rendered.append(render_stack(st, run["repo"], guides[st.id], findings, skipped, reviewing, run.get("mode") == "self"))
    data = {"repo": run["repo"], "viewer": run["viewer"], "mode": run.get("mode", "pr"), "source": run.get("source"), "effort": run.get("effort", "balanced"), "run_id": run["run_id"], "warnings": run.get("warnings") or [],
            "stacks": rendered}
    (args.workdir / "guide.json").write_text(json.dumps(data, indent=1))
    embedded = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    (args.workdir / "index.html").write_text(TEMPLATE.read_text().replace("/*DATA*/null", embedded))
    for w in warnings:
        print("warning:", w)
    for s in data["stacks"]:
        by_axis = {a: sum(c["axis"] == a for c in s["comments"]) for a in AXES}
        by_status = {k: sum(c["status"] == k for c in s["comments"]) for k in STATUSES}
        print(f"stack {s['id']}: {len(s['sections'])} sections, findings {by_axis}, {by_status}")
    print(args.workdir / "index.html")


if __name__ == "__main__":
    main()
