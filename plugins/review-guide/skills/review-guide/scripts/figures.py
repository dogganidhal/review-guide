"""Pictures of the HTML guide, drawn by their own agents while the author writes the prose.

The author puts a placeholder in a section, `<figure data-kind="map" data-figure="ref"><figcaption>…</figcaption></figure>`,
and writes `figures/<ref>.json` (what the picture must show). The runner starts a figure artist per spec as soon as it
lands; the artist writes `figures/<ref>.svg`. `fill` swaps each placeholder's body for its SVG, or for a note while it is
being drawn, and `check` holds the rules: every placeholder has a spec, every spec a placeholder, the SVG passes the
sanitizer and fits its labels, and the guide has the pictures its stack calls for.
"""

from __future__ import annotations

import json
import pathlib
import re

import guidehtml

KINDS = ("map", "timeline", "sequence", "lifecycle", "decision", "data", "before-after")
# A stack with this many changed production lines gets a map of where the change runs.
MAP_FROM_LINES = 200
MAX_FIGURES, MAX_SVG = 6, 40_000
SPEC_FIELDS = {"kind": 20, "question": 200, "show": 1500}


def spec_dir(sdir: pathlib.Path) -> pathlib.Path:
    return sdir / "figures"


def specs(sdir: pathlib.Path) -> dict[str, dict]:
    """The specs written so far, by ref. A spec still being written (not valid JSON) is left out until it is."""
    out = {}
    for p in sorted(spec_dir(sdir).glob("*.json")) if spec_dir(sdir).is_dir() else []:
        try:
            spec = json.loads(p.read_text())
        except ValueError:
            continue
        if isinstance(spec, dict):
            out[p.stem] = spec
    return out


def placeholders(nodes: list) -> list[guidehtml.Node]:
    return [n for n in guidehtml.walk(nodes) if n.tag == "figure" and n.attrs.get("data-figure")]


def svg_of(sdir: pathlib.Path, ref: str, notes: set[str] | None = None) -> list | None:
    """The artist's picture as sanitized nodes, or None while it is not drawn yet."""
    p = spec_dir(sdir) / f"{ref}.svg"
    if not p.is_file():
        return None
    nodes = guidehtml.parse(p.read_text(), notes)
    return [n for n in nodes if isinstance(n, guidehtml.Node) and n.tag == "svg"][:1] or None


def fill(nodes: list, sdir: pathlib.Path) -> None:
    """Put each drawn picture into its placeholder, before the caption; an undrawn one gets a note the page shows as pending.
    The picture's own `data-ref`s are prefixed with its ref: artists name parts on their own ("legend", "lane-1"), and the
    page needs them unique across the guide."""
    for fig in placeholders(nodes):
        ref = fig.attrs["data-figure"]
        caption = [k for k in fig.kids if isinstance(k, guidehtml.Node) and k.tag == "figcaption"]
        drawn = svg_of(sdir, ref)
        for n in guidehtml.walk(drawn or []):
            if "data-ref" in n.attrs:
                n.attrs["data-ref"] = f"{ref}--{n.attrs['data-ref']}"[:49]
        body = drawn if drawn else [guidehtml.Node("div", {"class": "muted"})]
        if not drawn:
            body[0].kids.append("This picture is being drawn.")
        fig.kids = [*body, *caption]


def check(nodes: list, sdir: pathlib.Path, prod_lines: int, timing_required: bool, done: bool, err) -> None:
    """The figure rules for --check. While the author writes, only what exists is checked; `done` adds the required kinds."""
    figs = placeholders(nodes)
    refs = [f.attrs["data-figure"] for f in figs]
    have = specs(sdir)
    if len(refs) != len(set(refs)):
        err("guide: a data-figure ref is used twice")
    if len(refs) > MAX_FIGURES:
        err(f"guide: {len(refs)} pictures; keep the {MAX_FIGURES} that carry the most")
    for f in figs:
        ref, kind = f.attrs["data-figure"], f.attrs.get("data-kind")
        if kind not in KINDS:
            err(f"figure {ref!r}: data-kind must be one of {KINDS}")
        if not any(isinstance(k, guidehtml.Node) and k.tag == "figcaption" for k in f.kids):
            err(f"figure {ref!r}: give it a <figcaption> that states what the picture shows")
        spec = have.get(ref)
        if spec is None:
            err(f"figure {ref!r}: write its spec, figures/{ref}.json, so an artist can draw it")
            continue
        for k, limit in SPEC_FIELDS.items():
            v = spec.get(k)
            if not isinstance(v, str) or not v.strip() or len(v) > limit:
                err(f"figures/{ref}.json: {k} is text of at most {limit} characters")
        if spec.get("kind") != kind:
            err(f"figures/{ref}.json: kind {spec.get('kind')!r} differs from the placeholder's data-kind {kind!r}")
        at = spec.get("at")
        if not isinstance(at, list) or not at or not all(isinstance(a, str) for a in at):
            err(f"figures/{ref}.json: at lists the path:line references the picture is drawn from")
        # The drawn SVG is the artist's, checked by its own `--only figure:<ref>`; the author cannot fix it.
    for ref in sorted(set(have) - set(refs)):
        err(f"figures/{ref}.json has no placeholder; add <figure data-figure=\"{ref}\"> to a section or delete the spec")
    if done:
        kinds = {f.attrs.get("data-kind") for f in figs}
        if prod_lines >= MAP_FROM_LINES and "map" not in kinds:
            err(f"guide: a stack of {prod_lines} changed production lines needs a map (data-kind=\"map\") of where the change runs")
        if timing_required and "timeline" not in kinds:
            err("guide: the brief lists time-dependent behavior; add a timeline (data-kind=\"timeline\") of how it plays out")


def check_one(sdir: pathlib.Path, ref: str, err) -> None:
    """One artist's picture: its spec exists, its SVG is there, passes the sanitizer, cites real code and fits its labels."""
    if not (spec_dir(sdir) / f"{ref}.json").is_file():
        return err(f"no spec figures/{ref}.json")
    if not drawn(sdir, ref):
        return err(f"figures/{ref}.svg is missing")
    notes: set[str] = set()
    picture = svg_of(sdir, ref, notes)
    if picture is None:
        return err(f"figures/{ref}.svg holds no <svg> element")
    if notes:
        err(f"figures/{ref}.svg: " + "; ".join(sorted(notes)[:8]) + ". Use only the elements, attributes and colors guide-schema.md lists")
    if len((spec_dir(sdir) / f"{ref}.svg").read_text()) > MAX_SVG:
        err(f"figures/{ref}.svg is over {MAX_SVG} characters; simplify it")
    if "viewBox" not in picture[0].attrs:
        err(f"figures/{ref}.svg: give the <svg> a viewBox sized to its content")
    if over := guidehtml.overflowing_labels(picture):
        err(f"figures/{ref}.svg: {len(over)} labels do not fit; widen the box or the viewBox, or shorten the label: " + "; ".join(over[:4]))


def drawn(sdir: pathlib.Path, ref: str) -> bool:
    return (spec_dir(sdir) / f"{ref}.svg").is_file()


def pending(nodes: list, sdir: pathlib.Path) -> list[str]:
    """Placeholders whose picture is not drawn yet."""
    return [f.attrs["data-figure"] for f in placeholders(nodes) if not drawn(sdir, f.attrs["data-figure"])]


REF = re.compile(r"^[a-z0-9][a-z0-9_-]{0,48}$")
