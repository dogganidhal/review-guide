#!/usr/bin/env python3
"""Fetch a set of pull requests, group them into stacks, and write what the guide agents read.

Usage: fetch.py [--workdir DIR] [--effort quick|balanced|deep] <pr-url | owner/repo#n>...

PRs whose base branch is another PR's head form a stack. A stack is reviewed as one change: the diff
from the bottom PR's base to the top PR's head (the target). Each line the stack changed is attributed
to the PR that wrote its final version, with its line number in that PR, so a comment placed on the
target can be posted on the right PR. A PR on its own is a stack of one.

Writes:
  run.json                       repo, viewer, effort, checkout, warnings, prs, stacks (bottom PR first)
  <stack>/target.diff            base..tip
  <stack>/tip/<path>             each changed file at the tip
  <stack>/attribution.json       path -> {tip line: [pr, line in that PR's head]} for lines the stack wrote
  <stack>/repo-guidance/         the repo's review rules at the tip
  <stack>/brief.md               everything the agents read first: descriptions, numbered diff with owners, symbol uses,
                                 and what removed names still touch
  <stack>/spec.md                the tickets the PRs name (Jira, when credentials are set) and the GitHub issues they close
  <n>/pr.diff, <n>/hunks.json    the PR's own diff at its review commit, and the lines GitHub accepts comments on
  <n>/existing-comments.json     what reviewers already said
A stack the viewer reviewed before carries `since`: the commit of their last review and the tip lines changed after it.
Prints the work directory on the last line.
"""

import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict

