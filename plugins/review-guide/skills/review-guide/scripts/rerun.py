#!/usr/bin/env python3
"""Rerun the review guide for the PRs of a work directory, from the page: what SKILL.md does, without a Claude Code session.

Usage: rerun.py WORKDIR [--effort quick|balanced|deep]

Fetches the PRs again (new commits land in a new work directory), renders the file tree and diffs, then runs the agents
through `claude -p` per stack: the guide writer and three reviewers (correctness, standards, spec) in parallel, then the
comment verifier once the reviewers are done. The page is rebuilt as each one lands.
The agents run in dontAsk mode: they write only inside the work directory and run only the read commands their prompts
name. Prints `workdir <path>` once the page is ready, then `status <text>` per stage. Agent output goes to agents.log.
"""

import argparse
import concurrent.futures
import json
import pathlib
import subprocess
import sys
import threading

HERE = pathlib.Path(__file__).resolve().parent
PLUGIN = HERE.parents[2]
EFFORTS = ("quick", "balanced", "deep")
AXES = ("correctness", "standards", "spec")
# balanced keeps each agent's own model and effort from its frontmatter.
MODELS = {"quick": ["--effort", "low"], "balanced": [], "deep": ["--model", "opus", "--effort", "high"]}
HEADLESS = ("You run without a person to approve tools. Write files only with the Write tool, inside the work directory. "
            "Bash runs only these commands, one per call, with no pipes, heredocs or redirections: "
            "python3 <build.py> W --check --stack S --only X (with W as an absolute path), and git -C <checkout> show|grep|log <tip> (the checkout holds the whole repository at the tip). Anything else is denied.")
DENY = ["Bash(git * --output*)", "Bash(git * -O*)", "Bash(git * --open-files-in-pager*)", "Bash(git * --ext-diff*)",
        *[f"Bash(gh api * {flag}*)" for flag in ("-X", "--method", "-f", "-F", "--field", "--raw-field", "--input")]]


def say(kind: str, text) -> None:
    print(kind, text, flush=True)


def build(w: pathlib.Path, *args: str) -> bool:
    return subprocess.run([sys.executable, str(HERE / "build.py"), str(w), *args], capture_output=True).returncode == 0


def agent_cmd(agent: str, w: pathlib.Path, sid: str, effort: str, checkout: str, axis: str | None = None) -> list[str]:
    allow = [f"Edit(/{w}/**)", f"Bash(python3 {HERE}/build.py:*)", f'Bash(python3 "{HERE}/build.py":*)',
             "Bash(gh search code:*)", "Bash(gh api -H:*)"]
    allow += [f"Bash(git -C {checkout} {sub}:*)" for sub in ("show", "grep", "log")]
    return ["claude", "-p", f"Work directory: {w}\nStack: {sid}\nEffort: {effort}" + (f"\nAxis: {axis}" if axis else ""),
            # The checkout must be a working directory too: without it, dontAsk refuses `git grep` there even when allowed.
            "--agent", f"review-guide:{agent}", "--plugin-dir", str(PLUGIN), "--add-dir", str(PLUGIN), "--add-dir", checkout,
            "--setting-sources", "", "--strict-mcp-config", "--permission-mode", "dontAsk", "--no-session-persistence",
            "--output-format", "json", *MODELS[effort], "--append-system-prompt", HEADLESS, "--allowedTools", *allow, "--disallowedTools", *DENY]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir", type=pathlib.Path)
    ap.add_argument("--effort", choices=EFFORTS)
    args = ap.parse_args()
    run = json.loads((args.workdir / "run.json").read_text())
    effort = args.effort or run.get("effort", "balanced")

    say("status", "Fetching the PRs again")
    refs = [f"{run['repo']}#{p['n']}" for p in run["prs"]]
    out = subprocess.run([sys.executable, str(HERE / "fetch.py"), "--effort", effort, *refs],
                         cwd=run.get("checkout") or None, capture_output=True, text=True)
    if out.returncode:
        sys.exit(out.stderr.strip() or out.stdout.strip())
    w = pathlib.Path(out.stdout.strip().splitlines()[-1])
    run = json.loads((w / "run.json").read_text())
    log = (w / "agents.log").open("a")

    lock = threading.Lock()

    def agent(name: str, sid: str, done: pathlib.Path, check: list[str], axis: str | None = None) -> bool:
        for _ in range(2):  # one retry, as SKILL.md does
            r = subprocess.run(agent_cmd(name, w, sid, effort, run["checkout"], axis), cwd=w, capture_output=True, text=True)
            with lock:
                log.write(f"--- {name} {axis or ''} {sid} exit {r.returncode}\n{r.stdout[-8000:]}{r.stderr[-2000:]}\n")
                log.flush()
            if done.is_file() and (not check or build(w, *check)):
                build(w, "--partial")
                return True
        return False

    def stack(sid: str) -> bool:
        say("status", f"Writing the guide and reviewing {sid}")
        jobs = {"guide": ("guide-writer", w / sid / "guide.json", None)}
        jobs |= {axis: ("reviewer", w / sid / f"review-{axis}.json", axis) for axis in AXES}
        with concurrent.futures.ThreadPoolExecutor(len(jobs)) as pool:
            ok = dict(zip(jobs, pool.map(lambda j: agent(j[0], sid, j[1], ["--check", "--stack", sid, "--only", j[2] or "guide"], j[2]), jobs.values())))
        for axis in AXES:
            if not ok[axis]:
                # The page shows the axis as skipped instead of waiting on it.
                (w / sid / f"review-{axis}.json").write_text(json.dumps({"skipped": "This reviewer failed; see agents.log.", "findings": []}))
        build(w, "--partial")
        say("status", f"Checking the findings of {sid}")
        if not agent("comment-verifier", sid, w / sid / "verify.json", []):
            # An empty report marks every finding as not checked, so the page stops waiting for it.
            (w / sid / "verify.json").write_text(json.dumps({"comments": {}}))
        build(w, "--partial")
        if not ok["guide"]:
            say("status", f"The guide writer failed on {sid}; see {w / 'agents.log'}")
        return ok["guide"]

    ids = [s["id"] for s in run["stacks"]]
    # Unchanged heads reuse the work directory: drop the old guides so the agents write fresh ones.
    for sid in ids:
        for name in ("guide.json", "verify.json", *(f"review-{a}.json" for a in AXES)):
            (w / sid / name).unlink(missing_ok=True)
    build(w, "--partial")
    say("workdir", w)
    with concurrent.futures.ThreadPoolExecutor(len(ids)) as pool:
        ok = all(list(pool.map(stack, ids)))
    if not ok:
        sys.exit(f"some guides failed; see {w / 'agents.log'}")
    build(w)
    say("status", "Done")


if __name__ == "__main__":
    main()
