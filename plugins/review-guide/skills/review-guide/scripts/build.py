#!/usr/bin/env python3
"""Check the agents' guide files against the fetched data, then render the review page.

Usage: build.py WORKDIR [--check] [--stack ID] [--only guide|plan|<reviewer>] [--partial]

  --check     validate the agents' files that exist, print every problem, exit 1 if any (each agent runs this)
  --stack ID  limit to one stack
  --only F    with --check, validate one agent's file: the guide (guide.html and files.json, or guide.json), plan.json,
              or review-<reviewer>.json for a reviewer of the stack's plan (correctness, standards and spec without one)
  --partial   render a stack with no guide yet as its file tree and diffs (the page opens before the agents finish)

Without --check, writes WORKDIR/index.html and WORKDIR/guide.json (the data serve.py reads).
Every comment is written against the stack's tip; this script routes it to the PR that wrote the line.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time

import figures
import guidehtml
from fetch import DOC_PATH, FIXTURE_PATH, OMIT_PATH, TEST_PATH, parse_diff

SEVERITIES = ("blocker", "should", "question", "nit")
# The reviewers of a stack come from its plan.json (the director's choice). Without one (quick, or a round from before
# the director) these three run.
DEFAULT_REVIEWERS = [
    {"id": "correctness", "title": "Correctness", "hint": "bugs, risk, and what depends on the change"},
    {"id": "standards", "title": "Standards", "hint": "the repo's documented rules and code smells"},
    {"id": "spec", "title": "Spec", "hint": "the ticket and the PR description"},
]
AXES = tuple(r["id"] for r in DEFAULT_REVIEWERS)
REVIEWER_ID = re.compile(r"^[a-z][a-z0-9-]{0,26}[a-z0-9]$")
RESERVED_IDS = {"guide", "plan", "check", "verify"}
PLAN_MODELS, PLAN_EFFORTS, MAX_REVIEWERS = ("opus", "sonnet", "haiku"), ("low", "medium", "high"), 8
ROLES = ("read", "skim", "skip")
KINDS = ("decide", "read")
STATUSES = ("verified", "unverified", "refuted")
NODE_STATES = ("new", "changed", "existing", "external", "removed")
SECTION_TYPES = ("diagram", "risks", "before_after", "table", "list", "steps", "html")
DIAGRAM_KINDS = ("sequence", "flowchart", "states", "structure", "data")
SHAPES = ("step", "decision", "store", "end")
FRAMES = ("alt", "opt", "loop")
FIELD_CHANGES = ("added", "removed", "changed")
RISK_STATUSES = ("open", "decided", "accepted")
LIMITS = {
    "summary": 300, "step_title": 70, "step_why": 220, "file_why": 90, "comment_title": 90,
    "comment_body": 700, "label": 40, "fact_title": 60, "fact_text": 200, "test": 140,
    "section_title": 70, "cell": 160, "item": 220, "skipped": 200, "role": 30, "note": 90,
    "field_type": 30, "risk_title": 70, "risk_text": 220, "focus": 400, "reviewer_title": 40, "reviewer_hint": 90, "charge": 1500,
}
MAX_HTML, MAX_SECTION_HTML, MIN_DATA_AT = 300_000, 60_000, 3
# Tables are for rows of like things; past three a guide reads as a spreadsheet (feedback on a real guide with ten).
MAX_TABLES = 3
# The guide is shorter than the change: words of prose per changed line of production code, with a floor for a small fix
# and a ceiling for a large stack. Tables, pictures and code blocks do not count; a table cell stays a phrase.
PROSE_PER_LINE, PROSE_FLOOR, PROSE_CEILING, MAX_CELL_WORDS = 1.5, 250, 1800, 40
MAX_FACTS, MAX_SECTIONS, MAX_ROWS, MAX_COLUMNS, MAX_ITEMS = 4, 8, 30, 6, 8
MAX_STEPS, MAX_ANCHOR_LINES, MAX_COMPONENTS, MAX_PER_PR_AXIS = 8, 60, 20, 3
# One diagram answers one question: past 12 boxes it reads as a tangle, so the writer splits it.
MAX_DIAGRAM_NODES, MAX_MESSAGES, MAX_EDGES, MAX_FIELDS, MAX_BRANCHES, MAX_FRAME_DEPTH, MAX_RISKS = 12, 24, 20, 12, 4, 2, 6
AT = re.compile(r"^(.+?):(\d+)(?:-(\d+))?$")
TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "ui" / "template.html"


class Stack:
    """One stack's fetched inputs, and the routing of a tip line to the PR that wrote it."""

    def __init__(self, workdir: pathlib.Path, run: dict, stack: dict):
        self.id, self.prs, self.base, self.tip = stack["id"], stack["prs"], stack["base"], stack["tip"]
        self.dir = workdir / stack["id"]
        self.files = {f["path"]: f for f in stack["files"]}
        self.attr = json.loads((self.dir / "attribution.json").read_text())
        self.target = parse_diff((self.dir / "target.diff").read_text())
        self.hunks = {n: json.loads((workdir / str(n) / "hunks.json").read_text()) for n in self.prs}
        self.pr_info = {p["n"]: p for p in run["prs"] if p["n"] in self.prs}
        self.since = stack.get("since")
        self.checkout = run.get("checkout")
        self.effort = run.get("effort") or "balanced"
        self._lines: dict[str, int | None] = {}
        # A director step is expected once the runner has started one, so the reviewers are not guessed before it writes the plan.
        try:
            started = json.loads((workdir / "progress.json").read_text()).get("agents") or {}
        except (OSError, ValueError):
            started = {}
        self.planned = (self.dir / "plan.json").is_file() or f"{self.id}/plan" in started
        self.reviewers = reviewers_of(self.dir, self.planned)
        self.axes = [r["id"] for r in self.reviewers]

    def line_counts(self, paths) -> dict[str, int | None]:
        """Lines in each file: a changed file at the tip, anything else from the checkout at the tip (a file the stack
        deleted, at the base). One `git cat-file --batch` for every path the tip files do not cover; cached per stack."""
        todo = {}
        for p in set(paths) - set(self._lines):
            lines = self.tip_lines(p)
            if lines is not None:
                self._lines[p] = len(lines)
            else:
                todo[p] = f"{self.base if self.files.get(p, {}).get('status') == 'deleted' else self.tip}:{p}"
        if todo and self.checkout:
            r = subprocess.run(["git", "-C", self.checkout, "cat-file", "--batch"], capture_output=True,
                               input="".join(spec + "\n" for spec in todo.values()).encode())
            data, i = r.stdout if r.returncode == 0 else b"", 0
            for p in todo:
                nl = data.find(b"\n", i)
                if nl < 0:
                    break
                head, i = data[i:nl].split(b" "), nl + 1
                if len(head) != 3 or head[1] != b"blob":
                    self._lines[p] = None
                    continue
                body = data[i:i + int(head[2])]
                i += int(head[2]) + 1
                self._lines[p] = body.count(b"\n") + (1 if body and not body.endswith(b"\n") else 0)
        for p in todo:
            self._lines.setdefault(p, None)
        return {p: self._lines[p] for p in paths}

    def tip_lines(self, path) -> list[str] | None:
        f = self.dir / "tip" / path
        return f.read_text().splitlines() if path in self.files and f.is_file() else None

    def owner(self, path: str, line) -> tuple[int, int] | None:
        v = self.attr.get(path, {}).get(str(line))
        if not v or v[1] is None or v[1] not in self.hunks[v[0]].get(path, {}).get("commentable", []):
            return None
        return v[0], v[1]

    def route(self, path, line, start=None) -> tuple[int, int, int | None] | str:
        """(pr, line in that PR, start line in that PR) for a tip location, or the reason it cannot be posted."""
        if path not in self.files:
            return f"path {path!r} is not changed in this stack"
        own = self.owner(path, line)
        if own is None:
            written = sorted(int(k) for k in self.attr.get(path, {}))
            near = sorted(written, key=lambda k: abs(k - line) if isinstance(line, int) else k)[:4]
            return (f"line {line} of {path} was not written by this stack; use a line it wrote (nearby: {sorted(near)}) "
                    "or a general comment with path null and pr set")
        if start is None:
            return own[0], own[1], None
        span = [self.owner(path, i) for i in range(start, line + 1)] if isinstance(start, int) and start < line else []
        if not span or any(s is None or s[0] != own[0] for s in span) or [s[1] for s in span] != list(range(span[0][1], span[0][1] + len(span))):
            return f"start_line {start}..line {line} of {path} must be consecutive lines written by one PR"
        return own[0], own[1], span[0][1]


