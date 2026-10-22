#!/usr/bin/env python3
"""Run the review agents for a work directory without a Claude Code session: the one path both the skill and the
page's Rerun button use.

Usage: rerun.py WORKDIR [--effort quick|balanced|deep] [--refs REF... [--prev DIR]] [--focus TEXT]

With --refs, fetches those PRs into WORKDIR first (serve.py starts each round this way; --prev is the previous round,
for the lines changed since). Without, it runs the agents on WORKDIR as fetched. Then, per stack, it runs
the guide writer and three reviewers (correctness, standards, spec) in parallel through `claude -p`, and once the
reviewers are done, one comment verifier per finding (at most VERIFIERS at a time). The page is rebuilt as each part
lands. The agents run in dontAsk mode: they write only inside the work directory and run only the read commands their
prompts name.

Prints `workdir <path>` once the page is ready, then `status <text>` per stage, and `done <summary>` at the end.
Writes `progress.json` (its pid, the latest status, each agent's state, tool calls and latest action, and `finished` or
`error` at the end, read by serve.py), one result line per agent to `agents.log`, and the run's duration to
`~/.cache/review-guide/timings.json`.
"""

import argparse
import concurrent.futures
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
PLUGIN = HERE.parents[2]
TIMINGS = pathlib.Path.home() / ".cache" / "review-guide" / "timings.json"
EFFORTS = ("quick", "balanced", "deep")
AXES = ("correctness", "standards", "spec")
VERIFIERS = 6
# balanced keeps each agent's own model and effort from its frontmatter.
MODELS = {"quick": ["--effort", "low"], "balanced": [], "deep": ["--model", "opus", "--effort", "high"]}
HEADLESS = ("You run without a person to approve tools. Write files only with the Write tool, inside the work directory. "
            "Bash runs only these commands, one per call, with no pipes, heredocs or redirections: "
            "python3 <build.py> W --check --stack S --only X (with W as an absolute path), and git -C <checkout> show|grep|log <tip> (the checkout holds the whole repository at the tip). Anything else is denied.")
DENY = ["Bash(git * --output*)", "Bash(git * -O*)", "Bash(git * --open-files-in-pager*)", "Bash(git * --ext-diff*)",
        *[f"Bash(gh api * {flag}*)" for flag in ("-X", "--method", "-f", "-F", "--field", "--raw-field", "--input")]]


PROGRESS = None


def say(kind: str, text) -> None:
    print(kind, text, flush=True)
    if kind == "status" and PROGRESS:
        PROGRESS.set(status=str(text))


def build(w: pathlib.Path, *args: str) -> bool:
    return subprocess.run([sys.executable, str(HERE / "build.py"), str(w), *args], capture_output=True).returncode == 0


def describe(tool: str, inp: dict) -> str:
    """A tool call as a few words for the progress strip: `reading Foo.java`, `git grep findX`."""
    if tool in ("Read", "Write", "Edit"):
        return f"{'reading' if tool == 'Read' else 'writing'} {pathlib.Path(inp.get('file_path', '')).name}"
    if tool in ("Grep", "Glob"):
        return f"searching {inp.get('pattern', '')}"[:70]
    if tool == "Bash":
        cmd = re.sub(r"git -C \S+", "git", inp.get("command", ""))
        cmd = re.sub(r"\b[0-9a-f]{40}\b", "tip", cmd)
        cmd = re.sub(r"\"?/\S*/([^/\s\"]+)\"?", r"\1", cmd)
        return cmd[:70]
    return tool.lower()


