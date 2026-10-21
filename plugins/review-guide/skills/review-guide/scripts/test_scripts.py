#!/usr/bin/env python3
"""Offline check: a two-PR stack in a scratch git repo, where the top PR edits a file the bottom PR added.

Covers line attribution across the stack, routing of tip lines to the right PR, build.py validation and
rendering, and serve.py's token and line guards. Run: python3 -I test_scripts.py

With --keep DIR, the built work directory is copied to DIR, so the page can be opened with
`serve.py DIR` to work on the UI without running the agents.
"""

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fetch import Git, attribute, changed_files, commit_owners, parse_diff, review_shas  # noqa: E402


def sh(cwd: pathlib.Path, *args: str) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root: pathlib.Path) -> dict[str, str]:
    repo = root / "repo"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    sh(repo, "git", "config", "user.email", "t@t")
    sh(repo, "git", "config", "user.name", "t")
    (repo / "A.java").write_text("class A {\n}\n")
    sh(repo, "git", "add", ".")
    sh(repo, "git", "commit", "-qm", "base")
    base = sh(repo, "git", "rev-parse", "HEAD")
    (repo / "A.java").write_text("class A {\n  int x;\n  int y;\n}\n")
    sh(repo, "git", "commit", "-qam", "pr1")
    pr1 = sh(repo, "git", "rev-parse", "HEAD")
    (repo / "A.java").write_text("class A {\n  int x;\n  long y;\n  void f() {}\n}\n")
    (repo / "B.java").write_text("class B {}\n")
    sh(repo, "git", "add", ".")
    sh(repo, "git", "commit", "-qm", "pr2")
    pr2 = sh(repo, "git", "rev-parse", "HEAD")
    return {"repo": str(repo), "base": base, "pr1": pr1, "pr2": pr2}