def read_plan(sdir: pathlib.Path) -> dict | None:
    try:
        plan = json.loads((sdir / "plan.json").read_text())
    except (OSError, ValueError):
        return None
    return plan if isinstance(plan, dict) and isinstance(plan.get("reviewers"), list) else None


def reviewers_of(sdir: pathlib.Path, planned: bool = False) -> list[dict]:
    """The stack's reviewers: its plan's, the three built-in ones when there is no plan, none while a plan is awaited."""
    plan = read_plan(sdir)
    if plan is None:
        return [] if planned else DEFAULT_REVIEWERS
    return [{"id": r["id"], "title": r.get("title") or r["id"], "hint": r.get("hint") or ""}
            for r in plan["reviewers"] if isinstance(r, dict) and isinstance(r.get("id"), str)]


def has_guide(st: Stack) -> bool:
    return (st.dir / "guide.json").is_file() or (st.dir / "guide.html").is_file() and (st.dir / "files.json").is_file()


def guide_parts(st: Stack) -> list[pathlib.Path]:
    """The sections the author has written so far, `guide/NN-<ref>.html`, in name order."""
    return sorted(p for p in (st.dir / "guide").glob("*.html") if p.is_file()) if (st.dir / "guide").is_dir() else []


def html_source(st: Stack) -> str | None:
    """The HTML guide as one fragment: guide.html, or the section files joined, while they are written and after."""
    if (st.dir / "guide.html").is_file():
        return (st.dir / "guide.html").read_text()
    parts = guide_parts(st)
    return "\n".join(p.read_text() for p in parts) if parts else None


def writing(st: Stack) -> bool:
    """Sections have landed but the author has not finished (files.json, its last file, is not there yet)."""
    return bool(guide_parts(st)) and not (st.dir / "files.json").is_file() and not (st.dir / "guide.html").is_file()


def split_key(stem: str, axes) -> tuple[str, str]:
    """`<reviewer>-<finding id>` as a verifier's file stem: reviewer ids and finding ids both hold dashes, so match the known ids."""
    for a in sorted(axes, key=len, reverse=True):
        if stem.startswith(a + "-"):
            return a, stem[len(a) + 1:]
    axis, _, fid = stem.partition("-")
    return axis, fid


def check_plan(st: Stack) -> list[str]:
    """plan.json, the director's file: what the stack's reviewers are asked to look for, and on which model."""
    errors: list[str] = []
    err = lambda m: errors.append(f"stack {st.id}: plan.json: {m}")  # noqa: E731
    try:
        plan = json.loads((st.dir / "plan.json").read_text())
    except FileNotFoundError:
        return [f"stack {st.id}: plan.json is missing"]
    except json.JSONDecodeError as e:
        return [f"stack {st.id}: plan.json is not valid JSON ({e})"]
    if not isinstance(plan, dict):
        return [f"stack {st.id}: plan.json must be an object"]
    check_text(err, "focus", plan.get("focus"), LIMITS["focus"])
    rs = plan.get("reviewers")
    if not isinstance(rs, list) or not 1 <= len(rs) <= MAX_REVIEWERS:
        return errors + [f"stack {st.id}: plan.json: reviewers: expected 1 to {MAX_REVIEWERS}"]
    seen = set()
    for i, r in enumerate(rs):
        r = r if isinstance(r, dict) else {}
        rid = r.get("id")
        label = f"reviewers[{i}] ({rid})"
        if not isinstance(rid, str) or not REVIEWER_ID.match(rid) or rid in RESERVED_IDS or rid in seen:
            err(f"{label}: id must be 2 to 28 lowercase letters, digits and dashes, start with a letter, be unique, and not be one of {sorted(RESERVED_IDS)}")
        seen.add(rid)
        check_text(err, f"{label}.title", r.get("title"), LIMITS["reviewer_title"])
        check_text(err, f"{label}.hint", r.get("hint"), LIMITS["reviewer_hint"])
        check_text(err, f"{label}.charge", r.get("charge"), LIMITS["charge"])
        if isinstance(r.get("charge"), str) and len(r["charge"]) < 40:
            err(f"{label}.charge: say what to hunt for, where, and what to leave alone (at least 40 characters)")
        if r.get("model") not in PLAN_MODELS:
            err(f"{label}.model must be one of {PLAN_MODELS}")
        if r.get("effort") not in PLAN_EFFORTS:
            err(f"{label}.effort must be one of {PLAN_EFFORTS}")
        elif st.effort != "deep" and r.get("model") == "opus" and r.get("effort") == "high":
            # Measured: opus at high effort ran 15+ minutes per reviewer; the whole balanced round should take about that.
            err(f"{label}: opus at high effort is for `deep` rounds only; at `{st.effort}` use opus at medium or sonnet at high")
        paths = r.get("paths")
        if paths is not None and (not isinstance(paths, list) or len(paths) > 12 or not all(isinstance(x, str) and 0 < len(x) <= 200 for x in paths)):
            err(f"{label}.paths is a list of up to 12 path globs, or null")
    return errors


def skeleton(st: Stack) -> dict:
    """What the page shows before the guide writer finishes: the tree and diffs, no map, steps or comments."""
    return {"pending": True, "summary": "", "key_facts": [], "components": None, "sections": [],
            "files": {p: {"role": "pending", "why": ""} for p in st.files}}


def check_text(err, label: str, value, limit: int) -> None:
    if not isinstance(value, str) or not value.strip():
        err(f"{label} is empty")
    elif len(value) > limit:
        err(f"{label} is {len(value)} characters; the limit is {limit}")


