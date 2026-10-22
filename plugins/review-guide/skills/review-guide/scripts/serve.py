#!/usr/bin/env python3
"""The review-guide server: one per machine, holding every review, and the command the skill sends reviews to it with.

Usage:
  serve.py [--home DIR] [--port N]       run the server (port 7357, or a free one when that is taken)
  serve.py open [--home DIR] [--effort quick|balanced|deep] [--focus TEXT] [--wait] [--no-browser] <pr-ref>...
                                         send PRs to the running server, starting one when none answers; print the
                                         review URL and open it; with --wait, follow the round and print its summary
  serve.py open --self [--branch B] [--base REF] [--wait-feedback] [...]
                                         a self-review of the local branch stack in the current repository: the
                                         feedback goes to the agent instead of GitHub; with --wait-feedback, wait until
                                         it is sent from the page (or the review is finished) and print it

A review is the set of PR refs given to one invocation: the same set again adds a round to it. Each round is a work
directory (fetch.py, then the agents through rerun.py, then build.py) under DIR/reviews/<id>/rounds/<n>/. The page state
is saved next to it in ui.json, so it survives restarts, port changes and browsers. The first load of a new round
carries drafts, verdict text and viewed marks over from the previous one. Runners are detached: stopping the server does
not stop a round, and the next server picks it up.

Binds 127.0.0.1 only. Every write carries the per-process token embedded in the served pages (and in server.json, mode
0600, for `open`), so another site open in the browser cannot post reviews in the user's name. Before posting, the PR
head is compared with the SHA the guide was built on, and each inline comment line is checked against the diff. Only the
latest built round of a review takes writes; earlier rounds are read-only.
"""

import argparse
import fcntl
import hashlib
import http.server
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from build import stages
from fetch import PR_REF

HERE = pathlib.Path(__file__).resolve().parent
UI = HERE.parent / "ui"
HOME = pathlib.Path.home() / ".cache" / "review-guide"
DEFAULT_PORT = 7357
EFFORTS = ("quick", "balanced", "deep")
TOKEN = secrets.token_urlsafe(24)


def code_version() -> str:
    """A hash of the scripts and pages this server runs, so `open` can tell a server started before an update."""
    h = hashlib.sha1(usedforsecurity=False)
    for f in sorted([*HERE.glob("*.py"), *UI.glob("*.html")]):
        h.update(f.name.encode() + f.read_bytes())
    return h.hexdigest()[:12]


VERSION = code_version()
ID = r"[\w-][\w.-]*"