def main(keep: str | None = None) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        r = make_repo(root)
        git = Git(r["repo"], via_gh=False)
        heads = {1: r["pr1"], 2: r["pr2"]}
        review, warnings = review_shas(git, [1, 2], heads)
        assert review == heads and not warnings, (review, warnings)

        owners = commit_owners(git, r["base"], [1, 2], review)
        attr = attribute(git, r["base"], [1, 2], review, owners, "A.java")
        # Tip: 1 class A {  2 int x; (pr1, line 2)  3 long y; (pr2)  4 void f() (pr2)  5 }
        assert attr == {"2": [1, 2], "3": [2, 3], "4": [2, 4]}, attr
        assert {f["path"] for f in changed_files(git, r["base"], r["pr2"])} == {"A.java", "B.java"}

        w = root / "w"
        sid = "1-2"
        (w / sid / "tip").mkdir(parents=True)
        for path in ("A.java", "B.java"):
            (w / sid / "tip" / path).write_text(sh(pathlib.Path(r["repo"]), "git", "show", f"{r['pr2']}:{path}") + "\n")
        (w / sid / "target.diff").write_text(git("diff", "--no-color", r["base"], r["pr2"]))
        (w / sid / "attribution.json").write_text(json.dumps({"A.java": attr, "B.java": attribute(git, r["base"], [1, 2], review, owners, "B.java")}))
        for n, (lo, hi) in {1: (r["base"], r["pr1"]), 2: (r["pr1"], r["pr2"])}.items():
            (w / str(n)).mkdir()
            parsed = parse_diff(git("diff", "--no-color", lo, hi))
            (w / str(n) / "hunks.json").write_text(json.dumps({p: {"added": f["added"], "commentable": f["commentable"]} for p, f in parsed.items()}))
        files = [{"path": f["path"], "old": f["old"], "status": f["status"], "adds": 1, "dels": 0, "prs": [1, 2] if f["path"] == "A.java" else [2]}
                 for f in changed_files(git, r["base"], r["pr2"])]
        pr = lambda n, author: {"n": n, "url": f"https://github.com/o/r/pull/{n}", "title": f"PR {n}", "body": "", "author": author,  # noqa: E731
                                "sha": heads[n], "review_sha": heads[n], "draft": False, "stack": sid, "checks": {"failed": [], "pending": 0, "passed": 1}}
        run = {"repo": "o/r", "viewer": "me", "run_id": "t", "checkout": None, "warnings": [], "prs": [pr(1, "me"), pr(2, "other")],
               "stacks": [{"id": sid, "prs": [1, 2], "base": r["base"], "tip": r["pr2"], "files": files}]}
        (w / "run.json").write_text(json.dumps(run))

        guide = {
            "summary": "Adds fields to A and a class B.",
            "prs": {"1": {"verdict": {"event": "COMMENT", "why": "One question."}, "review_body": "See inline."},
                    "2": {"verdict": {"event": "COMMENT", "why": "One question."}, "review_body": "See inline."}},
            "map": {"layers": [{"id": "core", "label": "core"}],
                    "nodes": [{"id": "a", "label": "A", "layer": "core", "state": "changed", "path": "A.java"},
                              {"id": "b", "label": "B", "layer": "core", "state": "new", "path": "B.java"}],
                    "edges": [{"from": "a", "to": "b", "label": "uses", "new": True}]},
            "flows": [{"title": "Use B", "steps": [{"from": "a", "to": "b", "label": "calls"}]}],
            "files": {"A.java": {"role": "read", "why": "Fields."}, "B.java": {"role": "skim", "why": "Empty class."}},
            "steps": [{"id": "fields", "kind": "read", "title": "Fields", "why": "New state on A.", "nodes": ["a"],
                       "anchor": {"path": "A.java", "start": 1, "end": 5},
                       "comments": [
                           {"id": "x", "severity": "question", "title": "Who reads x?", "path": "A.java", "line": 2, "body": "Who reads `x`?", "evidence": ["A.java:2"]},
                           {"id": "f", "severity": "nit", "title": "Empty f", "path": "A.java", "line": 4, "body": "`f` is empty.", "evidence": ["A.java:4"]},
                       ]}],
        }
        bad = json.loads(json.dumps(guide))
        bad["steps"][0]["comments"][0]["line"] = 1
        bad["prs"]["1"]["verdict"]["event"] = "APPROVE"
        bad["summary"] = "x" * 400
        (w / sid / "guide.json").write_text(json.dumps(bad))
        out = subprocess.run([sys.executable, str(HERE / "build.py"), str(w), "--check"], capture_output=True, text=True)
        assert out.returncode == 1, out.stdout
        for expected in ("line 1 of A.java was not written by this stack", "must be COMMENT", "summary is 400 characters"):
            assert expected in out.stdout, (expected, out.stdout)

        (w / sid / "guide.json").write_text(json.dumps(guide))
        (w / sid / "verify.json").write_text(json.dumps({"comments": {"x": {"status": "verified", "note": "No reader."}}}))
        out = subprocess.run([sys.executable, str(HERE / "build.py"), str(w)], capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr
        data = json.loads((w / "guide.json").read_text())
        cms = {c["id"].rsplit("-", 1)[-1]: c for c in data["stacks"][0]["comments"]}
        assert (cms["x"]["pr"], cms["x"]["pl"], cms["x"]["status"]) == (1, 2, "verified"), cms["x"]
        assert (cms["f"]["pr"], cms["f"]["pl"], cms["f"]["status"]) == (2, 4, "unverified"), cms["f"]
        page = (w / "index.html").read_text()
        assert "/*DATA*/" not in page and "/*TOKEN*/" in page

        server = subprocess.Popen([sys.executable, str(HERE / "serve.py"), str(w)], stdout=subprocess.PIPE, text=True)
        try:
            url = server.stdout.readline().strip()
            served = urllib.request.urlopen(url).read().decode()
            token = served.split("const TOKEN = '")[1].split("'")[0]
            assert len(token) > 20

            def post(body: dict, tok: str) -> int:
                req = urllib.request.Request(url + "api/review", json.dumps(body).encode(), {"Content-Type": "application/json", "X-Review-Token": tok})
                try:
                    return urllib.request.urlopen(req).status
                except urllib.error.HTTPError as e:
                    return e.code

            outside = {"pr": 2, "event": "COMMENT", "body": "x", "comments": [{"path": "A.java", "line": 99, "body": "y"}]}
            assert post(outside, "wrong") == 403
            assert post(outside, token) == 400
            assert post({**outside, "pr": 1, "event": "APPROVE", "comments": []}, token) == 400
        finally:
            server.terminate()
        if keep:
            shutil.copytree(w, keep, dirs_exist_ok=True)
    print("ok" + (f", fixture in {keep}" if keep else ""))


if __name__ == "__main__":
    main(sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--keep" else None)