class GuideCheck:
    """What one guide declares across its sections: node ids (unique over the whole guide, so a step can point at a node
    of any diagram), the `at` references to look up in one batch, and how many arrows cite a line."""

    def __init__(self, st: Stack, err, strict: bool):
        self.st, self.err, self.strict = st, err, strict
        self.ids: set[str] = set()
        self.components: dict[str, dict] = {}
        self.refs: list[tuple[str, str]] = []
        self.arrows = self.uncited = 0

    def new_id(self, nid, label: str) -> None:
        if not isinstance(nid, str) or not nid or nid in self.ids:
            self.err(f"{label}: id {nid!r} missing or used twice; node ids are unique across the guide")
        self.ids.add(nid)

    def at(self, item: dict, label: str, arrow: bool = False) -> None:
        ref = item.get("at")
        if arrow:
            self.arrows += 1
            self.uncited += ref is None
        if ref is None:
            return
        if not isinstance(ref, str) or not AT.match(ref):
            self.err(f"{label}: at {ref!r} must be \"path:line\"")
        else:
            self.refs.append((label, ref))

    def check_refs(self) -> None:
        parsed = [(label, ref, AT.match(ref)) for label, ref in self.refs]
        counts = self.st.line_counts({m[1] for _, _, m in parsed})
        for label, ref, m in parsed:
            n, start, end = counts[m[1]], int(m[2]), int(m[3] or m[2])
            if n is None:
                self.err(f"{label}: at {ref!r}: {m[1]} is neither a changed file nor in the repository at the tip")
            elif not 1 <= start <= end <= n:
                self.err(f"{label}: at {ref!r} is outside {m[1]} (1..{n})")

    def size(self, n: int, what: str, label: str) -> None:
        if n < 1:
            self.err(f"{label}: no {what}")
        elif self.strict and n > MAX_DIAGRAM_NODES:
            self.err(f"{label}: {n} {what}; the limit is {MAX_DIAGRAM_NODES}. Split it into diagrams that each answer one question")


def check_stack(st: Stack, strict: bool = True) -> tuple[dict | None, list[str], str]:
    """Validates guide.json, and returns it with the errors and a note on uncited arrows. `strict` is what --check runs:
    it caps diagram size and fails when most arrows cite no line. A build without --check renders an earlier round's
    guide too, so it is lenient and upgrades the old `flow` and `map` sections."""
    errors: list[str] = []
    err = lambda m: errors.append(f"stack {st.id}: {m}")  # noqa: E731
    try:
        guide = json.loads((st.dir / "guide.json").read_text())
    except FileNotFoundError:
        return None, [f"stack {st.id}: guide.json is missing"], ""
    except json.JSONDecodeError as e:
        return None, [f"stack {st.id}: guide.json is not valid JSON ({e})"], ""
    if not strict:
        guide = upgrade_legacy(guide)

    check_text(err, "summary", guide.get("summary"), LIMITS["summary"])
    facts = guide.get("key_facts") or []
    if len(facts) > MAX_FACTS:
        err(f"key_facts: at most {MAX_FACTS}, got {len(facts)}")
    for i, f in enumerate(facts):
        check_text(err, f"key_facts[{i}].title", (f or {}).get("title"), LIMITS["fact_title"])
        check_text(err, f"key_facts[{i}].text", (f or {}).get("text"), LIMITS["fact_text"])

    check_files(err, guide.get("files") or {}, st)

    g = GuideCheck(st, err, strict)
    check_components(guide, g)
    sections = guide.get("sections") or []
    if not 1 <= len(sections) <= MAX_SECTIONS:
        err(f"sections: expected 1 to {MAX_SECTIONS}, got {len(sections)}")
    step_ids, step_nodes = set(), []
    for i, sec in enumerate(sections):
        check_section(sec if isinstance(sec, dict) else {}, f"sections[{i}]", g, step_ids, step_nodes)
    for slabel, nodes in step_nodes:
        if bad := [n for n in nodes if n not in g.ids]:
            err(f"{slabel}: nodes not declared in components or any diagram: {bad}")
    if strict:  # a rebuild of an earlier round must not depend on what the checkout holds now
        g.check_refs()
    note = f"{g.uncited} of {g.arrows} arrows cite no line" if g.uncited else ""
    if strict and g.uncited * 2 > g.arrows:
        err(f"{note}; give each arrow `at` with the line that makes the call, or drop the arrows you cannot place")
    return guide, errors, note


def check_files(err, files: dict, st: Stack) -> None:
    if missing := sorted(set(st.files) - set(files)):
        err(f"files: every changed file needs a role; missing {missing}")
    if extra := sorted(set(files) - set(st.files)):
        err(f"files: not changed in this stack: {extra}")
    for path, f in files.items():
        if (f or {}).get("role") not in ROLES:
            err(f"files[{path}].role must be one of {ROLES}")
        check_text(err, f"files[{path}].why", (f or {}).get("why"), LIMITS["file_why"])


