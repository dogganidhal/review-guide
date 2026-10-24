"""Sharing a guide as one file, and taking one in.

`export` turns a built round into a single .html file: the page with its data, fonts and highlight.js inlined (no request
when it opens), plus an import bundle (a gzip and base64 copy of the round's source files) that another review-guide server
rebuilds the round from. `scan` is the secret and personal data reminder shown before saving. `read_bundle` and `stage` are
the receiving end: they check a bundle before anything is written under a review. `resolve_checkout` finds or fetches the
repository for an imported round.

Nothing here posts to GitHub. Only `resolve_checkout` talks to the network, through `gh` and `git`.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import html
import json
import pathlib
import re
import subprocess
import sys
import zlib

HERE = pathlib.Path(__file__).resolve().parent
UI = HERE.parent / "ui"
VENDOR = UI / "vendor"
TEMPLATE = UI / "template.html"
BUILD = HERE / "build.py"
sys.path.insert(0, str(HERE))
from build import reviewers_of, split_key  # noqa: E402

VERSION = 1
BUNDLE_CAP = 25_000_000      # the base64 bundle a server accepts
INFLATED_CAP = 120_000_000   # what it may expand to
MAX_FILES = 5000

REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
SID = r"\d+(?:-\d+)*"
# The only files a bundle may hold, relative to the round's work directory.
ALLOWED = re.compile(rf"^(?:run\.json|{SID}/(?:guide|attribution|plan|files)\.json|{SID}/guide\.html|{SID}/target\.diff|{SID}/review-[a-z][a-z0-9-]{{0,26}}[a-z0-9]\.json"
                     rf"|{SID}/verify/[\w.-]+\.json|{SID}/guide/\d\d-[a-z0-9_-]+\.html|{SID}/figures/[a-z0-9_-]+\.(?:json|svg)|{SID}/timing\.json"
                     rf"|{SID}/tip/.+|\d+/hunks\.json|threads/[\w-]+\.json)$")

# (kind, rule, pattern): about fifteen, tuned for few false alarms. A reminder, not a guarantee.
RULES = [
    ("secret", "private key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("secret", "AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("secret", "GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("secret", "JWT", re.compile(r"\beyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]{10,}")),
    ("secret", "Slack token", re.compile(r"\bxox[abprs]-[\w-]{10,}")),
    ("secret", "Google API key", re.compile(r"\bAIza[\w-]{35}\b")),
    ("secret", "Stripe key", re.compile(r"\b[sr]k_live_[0-9A-Za-z]{16,}")),
    ("secret", "bearer token", re.compile(r"(?i)\bauthorization\s*[:=]\s*[\"']?bearer\s+[\w.~+/=-]{16,}")),
    ("secret", "credentials in a URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@\"'<>]+:[^\s@/\"'<>${}]{3,}@[\w.-]+")),
    ("secret", "password in config", re.compile(r"(?i)\b(?:password|passwd|pwd)\b[\"']?\s*[:=]\s*[\"']?(?![\"'$<{(]|null\b|none\b|true\b|false\b|changeme|example|test|dummy|xxx)[^\s\"',;)]{6,}")),
    ("secret", "secret or key in config", re.compile(r"(?i)\b(?:secret|api[_-]?key|access[_-]?token|auth[_-]?token)[\"']?\s*[:=]\s*[\"'](?![$<{])[\w./+=-]{16,}[\"']")),
    ("personal", "email address", re.compile(r"\b(?!git@)[\w.+-]+@(?!example\.|test\.|localhost|users\.noreply)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?!(?:ya?ml|json|xml|java|kts?|tsx?|jsx?|mjs|md|py|rb|go|rs|sql|properties|txt|html?|css|svg|toml|ini|sh|lock|class|jar)\b)[A-Za-z]{2,}\b")),
    ("personal", "French phone number", re.compile(r"(?<![\w.])(?:(?:\+|00)33[\s.-]?|0)[1-9](?:[\s.-]?\d{2}){4}(?![\w.])")),
    ("personal", "French social security number (NIR)", re.compile(r"(?<!\d)[12][\s.]?\d{2}[\s.]?(?:0[1-9]|1[0-2])[\s.]?(?:\d{2}|2[AB])[\s.]?\d{3}[\s.]?\d{3}(?:[\s.]?\d{2})?(?!\d)")),
]
MAX_HITS = 100


class ShareError(ValueError):
    pass


def _json(path: pathlib.Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def embed(obj) -> str:
    """JSON safe to put inside a <script> element."""
    return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c")


def slug(s: str) -> str:
    return re.sub(r"[^\w.-]+", "-", s).strip("-")[:60]


# ---- the secret and personal data check ---------------------------------------------------------------------------------

def mask(kind: str, text: str) -> str:
    """The match, cut short, so the dialog does not repeat the secret."""
    if kind == "personal" and "@" in text:
        return text[0] + "…@" + text.split("@", 1)[1]
    return text[:4] + "…"


def scan_text(path: str, text: str, hits: list, seen: set, removed: bool = False) -> None:
    for i, line in enumerate(text.splitlines(), 1):
        if len(line) > 2000:  # minified or generated: skip rather than pay for the patterns
            continue
        for kind, rule, rx in RULES:
            m = rx.search(line)
            if m and (key := (path, rule, m.group(0))) not in seen:
                seen.add(key)
                hits.append({"kind": kind, "rule": rule, "path": path, "line": None if removed else i, "removed": removed, "text": mask(kind, m.group(0))})


def scan(rdir: pathlib.Path, run: dict) -> tuple[list[dict], int]:
    """What looks like a secret or personal data in the code the file carries: each changed file at the tip, and the lines the
    stack deleted (the page shows those too). The hits, and how many more there were past the cap."""
    hits: list[dict] = []
    seen: set = set()
    for st in run["stacks"]:
        sdir = rdir / st["id"]
        for f in sorted((sdir / "tip").rglob("*")):
            if f.is_file():
                try:
                    scan_text(str(f.relative_to(sdir / "tip")), f.read_text(), hits, seen)
                except UnicodeDecodeError:
                    pass
        path = None
        diff = (sdir / "target.diff").read_text(errors="replace") if (sdir / "target.diff").is_file() else ""
        for line in diff.splitlines():
            if line.startswith("--- "):
                path = line[6:] if line.startswith("--- a/") else None
            elif line.startswith("+++ "):
                path = line[6:] if line.startswith("+++ b/") else path
            elif path and line.startswith("-") and not line.startswith("---"):
                scan_text(path, line[1:], hits, seen, removed=True)
    return hits[:MAX_HITS], max(0, len(hits) - MAX_HITS)


# ---- export -------------------------------------------------------------------------------------------------------------

def scrub(data: dict) -> dict:
    """The page data without what is personal to the sender: their login, the path on their machine, and what they last
    reviewed (`since` and `last_review` describe them, not the stack)."""
    data = json.loads(json.dumps(data))
    data["viewer"] = "you"
    if data.get("source"):
        data["source"]["root"] = None
    for st in data["stacks"]:
        st["since"] = None
        for p in st["prs"]:
            p["last_review"] = None
    return data


def refuted_ids(rdir: pathlib.Path, sid: str) -> dict[str, set[str]]:
    axes = [r["id"] for r in reviewers_of(rdir / sid)]
    out: dict[str, set[str]] = {a: set() for a in axes}
    for f in (rdir / sid / "verify").glob("*.json"):
        axis, fid = split_key(f.stem, axes)
        if axis in out and (_json(f, {}) or {}).get("status") == "refuted":
            out[axis].add(fid)
    return out


def plain_thread(t: dict) -> dict:
    """A question thread as shared: the conversation and what it proposed, without the sender's session."""
    return {k: v for k, v in t.items() if k not in ("ctx", "live", "error")} | {"session_id": None, "status": "idle" if t.get("status") != "failed" else "failed"}