def agent_cmd(agent: str, w: pathlib.Path, prompt: str, effort: str, checkout: str) -> list[str]:
    allow = [f"Edit(/{w}/**)", f"Bash(python3 {HERE}/build.py:*)", f'Bash(python3 "{HERE}/build.py":*)',
             "Bash(gh search code:*)", "Bash(gh api -H:*)"]
    allow += [f"Bash(git -C {checkout} {sub}:*)" for sub in ("show", "grep", "log")]
    return ["claude", "-p", prompt,
            # The checkout must be a working directory too: without it, dontAsk refuses `git grep` there even when allowed.
            "--agent", f"review-guide:{agent}", "--plugin-dir", str(PLUGIN), "--add-dir", str(PLUGIN), "--add-dir", checkout,
            "--setting-sources", "", "--strict-mcp-config", "--permission-mode", "dontAsk", "--no-session-persistence",
            # stream-json needs --verbose with -p; it emits each tool call as it happens.
            "--output-format", "stream-json", "--verbose",
            *MODELS[effort], "--append-system-prompt", HEADLESS, "--allowedTools", *allow, "--disallowedTools", *DENY]


class Progress:
    """progress.json: what each agent is doing, written atomically because serve.py reads it while it changes."""

    def __init__(self, w: pathlib.Path, effort: str):
        self.path, self.lock = w / "progress.json", threading.Lock()
        self.data = {"pid": os.getpid(), "started": time.time(), "effort": effort, "status": "Starting", "agents": {}}
        self.save()

    def update(self, key: str, **fields) -> None:
        with self.lock:
            self.data["agents"].setdefault(key, {"state": "waiting", "calls": 0, "last": ""}).update(fields)
            self.save()

    def set(self, **fields) -> None:
        with self.lock:
            self.data.update(fields)
            self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        os.replace(tmp, self.path)


def record_timing(effort: str, seconds: float) -> None:
    try:
        timings = json.loads(TIMINGS.read_text())
    except (OSError, ValueError):
        timings = {}
    timings[effort] = (timings.get(effort) or [])[-9:] + [round(seconds)]
    TIMINGS.parent.mkdir(parents=True, exist_ok=True)
    TIMINGS.write_text(json.dumps(timings))


def main() -> None:
    global PROGRESS
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir", type=pathlib.Path)
    ap.add_argument("--effort", choices=EFFORTS)
    ap.add_argument("--refs", nargs="+", help="fetch these PRs into WORKDIR first")
    ap.add_argument("--prev", type=pathlib.Path, help="the previous round's work directory")
    ap.add_argument("--focus", help="one sentence the user asked the agents to focus on")
    args = ap.parse_args()
    w = args.workdir
    w.mkdir(parents=True, exist_ok=True)
    effort = args.effort or (json.loads((w / "run.json").read_text()).get("effort") if not args.refs else None) or "balanced"
    PROGRESS = Progress(w, effort)
    try:
        run_round(args, w, effort, PROGRESS)
    except SystemExit as e:
        if e.code not in (None, 0):
            PROGRESS.set(error=str(e.code).strip().splitlines()[-1][:300], finished=time.time())
        raise
    except Exception as e:
        PROGRESS.set(error=f"{type(e).__name__}: {e}"[:300], finished=time.time())
        raise
    PROGRESS.set(finished=time.time())