def trim(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def html_refs(nodes, g: "GuideCheck", label: str) -> None:
    for x in guidehtml.walk(nodes):
        if "data-at" in x.attrs:
            g.at({"at": x.attrs["data-at"]}, label)


def check_html_guide(st: Stack, strict: bool = True) -> tuple[dict | None, list[str], str]:
    """guide.html and files.json, the guide writer's files, read into the shape the page renders: one `html` section per
    top-level <section>, and the consecutive `data-step` sections as one `steps` section. `strict` is --check: anything the
    sanitizer drops, a section without a heading, a step without lines to read and code references that do not resolve are
    errors. A build without --check only drops what it cannot use."""
    errors: list[str] = []
    err = lambda m: errors.append(f"stack {st.id}: {m}")  # noqa: E731
    raw = html_source(st)
    if raw is None:
        return None, [f"stack {st.id}: no guide sections yet (guide/NN-<ref>.html)"], ""
    if writing(st):
        files = {p: {"role": "pending", "why": ""} for p in st.files}  # the page shows what has landed; roles come with files.json
    else:
        try:
            files = json.loads((st.dir / "files.json").read_text())
        except FileNotFoundError:
            return None, [f"stack {st.id}: files.json is missing; write it last, once every section passes"], ""
        except json.JSONDecodeError as e:
            return None, [f"stack {st.id}: files.json is not valid JSON ({e})"], ""
        check_files(err, files if isinstance(files, dict) else {}, st)
    if len(raw) > MAX_HTML:
        err(f"guide.html is {len(raw)} characters; keep it under {MAX_HTML}")
    notes: set[str] = set()
    nodes = guidehtml.parse(raw, notes)
    if strict and notes:
        err("guide.html: " + "; ".join(sorted(notes)[:12]) + ". Use only the tags, attributes, classes and colors guide-schema.md lists")
    if strict:
        figures.check(nodes, st.dir, prod_lines(st), timing_required(st), not writing(st), err)
        # The author's own inline pictures; each artist's SVG is checked by its own --only figure:<ref>
        if over := guidehtml.overflowing_labels(nodes):
            err(f"guide.html: {len(over)} picture labels do not fit; widen the box or the viewBox, or shorten the label: " + "; ".join(over[:4]))
    figures.fill(nodes, st.dir)
    g = GuideCheck(st, err, strict)
    sections: list[dict] = []
    steps: list[dict] = []
    refs: set[str] = set()

    def flush() -> None:
        # Steps right after a narrative section are its "read now": the code that section just explained. A run of steps
        # with no section before it (an older guide's reading order at the end) keeps the old title.
        if steps:
            after = sections[-1] if sections and sections[-1]["type"] == "html" else None
            sections.append({"type": "steps", "title": f"Read now: {after['title']}" if after else "Read in this order",
                             "inline": bool(after), "steps": list(steps)})
            steps.clear()

    for n in nodes:
        if isinstance(n, str):
            if strict and n.strip():
                err(f"guide.html: text outside a <section>: {n.strip()[:60]!r}")
            continue
        if n.tag != "section":
            if strict:
                err(f"guide.html: a top-level <{n.tag}>; put every part of the guide inside a <section>")
            continue
        ref = n.attrs.get("data-ref") or f"part-{len(sections) + len(steps) + 1}"
        if strict and not n.attrs.get("data-ref"):
            err(f"guide.html: a <section> without data-ref (after {sorted(refs)[-1:] or 'the start'})")
        for x in guidehtml.walk([n]):
            r = x.attrs.get("data-ref")
            if r and r in refs and strict:
                err(f"guide.html: data-ref {r!r} is used twice; each is unique")
            if r:
                refs.add(r)
        html_refs([n], g, f"guide.html section {ref!r}")
        head = next((k for k in n.kids if isinstance(k, guidehtml.Node) and k.tag in ("h2", "h3")), None)
        title = trim(guidehtml.text(head) if head else ref.replace("-", " "), 120)
        body = guidehtml.render([k for k in n.kids if k is not head])
        if "data-step" in n.attrs:
            step = html_step(n, ref, title, body, st, err, strict)
            if step:
                steps.append(step)
            continue
        flush()
        if strict and (head is None or head.tag != "h2"):
            err(f"guide.html section {ref!r}: start it with an <h2> that names it")
        if len(body) > MAX_SECTION_HTML:
            err(f"guide.html section {ref!r} is {len(body)} characters; split it (limit {MAX_SECTION_HTML})")
        sections.append({"type": "html", "title": title, "ref": ref, "html": body})
    flush()
    done = not writing(st)
    if strict:
        if done and not any(s["type"] == "html" for s in sections):
            err("guide.html: no <section> with an <h2>")
        if done and (not sections or sections[0].get("ref") != "overview"):
            err("the guide starts with <section data-ref=\"overview\">")
        g.check_refs()
        if done and len(g.refs) < MIN_DATA_AT:
            err(f"guide.html cites {len(g.refs)} lines of code; tie each claim about code to its line with data-at")
    if strict:
        words, budget = guidehtml.prose_words(nodes), prose_budget(st)
        if words > budget:
            err(f"guide.html has {words} words of prose; the budget for this stack is {budget} (tables, pictures and code blocks do not count). "
                "Cut what retells code the reviewer reads next, what another section already says, and code outside the diff; "
                "turn a list of hops into a table or a picture")
        tables = sum(1 for x in guidehtml.walk(nodes) if x.tag == "table")
        if tables > MAX_TABLES:
            err(f"guide.html has {tables} tables; keep at most {MAX_TABLES}, for rows of like things (settings, outcomes, failures). "
                "Say the rest in a sentence, or let a picture carry it")
        long = [guidehtml.text(x)[:50] for x in guidehtml.walk(nodes) if x.tag in ("td", "th") and len(guidehtml.text(x).split()) > MAX_CELL_WORDS]
        if long:
            err(f"guide.html: {len(long)} table cells over {MAX_CELL_WORDS} words; a cell is a phrase or one sentence (first: {long[0]!r})")
    guide = {"format": "html", "summary": "", "key_facts": [], "components": None, "sections": sections, "files": files, "writing": writing(st),
             "drawing": figures.pending(nodes, st.dir)}
    return guide, errors, ""


def prod_lines(st: Stack) -> int:
    return sum(f["adds"] + f["dels"] for p, f in st.files.items()
               if not (TEST_PATH.search(p) or FIXTURE_PATH.search(p) or OMIT_PATH.search(p) or DOC_PATH.search(p)))


def timing_required(st: Stack) -> bool:
    try:
        return bool(json.loads((st.dir / "timing.json").read_text()).get("required"))
    except (OSError, ValueError):
        return False  # a round fetched before the timing scan


def prose_budget(st: Stack) -> int:
    return int(min(PROSE_CEILING, max(PROSE_FLOOR, prod_lines(st) * PROSE_PER_LINE)))


def html_step(n, ref: str, title: str, body: str, st: Stack, err, strict: bool) -> dict | None:
    """A `<section data-step>`: what to read (its data-at, at most MAX_ANCHOR_LINES lines of a changed file) and why."""
    m = AT.match(n.attrs.get("data-at") or "")
    if not m:
        if strict:
            err(f"guide.html step {ref!r}: data-at=\"path:start-end\" names the lines it asks the reviewer to read")
        return None
    start = int(m[2])
    end = int(m[3] or m[2])
    if not strict:
        end = min(end, start + MAX_ANCHOR_LINES - 1)
    anchor = {"path": m[1], "start": start, "end": end}
    bad: list[str] = []
    check_anchor(anchor, f"guide.html step {ref!r}", st, bad.append)
    if bad:
        if strict:
            for b in bad:
                err(b)
        return None
    text = guidehtml.text(guidehtml.parse(body))
    return {"id": ref, "kind": "decide" if "data-decide" in n.attrs else "read", "title": trim(title, 90),
            "why": trim(text, LIMITS["step_why"]), "html": body, "nodes": [], "test": False, "anchor": anchor}


def upgrade_legacy(guide: dict) -> dict:
    """Guides written before the diagram kinds: a `flow` renders as a sequence and a `map` as a structure diagram, so an
    earlier round still builds. --check accepts neither."""
    comps, out = guide.get("components") or {}, []
    for sec in guide.get("sections") or []:
        if isinstance(sec, dict) and sec.get("type") == "flow":
            hops = sec.get("steps") or []
            parts = list(dict.fromkeys(i for h in hops for i in (h.get("from"), h.get("to"))))
            sec = {"type": "diagram", "kind": "sequence", "title": sec.get("title"), "participants": parts,
                   "messages": [{k: h[k] for k in ("from", "to", "label", "new") if k in h} for h in hops]}
        elif isinstance(sec, dict) and sec.get("type") == "map":
            sec = {"type": "diagram", "kind": "structure", "title": sec.get("title"),
                   "nodes": [n.get("id") for n in comps.get("nodes") or []], "edges": comps.get("edges") or []}
        out.append(sec)
    return {**guide, "sections": out}


def check_anchor(anchor: dict, label: str, st: Stack, err) -> None:
    lines = st.tip_lines(anchor.get("path"))
    start, end = anchor.get("start"), anchor.get("end")
    if lines is None:
        err(f"{label}: anchor.path {anchor.get('path')!r} is not a changed text file at the tip")
    elif not (isinstance(start, int) and isinstance(end, int) and 1 <= start <= end <= len(lines)):
        err(f"{label}: anchor {start}..{end} is outside {anchor['path']} (1..{len(lines)})")
    elif end - start + 1 > MAX_ANCHOR_LINES:
        err(f"{label}: anchor spans {end - start + 1} lines; the limit is {MAX_ANCHOR_LINES}")


def check_components(guide: dict, g: GuideCheck) -> None:
    """The code units, shared by `sequence` participants and `structure` nodes, each in a declared layer."""
    m, err = guide.get("components"), g.err
    if m is None:
        return
    layers = {l.get("id") for l in m.get("layers") or []}
    for l in m.get("layers") or []:
        check_text(err, f"components layer {l.get('id')!r} label", l.get("label"), LIMITS["label"])
    nodes = m.get("nodes") or []
    if not 1 <= len(nodes) <= MAX_COMPONENTS:
        err(f"components: expected 1 to {MAX_COMPONENTS} nodes, got {len(nodes)}")
    for node in nodes:
        nid = node.get("id")
        g.new_id(nid, f"component {nid!r}")
        g.components[nid] = node
        check_text(err, f"component {nid!r} label", node.get("label"), LIMITS["label"])
        if node.get("role") is not None:
            check_text(err, f"component {nid!r} role", node["role"], LIMITS["role"])
        if node.get("layer") not in layers:
            err(f"component {nid!r}: layer {node.get('layer')!r} is not declared")
        if node.get("state") not in NODE_STATES:
            err(f"component {nid!r}: state must be one of {NODE_STATES}")
        if node.get("state") in ("new", "changed") and node.get("path") not in g.st.files:
            err(f"component {nid!r}: a {node['state']} node needs path set to the changed file that holds it")
        elif node.get("state") == "removed" and node.get("path") is not None and node["path"] not in g.st.files:
            err(f"component {nid!r}: a removed node's path is the file the stack changed or deleted, or null")
        g.at(node, f"component {nid!r}")


def check_section(sec: dict, label: str, g: GuideCheck, step_ids: set[str], step_nodes: list) -> None:
    kind, err, st = sec.get("type"), g.err, g.st
    if kind not in SECTION_TYPES:
        return err(f"{label}.type must be one of {SECTION_TYPES}")
    if kind == "diagram":
        if sec.get("kind") not in DIAGRAM_KINDS:
            return err(f"{label}.kind must be one of {DIAGRAM_KINDS}")
        kind = sec["kind"]
    label = f"{label} ({kind})"
    check_text(err, f"{label}.title", sec.get("title"), 120 if kind == "html" else LIMITS["section_title"])
    if kind == "html":
        check_html_part(sec.get("html"), label, g, MAX_SECTION_HTML)
    elif kind == "sequence":
        check_sequence(sec, label, g)
    elif kind in ("flowchart", "states", "structure", "data"):
        check_graph(sec, kind, label, g)
    elif kind == "risks":
        items = sec.get("items") or []
        if not 1 <= len(items) <= MAX_RISKS:
            err(f"{label}: expected 1 to {MAX_RISKS} items, got {len(items)}")
        for j, r in enumerate(items):
            r = r if isinstance(r, dict) else {}
            check_text(err, f"{label}.items[{j}].title", r.get("title"), LIMITS["risk_title"])
            check_text(err, f"{label}.items[{j}].text", r.get("text"), LIMITS["risk_text"])
            if r.get("status") not in RISK_STATUSES:
                err(f"{label}.items[{j}].status must be one of {RISK_STATUSES}")
            g.at(r, f"{label}.items[{j}]")
    elif kind == "before_after":
        rows = sec.get("rows") or []
        if not 1 <= len(rows) <= MAX_ROWS:
            err(f"{label}: expected 1 to {MAX_ROWS} rows, got {len(rows)}")
        for j, r in enumerate(rows):
            for k in ("aspect", "before", "after"):
                check_text(err, f"{label}.rows[{j}].{k}", (r or {}).get(k), LIMITS["cell"])
    elif kind == "table":
        cols, rows = sec.get("columns") or [], sec.get("rows") or []
        if not 2 <= len(cols) <= MAX_COLUMNS:
            err(f"{label}: expected 2 to {MAX_COLUMNS} columns, got {len(cols)}")
        if not 1 <= len(rows) <= MAX_ROWS:
            err(f"{label}: expected 1 to {MAX_ROWS} rows, got {len(rows)}")
        for j, r in enumerate(rows):
            if not isinstance(r, list) or len(r) != len(cols):
                err(f"{label}.rows[{j}]: expected {len(cols)} cells")
                continue
            for c in r:
                if not isinstance(c, str) or len(c) > LIMITS["cell"]:
                    err(f"{label}.rows[{j}]: each cell is text of at most {LIMITS['cell']} characters")
    elif kind == "list":
        items = sec.get("items") or []
        if not 1 <= len(items) <= MAX_ITEMS:
            err(f"{label}: expected 1 to {MAX_ITEMS} items, got {len(items)}")
        for j, it in enumerate(items):
            check_text(err, f"{label}.items[{j}]", it, LIMITS["item"])
    elif kind == "steps":
        steps = sec.get("steps") or []
        if not 1 <= len(steps) <= MAX_STEPS:
            err(f"{label}: expected 1 to {MAX_STEPS} steps, got {len(steps)}")
        for s in steps:
            sid = s.get("id")
            slabel = f"step {sid!r}"
            if not isinstance(sid, str) or not sid or sid in step_ids:
                err(f"{slabel}: id missing or duplicated")
            step_ids.add(sid)
            if s.get("kind") not in KINDS:
                err(f"{slabel}: kind must be one of {KINDS}")
            check_text(err, f"{slabel}.title", s.get("title"), 90 if "html" in s else LIMITS["step_title"])
            if "html" in s:  # a step of an HTML guide: its text is HTML, and `why` is derived from it
                check_html_part(s["html"], slabel, g, MAX_SECTION_HTML)
            else:
                check_text(err, f"{slabel}.why", s.get("why"), LIMITS["step_why"])
            if s.get("test") is not None:
                check_text(err, f"{slabel}.test", s["test"], LIMITS["test"])
            step_nodes.append((slabel, s.get("nodes") or []))
            check_anchor(s.get("anchor") or {}, slabel, st, err)


def check_html_part(h, label: str, g: GuideCheck, limit: int) -> None:
    """A piece of a guide written as HTML (a section, a step, a guide fix): what the sanitizer would drop is an error."""
    if not isinstance(h, str) or not h.strip():
        return g.err(f"{label}.html is empty")
    if len(h) > limit:
        return g.err(f"{label}.html is {len(h)} characters; the limit is {limit}")
    notes: set[str] = set()
    nodes = guidehtml.parse(h, notes)
    if notes:
        g.err(f"{label}.html: " + "; ".join(sorted(notes)[:6]))
    html_refs(nodes, g, label)


def check_sequence(sec: dict, label: str, g: GuideCheck) -> None:
    """Participants are component ids. `messages` holds calls, returns, notes and alt/opt/loop frames, in order."""
    err, parts = g.err, sec.get("participants") or []
    g.size(len(parts), "participants", label)
    if bad := [p for p in parts if p not in g.components]:
        err(f"{label}: participants must be component ids; not found: {bad}")
    if len(set(parts)) != len(parts):
        err(f"{label}: a participant is listed twice")
    count = 0

    def walk(items, depth: int, where: str) -> None:
        nonlocal count
        if not isinstance(items, list) or not items:
            return err(f"{where}: needs at least one message")
        for j, it in enumerate(items):
            it, at = it if isinstance(it, dict) else {}, f"{where}[{j}]"
            if "frame" in it:
                f, branches = it["frame"], it.get("branches") or []
                if f not in FRAMES:
                    err(f"{at}.frame must be one of {FRAMES}")
                if depth >= MAX_FRAME_DEPTH:
                    err(f"{at}: frames nest {MAX_FRAME_DEPTH} deep at most")
                lo, hi = (2, MAX_BRANCHES) if f == "alt" else (1, 1)
                if not lo <= len(branches) <= hi:
                    err(f"{at}: an `alt` frame needs 2 to {MAX_BRANCHES} branches, one per outcome, and `opt` or `loop` exactly one; got {len(branches)}")
                for k, b in enumerate(branches):
                    b = b if isinstance(b, dict) else {}
                    check_text(err, f"{at}.branches[{k}].guard", b.get("guard"), LIMITS["label"])
                    walk(b.get("messages"), depth + 1, f"{at}.branches[{k}].messages")
                continue
            count += 1
            if "note" in it:
                check_text(err, f"{at}.note", it["note"], LIMITS["note"])
                if it.get("on") not in parts:
                    err(f"{at}: a note sits `on` one of the participants")
                continue
            if it.get("from") not in parts or it.get("to") not in parts:
                err(f"{at}: {it.get('from')!r}->{it.get('to')!r} must be participants")
            check_text(err, f"{at}.label", it.get("label"), LIMITS["label"])
            g.at(it, at, arrow=not it.get("return"))

    walk(sec.get("messages"), 0, f"{label}.messages")
    if count > MAX_MESSAGES:
        err(f"{label}: {count} messages and notes; the limit is {MAX_MESSAGES}. Split the diagram")


def check_graph(sec: dict, kind: str, label: str, g: GuideCheck) -> None:
    """flowchart, states and data declare their own nodes; structure lists component ids. Edges join nodes of the same diagram."""
    err, nodes = g.err, sec.get("nodes") or []
    g.size(len(nodes), "nodes", label)
    if sec.get("direction", "TB") not in ("TB", "LR"):
        err(f"{label}.direction must be TB or LR")
    if kind == "structure":
        if bad := [n for n in nodes if n not in g.components]:
            err(f"{label}: nodes must be component ids; not found: {bad}")
        ids = set(nodes)
    else:
        ids = set()
        for n in nodes:
            n = n if isinstance(n, dict) else {}
            nid, nl = n.get("id"), f"{label} node {n.get('id')!r}"
            g.new_id(nid, nl)
            ids.add(nid)
            check_text(err, f"{nl}.label", n.get("label"), LIMITS["label"])
            if n.get("state", "existing") not in NODE_STATES:
                err(f"{nl}.state must be one of {NODE_STATES}")
            if kind == "flowchart" and n.get("shape", "step") not in SHAPES:
                err(f"{nl}.shape must be one of {SHAPES}")
            if kind == "states" and n.get("initial") and n.get("terminal"):
                err(f"{nl}: a state is initial or terminal, not both")
            if kind == "data":
                fields = n.get("fields") or []
                if not 1 <= len(fields) <= MAX_FIELDS:
                    err(f"{nl}: expected 1 to {MAX_FIELDS} fields, got {len(fields)}")
                for k, fd in enumerate(fields):
                    fd = fd if isinstance(fd, dict) else {}
                    check_text(err, f"{nl}.fields[{k}].name", fd.get("name"), LIMITS["label"])
                    if fd.get("type") is not None:
                        check_text(err, f"{nl}.fields[{k}].type", fd["type"], LIMITS["field_type"])
                    if fd.get("change") not in (None, *FIELD_CHANGES):
                        err(f"{nl}.fields[{k}].change must be one of {FIELD_CHANGES} or left out")
            g.at(n, nl)
    if kind == "states" and len([n for n in nodes if isinstance(n, dict) and n.get("initial")]) != 1:
        err(f"{label}: exactly one state is `initial`")
    edges = sec.get("edges") or []
    if len(edges) > MAX_EDGES:
        err(f"{label}: {len(edges)} edges; the limit is {MAX_EDGES}")
    for j, e in enumerate(edges):
        e, el = e if isinstance(e, dict) else {}, f"{label}.edges[{j}]"
        if e.get("from") not in ids or e.get("to") not in ids:
            err(f"{el}: {e.get('from')!r}->{e.get('to')!r} must join nodes of this diagram")
        for k in ("label", "guard"):
            if e.get(k) is not None:
                check_text(err, f"{el}.{k}", e[k], LIMITS["label"])
        if kind == "states" and not e.get("label"):
            err(f"{el}: a transition needs `label`, the event that fires it")
        g.at(e, el, arrow=True)


def check_findings(st: Stack, axis: str) -> tuple[dict | None, list[str]]:
    """One reviewer's file: findings on lines the stack wrote, at most MAX_PER_PR_AXIS per PR."""
    errors: list[str] = []
    err = lambda m: errors.append(f"stack {st.id} {axis}: {m}")  # noqa: E731
    try:
        report = json.loads((st.dir / f"review-{axis}.json").read_text())
    except FileNotFoundError:
        return None, [f"stack {st.id}: review-{axis}.json is missing"]
    except json.JSONDecodeError as e:
        return None, [f"stack {st.id}: review-{axis}.json is not valid JSON ({e})"]
    if report.get("skipped") is not None:
        check_text(err, "skipped", report["skipped"], LIMITS["skipped"])
    ids, routed = set(), {n: 0 for n in st.prs}
    for c in report.get("findings") or []:
        cid = c.get("id")
        clabel = f"finding {cid!r}"
        if not isinstance(cid, str) or not cid or cid in ids:
            err(f"{clabel}: id missing or duplicated")
        ids.add(cid)
        if c.get("severity") not in SEVERITIES:
            err(f"{clabel}: severity must be one of {SEVERITIES}")
        check_text(err, f"{clabel}.title", c.get("title"), LIMITS["comment_title"])
        check_text(err, f"{clabel}.body", c.get("body"), LIMITS["comment_body"])
        if not isinstance(c.get("evidence"), list) or not c["evidence"]:
            err(f"{clabel}: evidence must list the path:line references the claim rests on")
        if c.get("path") is None:
            if c.get("pr") not in st.prs:
                err(f"{clabel}: a general comment needs pr set to one of {st.prs}")
            else:
                routed[c["pr"]] += 1
            continue
        r = st.route(c["path"], c.get("line"), c.get("start_line"))
        if isinstance(r, str):
            err(f"{clabel}: {r}")
        else:
            routed[r[0]] += 1
    for n, count in routed.items():
        if count > MAX_PER_PR_AXIS:
            err(f"#{n} receives {count} findings; keep the {MAX_PER_PR_AXIS} that matter most")
    return report, errors


def verify_results(sdir: pathlib.Path) -> dict[str, dict]:
    """The verifiers' files, one per finding: `verify/<axis>-<id>.json`. A file still being written counts as missing."""
    out = {}
    for f in (sdir / "verify").glob("*.json"):
        try:
            out[f.stem] = json.loads(f.read_text())
        except ValueError:
            pass
    return out


def stages(w: pathlib.Path) -> list[dict]:
    """Per stack, where each agent stands: from the files the agents write, and progress.json when the runner wrote it.
    A stage's `secs` is how long its agent ran (or has run so far); the stack's `secs` counts from the runner's start."""
    run = json.loads((w / "run.json").read_text())
    try:
        progress = json.loads((w / "progress.json").read_text())
    except (OSError, ValueError):
        progress = {}
    agents = progress.get("agents") or {}
    now = time.time()
    t0 = progress.get("started") or run.get("fetched_at") or now
    out = []
    for s in run["stacks"]:
        d, sid = w / s["id"], s["id"]
        planned = (d / "plan.json").is_file() or f"{sid}/plan" in agents
        axes = reviewers_of(d, planned)

        def stage(key: str, label: str, path: pathlib.Path) -> dict:
            a = agents.get(f"{sid}/{key}") or {}
            start = a.get("started") or t0
            if path.is_file():
                res = {"key": key, "label": label, "state": "done", "secs": round((a.get("ended") or path.stat().st_mtime) - start)}
                if key == "plan":
                    res["picked"] = len(axes)
                elif key != "guide" and not key.startswith("figure/"):
                    try:
                        report = json.loads(path.read_text())
                    except ValueError:
                        return {"key": key, "label": label, "state": "running", "secs": round(now - start)}
                    if report.get("skipped"):
                        res.update(state="skipped", note=report["skipped"])
                    else:
                        res["found"] = len(report.get("findings") or [])
                return res
            state = a.get("state") or ("running" if not agents else "waiting")
            secs = round((a.get("ended") or now) - start) if state != "waiting" else None
            return {"key": key, "label": label, "state": state, "secs": secs, "calls": a.get("calls", 0), "last": a.get("last", ""), "doing": a.get("doing", "")}

        # The HTML guide is done when its author writes files.json, last; the structured one when guide.json lands.
        guide_file = d / ("guide.json" if (d / "guide.json").is_file() or not planned else "files.json")
        row = [stage("plan", "Choosing reviewers", d / "plan.json")] if planned else []
        row += [stage("guide", "Guide", guide_file)]
        # One row per picture the author asked for, drawn by its own artist.
        row += [{**stage(f"figure/{ref}", spec.get("kind") or ref, d / "figures" / f"{ref}.svg"), "question": spec.get("question") or ""}
                for ref, spec in figures.specs(d).items()]
        row += [stage(r["id"], r["title"], d / f"review-{r['id']}.json") for r in axes]
        reviews = [x for x in row if x["key"] not in ("guide", "plan") and not x["key"].startswith("figure/")]
        total = None
        if (not planned or (d / "plan.json").is_file()) and all(x["state"] in ("done", "skipped", "failed") for x in reviews):
            total = sum(x.get("found", 0) for x in reviews)
        checked = verify_results(d)
        verifiers = [a for k, a in agents.items() if k.startswith(f"{sid}/verify/")]
        running = [a for a in verifiers if a.get("state") == "running"]
        vstart = min((a["started"] for a in verifiers if a.get("started")), default=t0)
        if total is None:
            check = {"state": "waiting"}
        elif len(checked) >= total:
            last = max((f.stat().st_mtime for f in (d / "verify").glob("*.json")), default=vstart)
            check = {"state": "done", "secs": round(last - vstart)}
        else:
            latest = max(running, key=lambda a: a.get("started", 0), default={})
            check = {"state": "running", "secs": round(now - vstart), "calls": sum(a.get("calls", 0) for a in running), "last": latest.get("last", "")}
        row.append({"key": "check", "label": "Fact check", "checked": len(checked), "total": total, **check})
        out.append({"id": sid, "secs": round(now - t0), "stages": row})
    return out


def collect_findings(st: Stack, reports: dict[str, dict]) -> tuple[list[dict], dict[str, str], list[str]]:
    """Every reviewer's findings with the verifier's status. Duplicates the verifier names are dropped."""
    warnings, skipped = [], {}
    results = verify_results(st.dir)
    out = []
    for axis in st.axes:
        report = reports.get(axis)
        if report is None:
            continue
        if report.get("skipped"):
            skipped[axis] = report["skipped"]
        for c in report.get("findings") or []:
            c = {**c, "axis": axis, "key": f"{axis}/{c['id']}"}
            r = results.get(f"{axis}-{c['id']}")
            if r is None:
                c["status"], c["note"] = "checking", "The verifier is still checking this comment."
                out.append(c)
                continue
            if r.get("status") == "duplicate":
                continue
            c["status"] = r.get("status") if r.get("status") in STATUSES else "unverified"
            c["note"] = r.get("note") or ("" if r else "The verifier did not check this comment.")
            if c.get("path") and r.get("line") and r["line"] != c.get("line"):
                if isinstance(st.route(c["path"], r["line"]), tuple):
                    c["line"] = r["line"]
                    c.pop("start_line", None)
                else:
                    warnings.append(f"stack {st.id}: verifier moved {c['key']} to {r['line']}, a line the stack did not write; kept {c.get('line')}")
            out.append(c)
    return out, skipped, warnings


def diff_anchor(path: str) -> str:
    return "diff-" + hashlib.sha256(path.encode()).hexdigest()


def render_stack(st: Stack, repo: str, guide: dict, findings: list[dict], skipped: dict[str, str], reviewing: list[str], local: bool = False) -> dict:
    # A self-review's commits may exist only on this machine: no GitHub link points at them.
    compare = None if local else f"https://github.com/{repo}/compare/{st.base}...{st.tip}"

    def located(path: str, line: int) -> dict:
        own = st.owner(path, line)
        return {"pr": own[0], "pl": own[1]} if own else {}

    def line_rows(path: str, start: int, end: int) -> list[dict]:
        text = st.tip_lines(path)
        added = set(st.target.get(path, {}).get("added", []))
        return [{"n": i, "t": text[i - 1], "k": "+" if i in added else " ", **located(path, i)} for i in range(start, end + 1)]

    comments = []
    for c in findings:
        out = {k: c.get(k) for k in ("severity", "title", "body", "evidence", "path", "line", "start_line", "status", "note", "axis")}
        out["id"] = f"{st.id}-{c['axis']}-{c['id']}"
        if c.get("path"):
            pr, pl, ps = st.route(c["path"], c["line"], c.get("start_line"))
            info = st.pr_info[pr]
            out |= {"pr": pr, "pl": pl, "ps": ps,
                    "link": None if local else f"https://github.com/{repo}/blob/{info['review_sha']}/{c['path']}#L{ps or pl}" + (f"-L{pl}" if ps else "")}
        else:
            out |= {"pr": c["pr"], "pl": None, "ps": None, "link": None}
        comments.append(out)

    sections = []
    for i, sec in enumerate(guide["sections"]):
        sec = dict(sec)
        if sec["type"] == "steps":
            steps = []
            for s in sec["steps"]:
                a = s["anchor"]
                steps.append({
                    "id": f"{st.id}-{s['id']}", "kind": s["kind"], "title": s["title"], "why": s["why"], "nodes": s.get("nodes") or [],
                    "test": s.get("test", False), "path": a["path"], "start": a["start"], "end": a["end"],
                    "lines": line_rows(a["path"], a["start"], a["end"]),
                    "link": compare and f"{compare}#{diff_anchor(a['path'])}R{a['start']}-R{a['end']}",
                    **({"html": guidehtml.clean(s["html"])} if "html" in s else {}),
                })
            sec["steps"] = steps
        elif sec["type"] == "html":
            sec["html"] = guidehtml.clean(sec["html"])  # again at render: a guide fix or an old round never reaches the page unsanitized
        sections.append(sec)

    files = []
    for path, f in st.files.items():
        hunks = [[{**line, **(located(path, line["n"]) if line["k"] == "+" else {})} for line in hunk] for hunk in st.target.get(path, {}).get("hunks", [])]
        g = guide["files"][path]
        files.append({**f, "role": g["role"], "why": g["why"], "hunks": hunks,
                      "link": compare and f"{compare}#{diff_anchor(path)}",
                      "pr_links": {n: st.pr_info[n]["url"] and f"{st.pr_info[n]['url']}/files#{diff_anchor(path)}" for n in f["prs"]}})

    prs = [{**{k: st.pr_info[n][k] for k in ("n", "url", "title", "author", "sha", "review_sha", "draft", "checks")},
            **{k: st.pr_info[n].get(k) for k in ("last_review", "label", "pr_number", "pushed", "head")}} for n in st.prs]
    plan = read_plan(st.dir)
    return {"id": st.id, "pending": bool(guide.get("pending")), "reviewing": reviewing, "skipped": skipped,
            "reviewers": st.reviewers, "planning": st.planned and plan is None, "focus": (plan or {}).get("focus"), "format": guide.get("format", "json"),
            "writing": bool(guide.get("writing")), "drawing": guide.get("drawing") or [],
            "base": st.base, "tip": st.tip, "since": st.since, "compare": compare, "summary": guide["summary"],
            "key_facts": guide.get("key_facts") or [], "components": guide.get("components"), "sections": sections,
            "prs": prs, "files": files, "comments": comments}


def atomic(path: pathlib.Path, text: str) -> None:
    """The runner rebuilds from several threads while the server reads: never leave a half-written file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir", type=pathlib.Path)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--stack")
    ap.add_argument("--only", help="guide, plan, figure:<ref>, or a reviewer id of the stack")
    ap.add_argument("--partial", action="store_true", help="render stacks without a guide as file tree and diffs only")
    args = ap.parse_args()
    run = json.loads((args.workdir / "run.json").read_text())
    stacks = [Stack(args.workdir, run, s) for s in run["stacks"] if args.stack in (None, s["id"])]
    if not stacks:
        sys.exit(f"no stack {args.stack!r}; stacks are {[s['id'] for s in run['stacks']]}")

    errors, guides, reports, notes = [], {}, {}, []
    for st in stacks:
        reports[st.id] = {}
        if args.only == "plan":
            errors += check_plan(st)
            continue
        if args.only and args.only.startswith("figure:"):
            ref = args.only.split(":", 1)[1]
            errs: list[str] = []
            figures.check_one(st.dir, ref, lambda m: errs.append(f"stack {st.id}: {m}"))
            # The picture's code links resolve like the guide's.
            g = GuideCheck(st, errs.append, True)
            if picture := figures.svg_of(st.dir, ref):
                html_refs(picture, g, f"figures/{ref}.svg")
                g.check_refs()
            errors += errs
            continue
        if args.only not in (None, "guide") and args.only not in st.axes:
            errors.append(f"stack {st.id}: no reviewer {args.only!r}; this stack's reviewers are {st.axes}")
            continue
        for axis in st.axes:
            if args.only in (None, axis) and (args.only == axis or (st.dir / f"review-{axis}.json").is_file()):
                reports[st.id][axis], errs = check_findings(st, axis)
                errors += errs
        # Reviewers check only their own file; a plain --check or --partial accepts a guide that is not written yet.
        if args.only not in (None, "guide") or args.only is None and (args.partial or args.check) and not has_guide(st) and not guide_parts(st):
            guides[st.id] = skeleton(st)
            continue
        html = (st.dir / "guide.html").is_file() or bool(guide_parts(st))
        if args.only is None and (args.partial or args.check) and html and writing(st) and not args.partial:
            guides[st.id] = skeleton(st)  # a plain --check while the author is still writing: its own --only guide checks it
            continue
        guide, errs, note = (check_html_guide if html else check_stack)(st, strict=args.check)
        errors += errs
        guides[st.id] = guide
        if note:
            notes.append(f"stack {st.id}: {note}")
    if errors:
        print("\n".join(errors))
        sys.exit(1)
    if args.check:
        print("\n".join(["ok", *notes]))
        return
    if args.only:
        sys.exit("--only goes with --check")

    warnings, rendered = [], []
    for st in stacks:
        findings, skipped, warns = collect_findings(st, reports[st.id])
        warnings += warns
        reviewing = [a for a in st.axes if a not in reports[st.id]]
        rendered.append(render_stack(st, run["repo"], guides[st.id], findings, skipped, reviewing, run.get("mode") == "self"))
    data = {"repo": run["repo"], "viewer": run["viewer"], "mode": run.get("mode", "pr"), "source": run.get("source"), "effort": run.get("effort", "balanced"), "run_id": run["run_id"], "warnings": run.get("warnings") or [],
            "stacks": rendered}
    atomic(args.workdir / "guide.json", json.dumps(data, indent=1))
    embedded = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    atomic(args.workdir / "index.html", TEMPLATE.read_text().replace("/*DATA*/null", embedded))
    for w in warnings:
        print("warning:", w)
    for s in data["stacks"]:
        by_axis = {r["id"]: sum(c["axis"] == r["id"] for c in s["comments"]) for r in s["reviewers"]}
        by_status = {k: sum(c["status"] == k for c in s["comments"]) for k in STATUSES}
        print(f"stack {s['id']}: {len(s['sections'])} sections, findings {by_axis}, {by_status}")
    for n in notes:
        print("note:", n)
    print(args.workdir / "index.html")


if __name__ == "__main__":
    main()