def bundle_files(rdir: pathlib.Path, run: dict, hide_refuted: bool, threads: list[dict] = ()) -> dict[str, str]:
    """The round's source files a receiving server rebuilds from: no checkout path, no viewer, no logs, no brief."""
    run = json.loads(json.dumps(run))
    run["checkout"], run["viewer"] = None, "you"
    if run.get("source"):
        run["source"]["root"] = None
    for p in run["prs"]:
        p["last_review"] = None
    for st in run["stacks"]:
        st["since"] = None
    files = {"run.json": json.dumps(run)}
    for t in threads:
        files[f"threads/{t['id']}.json"] = json.dumps(t)
    for p in run["prs"]:
        if (rdir / str(p["n"]) / "hunks.json").is_file():
            files[f"{p['n']}/hunks.json"] = (rdir / str(p["n"]) / "hunks.json").read_text()
    for st in run["stacks"]:
        sdir, sid = rdir / st["id"], st["id"]
        axes = [r["id"] for r in reviewers_of(sdir)]
        gone = refuted_ids(rdir, sid) if hide_refuted else {a: set() for a in axes}
        for name in ("guide.json", "guide.html", "files.json", "plan.json", "attribution.json", "target.diff", *(f"review-{a}.json" for a in axes)):
            if not (sdir / name).is_file():
                continue
            text = (sdir / name).read_text()
            if name.startswith("review-") and gone[name[7:-5]]:
                rep = json.loads(text)
                rep["findings"] = [c for c in rep.get("findings") or [] if c.get("id") not in gone[name[7:-5]]]
                text = json.dumps(rep)
            files[f"{sid}/{name}"] = text
        for sub, pattern in (("guide", "*.html"), ("figures", "*.json"), ("figures", "*.svg")):
            for f in sorted((sdir / sub).glob(pattern)) if (sdir / sub).is_dir() else []:
                files[f"{sid}/{sub}/{f.name}"] = f.read_text()
        if (sdir / "timing.json").is_file():
            files[f"{sid}/timing.json"] = (sdir / "timing.json").read_text()
        for f in sorted((sdir / "verify").glob("*.json")):
            axis, fid = split_key(f.stem, axes)
            if fid not in gone.get(axis, ()):
                files[f"{sid}/verify/{f.name}"] = f.read_text()
        for f in sorted((sdir / "tip").rglob("*")):
            if f.is_file():
                try:
                    files[f"{sid}/tip/{f.relative_to(sdir / 'tip')}"] = f.read_text()
                except UnicodeDecodeError:
                    pass
    return files


