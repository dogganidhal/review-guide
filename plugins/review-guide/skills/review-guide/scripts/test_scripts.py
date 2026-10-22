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
from fetch import Git, attribute, changed_files, changed_since, commit_owners, last_seen, leftovers, parse_diff, review_shas  # noqa: E402


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
        # The viewer last reviewed #1 at pr1: what #2 added since is new, and a stack they never reviewed has no `since`.
        seen, problem = last_seen(git, [1, 2], {1: {"state": "APPROVED", "sha": r["pr1"], "at": "2026-10-01T00:00:00Z"}, 2: None})
        assert seen["pr"] == 1 and problem is None, (seen, problem)
        assert changed_since(git, r["pr1"], r["pr2"], {"A.java", "B.java"}) == {"A.java": [3, 4], "B.java": [1]}
        assert last_seen(git, [1, 2], {1: None, 2: None}) == (None, None)

        # A removal: a metric family nothing in the repo reads any more, a fixture key, and a two-hop chain
        # (Api.onDuty forwards to Store.onDuty) whose only caller the stack removed.
        rm = root / "rm"
        (rm / "mappings").mkdir(parents=True)
        sh(rm, "git", "init", "-q", "-b", "main")
        (rm / "Job.java").write_text('class Job {\n  void run() {\n    api.onDuty();\n    counter("odc.review.sms.sent");\n'
                                     '    counter("odc.review.sms.failed");\n  }\n}\n')
        (rm / "Api.java").write_text("class Api {\n  public List<String> onDuty() {\n    return store.onDuty();\n  }\n}\n")
        (rm / "Store.java").write_text("interface Store {\n  List<String> onDuty();\n}\n")
        (rm / "mappings" / "sms.json").write_text('{"external_id": "x", "status": "sent"}\n')
        sh(rm, "git", "add", ".")
        sh(rm, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
        (rm / "Job.java").write_text("class Job {\n  void run() {\n  }\n}\n")
        (rm / "mappings" / "sms.json").write_text("{}\n")
        sh(rm, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "remove")
        rg = Git(str(rm), via_gh=False)
        left = "\n".join(leftovers(rg, "HEAD", parse_diff(rg("diff", "HEAD~1", "HEAD"))))
        assert "odc.review.sms.* (2 names)" in left and "no use left here" in left, left
        assert "external_id" not in left, left
        assert "onDuty has no caller left" in left and "Api.java:3" in left and "Store.java:2" in left, left

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
               "stacks": [{"id": sid, "prs": [1, 2], "base": r["base"], "tip": r["pr2"], "files": files,
                           "since": {"pr": 1, "state": "APPROVED", "sha": r["pr1"], "at": "2026-10-01T00:00:00Z", "files": {"A.java": [3, 4], "B.java": [1]}}}]}
        (w / "run.json").write_text(json.dumps(run))

        guide = {
            "summary": "Adds fields to A and a class B.",
            "key_facts": [{"title": "Nothing reads `x` yet.", "text": "It is written only."}],
            "components": {"layers": [{"id": "core", "label": "core"}],
                           "nodes": [{"id": "a", "label": "A", "layer": "core", "state": "changed", "path": "A.java"},
                                     {"id": "b", "label": "B", "layer": "core", "state": "new", "path": "B.java"}],
                           "edges": [{"from": "a", "to": "b", "label": "uses", "new": True}]},
            "sections": [
                {"type": "flow", "title": "Use B", "steps": [{"from": "a", "to": "b", "label": "calls"}]},
                {"type": "before_after", "title": "What changes", "rows": [{"aspect": "`y`", "before": "int", "after": "long"}]},
                {"type": "table", "title": "Per PR", "columns": ["PR", "Change"], "rows": [["#1", "Fields"], ["#2", "Class B"]]},
                {"type": "steps", "title": "Read in this order", "steps": [
                    {"id": "fields", "kind": "read", "title": "Fields", "why": "New state on A.", "nodes": ["a"], "test": None,
                     "anchor": {"path": "A.java", "start": 1, "end": 5}}]},
            ],
            "files": {"A.java": {"role": "read", "why": "Fields."}, "B.java": {"role": "skim", "why": "Empty class."}},
        }
        correctness = {"skipped": None, "findings": [
            {"id": "x", "severity": "question", "title": "Who reads x?", "path": "A.java", "line": 2, "body": "Who reads `x`?", "evidence": ["A.java:2"]},
            {"id": "f", "severity": "nit", "title": "Empty f", "path": "A.java", "line": 4, "body": "`f` is empty.", "evidence": ["A.java:4"]},
            {"id": "d", "severity": "nit", "title": "Same as x", "path": "A.java", "line": 2, "body": "Who reads `x`?", "evidence": ["A.java:2"]}]}
        check = lambda *a: subprocess.run([sys.executable, str(HERE / "build.py"), str(w), "--check", *a], capture_output=True, text=True)  # noqa: E731

        bad = json.loads(json.dumps(guide))
        bad["summary"] = "x" * 400
        bad["sections"][1] = {"type": "table", "title": "Per PR", "columns": ["PR", "Change"], "rows": [["#1"]]}
        bad["sections"].append({"type": "poem", "title": "x"})
        (w / sid / "guide.json").write_text(json.dumps(bad))
        bad_review = json.loads(json.dumps(correctness))
        bad_review["findings"][0]["line"] = 1
        bad_review["findings"] += [{**bad_review["findings"][1], "id": f"n{i}"} for i in range(3)]
        (w / sid / "review-correctness.json").write_text(json.dumps(bad_review))
        out = check()
        assert out.returncode == 1, out.stdout
        for expected in ("line 1 of A.java was not written by this stack", "#2 receives 4 findings", "summary is 400 characters",
                         "rows[0]: expected 2 cells", "sections[4].type must be one of"):
            assert expected in out.stdout, (expected, out.stdout)
        # A reviewer checks its own file, whatever state the guide is in.
        assert "summary" not in check("--only", "correctness").stdout

        (w / sid / "guide.json").write_text(json.dumps(guide))
        (w / sid / "review-correctness.json").write_text(json.dumps(correctness))
        assert check("--only", "guide").stdout.strip() == "ok"
        out = subprocess.run([sys.executable, str(HERE / "build.py"), str(w), "--partial"], capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr
        data = json.loads((w / "guide.json").read_text())
        assert data["stacks"][0]["reviewing"] == ["standards", "spec"], data["stacks"][0]["reviewing"]
        assert {c["status"] for c in data["stacks"][0]["comments"]} == {"checking"}

        (w / sid / "review-standards.json").write_text(json.dumps({"skipped": None, "findings": []}))
        (w / sid / "review-spec.json").write_text(json.dumps({"skipped": "No ticket and an empty description.", "findings": []}))
        (w / sid / "verify.json").write_text(json.dumps({"comments": {"correctness/x": {"status": "verified", "note": "No reader."},
                                                                      "correctness/d": {"status": "duplicate", "note": "Same as correctness/x."}}}))
        out = subprocess.run([sys.executable, str(HERE / "build.py"), str(w)], capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr
        data = json.loads((w / "guide.json").read_text())
        st0 = data["stacks"][0]
        cms = {c["id"].rsplit("-", 1)[-1]: c for c in st0["comments"]}
        assert set(cms) == {"x", "f"}, cms
        assert (cms["x"]["pr"], cms["x"]["pl"], cms["x"]["status"], cms["x"]["axis"]) == (1, 2, "verified", "correctness"), cms["x"]
        assert (cms["f"]["pr"], cms["f"]["pl"], cms["f"]["status"]) == (2, 4, "unverified"), cms["f"]
        assert st0["reviewing"] == [] and st0["skipped"] == {"spec": "No ticket and an empty description."}
        assert [x["type"] for x in st0["sections"]] == ["flow", "before_after", "table", "steps"]
        assert st0["sections"][3]["steps"][0]["lines"][1]["pr"] == 1
        assert data["stacks"][0]["since"]["files"] == {"A.java": [3, 4], "B.java": [1]}
        assert data["effort"] == "balanced"
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
            rerun = urllib.request.Request(url + "api/rerun", json.dumps({"effort": "max"}).encode(), {"X-Review-Token": "wrong"})
            for tok, code in (("wrong", 403), (token, 400)):
                rerun.headers["X-review-token"] = tok
                try:
                    urllib.request.urlopen(rerun)
                except urllib.error.HTTPError as e:
                    assert e.code == code, (tok, e.code)
            close = urllib.request.Request(url + "api/close", b"", {"X-Review-Token": token})
            assert urllib.request.urlopen(close).status == 200
            assert server.wait(timeout=5) == 0
        finally:
            server.terminate()
        if keep:
            shutil.copytree(w, keep, dirs_exist_ok=True)
    print("ok" + (f", fixture in {keep}" if keep else ""))


if __name__ == "__main__":
    main(sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--keep" else None)