def run_round(args, w: pathlib.Path, effort: str, progress: Progress) -> None:
    started = progress.data["started"]
    if args.refs:
        say("status", "Fetching the PRs")
        out = subprocess.run([sys.executable, str(HERE / "fetch.py"), "--workdir", str(w), "--effort", effort,
                              *(["--prev", str(args.prev)] if args.prev else []), *args.refs], capture_output=True, text=True)
        if out.returncode:
            sys.exit(out.stderr.strip() or out.stdout.strip())
    run = json.loads((w / "run.json").read_text())
    log, lock = (w / "agents.log").open("a"), threading.Lock()
    ids = [s["id"] for s in run["stacks"]]
    # Unchanged heads reuse the work directory: drop the old results so the agents write fresh ones.
    for sid in ids:
        for name in ("guide.json", *(f"review-{a}.json" for a in AXES)):
            (w / sid / name).unlink(missing_ok=True)
        for f in (w / sid / "verify").glob("*.json"):
            f.unlink()
    build(w, "--partial")
    say("workdir", w)

    def agent(key: str, name: str, prompt: str, done: pathlib.Path, check: list[str]) -> bool:
        progress.update(key, state="running", started=time.time())
        for _ in range(2):  # one retry
            proc = subprocess.Popen(agent_cmd(name, w, prompt, effort, run["checkout"]), cwd=w,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            result, raw = {}, []
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    raw = (raw + [line.strip()])[-5:]
                    continue
                if event.get("type") == "assistant":
                    for c in event["message"].get("content") or []:
                        if c.get("type") == "tool_use":
                            calls = progress.data["agents"][key]["calls"] + 1
                            progress.update(key, calls=calls, last=describe(c.get("name", ""), c.get("input") or {}))
                elif event.get("type") == "result":
                    result = event
            code = proc.wait()
            with lock:
                log.write(f"--- {key} exit {code}: {(result.get('result') or ' '.join(raw))[:600]!r} "
                          f"({result.get('num_turns')} turns, {round((result.get('duration_ms') or 0) / 1000)} s, "
                          f"${result.get('total_cost_usd') or 0:.2f}, {len(result.get('permission_denials') or [])} denied)\n")
                log.flush()
            if done.is_file() and (not check or build(w, *check)):
                progress.update(key, state="done", ended=time.time())
                return True
        progress.update(key, state="failed", ended=time.time())
        return False

    def base(sid: str) -> str:
        return f"Work directory: {w}\nStack: {sid}\nEffort: {effort}" + (f"\nFocus: {args.focus}" if args.focus else "")

    def stack(sid: str) -> bool:
        say("status", f"Writing the guide and reviewing {sid}")
        jobs = {"guide": ("guide-writer", base(sid), w / sid / "guide.json", "guide")}
        jobs |= {axis: ("reviewer", f"{base(sid)}\nAxis: {axis}", w / sid / f"review-{axis}.json", axis) for axis in AXES}

        def run_job(item) -> bool:
            key, (name, prompt, done, only) = item
            ok = agent(f"{sid}/{key}", name, prompt, done, ["--check", "--stack", sid, "--only", only])
            if not ok and key in AXES:
                # The page shows the axis as skipped instead of waiting on it.
                done.write_text(json.dumps({"skipped": "This reviewer failed; see agents.log.", "findings": []}))
            build(w, "--partial")
            return ok

        with concurrent.futures.ThreadPoolExecutor(len(jobs)) as pool:
            ok = dict(zip(jobs, pool.map(run_job, jobs.items())))

        findings = [(axis, f["id"]) for axis in AXES
                    for f in json.loads((w / sid / f"review-{axis}.json").read_text()).get("findings") or []]
        say("status", f"Checking {len(findings)} findings of {sid}")
        (w / sid / "verify").mkdir(exist_ok=True)

        def verify(item: tuple[str, str]) -> None:
            axis, fid = item
            out = w / sid / "verify" / f"{axis}-{fid}.json"
            if not agent(f"{sid}/verify/{axis}-{fid}", "comment-verifier", f"{base(sid)}\nFinding: {axis}/{fid}", out, []):
                out.write_text(json.dumps({"status": "unverified", "note": "The verifier failed on this finding; see agents.log."}))

        # ponytail: one rebuild when the stack's checks are all in, not per finding, so the page reloads once; the strip counts them live.
        with concurrent.futures.ThreadPoolExecutor(VERIFIERS) as pool:
            list(pool.map(verify, findings))
        build(w, "--partial")
        if not ok["guide"]:
            say("status", f"The guide writer failed on {sid}; see {w / 'agents.log'}")
        return ok["guide"]

    with concurrent.futures.ThreadPoolExecutor(len(ids)) as pool:
        ok = all(list(pool.map(stack, ids)))
    if not ok:
        sys.exit(f"some guides failed; see {w / 'agents.log'}")
    build(w)
    record_timing(effort, time.time() - started)
    data = json.loads((w / "guide.json").read_text())
    for s in data["stacks"]:
        by_status = {k: sum(c["status"] == k for c in s["comments"]) for k in ("verified", "unverified", "refuted")}
        say("done", f"{s['id']}: {len(s['comments'])} findings {by_status}, skipped axes {sorted(s['skipped']) or 'none'}, "
                    f"{round(time.time() - started)} s")
    say("status", "Done")


if __name__ == "__main__":
    main()
