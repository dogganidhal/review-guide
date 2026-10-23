#!/usr/bin/env python3
"""Dev tool: write the guides of the saved reviews again with the current guide writer, to compare old and new.

Usage: dev/replay.py [--home DIR] [--source DIR] [--only SUBSTRING] [--dry-run] [--fresh] [--jobs 3]

Copies every review in SOURCE (default ~/.cache/review-guide, read-only) into HOME (default
~/.cache/review-guide-replay), and imports the older `doctolib-*` work directories there as one-round reviews. Each copy
gets a new round: its latest built round's work directory without the stacks' guide.json. Only the guide writer runs
on it (balanced effort, the prompt rerun.py uses); the fetch, findings and verifications are reused. Then build.py
renders the new round. Numbers per stack go to HOME/replay.json; `dev/compare.py` shows old and new side by side.

Stacks already replayed are skipped on the next run (an interrupted run resumes); --fresh copies and runs everything again.
"""

import argparse
import concurrent.futures
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "plugins" / "review-guide" / "skills" / "review-guide" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import rerun  # noqa: E402
from serve import review_id  # noqa: E402

EFFORT = "balanced"
# The stacks of the plan (docs/plans/better-guides.md, Milestone 4) among the older work directories. Other stacks there
# (the 24-PR runs) are listed and skipped. Runs of these are deduplicated, newest first, except
# KEEP_BOTH, whose two runs the plan keeps.
PLAN_STACKS = {"3564-3565", "3569", "3564-3565-3571"}
KEEP_BOTH = {"3564-3565-3571"}
NOT_COPIED = {".DS_Store"}
# What a fresh round of the same work directory starts without: page state, progress and the old guide's output.
DROPPED = {"ui.json", "posted.json", "progress.json", "agents.log", "runner.log", "feedback.json", "threads", "index.html", "guide.json", ".DS_Store"}
AXES = ("correctness", "standards", "spec")
LOCK = threading.RLock()
DATA_NOTE = ("This sends the same source code to the model again, as the original runs did: one guide-writer run per stack "
             "(balanced effort, the checkout and brief of each saved review).")