def pack(meta: dict, files: dict[str, str]) -> str:
    return base64.b64encode(gzip.compress(json.dumps({"v": VERSION, "meta": meta, "files": files}).encode(), 6)).decode()


def fonts_css() -> str:
    css = (VENDOR / "fonts.css").read_text()
    return re.sub(r"url\(fonts/([\w.-]+)\)", lambda m: "url(data:font/woff2;base64," + base64.b64encode((VENDOR / "fonts" / m.group(1)).read_bytes()).decode() + ")", css)


def seed_of(ui: dict, data: dict) -> dict:
    """The sender's picked comments (findings they added, comments they wrote), as the first drafts of whoever opens the file."""
    cm = {k: v for k, v in (ui.get("cm") or {}).items() if v.get("on") and not v.get("gone") and (v.get("text") or "").strip()}
    extra = {sid: [c for c in cs if c["id"] in cm] for sid, cs in (ui.get("extra") or {}).items()}
    extra = {k: v for k, v in extra.items() if v}
    keep = {c["id"] for st in data["stacks"] for c in st["comments"]} | {c["id"] for cs in extra.values() for c in cs}
    return {"cm": {k: v for k, v in cm.items() if k in keep}, "extra": extra}


def export(rdir: pathlib.Path, ui: dict, opts: dict, n: int, rid: str, threads: list[dict] = ()) -> tuple[str, dict]:
    """The file for round `n` and its report: size, biggest files, check hits, what it holds. `opts`: `mine` (include the
    sender's picked comments), `refuted` (False hides refuted findings) and `threads` (include the questions and answers,
    passed in `threads`)."""
    data, run = _json(rdir / "guide.json"), _json(rdir / "run.json")
    if not data or not run:
        raise ShareError("this round has no built guide to share")
    mine, hide = bool(opts.get("mine")), opts.get("refuted") is False
    refuted = sum(c["status"] == "refuted" for st in data["stacks"] for c in st["comments"])
    data = scrub(data)
    if hide:
        for st in data["stacks"]:
            st["comments"] = [c for c in st["comments"] if c["status"] != "refuted"]
    picked = seed_of(ui, data)
    seed = picked if mine else {"cm": {}, "extra": {}}
    available = len(threads)
    threads = [plain_thread(t) for t in threads] if opts.get("threads") else []
    seed["threads"] = threads
    mode, prs = data.get("mode", "pr"), [p["n"] for st in data["stacks"] for p in st["prs"]]
    branch = (data.get("source") or {}).get("branch")
    name = f"review-{data['repo'].split('/')[1]}-{slug(branch if mode == 'self' and branch else '-'.join(map(str, prs)))}-round{n}.html"
    tip = data["stacks"][-1]["tip"]
    meta = {"repo": data["repo"], "author": run["viewer"], "built": int((rdir / "guide.json").stat().st_mtime), "tip": tip, "mode": mode, "round": n,
            "review": rid, "branch": branch, "prs": prs, "name": name}
    key = hashlib.sha1(f"{data['repo']} {rid} {tip}".encode(), usedforsecurity=False).hexdigest()[:12]
    boot = {"shared": {**meta, "key": key, "options": {"mine": mine, "refuted": not hide, "threads": bool(threads)}}, "live": False, "base": "", "review": None, "round": n,
            "status": "open", "ui": {}, "rounds": []}
    files = bundle_files(rdir, run, hide, threads)
    hits, more = scan(rdir, run)
    page = TEMPLATE.read_text()
    page = page.replace("/*DATA*/null", embed(data), 1).replace("'/*TOKEN*/'", "''", 1).replace("/*BOOT*/null", embed(boot), 1)
    page = re.sub(r'<link href="https://fonts\.googleapis\.com[^>]*>', lambda _: f"<style>{fonts_css()}</style>", page, count=1)
    hl = (VENDOR / "highlight.min.js").read_text()
    assert "</script" not in hl
    page = re.sub(r'<script src="https://cdnjs\.cloudflare\.com[^>]*></script>', lambda _: f"<script>{hl}</script>", page, count=1)
    title = f"Review guide: {branch}" if mode == "self" and branch else f"Review guide: {data['repo'].split('/')[1]} " + " ".join(f"#{x}" for x in prs)
    page = page.replace("<title>Review guide</title>", f"<title>{html.escape(title)}</title>", 1)
    assert "<!--SHARED-->" in page
    page = page.replace("<!--SHARED-->", f'<script type="application/json" id="rg-seed">{embed(seed)}</script>\n'
                                          f'<script type="application/octet-stream" id="rg-bundle">{pack(meta, files)}</script>\n', 1)
    tips = sorted(({"path": p.split("/tip/", 1)[1], "bytes": len(t.encode())} for p, t in files.items() if "/tip/" in p), key=lambda x: -x["bytes"])
    report = {"name": name, "size": len(page.encode()), "hits": hits, "more_hits": more, "picked": len(picked["cm"]), "refuted": refuted, "threads": len(threads), "threads_available": available,
              "findings": sum(len(st["comments"]) for st in data["stacks"]), "files": sum(len(st["files"]) for st in data["stacks"]),
              "mode": mode, "repo": data["repo"], "biggest": tips[:5]}
    return page, report


