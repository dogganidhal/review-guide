#!/usr/bin/env python3
"""Dev tool: the server behind dev/compare.html. Shows each replayed stack's old and new guide side by side and saves votes.

Usage: dev/compare.py [--home DIR] [--port N]       (home: the replay home, default ~/.cache/review-guide-replay)

Serves 127.0.0.1 only. The guides are the real pages of the two rounds (index.html with BOOT injected, read-only: no saving,
no posting, no polling). Votes go to HOME/compare.json as {"<review id>/<stack id>": {"vote": "old|new|same", "note": "", "at": N}}.
"""

import argparse
import http.server
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
ID = r"[\w-][\w.-]*"
VOTES = ("old", "new", "same")


def read(path: pathlib.Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write(path: pathlib.Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def stacks(home: pathlib.Path) -> list[dict]:
    """One row per replayed stack, with the numbers of replay.json."""
    replay = read(home / "replay.json") or {"reviews": {}, "stacks": {}}
    out = []
    for key, row in sorted(replay["stacks"].items()):
        rid, sid = key.split("/", 1)
        meta = replay["reviews"].get(rid) or {}
        rev = read(home / "reviews" / rid / "review.json") or {}
        out.append({"key": key, "rid": rid, "sid": sid, "old": meta.get("old"), "new": meta.get("new"), "ok": bool(row.get("ok")),
                    "refs": rev.get("refs") or [], "row": row})
    return out


def handler(home: pathlib.Path):
    class Handler(http.server.BaseHTTPRequestHandler):
        def reply(self, code: int, payload, ctype: str = "application/json") -> None:
            body = payload if isinstance(payload, bytes) else payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            url = urllib.parse.urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self.reply(200, (HERE / "compare.html").read_text(), "text/html; charset=utf-8")
            if url.path == "/api/data":
                return self.reply(200, {"stacks": stacks(home), "votes": read(home / "compare.json") or {}})
            if m := re.fullmatch(rf"/page/({ID})/(\d+)", url.path):
                rid, n = m.group(1), int(m.group(2))
                f = home / "reviews" / rid / "rounds" / str(n) / "index.html"
                if not f.is_file():
                    return self.reply(404, {"error": f"round {n} of {rid} has no page"})
                sid = (urllib.parse.parse_qs(url.query).get("stack") or [None])[0]
                boot = {"base": "/api/none", "review": rid, "round": n, "live": False, "status": "open", "rounds": [],
                        "ui": {"stack": sid, "view": "guide"} if sid else {"view": "guide"}}
                html = f.read_text().replace("/*TOKEN*/", "", 1).replace("/*BOOT*/null", json.dumps(boot).replace("<", "\\u003c"), 1)
                # Read-only pages: hide the "earlier round" banner, which would show on both sides.
                html = html.replace("</head>", "<style>.robox{display:none}</style></head>", 1)
                return self.reply(200, html, "text/html; charset=utf-8")
            self.reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            # A custom header forces a CORS preflight, which this server never answers: other sites cannot vote.
            if self.path != "/api/vote" or self.headers.get("X-Compare") != "1":
                return self.reply(403, {"error": "forbidden"})
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
                key, vote, note = req["key"], req.get("vote"), str(req.get("note") or "")[:2000]
                if key not in {s["key"] for s in stacks(home)} or vote not in (*VOTES, None):
                    raise ValueError("unknown stack or vote")
            except (KeyError, ValueError, TypeError) as e:
                return self.reply(400, {"error": str(e)})
            votes = read(home / "compare.json") or {}
            votes[key] = {"vote": vote, "note": note, "at": int(time.time())}
            write(home / "compare.json", votes)
            self.reply(200, {"ok": True})

        def log_message(self, *args) -> None:
            pass

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", type=pathlib.Path, default=pathlib.Path.home() / ".cache" / "review-guide-replay")
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()
    home = args.home.expanduser().resolve()
    if not (home / "replay.json").is_file():
        sys.exit(f"{home / 'replay.json'} not found; run dev/replay.py first")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler(home))
    print(f"compare http://127.0.0.1:{server.server_address[1]}/ (home {home})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
