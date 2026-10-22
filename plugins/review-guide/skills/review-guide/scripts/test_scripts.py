#!/usr/bin/env python3
"""Offline check: a two-PR stack in a scratch git repo, where the top PR edits a file the bottom PR added.

Covers line attribution across the stack, routing of tip lines to the right PR, build.py validation and
rendering, and serve.py: the inbox, rounds, saved page state across a restart, carry-over, and the token and line
guards. Run: python3 -I test_scripts.py

With --keep DIR, a server home holding the fixture as a review with two rounds is copied to DIR, so the page can be
opened with `serve.py --home DIR` to work on the UI without running the agents.
"""

import contextlib
import io
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
from fetch import UNCOMMITTED, Git, attribute, changed_files, changed_since, commit_owners, github_repo, last_seen, leftovers, local_layers, local_prs, parse_diff, previous_round, review_shas  # noqa: E402


def sh(cwd: pathlib.Path, *args: str) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root: pathlib.Path) -> dict[str, str]:
    repo = root / "repo"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    sh(repo, "git", "config", "user.email", "t@t")
    sh(repo, "git", "config", "user.name", "t")
    (repo / "A.java").write_text("class A {\n}\n")
    (repo / "C.java").write_text("class C {\n  void g() {}\n}\n")  # outside the stack: a diagram may cite it
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


def check_local(root: pathlib.Path) -> None:
    """A self-review's layers: feat-a and feat-b stacked on main, plus uncommitted changes, without touching the index."""
    repo = root / "local"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    sh(repo, "git", "config", "user.email", "t@t")
    sh(repo, "git", "config", "user.name", "t")
    (repo / "A.java").write_text("class A {\n}\n")
    sh(repo, "git", "add", ".")
    sh(repo, "git", "commit", "-qm", "base")
    for branch, body in (("feat-a", "  int x;\n"), ("feat-b", "  int x;\n  int y;\n")):
        sh(repo, "git", "checkout", "-qb", branch)
        (repo / "A.java").write_text(f"class A {{\n{body}}}\n")
        sh(repo, "git", "commit", "-qam", f"work on {branch}")
    (repo / "B.java").write_text("class B {}\n")
    (repo / "A.java").write_text("class A {\n  int x;\n  long y;\n}\n")
    sh(repo, "git", "add", "A.java")
    before = sh(repo, "git", "status", "--porcelain")
    git = Git(str(repo), via_gh=False)
    base, layers, warnings = local_layers(git, str(repo), "feat-b")
    assert [x["branch"] for x in layers] == ["feat-a", "feat-b", UNCOMMITTED] and not warnings, layers
    assert sh(repo, "git", "status", "--porcelain") == before, "the index or working tree changed"
    # The same working tree gives the same snapshot, so an untouched tree is not a new version.
    assert local_layers(git, str(repo), "feat-b")[1][-1]["sha"] == layers[-1]["sha"]
    assert github_repo(str(repo)) == "local/local"
    prs = local_prs(git, str(repo), "local/local", base, layers, "me")
    assert [p["label"] for p in prs.values()] == ["feat-a", "feat-b", "Uncommitted changes"] and prs[1]["title"] == "work on feat-a", prs
    assert prs[3]["baseRefOid"] == layers[1]["sha"] and not prs[1]["pushed"] and prs[1]["url"] == ""
    owners = commit_owners(git, base, [1, 2, 3], {n: p["headRefOid"] for n, p in prs.items()})
    attr = attribute(git, base, [1, 2, 3], {n: p["headRefOid"] for n, p in prs.items()}, owners, "A.java")
    assert attr == {"2": [1, 2], "3": [3, 3]}, attr
    # Above feat-a only: feat-a is the base, not a layer.
    assert [x["branch"] for x in local_layers(git, str(repo), "feat-b", "feat-a")[1]] == ["feat-b", UNCOMMITTED]
    # A clean working tree: no uncommitted layer. (A scratch repo, so its stash is its own.)
    sh(repo, "git", "stash", "push", "-q", "-u", "-m", "rg-test")
    assert [x["branch"] for x in local_layers(git, str(repo), "feat-b")[1]] == ["feat-a", "feat-b"]