# ---- import -------------------------------------------------------------------------------------------------------------

BUNDLE_RX = re.compile(r'<script type="application/octet-stream" id="rg-bundle">([A-Za-z0-9+/=\s]*)</script>')


def bundle_text(text: str) -> str:
    """The bundle inside a shared .html file, or the text itself when it is a bare bundle."""
    if m := BUNDLE_RX.search(text):
        return m.group(1)
    if "<" not in text[:200] and len(text) > 100:
        return text
    raise ShareError("this file holds no review-guide bundle; drop a shared guide (.html) or a feedback file (.json)")


def drafts_of(html_text: str) -> dict:
    """The comments a shared file carries (its `rg-seed`), as page state for the review created from it."""
    m = re.search(r'<script type="application/json" id="rg-seed">(.*?)</script>', html_text, re.S)
    try:
        seed = json.loads(m.group(1)) if m else {}
    except ValueError:
        seed = {}
    return {k: seed.get(k) for k in ("cm", "extra") if isinstance(seed.get(k), dict)}


def clean_drafts(d) -> dict:
    """Page state from a draft set a browser sent: only the parts that hold comments, in the shapes the page reads."""
    if not isinstance(d, dict):
        return {}
    out = {}
    if isinstance(d.get("cm"), dict):
        out["cm"] = {k: {"on": bool(v.get("on")), "gone": bool(v.get("gone")), "sev": str(v.get("sev") or "comment"), "text": str(v.get("text") or "")}
                     for k, v in d["cm"].items() if isinstance(k, str) and isinstance(v, dict)}
    if isinstance(d.get("extra"), dict):
        out["extra"] = {k: [c for c in v if isinstance(c, dict) and isinstance(c.get("id"), str)] for k, v in d["extra"].items() if isinstance(v, list)}
    if isinstance(d.get("verdict"), dict):
        out["verdict"] = {k: {x: y for x, y in v.items() if x in ("body", "event") and isinstance(y, str | type(None))} for k, v in d["verdict"].items() if isinstance(v, dict)}
    for k in ("done", "viewed"):
        if isinstance(d.get(k), dict):
            out[k] = {x: True for x, y in d[k].items() if y is True}
    return out