def read(path: pathlib.Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write(path: pathlib.Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def say(*parts) -> None:
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def build(w: pathlib.Path, *args: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(SCRIPTS / "build.py"), str(w), *args], capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def guide_stacks(w: pathlib.Path) -> list[str]:
    """The stacks of a work directory that have a guide."""
    return [s["id"] for s in (read(w / "run.json") or {}).get("stacks", []) if (w / s["id"] / "guide.json").is_file()]


def latest_built(review_dir: pathlib.Path, rev: dict) -> int | None:
    return max([r["n"] for r in rev.get("rounds", []) if (review_dir / "rounds" / str(r["n"]) / "index.html").is_file()], default=None)


def discover(source: pathlib.Path) -> tuple[list[dict], list[tuple[str, str]]]:
    """What can be replayed: saved reviews (their latest built round) and older work directories (one round each)."""
    found, skipped = [], []
    for rj in sorted(source.glob("reviews/*/review.json")):
        rev, rdir = read(rj), rj.parent
        n = rev and latest_built(rdir, rev)
        stacks = n and guide_stacks(rdir / "rounds" / str(n))
        if stacks:
            found.append({"rid": rev["id"], "kind": "review", "src": rdir, "old": n, "stacks": stacks})
        else:
            skipped.append((rdir.name, "no built round with a guide"))
    legacy = []
    for d in sorted(source.glob("doctolib-*/run.json")):
        run, d = read(d), d.parent
        stacks = run and guide_stacks(d)
        if not stacks:
            skipped.append((d.name, "no stack with a guide.json"))
        elif not set(stacks) & PLAN_STACKS:
            skipped.append((d.name, f"not one of the plan's stacks ({', '.join(s[:30] for s in stacks)})"))
        else:
            legacy.append((run, d, stacks))
    # Newest run first; a plan stack seen again is a duplicate, unless the plan keeps both runs.
    seen = set()
    for run, d, stacks in sorted(legacy, key=lambda x: -x[0].get("fetched_at", 0)):
        key = tuple(stacks)
        if key in seen and not set(stacks) & KEEP_BOTH:
            skipped.append((d.name, "an older run of the same PRs"))
            continue
        seen.add(key)
        refs = sorted(f"{run['repo']}#{p['n']}" for p in run["prs"])
        found.append({"rid": f"{review_id(refs)}-{d.name[-6:]}", "kind": "legacy", "src": d, "old": 1, "stacks": stacks, "run": run, "refs": refs})
    return found, skipped


def fill_missing_reviews(w: pathlib.Path) -> None:
    """Runs from before the three reviewers have none of their files; the page would show them as still running."""
    for s in (read(w / "run.json") or {}).get("stacks", []):
        for axis in AXES:
            f = w / s["id"] / f"review-{axis}.json"
            if not f.exists():
                write(f, {"skipped": "This run had no reviewer for this axis.", "findings": []})


def copy_round(src: pathlib.Path, dst: pathlib.Path, dropped: set[str] = frozenset()) -> None:
    shutil.copytree(src, dst, ignore=lambda d, names: [n for n in names if n in NOT_COPIED or (n in dropped and pathlib.Path(d) == src)])


def prepare(home: pathlib.Path, item: dict, meta: dict) -> dict | None:
    """The copied review with its new round; None when the old guide does not build. `meta` is replay.json's `reviews`."""
    rid, dest = item["rid"], home / "reviews" / item["rid"]
    if rid not in meta:
        shutil.rmtree(dest, ignore_errors=True)
        old = dest / "rounds" / str(item["old"])
        if item["kind"] == "review":
            shutil.copytree(item["src"], dest, ignore=lambda d, names: [n for n in names if n in NOT_COPIED])
        else:
            old.parent.mkdir(parents=True)
            copy_round(item["src"], old)
            at = int(item["run"].get("fetched_at") or time.time())
            write(dest / "review.json", {"id": rid, "refs": item["refs"], "created": at, "status": "open",
                  "rounds": [{"n": 1, "effort": item["run"].get("effort") or "balanced", "focus": None, "created": at}],
                  "cwd": item["run"].get("checkout"), "updated": at})
        fill_missing_reviews(old)
        # The old guide is shown by today's page, so only the guide differs between the two rounds.
        code, out = build(old)
        if code:
            shutil.rmtree(dest)
            say("skip", rid, "the old guide does not build:", out.splitlines()[0][:200])
            return None
        meta[rid] = {"source": str(item["src"]), "old": item["old"]}
    m = meta[rid]
    if "new" not in m:
        rev = read(dest / "review.json")
        m["new"] = max(r["n"] for r in rev["rounds"]) + 1
        write(dest / "review.json", rev | {"rounds": [*rev["rounds"], {"n": m["new"], "effort": EFFORT, "focus": None, "created": int(time.time())}],
                                           "updated": int(time.time())})
    new = dest / "rounds" / str(m["new"])
    if not new.exists():
        copy_round(dest / "rounds" / str(m["old"]), new, DROPPED)
        fill_missing_reviews(new)
        for sid in item["stacks"]:
            (new / sid / "guide.json").unlink(missing_ok=True)
    return m


def shapes(guide: dict | None) -> list[str]:
    """`diagram:sequence`, `risks`, ... per section; the old `flow` and `map` count as a sequence and a structure diagram."""
    out = []
    for sec in (guide or {}).get("sections") or []:
        t, k = sec.get("type"), sec.get("kind")
        out.append({"flow": "diagram:sequence", "map": "diagram:structure"}.get(t) or (f"diagram:{k}" if t == "diagram" else str(t)))
    return out


def old_numbers(old: pathlib.Path, sid: str) -> dict:
    a = ((read(old / "progress.json") or {}).get("agents") or {}).get(f"{sid}/guide") or {}
    secs = round(a["ended"] - a["started"]) if a.get("ended") and a.get("started") else None
    return {"seconds": secs, "calls": a.get("calls"), "sections": shapes(read(old / sid / "guide.json"))}


def write_guide(w: pathlib.Path, sid: str, checkout: str) -> dict:
    """The guide writer on one stack, the way rerun.py runs it (one retry), with what it did measured."""
    prompt = f"Work directory: {w}\nStack: {sid}\nEffort: {EFFORT}"
    out = {"model": None, "seconds": 0, "calls": 0, "cost": 0.0, "attempts": 0, "reply": "", "check_ok": False, "check": ""}
    for _ in range(2):
        out["attempts"] += 1
        (w / sid / "guide.json").unlink(missing_ok=True)
        started = time.time()
        proc = subprocess.Popen(rerun.agent_cmd("guide-writer", w, prompt, EFFORT, checkout), cwd=w, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        result, raw = {}, []
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                raw = (raw + [line.strip()])[-5:]
                continue
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                out["model"] = ev.get("model")
            elif ev.get("type") == "assistant":
                out["calls"] += sum(c.get("type") == "tool_use" for c in ev["message"].get("content") or [])
            elif ev.get("type") == "result":
                result = ev
        proc.wait()
        out["seconds"] += round(time.time() - started)
        out["cost"] = round(out["cost"] + (result.get("total_cost_usd") or 0), 2)
        out["reply"] = (result.get("result") or " ".join(raw)).strip().splitlines()[-1][:300] if (result.get("result") or raw) else ""
        if (w / sid / "guide.json").is_file():
            code, text = build(w, "--check", "--stack", sid, "--only", "guide")
            out |= {"check_ok": code == 0, "check": text[:1500]}
            if code == 0:
                break
        else:
            out["check"] = "the writer left no guide.json"
    return out


def replay_stack(home: pathlib.Path, state: dict, rid: str, sid: str, builder) -> dict:
    m = state["reviews"][rid]
    w = home / "reviews" / rid / "rounds" / str(m["new"])
    old = home / "reviews" / rid / "rounds" / str(m["old"])
    checkout = (read(w / "run.json") or {})["checkout"]
    r = write_guide(w, sid, checkout)
    note = next((l for l in r["check"].splitlines() if l.startswith("note:")), "")
    cited = re.search(r"(\d+) of (\d+) arrows", note)
    guide = read(w / sid / "guide.json")
    row = {"rid": rid, "sid": sid, "old_round": m["old"], "new_round": m["new"], "ok": r["check_ok"], **r,
           "note": note.removeprefix("note: "), "uncited": int(cited[1]) if cited else 0, "arrows": int(cited[2]) if cited else None,
           "new": {"seconds": r["seconds"], "calls": r["calls"], "sections": shapes(guide)}, "old": old_numbers(old, sid)}
    builder(rid, w)
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", type=pathlib.Path, default=pathlib.Path.home() / ".cache" / "review-guide-replay")
    ap.add_argument("--source", type=pathlib.Path, default=pathlib.Path.home() / ".cache" / "review-guide")
    ap.add_argument("--only", help="replay only reviews whose id contains this")
    ap.add_argument("--dry-run", action="store_true", help="list what would run and write nothing")
    ap.add_argument("--fresh", action="store_true", help="copy the reviews again and run every stack again")
    ap.add_argument("--jobs", type=int, default=3, help="guide writers at once, across all reviews (default 3)")
    args = ap.parse_args()
    home, source = args.home.expanduser().resolve(), args.source.expanduser().resolve()
    if home == source or source in home.parents or home in source.parents:
        sys.exit("--home must be a different directory from --source, outside it")
    say(DATA_NOTE)

    found, skipped = discover(source)
    found = [f for f in found if not args.only or args.only in f["rid"]]
    say(f"found {len(found)} reviews to replay in {source}:")
    state = read(home / "replay.json") if not args.fresh else None
    state = state or {"created": int(time.time()), "reviews": {}, "stacks": {}}
    for f in found:
        done = [s for s in f["stacks"] if (state["stacks"].get(f"{f['rid']}/{s}") or {}).get("ok")]
        say(f"  {f['kind']:6} {f['rid']}  round {f['old']}  stacks {', '.join(f['stacks'])}" + (f"  (done: {', '.join(done)})" if done else ""))
    for name, why in skipped:
        say(f"  skipped {name}: {why}")
    if args.dry_run or not found:
        return

    if args.fresh:
        for f in found:
            state["reviews"].pop(f["rid"], None)
            for k in [k for k in state["stacks"] if k.startswith(f["rid"] + "/")]:
                del state["stacks"][k]
    (home / "reviews").mkdir(parents=True, exist_ok=True)
    save = lambda: write(home / "replay.json", state)  # noqa: E731
    prepared = [f for f in found if prepare(home, f, state["reviews"])]
    save()
    jobs = [(f["rid"], s) for f in prepared for s in f["stacks"] if not (state["stacks"].get(f"{f['rid']}/{s}") or {}).get("ok")]

    def builder(rid: str, w: pathlib.Path) -> None:
        with LOCK:  # a page of what is done so far; a stack without a guide shows as its files and diffs
            build(w, "--partial")

    def run(i_job: tuple[int, tuple[str, str]]) -> None:
        i, (rid, sid) = i_job
        say(f"[{i}/{len(jobs)}] start {rid} / {sid}")
        try:
            row = replay_stack(home, state, rid, sid, builder)
        except Exception as e:  # one stack failing must not stop the rest
            row = {"rid": rid, "sid": sid, "ok": False, "check": f"{type(e).__name__}: {e}"}
        with LOCK:
            state["stacks"][f"{rid}/{sid}"] = row
            save()
        say(f"[{i}/{len(jobs)}] {'done ' if row['ok'] else 'FAILED'} {rid} / {sid}: {row.get('seconds')} s, {row.get('calls')} calls, "
            f"check {'ok' if row['ok'] else 'failed: ' + row['check'].splitlines()[0][:160] if row.get('check') else 'failed'}"
            + (f", {row['note']}" if row.get("note") else ""))

    say(f"running {len(jobs)} guide writers, {args.jobs} at once")
    with concurrent.futures.ThreadPoolExecutor(max(1, args.jobs)) as pool:
        list(pool.map(run, enumerate(jobs, 1)))
    for rid, m in state["reviews"].items():  # the final pages of the new rounds
        w = home / "reviews" / rid / "rounds" / str(m["new"])
        if w.is_dir() and build(w)[0]:
            build(w, "--partial")
    save()
    say(f"done; results in {home / 'replay.json'}. Compare: python3 dev/compare.py --home {home}")


if __name__ == "__main__":
    main()