def check_self_server(w: pathlib.Path, home: pathlib.Path) -> None:
    """A self-review round: never posts to GitHub, sends its feedback once, and the waiting command prints it."""
    from serve import feedback_text, review_id, uncollected, wait_feedback  # noqa: E402
    refs = ["self:/repo@feat-b"]
    rid = review_id(refs)
    assert rid.startswith("self-repo-feat-b-") and rid == review_id(["self:/repo@feat-b"]) != review_id(["self:/repo@feat-c"])
    rdir = home / "reviews" / rid / "rounds" / "1"
    shutil.copytree(w, rdir)
    (rdir / "progress.json").unlink(missing_ok=True)
    data = json.loads((rdir / "guide.json").read_text())
    data |= {"mode": "self", "source": {"root": "/repo", "branch": "feat-b"}}
    (rdir / "guide.json").write_text(json.dumps(data))
    (home / "reviews" / rid / "review.json").write_text(json.dumps({"id": rid, "refs": refs, "created": 1, "status": "open",
                                                                    "rounds": [{"n": 1, "effort": "quick", "created": 1}]}))
    proc = subprocess.Popen([sys.executable, str(HERE / "serve.py"), "--home", str(home), "--port", "0"], stdout=subprocess.PIPE, text=True)
    url = proc.stdout.readline().strip()
    token = json.loads((home / "server.json").read_text())["token"]

    def post(path, body):
        r = urllib.request.Request(url + path, json.dumps(body).encode(), {"Content-Type": "application/json", "X-Review-Token": token})
        try:
            return urllib.request.urlopen(r).status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        base = f"api/r/{rid}/1"
        assert post(base + "/review", {"pr": 1, "event": "COMMENT", "body": "x", "comments": []}) == 400
        fb = {"layers": [{"n": 1, "label": "feat-a", "verdict": "looks good", "summary": "", "comments": []},
                         {"n": 2, "label": "feat-b", "verdict": "needs changes", "summary": "Tighten y.",
                          "comments": [{"path": "A.java", "line": 3, "severity": "nit", "body": "long is wide", "code": "  long y;"}]}]}
        assert post(base + "/feedback", fb) == 200
        assert post(base + "/feedback", fb) == 409
        assert set(json.loads((rdir / "posted.json").read_text())) == {"1", "2"}
        text = feedback_text(rdir, json.loads((rdir / "feedback.json").read_text()))
        assert "## feat-b: needs changes" in text and "- A.java:3 (nit): long is wide" in text and "at: `long y;`" in text, text
        with contextlib.redirect_stdout(io.StringIO()):
            assert uncollected(home, rid).startswith("Feedback from an earlier")
            assert uncollected(home, rid) is None
            assert wait_feedback(home, rid, rdir) == 0
        inbox = json.loads(urllib.request.urlopen(url + "api/reviews").read())["reviews"][0]
        assert inbox["mode"] == "self" and inbox["branch"] == "feat-b" and inbox["sent"], inbox
    finally:
        proc.terminate()
        proc.wait()


def check_server(w: pathlib.Path, home: pathlib.Path) -> None:
    """One review, two rounds of the fixture: round 2 has #2 moved, with A.java line 3 changed and line 4 kept."""
    from serve import norm_refs, review_id  # noqa: E402
    refs = norm_refs(["https://github.com/o/r/pull/2", "o/r#1"])
    assert refs == ["o/r#1", "o/r#2"] and review_id(refs) == review_id(norm_refs(["o/r#2", "o/r#1"]))
    rid = review_id(refs)
    rev_dir = home / "reviews" / rid
    r1, r2 = rev_dir / "rounds" / "1", rev_dir / "rounds" / "2"
    shutil.copytree(w, r1)
    shutil.copytree(w, r2)
    for rdir in (r1, r2):
        (rdir / "progress.json").unlink(missing_ok=True)
    data2 = json.loads((r2 / "guide.json").read_text())
    st = data2["stacks"][0]
    st["prs"][1]["sha"] = "f" * 40
    st["since"] = {"state": "ROUND", "pr": 2, "sha": "e" * 40, "at": "2026-10-20T00:00:00Z", "files": {"A.java": [3]}}
    a = next(f for f in st["files"] if f["path"] == "A.java")
    for row in (x for h in a["hunks"] for x in h):
        if row.get("n") == 3 and row["k"] == "+":
            row["t"] = "  short y;"
    (r2 / "guide.json").write_text(json.dumps(data2))
    (rev_dir / "review.json").write_text(json.dumps({"id": rid, "refs": refs, "created": 1, "status": "open",
                                                    "rounds": [{"n": 1, "effort": "balanced", "created": 1}, {"n": 2, "effort": "deep", "created": 2}]}))
    sid = st["id"]
    # Round 1's state: a comment on line 4 (kept), one on line 3 (changed), an accepted finding, viewed files, a verdict.
    (r1 / "ui.json").write_text(json.dumps({
        "stack": sid, "viewed": {f"{sid}A.java": True, f"{sid}B.java": True}, "verdict": {"2": {"body": "Looks close.", "event": "COMMENT"}},
        "extra": {sid: [{"id": f"{sid}-x1", "custom": True, "severity": "comment", "path": "A.java", "line": 4, "pr": 2, "pl": 4, "ps": None},
                        {"id": f"{sid}-x2", "custom": True, "severity": "nit", "path": "A.java", "line": 3, "pr": 2, "pl": 3, "ps": None}]},
        "cm": {f"{sid}-x1": {"on": True, "gone": False, "sev": "comment", "text": "Why empty?"},
               f"{sid}-x2": {"on": True, "gone": False, "sev": "nit", "text": "long is wide"},
               next(c["id"] for c in st["comments"] if c["id"].endswith("-x")): {"on": True, "gone": False, "sev": "question", "text": "Who reads x?"}}}))

    def start():
        proc = subprocess.Popen([sys.executable, str(HERE / "serve.py"), "--home", str(home), "--port", "0"], stdout=subprocess.PIPE, text=True)
        return proc, proc.stdout.readline().strip()

    def req(url, method="GET", body=None, tok=None):
        r = urllib.request.Request(url, None if body is None else json.dumps(body).encode(), method=method,
                                   headers={"Content-Type": "application/json", **({"X-Review-Token": tok} if tok else {})})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    server, url = start()
    try:
        info = json.loads((home / "server.json").read_text())
        assert url == f"http://127.0.0.1:{info['port']}/" and oct((home / "server.json").stat().st_mode)[-3:] == "600"
        # A second server finds the first one and exits.
        second = subprocess.run([sys.executable, str(HERE / "serve.py"), "--home", str(home), "--port", "0"], capture_output=True, text=True, timeout=10)
        assert second.stdout.strip() == url, second.stdout
        token = info["token"]
        inbox = json.loads(req(url + "api/reviews")[1])["reviews"]
        assert [(x["id"], x["rounds"], x["live"], x["status"]) for x in inbox] == [(rid, 2, 2, "open")], inbox
        assert rid in req(url)[1]
        page = req(url + f"r/{rid}")[1]
        assert '"round": 2' in page and '"live": true' in page and token in page
        ui = json.loads((r2 / "ui.json").read_text())
        moved = ui["extra"][sid]
        assert [(c["line"], c["pr"]) for c in moved] == [(2, 1), (4, 2)], moved
        assert [c["text"] for c in ui["lost"]] == ["long is wide"], ui["lost"]
        assert sorted(ui["cm"][c["id"]]["text"] for c in moved) == ["Who reads x?", "Why empty?"]
        assert ui["viewed"] == {f"{sid}B.java": True}, ui["viewed"]
        assert ui["verdict"] == {"2": {"body": "Looks close."}}, ui["verdict"]
        assert '"live": false' in req(url + f"r/{rid}/1")[1]
        base = url + f"api/r/{rid}"
        assert req(base + "/2/ui", "PUT", {"stack": sid, "viewed": {"x": True}}, "wrong")[0] == 403
        assert req(base + "/1/ui", "PUT", {"stack": sid}, token)[0] == 409
        assert req(base + "/2/ui", "PUT", {"stack": sid, "viewed": {"saved": True}}, token)[0] == 200
        state = json.loads(req(base + "/2/state")[1])
        assert state["version"].startswith("2:") and state["live"] == 2 and not state["rerun"]["running"]
        outside = {"pr": 2, "event": "COMMENT", "body": "x", "comments": [{"path": "A.java", "line": 99, "body": "y"}]}
        assert req(base + "/2/review", "POST", outside, "wrong")[0] == 403
        assert req(base + "/2/review", "POST", outside, token)[0] == 400
        assert req(base + "/2/review", "POST", {**outside, "pr": 1, "event": "APPROVE", "comments": []}, token)[0] == 400
        assert req(base + "/1/review", "POST", outside, token)[0] == 409
        assert req(base + "/rerun", "POST", {"effort": "max"}, token)[0] == 400
        assert req(base + "/finish", "POST", {}, token)[0] == 200
        assert json.loads(req(url + "api/reviews")[1])["reviews"][0]["status"] == "done"
        assert req(url + "api/stop", "POST", {}, token)[0] == 200
        assert server.wait(timeout=5) == 0 and not (home / "server.json").exists()
    finally:
        server.terminate()
    # A new server on another port: the saved state, the status and the review are all still there.
    server, url = start()
    try:
        page = req(url + f"r/{rid}")[1]
        assert '"saved": true' in page and '"status": "done"' in page, page[:200]
        # A server whose code differs from the installed code is stopped by the next `open`, so it comes back updated.
        import serve  # noqa: E402
        assert serve.find_server(home, current=True)
        real, serve.VERSION = serve.VERSION, "older"
        try:
            assert serve.find_server(home) and not serve.find_server(home, current=True)
            with contextlib.redirect_stdout(io.StringIO()):
                serve.replace_old_server(home)
            assert server.wait(timeout=5) == 0 and not serve.find_server(home)
        finally:
            serve.VERSION = real
    finally:
        server.terminate()
        server.wait()