def read(path: pathlib.Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write(path: pathlib.Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def gh(*args: str, stdin: str | None = None) -> str:
    r = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout


SELF_REF = re.compile(r"^self:(.+)@([^@]+)$")


def norm_refs(refs: list[str]) -> list[str]:
    """`owner/repo#n` for each PR, sorted, so the same PRs in any order or form name the same review. A self-review is
    one `self:<repo root>@<branch>` ref."""
    if len(refs) == 1 and SELF_REF.match(refs[0]):
        return refs
    out = set()
    for ref in refs:
        m = PR_REF.search(ref)
        if not m:
            raise ValueError(f"not a PR reference: {ref}")
        out.add(f"{m.group(1)}/{m.group(2)}#{int(m.group(3))}")
    return sorted(out, key=lambda r: (r.split("#")[0].lower(), int(r.split("#")[1])))


def review_id(refs: list[str]) -> str:
    if m := SELF_REF.match(refs[0]):
        slug = re.sub(r"[^\w.-]", "-", f"self-{pathlib.Path(m.group(1)).name}-{m.group(2)}")[:80]
        return f"{slug}-{hashlib.sha1(refs[0].encode(), usedforsecurity=False).hexdigest()[:8]}"
    repos = sorted({r.split("#")[0].split("/")[1] for r in refs})
    slug = re.sub(r"[^\w.-]", "-", "-".join(repos) + "-" + "-".join(r.split("#")[1] for r in refs))[:80]
    key = "\n".join(refs).lower().encode()
    return f"{slug}-{hashlib.sha1(key, usedforsecurity=False).hexdigest()[:8]}"


def runner_alive(pid) -> bool:
    """A runner started by an earlier server: alive while its pid still runs rerun.py (a reused pid does not)."""
    if not pid:
        return False
    return "rerun.py" in subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout


def usual(home: pathlib.Path) -> dict:
    """Average run time per effort level, in seconds, from the runner's last ten runs of each."""
    return {k: round(sum(v) / len(v)) for k, v in (read(home / "timings.json") or {}).items() if v}


def rows_of(stack: dict, path: str) -> list[dict]:
    f = next((f for f in stack["files"] if f["path"] == path), None)
    return [r for hunk in (f or {}).get("hunks", []) for r in hunk]


def carry_over(prev_ui: dict, prev_data: dict, new_data: dict, prev_posted: dict) -> dict:
    """The first page state of a new round from an earlier round's: your comments and accepted findings, moved to the
    same text in the new diff (or listed in `lost`), verdict text, and viewed marks on files that did not change.
    Nothing is carried for a PR that round already posted on."""
    posted = {int(k) for k in prev_posted}
    cm, prev_extra = prev_ui.get("cm") or {}, prev_ui.get("extra") or {}
    stack_of = {p["n"]: st for st in new_data["stacks"] for p in st["prs"]}
    ids = {st["id"] for st in new_data["stacks"]}
    ui = {"view": "guide", "extra": {}, "cm": {}, "verdict": {}, "viewed": {}, "lost": []}
    if prev_ui.get("stack") in ids:
        ui |= {"stack": prev_ui["stack"], "filter": prev_ui.get("filter", "all")}
    count = 0
    for st in prev_data["stacks"]:
        for c in st["comments"] + prev_extra.get(st["id"], []):
            cs = cm.get(c["id"])
            if not cs or cs.get("gone") or not cs.get("on") or not (cs.get("text") or "").strip() or c["pr"] in posted:
                continue
            lost = {"pr": c["pr"], "path": c.get("path"), "line": c.get("line"), "sev": cs.get("sev"), "text": cs["text"]}
            target = stack_of.get(c["pr"])
            new = {"custom": True, "severity": cs.get("sev") or "comment", "path": None, "line": None, "pr": c["pr"], "pl": None, "ps": None}
            if target and c.get("path"):
                old = next((r["t"] for r in rows_of(st, c["path"]) if r["k"] != "-" and r.get("n") == c["line"]), None)
                same = [r for r in rows_of(target, c["path"]) if r["k"] == "+" and r.get("pr") and r["t"] == old]
                if old is not None and same:
                    r = min(same, key=lambda r: abs(r["n"] - c["line"]))
                    new |= {"path": c["path"], "line": r["n"], "pr": r["pr"], "pl": r["pl"]}
                else:
                    target = None
            if target is None:
                ui["lost"].append(lost)
                continue
            count += 1
            new["id"] = f"{target['id']}-c{count}"
            ui["extra"].setdefault(target["id"], []).append(new)
            ui["cm"][new["id"]] = {"on": True, "gone": False, "sev": new["severity"], "text": cs["text"]}
    for n, v in (prev_ui.get("verdict") or {}).items():
        if int(n) not in posted and int(n) in stack_of and (v.get("body") or "").strip():
            ui["verdict"][n] = {"body": v["body"]}
    prev_heads = {p["n"]: p["sha"] for st in prev_data["stacks"] for p in st["prs"]}
    viewed = prev_ui.get("viewed") or {}
    for st in prev_data["stacks"]:
        for path in (f["path"] for f in st["files"] if viewed.get(st["id"] + f["path"])):
            target = next((t for t in new_data["stacks"] if any(f["path"] == path for f in t["files"])), None)
            if target is None:
                continue
            since = target.get("since") or {}
            moved = path in since["files"] if since.get("state") == "ROUND" else any(prev_heads.get(p["n"]) != p["sha"] for p in target["prs"])
            if not moved:
                ui["viewed"][target["id"] + path] = True
    return ui


class Home:
    """Every review on disk, and the runners this server started."""

    def __init__(self, path: pathlib.Path):
        self.path, self.lock, self.procs = path, threading.RLock(), {}
        self.counts: dict[tuple, int] = {}

    def rdir(self, rid: str, n: int) -> pathlib.Path:
        return self.path / "reviews" / rid / "rounds" / str(n)

    def review(self, rid: str) -> dict | None:
        return read(self.path / "reviews" / rid / "review.json")

    def save_review(self, rev: dict) -> None:
        write(self.path / "reviews" / rev["id"] / "review.json", rev)

    def built(self, rid: str, rev: dict) -> list[int]:
        return [r["n"] for r in rev["rounds"] if (self.rdir(rid, r["n"]) / "index.html").is_file()]

    def live(self, rid: str, rev: dict) -> int | None:
        """The round that takes writes: the latest one with a page."""
        return (self.built(rid, rev) or [None])[-1]

    def round_info(self, rid: str, n: int) -> dict:
        rdir = self.rdir(rid, n)
        p = read(rdir / "progress.json") or {}
        proc = self.procs.get(rdir)
        built = (rdir / "index.html").is_file()
        if p.get("finished"):
            state = "failed" if p.get("error") else "done"
        elif proc and proc.poll() is None or runner_alive(p.get("pid")):
            state = "running"
        else:
            state = "done" if built and not p else "failed"
        error = (p.get("error") or "The runner stopped before the end; see runner.log.") if state == "failed" else ""
        return {"n": n, "state": state, "status": p.get("status", ""), "error": error, "built": built, "effort": p.get("effort")}

    def start_round(self, rid: str, effort: str, focus: str | None) -> tuple[int, bool]:
        """Add a round and start its runner, unless the latest round is still running."""
        with self.lock:
            rev = self.review(rid)
            if rev["rounds"] and self.round_info(rid, rev["rounds"][-1]["n"])["state"] == "running":
                return rev["rounds"][-1]["n"], False
            n = len(rev["rounds"]) + 1
            rdir = self.rdir(rid, n)
            rdir.mkdir(parents=True)
            prev = next((self.rdir(rid, m) for m in range(n - 1, 0, -1) if (self.rdir(rid, m) / "run.json").is_file()), None)
            cmd = [sys.executable, str(HERE / "rerun.py"), str(rdir), "--effort", effort, "--refs", *rev["refs"],
                   *(["--prev", str(prev)] if prev else []), *(["--focus", focus] if focus else [])]
            cwd = rev.get("cwd") if rev.get("cwd") and pathlib.Path(rev["cwd"]).is_dir() else None
            with (rdir / "runner.log").open("w") as log:
                self.procs[rdir] = subprocess.Popen(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            rev["rounds"].append({"n": n, "effort": effort, "focus": focus, "created": int(time.time())})
            rev.update(status="open", updated=int(time.time()))
            self.save_review(rev)
            return n, True

    def ui(self, rid: str, rev: dict, n: int) -> dict:
        """The saved page state of a round. The first load of the live round builds it from the newest earlier round
        that has one."""
        rdir = self.rdir(rid, n)
        with self.lock:
            saved = read(rdir / "ui.json")
            if saved is not None or n != self.live(rid, rev):
                return saved or {}
            src = next((self.rdir(rid, m) for m in range(n - 1, 0, -1) if (self.rdir(rid, m) / "ui.json").is_file()), None)
            ui = {}
            if src:
                prev_data, new_data = read(src / "guide.json"), read(rdir / "guide.json")
                prev_posted = read(src / "posted.json") or {}
                ui = carry_over(read(src / "ui.json") or {}, prev_data, new_data, prev_posted)
                heads = {p["n"]: p["sha"] for st in new_data["stacks"] for p in st["prs"]}
                old = {p["n"]: p["sha"] for st in prev_data["stacks"] for p in st["prs"]}
                # A review posted on a head that did not move stays posted, so the same PR is not reviewed twice. Feedback
                # sent to the agent does not carry: each self-review round can send its own.
                if not SELF_REF.match(rev["refs"][0]) and (keep := {k: v for k, v in prev_posted.items() if old.get(int(k)) == heads.get(int(k))}):
                    write(rdir / "posted.json", (read(rdir / "posted.json") or {}) | keep)
            write(rdir / "ui.json", ui)
            return ui

    def summary(self, rid: str, rev: dict) -> dict:
        """One inbox row."""
        live, newest = self.live(rid, rev), rev["rounds"][-1]["n"] if rev["rounds"] else None
        rdir = self.rdir(rid, live or newest or 1)
        run = read(rdir / "run.json") or {}
        posted = read(rdir / "posted.json") or {}
        findings = None
        if live:
            path = rdir / "guide.json"
            key = (str(path), path.stat().st_mtime_ns)
            if key not in self.counts:
                self.counts[key] = sum(len(s["comments"]) for s in (read(path) or {"stacks": []})["stacks"])
            findings = self.counts[key]
        touched = max([rev.get("updated", 0)] + [int((self.rdir(rid, r["n"]) / "ui.json").stat().st_mtime)
                                                 for r in rev["rounds"] if (self.rdir(rid, r["n"]) / "ui.json").is_file()])
        prs = [{"n": p["n"], "title": p["title"], "author": p["author"], "label": p.get("label"), "posted": str(p["n"]) in posted} for p in run.get("prs", [])]
        self_ref = SELF_REF.match(rev["refs"][0])
        return {"id": rid, "refs": rev["refs"], "mode": "self" if self_ref else "pr", "branch": self_ref and self_ref.group(2),
                "sent": (read(rdir / "feedback.json") or {}).get("sent_at"), "repo": run.get("repo") or rev["refs"][0].split("#")[0], "status": rev.get("status", "open"),
                "created": rev.get("created"), "updated": touched, "rounds": len(rev["rounds"]), "live": live, "prs": prs, "findings": findings,
                "newest": self.round_info(rid, newest) if newest else None}

    def reviews(self) -> list[dict]:
        out = []
        for d in sorted((self.path / "reviews").glob("*/review.json")):
            rev = read(d)
            if rev:
                out.append(self.summary(rev["id"], rev))
        return sorted(out, key=lambda r: -r["updated"])


def waiting_page(info: dict | None, theme: str | None) -> str:
    """The page of a review whose first round is still fetching, or failed before it had a page."""
    msg = f"Could not build this review: {info['error']}" if info and info["state"] == "failed" else f"{(info or {}).get('status') or 'Starting'}…"
    refresh = "" if info and info["state"] == "failed" else '<meta http-equiv="refresh" content="2">'
    esc = msg.replace("&", "&amp;").replace("<", "&lt;")
    return (f'<!doctype html><html lang="en"{f" data-theme={theme}" if theme else ""}><head><meta charset="utf-8">{refresh}'
            '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Review guide</title>'
            '<link href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;700&display=swap" rel="stylesheet">'
            '<style>:root{--bg:#fff;--text:#00264c;--muted:#4f6a85;--accent:#107aca}'
            '@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#101010;--text:#ececec;--muted:#a0a0a0;--accent:#00bfff}}'
            ':root[data-theme=dark]{--bg:#101010;--text:#ececec;--muted:#a0a0a0;--accent:#00bfff}'
            "body{margin:0;background:var(--bg);color:var(--text);font-family:'Figtree',system-ui,sans-serif}"
            'main{max-width:520px;margin:18vh auto;padding:0 16px;text-align:center;color:var(--muted)}h1{color:var(--text)}a{color:var(--accent)}</style>'
            f'</head><body><main><h1>Review guide</h1><p>{esc}</p><p><a href="/">All reviews</a></p></main></body></html>')


def handler(home: Home):
    heads_cache: dict = {}
    write_lock = threading.Lock()

    def prefs() -> dict:
        return read(home.path / "prefs.json") or {}

    def with_theme(html: str) -> str:
        theme = prefs().get("theme")
        return html.replace('<html lang="en">', f'<html lang="en" data-theme="{theme}">', 1) if theme in ("light", "dark") else html

    def heads(rdir: pathlib.Path) -> dict:
        """The PRs' current head SHAs on GitHub, in one GraphQL call, cached for a minute. For a self-review, the local
        branch tips; uncommitted changes are not watched."""
        hit = heads_cache.get(rdir)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        data = read(rdir / "guide.json")
        if data.get("mode") == "self":
            root = data["source"]["root"]
            tips = {}
            for p in (p for st in data["stacks"] for p in st["prs"] if p.get("head") != "(uncommitted)"):
                r = subprocess.run(["git", "-C", root, "rev-parse", "--verify", "--quiet", f"refs/heads/{p['head']}"], capture_output=True, text=True)
                if r.returncode == 0:
                    tips[str(p["n"])] = r.stdout.strip()
            heads_cache[rdir] = (time.time(), tips)
            return tips
        owner, name = data["repo"].split("/")
        fields = " ".join(f"p{p['n']}: pullRequest(number: {p['n']}) {{ headRefOid }}" for st in data["stacks"] for p in st["prs"])
        out = json.loads(gh("api", "graphql", "-f", f"query=query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {fields} }} }}"))
        heads_cache[rdir] = (time.time(), {k[1:]: v["headRefOid"] for k, v in out["data"]["repository"].items() if v})
        return heads_cache[rdir][1]

    class Handler(http.server.BaseHTTPRequestHandler):
        def reply(self, code: int, payload, ctype: str = "application/json") -> None:
            body = payload if isinstance(payload, bytes) else payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def body(self) -> dict:
            size = int(self.headers.get("Content-Length", 0))
            if size > 8_000_000:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(size) or "{}")

        def found(self, rid: str) -> dict | None:
            rev = home.review(rid)
            if rev is None:
                self.reply(404, {"error": f"no review {rid}"})
            return rev

        def page(self, rid: str, rev: dict, n: int, live: bool) -> None:
            rounds = [{**home.round_info(rid, r["n"]), "effort": r["effort"], "created": r["created"]} for r in rev["rounds"]]
            boot = {"base": f"/api/r/{rid}/{n}", "review": rid, "round": n, "live": live, "status": rev.get("status", "open"),
                    "ui": home.ui(rid, rev, n), "rounds": rounds}
            html = (home.rdir(rid, n) / "index.html").read_text()
            html = html.replace("/*TOKEN*/", TOKEN, 1).replace("/*BOOT*/null", json.dumps(boot).replace("<", "\\u003c"), 1)
            self.reply(200, with_theme(html), "text/html; charset=utf-8")

        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            if path == "/api/ping":
                return self.reply(200, {"app": "review-guide", "pid": os.getpid(), "version": VERSION})
            if path in ("/", "/index.html"):
                html = (UI / "inbox.html").read_text().replace("/*TOKEN*/", TOKEN, 1)
                html = html.replace("/*DATA*/null", json.dumps(home.reviews()).replace("<", "\\u003c"), 1)
                return self.reply(200, with_theme(html), "text/html; charset=utf-8")
            if path == "/api/reviews":
                return self.reply(200, {"reviews": home.reviews()})
            if m := re.fullmatch(rf"/r/({ID})(?:/(\d+))?/?", path):
                rid = m.group(1)
                if not (rev := self.found(rid)):
                    return
                live = home.live(rid, rev)
                n = int(m.group(2)) if m.group(2) else live
                if n is None:
                    newest = rev["rounds"][-1]["n"] if rev["rounds"] else None
                    return self.reply(200, waiting_page(home.round_info(rid, newest) if newest else None, prefs().get("theme")), "text/html; charset=utf-8")
                if not (home.rdir(rid, n) / "index.html").is_file():
                    return self.reply(404, {"error": f"round {n} has no page"})
                return self.page(rid, rev, n, n == live)
            if m := re.fullmatch(rf"/api/r/({ID})/(\d+)/state", path):
                rid, n = m.group(1), int(m.group(2))
                if not (rev := self.found(rid)):
                    return
                rdir, live = home.rdir(rid, n), home.live(rid, rev)
                info, newest = home.round_info(rid, n), home.round_info(rid, rev["rounds"][-1]["n"])
                try:
                    progress = stages(rdir)
                except (OSError, ValueError, KeyError):
                    progress = []
                if info["state"] != "running":
                    # A runner that died leaves its agents marked running; show them as failed instead.
                    for row in progress:
                        for x in row["stages"]:
                            if x["state"] in ("running", "waiting"):
                                x["state"] = "failed"
                later = newest["n"] > n and n == live
                rerun = {"running": later and newest["state"] == "running", "status": newest["status"] if later else "",
                         "error": newest["error"] if later and not newest["built"] else ""}
                version = f"{live}:{(home.rdir(rid, live) / 'index.html').stat().st_mtime_ns}" if n == live else f"{n}"
                return self.reply(200, {"posted": read(rdir / "posted.json") or {}, "version": version, "live": live, "rerun": rerun,
                                        "stages": progress, "usual": usual(home.path)})
            if m := re.fullmatch(rf"/api/r/({ID})/heads", path):
                if not (rev := self.found(m.group(1))):
                    return
                live = home.live(m.group(1), rev)
                try:
                    return self.reply(200, {"heads": heads(home.rdir(m.group(1), live)) if live else {}})
                except (RuntimeError, KeyError, ValueError, TypeError) as e:
                    return self.reply(502, {"error": str(e)})
            self.reply(404, {"error": "not found"})

        def do_PUT(self) -> None:
            self.do_POST()

        def do_POST(self) -> None:
            path = self.path.split("?")[0]
            if not secrets.compare_digest(self.headers.get("X-Review-Token", ""), TOKEN):
                return self.reply(403, {"error": "missing or wrong token; reload the page"})
            try:
                if path == "/api/stop":
                    self.reply(200, {"stopped": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                if path == "/api/prefs":
                    theme = self.body().get("theme")
                    write(home.path / "prefs.json", prefs() | {"theme": theme if theme in ("light", "dark") else None})
                    return self.reply(200, {"ok": True})
                if path == "/api/reviews":
                    req = self.body()
                    refs = norm_refs(req["refs"])
                    if len({r.split("#")[0].lower() for r in refs}) > 1:
                        return self.reply(400, {"error": "PRs from several repositories are not supported yet; send one review per repository"})
                    effort = req.get("effort") or "balanced"
                    if effort not in EFFORTS:
                        return self.reply(400, {"error": "effort must be quick, balanced or deep"})
                    rid = review_id(refs)
                    with home.lock:
                        rev = home.review(rid) or {"id": rid, "refs": refs, "created": int(time.time()), "rounds": []}
                        rev["cwd"] = req.get("cwd") or rev.get("cwd")
                        (home.path / "reviews" / rid).mkdir(parents=True, exist_ok=True)
                        home.save_review(rev)
                        n, started = home.start_round(rid, effort, req.get("focus"))
                    return self.reply(200, {"id": rid, "round": n, "started": started, "url": f"/r/{rid}"})
                m = re.fullmatch(rf"/api/r/({ID})(?:/(\d+))?/(\w+)", path)
                if not m:
                    return self.reply(404, {"error": "not found"})
                rid, n, action = m.group(1), m.group(2) and int(m.group(2)), m.group(3)
                if not (rev := self.found(rid)):
                    return
                if action in ("finish", "reopen") and n is None:
                    with home.lock:
                        rev = home.review(rid)
                        rev.update(status="done" if action == "finish" else "open", updated=int(time.time()))
                        home.save_review(rev)
                    return self.reply(200, {"status": rev["status"]})
                if action == "rerun" and n is None:
                    effort = self.body().get("effort")
                    if effort not in EFFORTS:
                        return self.reply(400, {"error": "effort must be quick, balanced or deep"})
                    focus = rev["rounds"][-1].get("focus") if rev["rounds"] else None
                    n, started = home.start_round(rid, effort, focus)
                    if not started:
                        return self.reply(409, {"error": "a round is already running"})
                    return self.reply(200, {"running": True, "status": "Starting", "error": "", "round": n})
                if n is None or action not in ("ui", "review", "feedback"):
                    return self.reply(404, {"error": "not found"})
                live, rdir = home.live(rid, rev), home.rdir(rid, n)
                built = home.built(rid, rev)
                # The page of the round before still saves until the new round's page first loads and carries its state over.
                handover = action == "ui" and len(built) > 1 and n == built[-2] and not (home.rdir(rid, live) / "ui.json").is_file()
                if n != live and not handover:
                    return self.reply(409, {"error": "this is an earlier round of the review; open the latest one"})
                if action == "ui":
                    state = self.body()
                    if not isinstance(state, dict):
                        return self.reply(400, {"error": "expected an object"})
                    write(rdir / "ui.json", state)
                    return self.reply(200, {"ok": True})
                is_self = bool(SELF_REF.match(rev["refs"][0]))
                if action == "feedback":
                    if not is_self:
                        return self.reply(400, {"error": "feedback goes to an agent only in a self-review"})
                    return self.send_feedback(rdir, rev)
                if is_self:
                    return self.reply(400, {"error": "a self-review is never posted to GitHub"})
                return self.post_review(rdir, rev)
            except (KeyError, ValueError, TypeError) as e:
                self.reply(400, {"error": f"bad request: {e}"})
            except RuntimeError as e:
                self.reply(502, {"error": str(e)})

        def send_feedback(self, rdir: pathlib.Path, rev: dict) -> None:
            """Write the self-review's feedback for the waiting `serve.py open --wait-feedback`, once per round."""
            req = self.body()
            layers = req["layers"]
            if not isinstance(layers, list) or not all(isinstance(x, dict) for x in layers):
                return self.reply(400, {"error": "layers must be a list"})
            with write_lock:
                if (rdir / "feedback.json").is_file():
                    return self.reply(409, {"error": "feedback was already sent for this round; rerun the guide for the next pass"})
                at = int(time.time())
                write(rdir / "feedback.json", {"sent_at": at, "text": str(req.get("text") or ""), "layers": layers})
                write(rdir / "posted.json", (read(rdir / "posted.json") or {}) | {str(x["n"]): f"sent:{at}" for x in layers if "n" in x})
                with home.lock:
                    rev = home.review(rev["id"])
                    rev["updated"] = at
                    home.save_review(rev)
            self.reply(200, {"sent_at": at})

        def post_review(self, rdir: pathlib.Path, rev: dict) -> None:
            req = self.body()
            data = read(rdir / "guide.json")
            prs = {p["n"]: p for st in data["stacks"] for p in st["prs"]}
            pr = prs[int(req["pr"])]
            n = pr["n"]
            with write_lock:
                posted = read(rdir / "posted.json") or {}
                if str(n) in posted:
                    return self.reply(409, {"error": f"a review on #{n} was already posted from this page"})
                if req["event"] not in ("COMMENT", "APPROVE", "REQUEST_CHANGES"):
                    return self.reply(400, {"error": "unknown review type"})
                if req["event"] != "COMMENT" and pr["author"] == data["viewer"]:
                    return self.reply(400, {"error": "GitHub only accepts comment reviews on your own PR"})
                hunks = read(rdir / str(n) / "hunks.json")
                comments = []
                for c in req["comments"]:
                    commentable = set(hunks.get(c["path"], {}).get("commentable", []))
                    span = range(c.get("start_line") or c["line"], c["line"] + 1)
                    if not span or any(i not in commentable for i in span):
                        return self.reply(400, {"error": f"{c['path']}:{c['line']} is outside the diff"})
                    comment = {"path": c["path"], "line": c["line"], "side": "RIGHT", "body": c["body"]}
                    if c.get("start_line"):
                        comment |= {"start_line": c["start_line"], "start_side": "RIGHT"}
                    comments.append(comment)
                head = gh("pr", "view", str(n), "-R", data["repo"], "--json", "headRefOid", "--jq", ".headRefOid").strip()
                if head != pr["sha"]:
                    return self.reply(409, {"error": f"#{n} has new commits ({pr['sha'][:8]} to {head[:8]}); rerun the review guide"})
                # A PR lower in a lagging stack is reviewed at the commit its child contains; comments anchor there.
                payload = {"commit_id": pr["review_sha"], "event": req["event"], "body": req["body"], "comments": comments}
                out = json.loads(gh("api", "-X", "POST", f"repos/{data['repo']}/pulls/{n}/reviews", "--input", "-", stdin=json.dumps(payload)))
                write(rdir / "posted.json", posted | {str(n): out["html_url"]})
                with home.lock:
                    rev = home.review(rev["id"])
                    rev["updated"] = int(time.time())
                    home.save_review(rev)
            self.reply(200, {"url": out["html_url"]})

        def log_message(self, *args) -> None:
            pass

    return Handler


def find_server(home: pathlib.Path, current: bool = False) -> tuple[str, str] | None:
    """The running server's base URL and token, from server.json, when it answers. With `current`, only a server
    running this code."""
    info = read(home / "server.json")
    if not info:
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{info['port']}/api/ping", timeout=2) as r:
            ping = json.load(r)
            if ping.get("pid") == info["pid"] and (not current or ping.get("version") == VERSION):
                return f"http://127.0.0.1:{info['port']}", info["token"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def replace_old_server(home: pathlib.Path) -> None:
    """Stop a server started before the code was updated, so the next one runs the new code on the same port. Rounds
    are detached, so a running one keeps going and the new server picks it up."""
    found = find_server(home)
    if not found or find_server(home, current=True):
        return
    base, token = found
    print("restarting the review-guide server on the updated code", flush=True)
    call(base, token, "POST", "/api/stop", {})
    for _ in range(50):
        if not find_server(home):
            return
        time.sleep(0.1)


def serve(home_path: pathlib.Path, port: int) -> None:
    home_path.mkdir(parents=True, exist_ok=True)
    # Held for the server's life: a second server, even one started at the same moment, exits here.
    lock = (home_path / "server.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        found = find_server(home_path)
        print(f"{found[0]}/" if found else "another review-guide server is starting", flush=True)
        return
    home = Home(home_path)
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler(home))
    except OSError:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler(home))
    info = {"port": server.server_address[1], "pid": os.getpid(), "token": TOKEN}
    fd = os.open(home_path / "server.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(info, f)
    print(f"http://127.0.0.1:{info['port']}/", flush=True)
    try:
        server.serve_forever()
    finally:
        if (read(home_path / "server.json") or {}).get("pid") == os.getpid():
            (home_path / "server.json").unlink(missing_ok=True)


def call(base: str, token: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json", "X-Review-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(json.loads(e.read() or "{}").get("error") or str(e))


def open_review(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(prog="serve.py open")
    ap.add_argument("--home", type=pathlib.Path, default=HOME)
    ap.add_argument("--effort", choices=EFFORTS, default="balanced")
    ap.add_argument("--focus")
    ap.add_argument("--wait", action="store_true", help="follow the round and print its summary")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--self", action="store_true", help="review the local branch stack; feedback goes to the agent")
    ap.add_argument("--branch", help="with --self, the top branch (default: the current one)")
    ap.add_argument("--base", help="with --self, review only the layers above this ref instead of the default branch")
    ap.add_argument("--wait-feedback", action="store_true", help="with --self, wait for the feedback and print it")
    ap.add_argument("prs", nargs="*")
    args = ap.parse_args(argv)
    if args.self:
        root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip()
        if not root:
            sys.exit("--self runs inside the repository being worked on")
        branch = args.branch or subprocess.run(["git", "-C", root, "symbolic-ref", "--quiet", "--short", "HEAD"], capture_output=True, text=True).stdout.strip() or "HEAD"
        refs = [f"self:{root}@{args.base + '..' if args.base else ''}{branch}"]
        if uncollected(args.home, review_id(refs)):
            sys.exit(0)
    elif not args.prs:
        sys.exit("give PR references, or --self for the local branch stack")
    else:
        try:
            refs = norm_refs(args.prs)
        except ValueError as e:
            sys.exit(str(e))
    replace_old_server(args.home)
    found = find_server(args.home)
    if not found:
        args.home.mkdir(parents=True, exist_ok=True)
        with (args.home / "server.log").open("a") as log:
            subprocess.Popen([sys.executable, str(HERE / "serve.py"), "--home", str(args.home)], stdout=log, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        for _ in range(50):
            time.sleep(0.2)
            if found := find_server(args.home):
                break
        else:
            sys.exit(f"the review-guide server did not start; see {args.home / 'server.log'}")
    base, token = found
    out = call(base, token, "POST", "/api/reviews", {"refs": refs, "effort": args.effort, "focus": args.focus, "cwd": os.getcwd()})
    print(f"review {base}{out['url']}", flush=True)
    print(f"inbox {base}/", flush=True)
    print(f"round {out['round']} {'started' if out['started'] else 'already running; following it'}", flush=True)
    if not args.no_browser:
        webbrowser.open(base + out["url"])
    rdir = args.home / "reviews" / out["id"] / "rounds" / str(out["round"])
    if args.wait or args.wait_feedback:
        code = follow(rdir)
        if code or not args.wait_feedback:
            sys.exit(code)
        print(f"waiting for feedback from {base}{out['url']}", flush=True)
        sys.exit(wait_feedback(args.home, out["id"], rdir))


def feedback_text(rdir: pathlib.Path, fb: dict) -> str:
    rev_round = rdir.name
    sent = time.strftime("%H:%M", time.localtime(fb["sent_at"]))
    out = [f"review-guide feedback, round {rev_round}, sent at {sent}", ""]
    if fb.get("text"):
        out += [fb["text"].strip(), ""]
    for layer in fb["layers"]:
        out.append(f"## {layer.get('label') or layer.get('n')}" + (f": {layer['verdict']}" if layer.get("verdict") else ""))
        if layer.get("summary"):
            out.append(f"Summary: {layer['summary'].strip()}")
        for c in layer.get("comments") or []:
            where = f"{c['path']}:{c['line']}" if c.get("path") else "general"
            if c.get("start_line"):
                where = f"{c['path']}:{c['start_line']}-{c['line']}"
            out.append(f"- {where} ({c.get('severity') or 'comment'}): {c['body'].strip()}")
            if c.get("code"):
                out.append(f"  at: `{c['code'].strip()[:160]}`")
        out.append("")
    if not any(layer.get("comments") or layer.get("summary") for layer in fb["layers"]):
        out.append("No comment: the reviewer sent their verdicts only.")
    return "\n".join(out).rstrip() + "\n"


def uncollected(home: pathlib.Path, rid: str) -> str | None:
    """Feedback sent after the waiting command stopped (its session ended): printed by the next --self run instead
    of starting a round, then marked collected."""
    for rdir in sorted((home / "reviews" / rid / "rounds").glob("*"), key=lambda d: int(d.name) if d.name.isdigit() else 0, reverse=True):
        fb = read(rdir / "feedback.json")
        if fb and not fb.get("collected"):
            text = "Feedback from an earlier self-review round was never delivered:\n\n" + feedback_text(rdir, fb)
            print(text, flush=True)
            # Marked only once printed, so a failed print leaves it for the next run.
            write(rdir / "feedback.json", fb | {"collected": int(time.time())})
            return text
    return None


def wait_feedback(home: pathlib.Path, rid: str, rdir: pathlib.Path) -> int:
    """Block until the page sends feedback, or the review is finished without it. A Rerun from the page moves the wait
    to the newer round."""
    while True:
        rev = read(home / "reviews" / rid / "review.json") or {}
        if rev.get("rounds") and rev["rounds"][-1]["n"] > int(rdir.name):
            rdir = rdir.parent / str(rev["rounds"][-1]["n"])
        fb = read(rdir / "feedback.json")
        if fb:
            print(feedback_text(rdir, fb), flush=True)
            write(rdir / "feedback.json", fb | {"collected": int(time.time())})
            return 0
        if rev.get("status") == "done":
            print("The review was finished without sending feedback.", flush=True)
            return 0
        time.sleep(2)


def follow(rdir: pathlib.Path) -> int:
    """Print the runner's status and done lines as they come, then the warnings and findings, for the skill's report."""
    seen, started = 0, time.time()
    while True:
        lines = (rdir / "runner.log").read_text(errors="replace").splitlines() if (rdir / "runner.log").is_file() else []
        for line in lines[seen:]:
            if line.split(" ", 1)[0] in ("status", "done"):
                print(line, flush=True)
        seen = len(lines)
        p = read(rdir / "progress.json") or {}
        if p.get("finished") or (time.time() - started > 10 and not runner_alive(p.get("pid"))):
            break
        time.sleep(2)
    for w in (read(rdir / "run.json") or {}).get("warnings", []):
        print(f"warning: {w}")
    for st in (read(rdir / "guide.json") or {"stacks": []})["stacks"]:
        for c in st["comments"]:
            print(f"finding #{c['pr']} {c['severity']} {c['axis']} {c['status']}: {c['title']}")
    if err := p.get("error") or ("" if p.get("finished") else "the runner stopped before the end"):
        print(f"error: {err}; see {rdir / 'runner.log'} and {rdir / 'agents.log'}")
        return 1
    return 0


def main() -> None:
    if sys.argv[1:2] == ["open"]:
        return open_review(sys.argv[2:])
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", type=pathlib.Path, default=HOME)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()
    serve(args.home, args.port)


if __name__ == "__main__":
    main()