CACHE = pathlib.Path.home() / ".cache" / "review-guide"
EFFORTS = ("quick", "balanced", "deep")
GUIDANCE_FILES = ("PR_REVIEWER.md", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", ".github/PULL_REQUEST_TEMPLATE.md", ".github/pull_request_template.md")
MAX_FILE_BYTES = 400_000
PR_REF = re.compile(r"(?:https://github\.com/)?([\w.-]+)/([\w.-]+)(?:/pull/|#)(\d+)")
SHA = re.compile(r"[0-9a-f]{40}")


def run(cmd: list[str], *, check: bool = True, binary: bool = False):
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode:
        if check:
            sys.exit(f"{' '.join(cmd[:6])}: {r.stderr.decode(errors='replace').strip()}")
        return None
    return r.stdout if binary else r.stdout.decode(errors="replace")


def gh(*args: str, check: bool = True):
    return run(["gh", *args], check=check)


class Git:
    """The repository the scripts read: the user's clone, or a blobless bare clone in the cache that authenticates through gh."""

    def __init__(self, path: str, via_gh: bool):
        auth = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"] if via_gh else []
        self.prefix = ["git", "-C", path, *auth]

    def __call__(self, *args: str, check: bool = True, binary: bool = False):
        return run([*self.prefix, *args], check=check, binary=binary)

    def ok(self, *args: str) -> bool:
        return subprocess.run([*self.prefix, *args], capture_output=True).returncode == 0

    def text(self, rev: str, path: str) -> str | None:
        data = self("show", f"{rev}:{path}", check=False, binary=True)
        if data is None or len(data) > MAX_FILE_BYTES or b"\0" in data[:8000]:
            return None
        try:
            return data.decode()
        except UnicodeDecodeError:
            return None


def prefetch_tree(git: Git, rev: str, scratch: pathlib.Path) -> None:
    """Download every blob of `rev` into a blobless clone in one batch, so `git grep` and `git show` at that commit stay
    local. A throwaway worktree is the cheapest way to make git fetch a whole tree at once."""
    git("worktree", "add", "--detach", "--quiet", str(scratch), rev)
    git("worktree", "remove", "--force", str(scratch))


def open_repo(repo: str) -> tuple[Git, str | None]:
    root = (run(["git", "rev-parse", "--show-toplevel"], check=False) or "").strip()
    origin = (run(["git", "-C", root, "remote", "get-url", "origin"], check=False) or "").strip() if root else ""
    if root and re.search(rf"[:/]{re.escape(repo)}(\.git)?$", origin, re.IGNORECASE):
        return Git(root, via_gh=False), root
    path = CACHE / "repos" / repo.replace("/", "__")
    if not (path / "HEAD").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        gh("repo", "clone", repo, str(path), "--", "--bare", "--filter=blob:none", "--quiet")
    return Git(str(path), via_gh=True), None


def parse_diff(diff: str) -> dict[str, dict]:
    """Per file of a unified diff (keyed by its new path, or old path when deleted): status, old path, the
    new-side `added` and `commentable` lines, and the hunks as lines `{k: "+"|"-"|" ", o, n, t}`."""
    files: dict[str, dict] = {}
    cur, old, o, n = None, None, 0, 0
    for raw in diff.splitlines():
        if raw.startswith("diff --git"):
            cur, old = None, None
        elif cur is None and raw.startswith("--- "):
            old = None if raw[4:] == "/dev/null" else raw[4:].removeprefix("a/")
        elif cur is None and raw.startswith("+++ "):
            new = None if raw[4:] == "/dev/null" else raw[4:].removeprefix("b/")
            status = "added" if old is None else "deleted" if new is None else "renamed" if old != new else "modified"
            cur = files[new or old] = {"old": old, "status": status, "added": [], "commentable": [], "hunks": []}
        elif cur is not None and raw.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", raw)
            o, n = int(m[1]), int(m[2])
            cur["hunks"].append([])
        elif cur is not None and cur["hunks"] and raw[:1] in ("+", "-", " "):
            k = raw[0]
            if k == "+":
                cur["hunks"][-1].append({"k": k, "n": n, "t": raw[1:]})
                cur["added"].append(n)
                cur["commentable"].append(n)
                n += 1
            elif k == "-":
                cur["hunks"][-1].append({"k": k, "o": o, "t": raw[1:]})
                o += 1
            else:
                cur["hunks"][-1].append({"k": k, "o": o, "n": n, "t": raw[1:]})
                cur["commentable"].append(n)
                o, n = o + 1, n + 1
    return files


def changed_files(git: Git, base: str, tip: str) -> list[dict]:
    parts = git("diff", "--name-status", "-z", "-M", base, tip).split("\0")
    files, i = [], 0
    while i < len(parts) - 1:
        status = parts[i]
        if status[:1] in ("R", "C"):
            files.append({"path": parts[i + 2], "old": parts[i + 1], "status": "renamed"})
            i += 3
        else:
            files.append({"path": parts[i + 1], "old": None, "status": {"A": "added", "D": "deleted"}.get(status[:1], "modified")})
            i += 2
    return files


def blame(git: Git, rev_range: str, path: str) -> list[tuple[str, int] | None]:
    """For each line of `path` at the end of the range: (commit, line number in that commit), or None when the line is older than the range.

    The pair identifies a line across later commits, which is how a tip line is found again in a lower PR's head."""
    out = git("blame", "--porcelain", rev_range, "--", path, check=False)
    lines: list[tuple[str, int] | None] = []
    boundary: set[str] = set()
    cur: tuple[str, int] | None = None
    for raw in (out or "").splitlines():
        if raw.startswith("\t"):
            lines.append(None if cur[0] in boundary else cur)
        elif raw == "boundary":
            boundary.add(cur[0])
        else:
            parts = raw.split(" ")
            if len(parts) >= 3 and SHA.fullmatch(parts[0]):
                cur = (parts[0], int(parts[1]))
    return lines


def commit_owners(git: Git, base: str, chain: list[int], review: dict[int, str]) -> dict[str, int]:
    owners, prev = {}, base
    for n in chain:
        for commit in git("rev-list", f"{prev}..{review[n]}").split():
            owners[commit] = n
        prev = review[n]
    return owners


def attribute(git: Git, base: str, chain: list[int], heads: dict[int, str], owners: dict[str, int], path: str) -> dict[str, list]:
    """Tip line -> [PR that wrote its final version, line number in that PR's head (None if not found)]."""
    tip = chain[-1]
    by_pr: dict[int, dict[tuple[str, int], int]] = defaultdict(dict)
    for line, origin in enumerate(blame(git, f"{base}..{heads[tip]}", path), 1):
        if origin and origin[0] in owners:
            by_pr[owners[origin[0]]][origin] = line
    out = {}
    for n, origins in by_pr.items():
        index = None if n == tip else {o: j for j, o in enumerate(blame(git, f"{base}..{heads[n]}", path), 1) if o}
        for origin, line in origins.items():
            out[str(line)] = [n, line if index is None else index.get(origin)]
    return dict(sorted(out.items(), key=lambda kv: int(kv[0])))


def link_stacks(git: Git, prs: dict[int, dict], heads: dict[int, str]) -> tuple[list[list[int]], list[str]]:
    """Chains of PRs, bottom first. A PR is stacked on the PR whose head branch is its base branch, as long as it
    contains at least one of that PR's commits (it may lag behind the newest ones)."""
    warnings = []
    by_branch = {p["headRefName"]: n for n, p in prs.items()}
    parent = {}
    for n, p in prs.items():
        q = by_branch.get(p["baseRefName"])
        if q is None or q == n:
            continue
        shared = git("merge-base", heads[q], heads[n], check=False)
        q_base = git("merge-base", f"refs/review-guide/base/{q}", heads[q], check=False)
        if shared and q_base and not git.ok("merge-base", "--is-ancestor", shared.strip(), q_base.strip()):
            parent[n] = q
        else:
            warnings.append(f"#{n} targets #{q}'s branch but contains none of its commits, so they are reviewed separately")
    kids = defaultdict(list)
    for c, q in parent.items():
        kids[q].append(c)
    for q, cs in kids.items():
        if len(cs) > 1:
            for c in cs:
                parent.pop(c)
            warnings.append(f"{', '.join(f'#{c}' for c in sorted(cs))} all build on #{q}; each is reviewed as its own stack")
    child = {q: c for c, q in parent.items()}
    chains = []
    for n in sorted(prs):
        if n in parent:
            continue
        chain = [n]
        while chain[-1] in child:
            chain.append(child[chain[-1]])
        chains.append(chain)
    return chains, warnings


def review_shas(git: Git, chain: list[int], heads: dict[int, str]) -> tuple[dict[int, str], list[str]]:
    """The commit each PR is reviewed at: the top PR's head, and below it the version of each PR its child contains.

    When a child lags behind its parent, the parent's newer commits are not part of the stack's target."""
    review, warnings = {chain[-1]: heads[chain[-1]]}, []
    for q, n in zip(reversed(chain[:-1]), reversed(chain[1:])):
        review[q] = git("merge-base", heads[q], review[n]).strip()
        if review[q] != heads[q]:
            newer = git("log", "--format=%h %s", f"{review[q]}..{heads[q]}").splitlines()
            warnings.append(
                f"#{n} is built on an older #{q} ({review[q][:8]}). Not in this view, {len(newer)} newer #{q} commit(s): "
                + "; ".join(newer[:3])
                + f". Comments for #{q} are posted on {review[q][:8]}; GitHub marks them outdated where newer commits changed the line."
            )
    return review, warnings


TEST_PATH = re.compile(r"(^|/)(src/test|tests?|__tests__|spec)/|(Test|Tests|IT|Spec)\.\w+$|[._-](test|spec)\.\w+$")
OMIT_PATH = re.compile(r"(\.lock|-lock\.json|\.min\.(js|css)|\.snap|\.golden|\.svg|\.png|\.jpe?g|\.gif)$")
CODE_PATH = re.compile(r"\.(java|kt|kts|scala|py|ts|tsx|js|jsx|mjs|go|rb|rs|cs|swift|php)$")
DECLARATIONS = (
    re.compile(r"\b(?:class|interface|record|enum|object|struct|trait)\s+([A-Z]\w{2,})"),
    re.compile(r"\b(?:def|fun|func|function)\s+([A-Za-z_]\w{3,})"),
    re.compile(r"^\s*(?:(?:public|protected|private|static|final|abstract|synchronized|override|suspend|async|default)\s+)+(?:<[^>]+>\s*)?[\w<>\[\],.?]+\s+([a-z]\w{3,})\s*\("),
)
COMMON_NAMES = frozenset({"main", "toString", "equals", "hashCode", "build", "close", "apply", "test", "setUp", "tearDown", "init", "start", "stop", "create", "value", "values", "from"})
BRIEF_BUDGET = 220_000
MAX_SYMBOLS, MAX_REFS = 40, 6
CALL = re.compile(r"\b([a-z]\w{3,})\s*\(")
# A string that names something read outside the code: `odc.review.sms_sent`, `on_demand_care_chat_send_sms_pro`.
# Dotted names need two dots; snake names need three parts. A JSON or YAML key (`"external_id":`) is not one.
EXTERNAL_NAME = re.compile(r"[\"']((?:[a-z][a-z0-9_]*\.){2,}[a-z][a-z0-9_]*|[a-z][a-z0-9]*(?:_[a-z0-9]+){2,})[\"'](?!\s*:)")
FIXTURE_PATH = re.compile(r"(^|/)(fixtures?|mocks?|wiremock|mappings|testdata|test-data|__snapshots__|load-?tests?)(/|$)|(^|/)backend-load-test/")
DOC_PATH = re.compile(r"\.(md|mdx|rst|txt|adoc)$")
MAX_LEFTOVERS = 12
TICKET = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")


def numbered_diff(d: dict, owners: dict) -> str:
    """A diff with tip line numbers, and on each added line the PR that wrote it."""
    rows = []
    for i, hunk in enumerate(d["hunks"]):
        if i:
            rows.append("      ⋯")
        for line in hunk:
            if line["k"] == "-":
                rows.append(f"{'':>12}- {line['t']}")
            else:
                own = owners.get(str(line["n"])) if line["k"] == "+" else None
                rows.append(f"{line['n']:>5} {('#' + str(own[0])) if own else '':>6}{line['k']} {line['t']}")
    return "\n".join(rows)


def references(git: Git, tip: str, parsed: dict, stack_paths: set[str]) -> list[str]:
    """Where each class or function the stack declares is used, so agents need not search for callers."""
    symbols: dict[str, tuple[str, int]] = {}
    for path, d in parsed.items():
        if TEST_PATH.search(path) or not CODE_PATH.search(path):
            continue
        for line in (line for hunk in d["hunks"] for line in hunk if line["k"] == "+"):
            for rx in DECLARATIONS:
                for m in rx.finditer(line["t"]):
                    if m[1] not in COMMON_NAMES:
                        symbols.setdefault(m[1], (path, line["n"]))
    symbols = dict(list(symbols.items())[:MAX_SYMBOLS])

    def lookup(item: tuple[str, tuple[str, int]]) -> list[str]:
        name, (path, line) = item
        hits = []
        for raw in (git("grep", "-n", "-w", "-I", "-F", "-e", name, tip, check=False) or "").splitlines():
            parts = raw.split(":", 3)
            if len(parts) == 4 and parts[2].isdigit() and not (parts[1] == path and int(parts[2]) == line):
                hits.append((parts[1] in stack_paths, parts[1], int(parts[2]), parts[3].strip()[:160]))
        hits.sort()
        out = [f"### {name} (declared {path}:{line}; {len(hits)} other uses)"]
        out += [f"{p}:{n}{' (in stack)' if inside else ''}: {t}" for inside, p, n, t in hits[:MAX_REFS]]
        if len(hits) > MAX_REFS:
            out.append(f"... {len(hits) - MAX_REFS} more")
        return out + [""]

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        return [row for block in pool.map(lookup, symbols.items()) for row in block]


def leftovers(git: Git, tip: str, parsed: dict) -> list[str]:
    """What the stack removed that something may still need, for the reviewers. Only removed lines of production code
    and config count, never tests, fixtures or docs. Three kinds, most useful first:
    - external names (metric, config key, event, feature switch), listed even with no use left in the repo, because
      dashboards, monitors and other services read them from outside;
    - removed declarations the repo still references;
    - functions whose callers the stack removed, where only declarations and delegations remain."""
    names, decls, called, added_text = {}, {}, set(), []
    for path, d in parsed.items():
        prod = not (TEST_PATH.search(path) or FIXTURE_PATH.search(path) or OMIT_PATH.search(path) or DOC_PATH.search(path))
        for line in (line for hunk in d["hunks"] for line in hunk):
            if line["k"] == "+":
                added_text.append(line["t"])
            elif line["k"] == "-" and prod:
                if CODE_PATH.search(path):
                    for rx in DECLARATIONS:
                        for m in rx.finditer(line["t"]):
                            decls.setdefault(m[1], path)
                    called.update(m[1] for m in CALL.finditer(line["t"]))
                for m in EXTERNAL_NAME.finditer(line["t"]):
                    names.setdefault(m[1], path)
    added = "\n".join(added_text)
    names = {n: p for n, p in names.items() if n not in added}
    decls = {n: p for n, p in decls.items() if n not in COMMON_NAMES and not re.search(rf"\b{re.escape(n)}\b", added)}
    callees = [n for n in called - set(decls) - COMMON_NAMES if not re.search(rf"\b{re.escape(n)}\s*\(", added)]

    def hits(name: str) -> list[tuple[str, int, str]]:
        out = []
        for raw in (git("grep", "-n", "-w", "-I", "-F", "-e", name, tip, check=False) or "").splitlines():
            parts = raw.split(":", 3)
            if len(parts) == 4 and parts[2].isdigit():
                out.append((parts[1], int(parts[2]), parts[3].strip()[:160]))
        return out

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        external = dict(zip(names, pool.map(hits, names)))
        dangling = dict(zip(decls, pool.map(hits, decls)))
        orphans = dict(zip(callees, pool.map(hits, callees)))
    rows = lambda found: [f"{p}:{n}: {t}" for p, n, t in found[:MAX_REFS]]  # noqa: E731
    out = []
    # One entry per family of dotted names (`odc.review_duty.notifications.*`), whatever is left in the repo.
    families: dict[str, list[str]] = defaultdict(list)
    for name in names:
        # The family drops the last segment, and a metric type suffix before it (`.count`, `.total`).
        stem = re.sub(r"\.(count|total|sum|max|min|avg|rate|gauge|timer|seconds|ms)$", "", name)
        families[stem.rsplit(".", 1)[0] + ".*" if stem.count(".") >= 2 else name].append(name)
    for key, members in list(families.items())[:MAX_LEFTOVERS]:
        left = [h for m in members for h in external[m]]
        title = f"{key} ({len(members)} names)" if len(members) > 1 else members[0]
        where = f"{len(left)} uses left here" if left else "no use left here"
        out += [f"### {title}: removed from {names[members[0]]}; {where}. May be read outside this repository "
                "(dashboards, monitors, other services).", *rows(left), ""]
    for name, found in [(n, f) for n, f in dangling.items() if f][:MAX_LEFTOVERS]:
        out += [f"### {name} (removed from {decls[name]}; {len(found)} uses left)", *rows(found), ""]
    for name, found in orphans.items():
        # The stack removed the calls; what is left only declares the method or forwards it to the next layer.
        if found and not any(is_use(name, t) for _, _, t in found):
            out += [f"### {name} has no caller left; still declared in:", *rows(found), ""]
    return out


def is_use(name: str, text: str) -> bool:
    """Whether a line really calls `name`: not its declaration, not an interface method, and not a one-line delegation
    (`return x.name(`, `x::name`) that only forwards a call nobody makes any more."""
    n = re.escape(name)
    if any(m[1] == name for rx in DECLARATIONS for m in rx.finditer(text)):
        return False
    if re.search(rf"^\s*[\w<>,.?\[\] ]+\s+{n}\s*\([^)]*\)\s*;", text):  # an interface or abstract method
        return False
    if re.search(rf"return\s+[\w.]+\.{n}\s*\(|::{n}\b", text):
        return False
    return bool(re.search(rf"\b{n}\s*\(", text))


def jira_issue(key: str) -> str | None:
    """A ticket's title, status and description as text, when ATLASSIAN_SITE, ATLASSIAN_EMAIL and a token are set."""
    site, email = os.environ.get("ATLASSIAN_SITE"), os.environ.get("ATLASSIAN_EMAIL")
    if not site or not email:
        return None
    token = os.environ.get("ATLASSIAN_API_TOKEN") or (run(["security", "find-generic-password", "-a", email, "-s", "atlassian-api-token", "-w"], check=False) or "").strip()
    if not token:
        return None
    req = urllib.request.Request(f"{site.rstrip('/')}/rest/api/3/issue/{key}?fields=summary,status,issuetype,description,parent",
                                 headers={"Authorization": "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode(), "Accept": "application/json"})
    try:
        f = json.load(urllib.request.urlopen(req, timeout=10))["fields"]
    except (OSError, ValueError, KeyError):
        return None

    def text(node) -> str:
        if not isinstance(node, dict):
            return ""
        if node.get("type") == "text":
            return node.get("text", "")
        inner = "".join(text(c) for c in node.get("content") or [])
        return inner + "\n" if node.get("type") in ("paragraph", "heading", "listItem", "codeBlock") else inner

    parent = (f.get("parent") or {}).get("fields", {}).get("summary")
    head = f"## {key}: {f.get('summary')}\n{(f.get('issuetype') or {}).get('name')} · {(f.get('status') or {}).get('name')}" + (f" · parent: {parent}" if parent else "")
    return head + "\n\n" + re.sub(r"\n{3,}", "\n\n", text(f.get("description"))).strip()[:6000]


def write_spec(sdir: pathlib.Path, chain: list[int], prs: dict[int, dict]) -> None:
    """The spec the spec reviewer checks the stack against: Jira tickets named by the PRs, and the issues they close."""
    keys = list(dict.fromkeys(k for n in chain for k in TICKET.findall(f"{prs[n]['title']} {prs[n]['headRefName']}")))
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        tickets = [t for t in pool.map(jira_issue, keys) if t]
    issues = [f"## {i.get('url') or i.get('number')}: {i.get('title')}\n\n{(i.get('body') or '').strip()[:6000]}"
              for n in chain for i in prs[n].get("closingIssuesReferences") or []]
    found = tickets + issues
    missing = [k for k in keys if not any(t.startswith(f"## {k}:") for t in tickets)]
    body = ["# Spec", "", *found] if found else ["# Spec", "", "No ticket or linked issue was found. Use the PR descriptions in `brief.md` as the spec."]
    if missing:
        body += ["", f"Named but not fetched (no Jira access or not found): {', '.join(missing)}"]
    (sdir / "spec.md").write_text("\n\n".join(body) + "\n")


def write_brief(git: Git, sdir: pathlib.Path, chain: list[int], base: str, tip: str, files: list[dict],
                attribution: dict, prs: dict[int, dict], warnings: list[str], since: dict | None = None) -> int:
    """One file holding what the agents need, so they read once instead of exploring turn by turn."""
    parsed = parse_diff(git("diff", "--no-color", "-M", "-U8", base, tip))
    head = [f"# Brief for stack {sdir.name}", "",
            "Line numbers are the tip's. `#N+` marks an added line and the PR that wrote it; only those lines take inline comments.", "",
            "## PRs, bottom first", ""]
    for n in chain:
        p = prs[n]
        head += [f"### #{n}: {p['title']}", f"{p['author']['login']} · {p['headRefName']} → {p['baseRefName']}", "",
                 (p["body"] or "(no description)").strip()[:2500], ""]
    if relevant := [w for w in warnings if any(f"#{n}" in w for n in chain)]:
        head += ["## Warnings", "", *[f"- {w}" for w in relevant], ""]
    if since:
        changed = sorted(since["files"])
        head += ["## Since the viewer's last review", "",
                 f"They last reviewed #{since['pr']} ({since['state'].lower()}) at {since['sha'][:8]} on {since['at'][:10]}. "
                 + (f"{len(changed)} files changed since: " + ", ".join(changed[:60]) + (" ..." if len(changed) > 60 else "") if changed else "Nothing changed since."), ""]
    guidance = sorted(p.name for p in (sdir / "repo-guidance").iterdir())
    head += ["## Repo rules", "", f"In `repo-guidance/`: {', '.join(guidance) or 'none'}.", "",
             "## Changed files", "", "| path | status | +/- | PRs |", "| --- | --- | --- | --- |"]
    head += [f"| {f['path']} | {f['status']} | +{f['adds']} -{f['dels']} | {' '.join(f'#{n}' for n in f['prs'])} |" for f in files]
    sections, omitted = {}, []
    for f in files:
        d = parsed.get(f["path"])
        if not d or not d["hunks"]:
            continue
        if OMIT_PATH.search(f["path"]) or f["adds"] + f["dels"] > 1000:
            omitted.append(f"{f['path']} (+{f['adds']} -{f['dels']})")
        else:
            sections[f["path"]] = f"### {f['path']}\n\n```\n{numbered_diff(d, attribution.get(f['path'], {}))}\n```\n"
    refs = references(git, tip, parsed, {f["path"] for f in files})
    fixed = len("\n".join(head + refs))
    for path in sorted((p for p in sections if TEST_PATH.search(p)), key=lambda p: -len(sections[p])):
        if fixed + sum(map(len, sections.values())) <= BRIEF_BUDGET:
            break
        omitted.append(f"{path} (test)")
        del sections[path]
    body = [*head, "", "## Target diff", "", *sections.values()]
    if omitted:
        body += ["Not shown above; read `tip/<path>` if needed: " + ", ".join(omitted), ""]
    body += ["## Where declared symbols are used", "", *refs]
    body += ["## Removed names still in use, and code left without a caller", "",
             *(leftovers(git, tip, parsed) or ["Nothing the stack removed is still referenced at the tip.", ""])]
    text = "\n".join(body)
    (sdir / "brief.md").write_text(text)
    return len(text)


def existing_comments(repo: str, n: int, viewer: str) -> tuple[list[dict], dict | None]:
    """What reviewers already said, and the viewer's latest submitted review: state, commit and time."""
    inline = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repo}/pulls/{n}/comments", check=False) or "[]")
    reviews = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repo}/pulls/{n}/reviews", check=False) or "[]")
    login = lambda x: (x.get("user") or {}).get("login")  # noqa: E731
    out = [{"user": login(c), "path": c.get("path"), "line": c.get("line"), "body": c["body"]} for page in inline for c in page]
    out += [{"user": login(r), "state": r["state"], "body": r["body"]} for page in reviews for r in page if r.get("body")]
    mine = [r for page in reviews for r in page if login(r) == viewer and r.get("submitted_at") and r["state"] != "PENDING"]
    last = max(mine, key=lambda r: r["submitted_at"], default=None)
    return out, last and {"state": last["state"], "sha": last["commit_id"], "at": last["submitted_at"]}


def changed_since(git: Git, since: str, tip: str, paths: set[str]) -> dict[str, list[int]]:
    """Per stack file changed after `since`: the tip lines added since then."""
    # ponytail: a plain diff, so a rebase also counts base-branch edits to these files; a range-diff would separate them.
    if not paths:
        return {}
    parsed = parse_diff(git("diff", "--no-color", since, tip, "--", *sorted(paths)))
    return {p: d["added"] for p, d in parsed.items() if p in paths}


def last_seen(git: Git, chain: list[int], last: dict[int, dict | None]) -> tuple[dict | None, str | None]:
    """The viewer's latest review on the highest PR of the chain they reviewed: what they last saw of the stack."""
    n = next((n for n in reversed(chain) if last.get(n)), None)
    if n is None:
        return None, None
    seen = {"pr": n, **last[n]}
    if not git.ok("cat-file", "-e", f"{seen['sha']}^{{commit}}"):
        git("fetch", "--quiet", "--no-tags", "origin", seen["sha"], check=False)
    if not git.ok("cat-file", "-e", f"{seen['sha']}^{{commit}}"):
        return None, f"your last review of #{n} is on {seen['sha'][:8]}, which could not be fetched; showing the whole stack"
    return seen, None


def checks_summary(rollup: list[dict]) -> dict:
    failed, pending, passed = [], 0, 0
    for c in rollup or []:
        state = (c.get("conclusion") or c.get("state") or "").upper()
        if state in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"):
            failed.append(c.get("name") or c.get("context") or "?")
        elif state in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            passed += 1
        else:
            pending += 1
    return {"failed": sorted(set(failed)), "pending": pending, "passed": passed}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir")
    ap.add_argument("--effort", choices=EFFORTS, default="balanced")
    ap.add_argument("prs", nargs="+")
    args = ap.parse_args()

    refs = []
    for arg in args.prs:
        m = PR_REF.search(arg)
        if not m:
            sys.exit(f"not a PR reference: {arg}")
        refs.append((f"{m.group(1)}/{m.group(2)}", int(m.group(3))))
    if len({r for r, _ in refs}) != 1:
        sys.exit(f"all PRs must be in one repository, got {sorted({r for r, _ in refs})}")
    repo = refs[0][0]
    numbers = sorted({n for _, n in refs})

    fields = "number,title,body,closingIssuesReferences,author,baseRefName,baseRefOid,headRefName,headRefOid,isDraft,state,additions,deletions,url,statusCheckRollup"
    prs = {n: json.loads(gh("pr", "view", str(n), "-R", repo, "--json", fields)) for n in numbers}
    heads = {n: p["headRefOid"] for n, p in prs.items()}
    viewer = gh("api", "user", "--jq", ".login").strip()
    run_id = hashlib.sha1(json.dumps(sorted(heads.items())).encode(), usedforsecurity=False).hexdigest()[:10]
    workdir = pathlib.Path(args.workdir or CACHE / f"{repo.replace('/', '-')}-{'-'.join(map(str, numbers))}-{run_id}")
    workdir.mkdir(parents=True, exist_ok=True)

    git, checkout = open_repo(repo)
    cached = checkout is None
    checkout = checkout or git.prefix[2]
    specs = [f"+pull/{n}/head:refs/review-guide/{n}" for n in numbers] + [f"+{p['baseRefOid']}:refs/review-guide/base/{n}" for n, p in prs.items()]
    git("fetch", "--quiet", "--no-tags", "origin", *specs)
    chains, warnings = link_stacks(git, prs, heads)
    review: dict[int, str] = {}
    for chain in chains:
        shas, lag_warnings = review_shas(git, chain, heads)
        review |= shas
        warnings += lag_warnings
    if cached:
        # The cache clone is blobless: fetch each tip's tree once, so symbol search and the agents' git grep stay local.
        for chain in chains:
            prefetch_tree(git, review[chain[-1]], workdir / f"tree-{chain[-1]}")

    pr_files: dict[int, set[str]] = {}
    last: dict[int, dict | None] = {}
    for n in numbers:
        prdir = workdir / str(n)
        prdir.mkdir(exist_ok=True)
        # The PR's own diff at the commit it is reviewed at, which is what a comment on it is anchored to.
        diff = git("diff", "--no-color", "-M", git("merge-base", f"refs/review-guide/base/{n}", review[n]).strip(), review[n])
        parsed = parse_diff(diff)
        pr_files[n] = set(parsed) | {f["old"] for f in parsed.values() if f["old"]}
        (prdir / "pr.diff").write_text(diff)
        (prdir / "hunks.json").write_text(json.dumps({p: {"added": f["added"], "commentable": f["commentable"]} for p, f in parsed.items()}))
        existing, last[n] = existing_comments(repo, n, viewer)
        (prdir / "existing-comments.json").write_text(json.dumps(existing, indent=1))

    stacks = []
    for chain in chains:
        sid = "-".join(map(str, chain))
        sdir = workdir / sid
        (sdir / "tip").mkdir(parents=True, exist_ok=True)
        base = git("merge-base", f"refs/review-guide/base/{chain[0]}", review[chain[0]]).strip()
        tip = review[chain[-1]]
        diff = git("diff", "--no-color", "-M", base, tip)
        (sdir / "target.diff").write_text(diff)
        parsed = parse_diff(diff)
        owners = commit_owners(git, base, chain, review)
        files = changed_files(git, base, tip)

        def collect(f: dict) -> tuple[str, dict | None]:
            if f["status"] == "deleted":
                return f["path"], None
            text = git.text(tip, f["path"])
            if text is not None:
                dest = sdir / "tip" / f["path"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text)
            return f["path"], attribute(git, base, chain, review, owners, f["path"])

        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            attribution = {path: attr for path, attr in pool.map(collect, files) if attr is not None}
        (sdir / "attribution.json").write_text(json.dumps(attribution))
        for f in files:
            hunks = parsed.get(f["path"], {}).get("hunks", [])
            f["adds"] = sum(line["k"] == "+" for hunk in hunks for line in hunk)
            f["dels"] = sum(line["k"] == "-" for hunk in hunks for line in hunk)
            f["prs"] = [n for n in chain if f["path"] in pr_files[n] or f["old"] in pr_files[n]]
        guidance = sdir / "repo-guidance"
        guidance.mkdir(exist_ok=True)
        for name in GUIDANCE_FILES:
            text = git.text(tip, name)
            if text is not None:
                (guidance / name.replace("/", "__")).write_text(text)
        since, problem = last_seen(git, chain, last)
        if problem:
            warnings.append(problem)
        if since:
            since["files"] = changed_since(git, since["sha"], tip, {f["path"] for f in files if f["status"] != "deleted"})
        stacks.append({"id": sid, "prs": chain, "base": base, "tip": tip, "files": files, "since": since})
        size = write_brief(git, sdir, chain, base, tip, files, attribution, prs, warnings, since)
        write_spec(sdir, chain, prs)
        print(f"stack {sid}: brief {size // 1000} KB")

    stack_of = {n: s["id"] for s in stacks for n in s["prs"]}
    out_prs = [{
        "n": n, "url": p["url"], "title": p["title"], "body": p["body"] or "", "author": p["author"]["login"],
        "base": p["baseRefName"], "head": p["headRefName"], "sha": p["headRefOid"], "review_sha": review[n], "draft": p["isDraft"], "state": p["state"],
        "adds": p["additions"], "dels": p["deletions"], "stack": stack_of[n], "checks": checks_summary(p["statusCheckRollup"]),
        "last_review": last[n],
    } for n, p in prs.items()]
    run_data = {"repo": repo, "viewer": viewer, "effort": args.effort, "run_id": run_id, "fetched_at": int(time.time()), "checkout": checkout,
                "warnings": warnings, "prs": out_prs, "stacks": stacks}
    (workdir / "run.json").write_text(json.dumps(run_data, indent=1))

    for s in stacks:
        print(f"stack {s['id']}: {' -> '.join(f'#{n}' for n in s['prs'])}, {len(s['files'])} files in the target")
    for w in warnings:
        print(f"warning: {w}")
    print(f"code search runs in {checkout}")
    print(workdir)


if __name__ == "__main__":
    main()