STUB = '''#!{python}
import json, os, pathlib, random, sys, time
argv = sys.argv[1:]
with open(os.environ["STUB_ARGV"], "a") as f:
    f.write(json.dumps(argv) + "\\n")
sid = argv[argv.index("--resume") + 1] if "--resume" in argv else "sess-%06d" % random.randrange(10**6)
reply = pathlib.Path(os.environ["STUB_REPLY"]).read_text()
gate = pathlib.Path(os.environ["STUB_GATE"])
out = lambda o: (print(json.dumps(o), flush=True), time.sleep(0.12))
out({{"type": "system", "subtype": "init", "session_id": sid}})
out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Read", "input": {{"file_path": "/w/tip/A.java"}}}}]}}}})
while gate.exists():
    time.sleep(0.05)
out({{"type": "stream_event", "event": {{"type": "message_start"}}}})
for word in reply.split(" "):
    out({{"type": "stream_event", "event": {{"type": "content_block_delta", "delta": {{"type": "text_delta", "text": word + " "}}}}}})
out({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": reply}}]}}}})
out({{"type": "result", "subtype": "success", "is_error": False, "result": reply, "session_id": sid}})
'''


def check_ask(w: pathlib.Path, root: pathlib.Path) -> None:
    """Questions: threads on disk, a stubbed agent whose stream is replayed, resume, the concurrency cap, proposals and their
    apply, the read-only and token guards, and carry-over of threads to a new round."""
    import os
    import time
    home = root / "ask-home"
    stub, argv_log, reply, gate = root / "stub_claude", root / "stub-argv.jsonl", root / "stub-reply.txt", root / "stub-gate"
    stub.write_text(STUB.format(python=sys.executable))
    stub.chmod(0o755)
    reply.write_text("The caller is `A.f` at `A.java:4`. It is empty. ")
    rid = "ask-test"
    r1, r2 = home / "reviews" / rid / "rounds" / "1", home / "reviews" / rid / "rounds" / "2"
    shutil.copytree(w, r1)
    (r1 / "progress.json").unlink(missing_ok=True)
    sid = json.loads((r1 / "guide.json").read_text())["stacks"][0]["id"]
    (r1 / "ui.json").write_text(json.dumps({"stack": sid}))
    review = {"id": rid, "refs": ["o/r#1", "o/r#2"], "created": 1, "status": "open", "rounds": [{"n": 1, "effort": "balanced", "created": 1}]}
    (home / "reviews" / rid / "review.json").write_text(json.dumps(review))
    env = {**os.environ, "REVIEW_GUIDE_CLAUDE": str(stub), "STUB_ARGV": str(argv_log), "STUB_REPLY": str(reply), "STUB_GATE": str(gate)}
    proc = subprocess.Popen([sys.executable, str(HERE / "serve.py"), "--home", str(home), "--port", "0"], stdout=subprocess.PIPE, text=True, env=env)
    url = proc.stdout.readline().strip()
    token = json.loads((home / "server.json").read_text())["token"]

    def req(path, body=None, tok=token):
        r = urllib.request.Request(url + "api/r/" + rid + path, None if body is None else json.dumps(body).encode(),
                                   method="GET" if body is None else "POST", headers={"Content-Type": "application/json", **({"X-Review-Token": tok} if tok else {})})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or "{}")

    def wait(rnd, tid, until="idle", seen=None):
        for _ in range(300):
            t = req(f"/{rnd}/threads/{tid}")[1]
            if seen is not None and t["messages"][-1]["role"] == "agent":
                seen.append(t["messages"][-1]["text"])
            if t["status"] == until:
                return t
            time.sleep(0.03)
        raise AssertionError(f"thread {tid} stayed {t['status']}: {t}")

    def ask(rnd, tid=None, target=None, q="Why?", deep=False):
        code, out = req(f"/{rnd}/ask", {"question": q, **({"thread": tid} if tid else {"target": target}), **({"deep": True} if deep else {})})
        return code, out

    def runs():
        return [json.loads(x) for x in argv_log.read_text().splitlines()]

    finding, line = {"kind": "finding", "ref": f"{sid}-correctness-x"}, {"kind": "line", "path": "A.java", "start": 3, "end": 4}
    try:
        assert req("/1/ask", {"question": "x", "target": finding}, tok=None)[0] == 403
        for bad in ({"kind": "line", "path": "A.java", "start": 3, "end": 99}, {"kind": "line", "path": "Z.java", "start": 1}, {"kind": "finding", "ref": "nope"},
                    {"kind": "section", "ref": 9}, {"kind": "step", "ref": "nope"}, {"kind": "poem"}, "x"):
            assert ask(1, target=bad)[0] == 400, bad
        secs = json.loads((r1 / "guide.json").read_text())["stacks"][0]["sections"]
        ri, ba = (next(i for i, x in enumerate(secs) if x["type"] == t) for t in ("risks", "before_after"))
        for bad in ({"kind": "item", "ref": ri, "item": 99}, {"kind": "item", "ref": ri}, {"kind": "item", "ref": 0, "item": 0}):
            assert ask(1, target=bad)[0] == 400, bad
        assert ask(1, target=finding, q="  ")[0] == 400 and ask(1, tid="nope")[0] == 404 and not argv_log.exists()
        import ask as askmod
        data = json.loads((r1 / "guide.json").read_text())
        got = askmod.resolve(data, r1, {"kind": "item", "stack": sid, "ref": ba, "item": 0})[0]
        assert got["label"] == secs[ba]["rows"][0]["aspect"][:80] and got["section_title"] == secs[ba]["title"], got
        moved = askmod.move_target(got, r1, data)
        assert moved and moved["item"] == 0, moved

        # A finding: the streamed answer grows, the session id is stored, the text and target are in the prompt.
        gate.touch()
        code, out = ask(1, target=finding, q="Is this real?")
        assert code == 202, out
        tid, partial = out["thread"], []
        assert ask(1, tid=tid, q="again")[0] == 409
        gate.unlink()
        t = wait(1, tid, seen=partial)
        text = "The caller is `A.f` at `A.java:4`. It is empty."
        assert t["messages"][-1]["text"] == text and t["messages"][0]["text"] == "Is this real?" and t["session_id"].startswith("sess-"), t
        assert any(p and p != text and text.startswith(p.strip()) for p in partial), partial
        assert t["target"]["label"] == "Who reads x?" and t["proposal"] is None and not t.get("error") and t["messages"][-1]["model"] == "sonnet", t
        first = runs()[0]
        prompt = first[first.index("-p") + 1]
        assert "Question: Is this real?" in prompt and "Who reads `x`?" in prompt and "brief.md" in prompt, prompt
        assert "--resume" not in first and "--no-session-persistence" not in first and first[first.index("--model") + 1] == "sonnet"
        assert first[first.index("--agent") + 1] == "review-guide:answerer" and "--include-partial-messages" in first
        assert first[first.index("--tools") + 1] == "Read,Grep,Glob,Bash" and "mcp__*" in first and first[first.index("--permission-mode") + 1] == "dontAsk"
        assert [r["id"] for r in req("/1/threads")[1]["threads"]] == [tid]

        # A follow-up resumes the session; "dig deeper" runs Opus.
        assert ask(1, tid=tid, q="And the test?")[0] == 202
        t2 = wait(1, tid)
        assert [m["role"] for m in t2["messages"]] == ["user", "agent", "user", "agent"] and t2["session_id"] == t["session_id"]
        follow = runs()[1]
        assert follow[follow.index("--resume") + 1] == t["session_id"] and "--plugin-dir" in follow and follow[follow.index("-p") + 1] == "Question: And the test?"
        assert ask(1, tid=tid, q="Dig", deep=True)[0] == 202
        assert wait(1, tid)["messages"][-1]["deep"]
        deep = runs()[2]
        assert deep[deep.index("--model") + 1] == "opus" and deep[deep.index("--effort") + 1] == "high" and "Depth: deep" in deep[deep.index("-p") + 1]

        # A line, and the cap: four answers, three running, one queued; a thread answers one turn at a time.
        gate.touch()
        ids = [ask(1, target=line, q=f"q{i}")[1]["thread"] for i in range(4)]
        for _ in range(100):
            states = sorted(req(f"/1/threads/{i}")[1]["status"] for i in ids)
            if states == ["queued", "running", "running", "running"]:
                break
            time.sleep(0.05)
        assert states == ["queued", "running", "running", "running"], states
        gate.unlink()
        for i in ids:
            wait(1, i)
        lt = req(f"/1/threads/{ids[0]}")[1]["target"]
        assert lt["label"] == "A.java:3-4" and lt["text"] == "  long y;\n  void f() {}", lt

        # Diagram targets: a component, a flowchart's own box, an arrow of a structure diagram and a message of a sequence.
        for tgt, label in (({"kind": "node", "ref": "a", "section": 3}, "A"), ({"kind": "node", "ref": "q"}, "y set?"), ({"kind": "node", "ref": "in>q"}, "f called to y set?"),
                           ({"kind": "node", "ref": "a>b"}, "A to B"), ({"kind": "node", "ref": "b>c"}, "B to C")):
            code, out = ask(1, target=tgt, q="What is this?")
            assert code == 202, (tgt, out)
            assert wait(1, out["thread"])["target"]["label"] == label, tgt
        assert ask(1, target={"kind": "node", "ref": "a>q"}, q="x")[0] == 400 and ask(1, target={"kind": "node", "ref": "nope"}, q="x")[0] == 400

        # Proposals: a comment on a line the stack wrote, a re-judge that does not belong on a line thread, one that does, a guide fix.
        def propose(tid_or_target, action, q="Settle it"):
            reply.write_text("It does. " + "```action\n" + json.dumps(action) + "\n```")
            tid = ask(1, tid=tid_or_target, q=q)[1]["thread"] if isinstance(tid_or_target, str) else ask(1, target=tid_or_target, q=q)[1]["thread"]
            return wait(1, tid)

        c = propose(line, {"type": "comment", "path": "A.java", "line": 3, "body": "`long` doubles the field width.", "severity": "nit"})
        assert c["messages"][-1]["text"] == "It does." and c["proposal"]["pr"] == 2 and c["proposal"]["pl"] == 3 and not c["proposal"]["applied"], c
        assert propose(line, {"type": "comment", "path": "A.java", "line": 1, "body": "x"})["proposal"] is None
        bad = propose(line, {"type": "rejudge", "finding": finding["ref"], "status": "refuted", "note": "n"})
        assert bad["proposal"] is None and "finding thread" in bad["proposal_error"], bad
        rj = {"type": "rejudge", "finding": finding["ref"], "status": "refuted", "note": "`A.java:2` is read."}
        assert propose(finding, {**rj, "finding": "other"})["proposal"] is None
        j = propose(finding, rj)
        assert j["proposal"]["status"] == "refuted" and not j["proposal"]["applied"], j
        assert req(f"/1/threads/{j['id']}/apply", {}, tok=None)[0] == 403 and req(f"/1/threads/{ids[0]}/apply", {})[0] == 409
        code, out = req(f"/1/threads/{j['id']}/apply", {})
        assert code == 200 and out["applied"], out
        assert json.loads((r1 / sid / "rejudged.json").read_text()) == {"correctness-x": {"status": "refuted", "note": "`A.java:2` is read.", "thread": j["id"]}}
        assert req(f"/1/threads/{j['id']}")[1]["proposal"]["applied"] and req(f"/1/threads/{c['id']}/apply", {})[1]["proposal"]["type"] == "comment"
        fact = {"kind": "fact", "ref": 0}
        assert propose(fact, {"type": "guide_fix", "ref": {"kind": "fact"}, "replacement": {"title": "t" * 80, "text": "x"}})["proposal"] is None
        fx = propose(fact, {"type": "guide_fix", "ref": {"kind": "fact"}, "replacement": {"title": "`x` is write-only.", "text": "Nothing reads it yet."}})
        assert fx["proposal"]["ref"] == {"kind": "fact", "stack": sid, "index": 0}, fx
        assert req(f"/1/threads/{fx['id']}/apply", {})[0] == 200
        fixes = json.loads((r1 / sid / "guide-fixes.json").read_text())
        assert len(fixes) == 1 and fixes[0]["replacement"]["title"] == "`x` is write-only." and fixes[0]["thread"] == fx["id"]
        step = {"kind": "step", "ref": f"{sid}-fields"}
        st = propose(step, {"type": "guide_fix", "ref": {"kind": "step"}, "replacement": {"kind": "read", "title": "Fields", "why": "New state.", "nodes": ["zz"], "test": None,
                                                                                         "anchor": {"path": "A.java", "start": 1, "end": 5}}})
        assert st["proposal"] is None and "nodes not declared" in st["proposal_error"], st

        # Round 2 (A.java gains a header line, `long y` becomes `short y`): the thread on `void f` follows it one line down, the
        # finding thread stays, the ones on the changed lines are listed as lost, and an unapplied proposal does not carry.
        keep = ask(1, target={"kind": "line", "path": "A.java", "start": 4, "end": 4}, q="What is f for?")[1]["thread"]
        wait(1, keep)
        shutil.copytree(w, r2)
        (r2 / "progress.json").unlink(missing_ok=True)
        tip = r2 / sid / "tip" / "A.java"
        tip.write_text("// header\n" + tip.read_text().replace("long y", "short y"))
        review["rounds"].append({"n": 2, "effort": "balanced", "created": 2})
        (home / "reviews" / rid / "review.json").write_text(json.dumps(review))
        urllib.request.urlopen(url + f"r/{rid}").read()
        ui2 = json.loads((r2 / "ui.json").read_text())
        kept = {f.stem: json.loads(f.read_text()) for f in (r2 / "threads").glob("*.json")}
        assert {tid, keep, j["id"], fx["id"]} <= set(kept) and not {*ids, c["id"]} & set(kept), sorted(kept)
        assert (kept[keep]["target"]["start"], kept[keep]["target"]["text"]) == (5, "  void f() {}") and kept[keep]["session_id"], kept[keep]
        assert kept[tid]["session_id"] == t["session_id"] and kept[j["id"]]["proposal"]["applied"] and kept[fx["id"]]["proposal"]["applied"], kept
        assert {x["id"] for x in ui2["lost_threads"]} >= {*ids, c["id"]} and not {x["id"] for x in ui2["lost_threads"]} & set(kept), ui2["lost_threads"]
        assert ask(1, target=finding)[0] == 409 and req(f"/1/threads/{tid}/apply", {})[0] == 409  # round 1 is read-only now
        assert ask(2, tid=keep, q="Still there?")[0] == 202
        wait(2, keep)
        last = runs()[-1]
        assert last[last.index("--resume") + 1] == kept[keep]["session_id"] and f"Work directory: {r2}" in last[last.index("-p") + 1]
    finally:
        gate.unlink(missing_ok=True)
        proc.terminate()
        proc.wait()


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
        # The previous round of the guide saw #2 at pr1: only #2's move counts, and it feeds last_seen like a review.
        prev = root / "prev"
        prev.mkdir()
        (prev / "run.json").write_text(json.dumps({"fetched_at": 0, "prs": [{"n": 1, "sha": r["pr1"], "review_sha": r["pr1"]},
                                                                             {"n": 2, "sha": r["pr1"], "review_sha": r["pr1"]}]}))
        names = {1: 1, 2: 2}
        rounds = previous_round(prev, [[1, 2]], heads, names)
        assert rounds[2][2]["state"] == "ROUND" and last_seen(git, [1, 2], rounds[2])[0]["sha"] == r["pr1"], rounds
        assert previous_round(prev, [[1, 2]], {1: r["pr1"], 2: r["pr1"]}, names) == {}

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

        check_local(root)

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
        run = {"repo": "o/r", "viewer": "me", "run_id": "t", "checkout": r["repo"], "warnings": [], "prs": [pr(1, "me"), pr(2, "other")],
               "stacks": [{"id": sid, "prs": [1, 2], "base": r["base"], "tip": r["pr2"], "files": files,
                           "since": {"pr": 1, "state": "APPROVED", "sha": r["pr1"], "at": "2026-10-01T00:00:00Z", "files": {"A.java": [3, 4], "B.java": [1]}}}]}
        (w / "run.json").write_text(json.dumps(run))

        guide = {
            "summary": "Adds fields to A and a class B.",
            "key_facts": [{"title": "Nothing reads `x` yet.", "text": "It is written only."}],
            "components": {"layers": [{"id": "core", "label": "core"}, {"id": "lib", "label": "lib"}],
                           "nodes": [{"id": "a", "label": "A", "role": "the holder", "layer": "core", "state": "changed", "path": "A.java"},
                                     {"id": "b", "label": "B", "layer": "core", "state": "new", "path": "B.java"},
                                     {"id": "c", "label": "C", "layer": "lib", "state": "existing", "path": None, "at": "C.java:2"},
                                     {"id": "old", "label": "OldA", "layer": "lib", "state": "removed", "path": None}]},
            "sections": [
                {"type": "diagram", "kind": "sequence", "title": "Use B", "participants": ["a", "b", "c"], "messages": [
                    {"from": "a", "to": "b", "label": "calls", "new": True, "at": "A.java:4"},
                    {"frame": "alt", "branches": [
                        {"guard": "x set", "messages": [{"from": "b", "to": "c", "label": "g()", "at": "C.java:2"},
                                                        {"from": "c", "to": "b", "label": "done", "return": True}]},
                        {"guard": "otherwise", "messages": [{"note": "nothing to do", "on": "b"}]}]},
                    {"frame": "loop", "branches": [{"guard": "each field", "messages": [{"from": "a", "to": "a", "label": "f()", "at": "A.java:4"}]}]}]},
                {"type": "diagram", "kind": "flowchart", "title": "How f decides", "nodes": [
                    {"id": "in", "label": "f called", "shape": "step", "at": "A.java:4"}, {"id": "q", "label": "y set?", "shape": "decision"},
                    {"id": "save", "label": "store y", "shape": "store", "state": "new"}, {"id": "out", "label": "done", "shape": "end"}],
                 "edges": [{"from": "in", "to": "q", "at": "A.java:4"}, {"from": "q", "to": "save", "label": "yes", "at": "A.java:3"},
                           {"from": "q", "to": "out", "label": "no", "at": "A.java:4"}, {"from": "save", "to": "in", "label": "retry"}]},
                {"type": "diagram", "kind": "states", "title": "Life of an A", "nodes": [
                    {"id": "s-new", "label": "new", "initial": True}, {"id": "s-set", "label": "set", "state": "new"}, {"id": "s-done", "label": "done", "terminal": True}],
                 "edges": [{"from": "s-new", "to": "s-set", "label": "f()", "guard": "y > 0", "at": "A.java:4"},
                           {"from": "s-set", "to": "s-new", "label": "reset", "at": "A.java:2"}, {"from": "s-set", "to": "s-done", "label": "close", "at": "A.java:5"}]},
                {"type": "diagram", "kind": "structure", "title": "What depends on what", "nodes": ["a", "b", "c", "old"],
                 "edges": [{"from": "a", "to": "b", "label": "uses", "new": True, "at": "A.java:4"}, {"from": "b", "to": "c", "at": "C.java:2"},
                           {"from": "a", "to": "old", "label": "was"}]},
                {"type": "diagram", "kind": "data", "title": "The A record", "nodes": [
                    {"id": "rec", "label": "A", "state": "changed", "at": "A.java:1", "fields": [
                        {"name": "x", "type": "int", "change": "added"}, {"name": "y", "type": "long", "change": "changed"}, {"name": "z", "type": "int", "change": "removed"}]},
                    {"id": "brec", "label": "B", "state": "new", "fields": [{"name": "a", "type": "A"}]}],
                 "edges": [{"from": "brec", "to": "rec", "label": "holds", "at": "B.java:1"}]},
                {"type": "risks", "title": "Risks", "items": [
                    {"title": "`y` widens", "text": "Readers of `y` as int truncate it.", "status": "open", "at": "A.java:3"},
                    {"title": "B is empty", "text": "Kept for the next PR.", "status": "accepted"}]},
                {"type": "before_after", "title": "What changes", "rows": [{"aspect": "`y`", "before": "int", "after": "long"}]},
                {"type": "steps", "title": "Read in this order", "steps": [
                    {"id": "fields", "kind": "read", "title": "Fields", "why": "New state on A.", "nodes": ["a", "q", "s-set", "rec"], "test": None,
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
        bad["sections"][6] = {"type": "table", "title": "Per PR", "columns": ["PR", "Change"], "rows": [["#1"]]}
        bad["sections"][5] = {"type": "poem", "title": "x"}
        (w / sid / "guide.json").write_text(json.dumps(bad))
        bad_review = json.loads(json.dumps(correctness))
        bad_review["findings"][0]["line"] = 1
        bad_review["findings"] += [{**bad_review["findings"][1], "id": f"n{i}"} for i in range(3)]
        (w / sid / "review-correctness.json").write_text(json.dumps(bad_review))
        out = check()
        assert out.returncode == 1, out.stdout
        for expected in ("line 1 of A.java was not written by this stack", "#2 receives 4 findings", "summary is 400 characters",
                         "rows[0]: expected 2 cells", "sections[5].type must be one of"):
            assert expected in out.stdout, (expected, out.stdout)
        # A reviewer checks its own file, whatever state the guide is in.
        assert "summary" not in check("--only", "correctness").stdout

        # The diagram mistakes a writer makes: an unknown node, too many boxes, a line that is not there, guessed arrows,
        # an alt with one branch, and a node id used twice.
        bad = json.loads(json.dumps(guide))
        seq, flow, states = bad["sections"][0], bad["sections"][1], bad["sections"][2]
        seq["messages"][1]["branches"].pop()
        seq["messages"][0]["at"] = "A.java:99"
        flow["nodes"] += [{"id": f"n{i}", "label": f"n{i}"} for i in range(9)]
        flow["edges"].append({"from": "q", "to": "ghost", "label": "maybe"})
        states["nodes"][1]["id"] = "q"
        states["edges"][0]["from"] = "q"
        bad["sections"][3]["edges"][1]["at"] = "Nowhere.java:1"
        for e in flow["edges"] + states["edges"] + bad["sections"][4]["edges"]:
            e.pop("at", None)
        bad["sections"][7]["steps"][0]["nodes"].append("nope")
        (w / sid / "guide.json").write_text(json.dumps(bad))
        out = check("--only", "guide")
        assert out.returncode == 1, out.stdout
        for expected in ("an `alt` frame needs 2 to 4 branches", "at 'A.java:99' is outside A.java (1..5)", "13 nodes; the limit is 12. Split",
                         "'q'->'ghost' must join nodes of this diagram", "id 'q' missing or used twice", "Nowhere.java is neither a changed file",
                         "nodes not declared in components or any diagram: ['s-set', 'nope']", "arrows cite no line; give each arrow `at`"):
            assert expected in out.stdout, (expected, out.stdout)
        # A guide from before the diagram kinds still builds (an earlier round), but --check no longer takes it.
        legacy = {**guide, "sections": [{"type": "flow", "title": "Use B", "steps": [{"from": "a", "to": "b", "label": "calls"}]},
                                        {"type": "map", "title": "Components"}, guide["sections"][-1]]}
        legacy["sections"][2]["steps"][0]["nodes"] = ["a"]
        (w / sid / "guide.json").write_text(json.dumps(legacy))
        (w / sid / "review-correctness.json").write_text(json.dumps(correctness))
        assert "type must be one of" in check("--only", "guide").stdout
        out = subprocess.run([sys.executable, str(HERE / "build.py"), str(w), "--partial"], capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr
        kinds = [(x["type"], x.get("kind")) for x in json.loads((w / "guide.json").read_text())["stacks"][0]["sections"]]
        assert kinds == [("diagram", "sequence"), ("diagram", "structure"), ("steps", None)], kinds

        (w / sid / "guide.json").write_text(json.dumps(guide))
        (w / sid / "review-correctness.json").write_text(json.dumps(correctness))
        out = check("--only", "guide")
        assert out.stdout.splitlines() == ["ok", "stack 1-2: 2 of 14 arrows cite no line"], out.stdout
        out = subprocess.run([sys.executable, str(HERE / "build.py"), str(w), "--partial"], capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr
        data = json.loads((w / "guide.json").read_text())
        assert data["stacks"][0]["reviewing"] == ["standards", "spec"], data["stacks"][0]["reviewing"]
        assert {c["status"] for c in data["stacks"][0]["comments"]} == {"checking"}

        (w / sid / "review-standards.json").write_text(json.dumps({"skipped": None, "findings": []}))
        (w / sid / "review-spec.json").write_text(json.dumps({"skipped": "No ticket and an empty description.", "findings": []}))
        sys.path.insert(0, str(HERE))
        from build import stages  # noqa: E402
        from rerun import PLUGIN, agent_cmd  # noqa: E402

        # The model each agent runs on: --model when rerun.py passes one, else the agent's frontmatter.
        def model(agent: str, effort: str) -> str:
            cmd = agent_cmd(agent, w, "p", effort, str(root))
            if "--model" in cmd:
                return cmd[cmd.index("--model") + 1]
            return next(ln.split()[1] for ln in (PLUGIN / "agents" / f"{agent}.md").read_text().splitlines() if ln.startswith("model:"))
        assert {(a, e): model(a, e) for a in ("guide-writer", "reviewer", "comment-verifier") for e in ("quick", "balanced", "deep")} == {
            ("guide-writer", "quick"): "sonnet", ("guide-writer", "balanced"): "opus", ("guide-writer", "deep"): "opus",
            ("reviewer", "quick"): "sonnet", ("reviewer", "balanced"): "sonnet", ("reviewer", "deep"): "opus",
            ("comment-verifier", "quick"): "sonnet", ("comment-verifier", "balanced"): "sonnet", ("comment-verifier", "deep"): "opus"}
        (w / "progress.json").write_text(json.dumps({"agents": {f"{sid}/verify/correctness-f": {"state": "running", "calls": 3, "last": "reading A.java"}}}))
        (w / sid / "verify").mkdir()
        (w / sid / "verify" / "correctness-x.json").write_text(json.dumps({"status": "verified", "note": "No reader."}))
        (w / sid / "verify" / "correctness-d.json").write_text(json.dumps({"status": "duplicate", "note": "Same as correctness/x."}))
        check_stage = stages(w)[0]["stages"][-1]
        assert (check_stage["state"], check_stage["checked"], check_stage["total"], check_stage["last"]) == ("running", 2, 3, "reading A.java"), check_stage
        assert [x["state"] for x in stages(w)[0]["stages"]] == ["done", "done", "done", "skipped", "running"]
        (w / sid / "verify" / "correctness-f.json").write_text(json.dumps({"status": "unverified", "note": "Needs runtime."}))
        assert stages(w)[0]["stages"][-1]["state"] == "done"
        out = subprocess.run([sys.executable, str(HERE / "build.py"), str(w)], capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr
        data = json.loads((w / "guide.json").read_text())
        st0 = data["stacks"][0]
        cms = {c["id"].rsplit("-", 1)[-1]: c for c in st0["comments"]}
        assert set(cms) == {"x", "f"}, cms
        assert (cms["x"]["pr"], cms["x"]["pl"], cms["x"]["status"], cms["x"]["axis"]) == (1, 2, "verified", "correctness"), cms["x"]
        assert (cms["f"]["pr"], cms["f"]["pl"], cms["f"]["status"]) == (2, 4, "unverified"), cms["f"]
        assert st0["reviewing"] == [] and st0["skipped"] == {"spec": "No ticket and an empty description."}
        assert [x.get("kind", x["type"]) for x in st0["sections"]] == ["sequence", "flowchart", "states", "structure", "data", "risks", "before_after", "steps"]
        assert st0["sections"][7]["steps"][0]["lines"][1]["pr"] == 1
        assert data["stacks"][0]["since"]["files"] == {"A.java": [3, 4], "B.java": [1]}
        assert data["effort"] == "balanced"
        page = (w / "index.html").read_text()
        assert "/*DATA*/" not in page and "/*TOKEN*/" in page

        home = root / "home"
        check_server(w, home)
        check_self_server(w, root / "self-home")
        check_ask(w, root)
        if keep:
            shutil.copytree(home, keep, dirs_exist_ok=True)
    print("ok" + (f", fixture in {keep}" if keep else ""))


if __name__ == "__main__":
    main(sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--keep" else None)
