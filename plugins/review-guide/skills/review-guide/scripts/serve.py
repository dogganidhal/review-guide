#!/usr/bin/env python3
"""Serve a built review page on localhost and post reviews through the user's own `gh` login.

Usage: serve.py WORKDIR [--port N]

Binds 127.0.0.1 only. Every POST must carry the per-process token embedded in the served page, so another
site open in the browser cannot post reviews in the user's name. Before posting, the PR head is compared
with the SHA the guide was built on, and each inline comment line is checked against the diff.

The page's Close button stops the server (POST /api/close, same token). The page can also rerun the guide (POST /api/rerun, same token) through rerun.py. When the PRs have new commits the
rerun lands in a new work directory, and the server switches to it, so the open page reloads onto the new guide.
"""

import argparse
import http.server
import json
import pathlib
import secrets
import subprocess
import sys
import threading
import time

TOKEN = secrets.token_urlsafe(24)


def gh(*args: str, stdin: str | None = None) -> str:
    r = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout


def handler(workdir: pathlib.Path):
    cur = {"w": workdir}  # the work directory being served; a rerun on new commits moves it
    rerun = {"running": False, "status": "", "error": ""}
    heads_cache = {"at": 0.0, "w": None, "heads": {}}

    def load() -> tuple[dict, dict]:
        data = json.loads((cur["w"] / "guide.json").read_text())
        return data, {p["n"]: p for st in data["stacks"] for p in st["prs"]}
    lock = threading.Lock()

    def posted() -> dict:
        path = cur["w"] / "posted.json"
        return json.loads(path.read_text()) if path.is_file() else {}

    def heads() -> dict:
        """The PRs' current head SHAs on GitHub, in one GraphQL call, cached for a minute."""
        if heads_cache["w"] == cur["w"] and time.time() - heads_cache["at"] < 60:
            return heads_cache["heads"]
        data, prs = load()
        owner, name = data["repo"].split("/")
        fields = " ".join(f"p{n}: pullRequest(number: {n}) {{ headRefOid }}" for n in prs)
        out = json.loads(gh("api", "graphql", "-f", f"query=query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {fields} }} }}"))
        heads_cache.update(at=time.time(), w=cur["w"], heads={k[1:]: v["headRefOid"] for k, v in out["data"]["repository"].items() if v})
        return heads_cache["heads"]

    def start_rerun(effort: str) -> None:
        proc = subprocess.Popen([sys.executable, str(pathlib.Path(__file__).with_name("rerun.py")), str(cur["w"]), "--effort", effort],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def follow() -> None:
            for line in proc.stdout:
                kind, _, text = line.strip().partition(" ")
                if kind == "workdir":
                    cur["w"] = pathlib.Path(text)
                elif kind == "status":
                    rerun["status"] = text
            err = proc.stderr.read().strip()
            rerun.update(running=False, error=err.splitlines()[-1] if proc.wait() and err else "")
        threading.Thread(target=follow, daemon=True).start()

    class Handler(http.server.BaseHTTPRequestHandler):
        def reply(self, code: int, payload, ctype: str = "application/json") -> None:
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                page = (cur["w"] / "index.html").read_text().replace("/*TOKEN*/", TOKEN)
                self.reply(200, page.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self.reply(200, {"posted": posted(), "version": (cur["w"] / "index.html").stat().st_mtime_ns, "rerun": rerun})
            elif self.path == "/api/heads":
                try:
                    self.reply(200, {"heads": heads()})
                except (RuntimeError, KeyError, ValueError) as e:
                    self.reply(502, {"error": str(e)})
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path not in ("/api/review", "/api/rerun", "/api/close"):
                return self.reply(404, {"error": "not found"})
            if not secrets.compare_digest(self.headers.get("X-Review-Token", ""), TOKEN):
                return self.reply(403, {"error": "missing or wrong token; reload the page"})
            if self.path == "/api/close":
                self.reply(200, {"closed": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path == "/api/rerun":
                effort = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}").get("effort")
                with lock:
                    if rerun["running"]:
                        return self.reply(409, {"error": "a rerun is already running"})
                    if effort not in ("quick", "balanced", "deep"):
                        return self.reply(400, {"error": "effort must be quick, balanced or deep"})
                    rerun.update(running=True, status="Starting", error="")
                    start_rerun(effort)
                return self.reply(200, rerun)
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                data, prs = load()
                pr = prs[int(req["pr"])]
                n = pr["n"]
                with lock:
                    if str(n) in posted():
                        return self.reply(409, {"error": f"a review on #{n} was already posted from this page"})
                    if req["event"] not in ("COMMENT", "APPROVE", "REQUEST_CHANGES"):
                        return self.reply(400, {"error": "unknown review type"})
                    if req["event"] != "COMMENT" and pr["author"] == data["viewer"]:
                        return self.reply(400, {"error": "GitHub only accepts comment reviews on your own PR"})
                    hunks = json.loads((cur["w"] / str(n) / "hunks.json").read_text())
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
                    done = posted() | {str(n): out["html_url"]}
                    (cur["w"] / "posted.json").write_text(json.dumps(done))
                self.reply(200, {"url": out["html_url"]})
            except (KeyError, ValueError, TypeError) as e:
                self.reply(400, {"error": f"bad request: {e}"})
            except RuntimeError as e:
                self.reply(502, {"error": str(e)})

        def log_message(self, *args) -> None:
            pass

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir", type=pathlib.Path)
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler(args.workdir))
    print(f"http://127.0.0.1:{server.server_address[1]}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
