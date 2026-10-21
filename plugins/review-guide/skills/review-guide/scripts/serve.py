#!/usr/bin/env python3
"""Serve a built review page on localhost and post reviews through the user's own `gh` login.

Usage: serve.py WORKDIR [--port N]

Binds 127.0.0.1 only. Every POST must carry the per-process token embedded in the served page, so another
site open in the browser cannot post reviews in the user's name. Before posting, the PR head is compared
with the SHA the guide was built on, and each inline comment line is checked against the diff.
"""

import argparse
import http.server
import json
import pathlib
import secrets
import subprocess
import threading

TOKEN = secrets.token_urlsafe(24)


def gh(*args: str, stdin: str | None = None) -> str:
    r = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout


def handler(workdir: pathlib.Path):
    def load() -> tuple[dict, dict]:
        data = json.loads((workdir / "guide.json").read_text())
        return data, {p["n"]: p for st in data["stacks"] for p in st["prs"]}
    posted_path = workdir / "posted.json"
    lock = threading.Lock()

    def posted() -> dict:
        return json.loads(posted_path.read_text()) if posted_path.is_file() else {}

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
                page = (workdir / "index.html").read_text().replace("/*TOKEN*/", TOKEN)
                self.reply(200, page.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self.reply(200, {"posted": posted(), "version": (workdir / "index.html").stat().st_mtime_ns})
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/api/review":
                return self.reply(404, {"error": "not found"})
            if not secrets.compare_digest(self.headers.get("X-Review-Token", ""), TOKEN):
                return self.reply(403, {"error": "missing or wrong token; reload the page"})
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
                    hunks = json.loads((workdir / str(n) / "hunks.json").read_text())
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
                    posted_path.write_text(json.dumps(done))
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