def read_feedback(text: str) -> dict:
    """A feedback file for the author of a self-review: the layers' verdicts and comments, from `Download feedback for the author`."""
    try:
        fb = json.loads(text)
        layers = fb["layers"]
        ok = (fb.get("review-guide-feedback") == 1 and isinstance(fb["review"], str) and isinstance(layers, list) and layers
              and all(isinstance(x, dict) and isinstance(x.get("n"), int) and isinstance(x.get("comments") or [], list) for x in layers)
              and all(isinstance(c, dict) and isinstance(c.get("body"), str) and isinstance(c.get("path"), (str, type(None))) and isinstance(c.get("line"), (int, type(None)))
                      and isinstance(c.get("start_line"), (int, type(None))) for x in layers for c in x.get("comments") or []))
    except (ValueError, KeyError, TypeError, AttributeError):
        ok = False
    if not ok:
        raise ShareError("this is not a review-guide feedback file")
    return fb


def read_bundle(text: str) -> dict:
    """Decode a bundle and check its shape: size, JSON, only known file names, a repository and commits that look like ones.
    Raises ShareError. Nothing is written."""
    text = text.strip()
    if len(text) > BUNDLE_CAP:
        raise ShareError(f"the file is over the {BUNDLE_CAP // 1_000_000} MB limit")
    try:
        d = zlib.decompressobj(31)
        body = d.decompress(base64.b64decode(text, validate=True), INFLATED_CAP)
        if d.unconsumed_tail:
            raise ShareError("the bundle expands past the size limit")
        bundle = json.loads(body)
    except ShareError:
        raise
    except (ValueError, zlib.error) as e:
        raise ShareError(f"this is not a review-guide bundle ({type(e).__name__})") from None
    if not isinstance(bundle, dict) or bundle.get("v") != VERSION or not isinstance(bundle.get("files"), dict) or not isinstance(bundle.get("meta"), dict):
        raise ShareError("this bundle is from a different version of review-guide")
    files = bundle["files"]
    if not 0 < len(files) <= MAX_FILES:
        raise ShareError("the bundle holds no files, or too many")
    for path, content in files.items():
        parts = path.split("/")
        if not ALLOWED.match(path) or ".." in parts or "" in parts or "\0" in path or not isinstance(content, str):
            raise ShareError(f"the bundle holds a file it should not: {path[:80]!r}")
    if "run.json" not in files:
        raise ShareError("the bundle has no run.json")
    try:
        run = json.loads(files["run.json"])
        if not REPO.match(run["repo"]) or not run["prs"] or not run["stacks"]:
            raise ShareError("the bundle names no repository or pull request")
        for p in run["prs"]:
            if not isinstance(p["n"], int) or not SHA.match(p["sha"]) or not SHA.match(p["review_sha"]):
                raise ShareError(f"PR {p.get('n')!r} has no valid commit")
        for st in run["stacks"]:
            if not re.fullmatch(SID, st["id"]) or not SHA.match(st["tip"]) or not SHA.match(st["base"]):
                raise ShareError(f"stack {st.get('id')!r} has no valid commits")
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise ShareError(f"run.json is not usable: {e}") from None
    return bundle


def stage(work: pathlib.Path, bundle: dict, viewer: str) -> dict:
    """Write the bundle's files into `work` and build the round there with build.py, which runs the guide and finding checks of
    `build.py --check` (the size caps and the references into a checkout, which this machine may not have yet, are the two it
    skips, as for any earlier round). Raises ShareError with build.py's complaints. Returns run.json."""
    root = work.resolve()
    for path, text in bundle["files"].items():
        dest = (work / path).resolve()
        if root not in dest.parents:
            raise ShareError(f"unsafe path {path!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
    run = json.loads((work / "run.json").read_text())
    run["checkout"], run["viewer"] = None, viewer
    (work / "run.json").write_text(json.dumps(run, indent=1))
    rebuild(work)
    return run


def rebuild(work: pathlib.Path) -> None:
    r = subprocess.run([sys.executable, str(BUILD), str(work)], capture_output=True, text=True, timeout=180)
    if r.returncode:
        raise ShareError("the guide fails the checks:\n" + (r.stdout or r.stderr).strip()[:1500])


def resolve_checkout(home: pathlib.Path, run: dict, scratch: pathlib.Path) -> str | None:
    """A repository holding the reviewed commits, for Ask and code search: the clone cache under `home` (the one fetch.py uses
    by default), cloned when missing, with the reviewed commits fetched. None when they cannot be had (no access, or local
    commits of a self-review that were never pushed)."""
    from fetch import Git, prefetch_tree
    repo, local = run["repo"], run.get("mode") == "self"
    path = home / "repos" / repo.replace("/", "__")
    if not (path / "HEAD").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if subprocess.run(["gh", "repo", "clone", repo, str(path), "--", "--bare", "--filter=blob:none", "--quiet"], capture_output=True, timeout=900).returncode:
            return None
    git = Git(str(path), via_gh=True)
    has = lambda s: git.ok("cat-file", "-e", f"{s}^{{commit}}")  # noqa: E731
    shas = sorted({p["review_sha"] for p in run["prs"]} | {s["tip"] for s in run["stacks"]})
    if missing := [s for s in shas if not has(s)]:
        if not local:  # a self-review's layer numbers are not PR numbers
            git("fetch", "--quiet", "--no-tags", "origin", *[f"+pull/{p['n']}/head:refs/review-guide/{p['n']}" for p in run["prs"]], check=False)
        if missing := [s for s in missing if not has(s)]:
            git("fetch", "--quiet", "--no-tags", "origin", *missing, check=False)
        if any(not has(s) for s in shas):
            return None
    for st in run["stacks"]:
        try:
            prefetch_tree(git, st["tip"], scratch / f"tree-{st['id']}")
        except BaseException:  # a blobless clone that cannot fetch its blobs still answers `git show` for what it holds
            pass
    return str(path)
