"""The guide a model writes as HTML, made safe for the page.

The page puts a guide's HTML into its own document, next to the posting token, so nothing the model wrote is trusted:
`parse` reads it into a tree and keeps only the tags, attributes, classes and style values below, and `render` writes
that tree back out. Anything else is dropped (scripts, handlers, links, images, hex colors, ids) and named in `notes`;
`build.py --check` fails on a non-empty `notes` so the writer fixes its file, and a plain build just uses the clean tree.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

HTML_TAGS = {"h2", "h3", "h4", "p", "ul", "ol", "li", "code", "pre", "em", "strong", "table", "thead", "tbody", "tr", "th", "td",
             "figure", "figcaption", "section", "details", "summary", "blockquote", "span", "div", "br"}
SVG_TAGS = {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "tspan", "title", "desc"}
ALLOWED = HTML_TAGS | SVG_TAGS
# Dropped without a note: they carry nothing the page uses, and failing a picture over them only costs a retry.
HARMLESS = {"xmlns", "xmlns:xlink", "version", "font-family", "aria-hidden", "focusable"}
INLINE = {"code", "em", "strong", "span", "tspan"}
VOID = {"br", "hr", "img", "link", "meta", "input", "base", "col", "wbr"}
# Dropped with everything inside them.
DROP = {"script", "style", "iframe", "object", "embed", "template", "noscript", "form", "textarea", "select", "option", "button",
        "input", "link", "meta", "head", "base", "audio", "video", "canvas", "math", "img", "foreignobject", "use", "image", "a"}
CLASSES = {"callout", "facts", "cols", "muted", "tag-new", "tag-changed", "tag-removed"}
# The page's own colors; the guide has no others, so it follows the theme.
COLORS = {"ink", "muted", "line", "card", "accent", "accent-soft", "warn", "warn-soft", "good", "good-soft"}
MARKERS = {"url(#rg-arrow)", "url(#rg-arrow-dash)"}
MAX_DEPTH = 40

NUM = r"-?(?:\d+(?:\.\d+)?|\.\d+)(?:px|%|em)?"
KEYWORD = {"none", "currentColor", "transparent", "bold", "normal", "italic", "middle", "start", "end", "central", "hanging", "auto",
           "round", "butt", "square", "miter", "bevel", "solid", "dashed"}
VAR = re.compile(r"^var\(--([a-z0-9-]+)\)$")
NUM_RE = re.compile(rf"^{NUM}$")
LIST_RE = re.compile(rf"^(?:{NUM})(?:[\s,]+(?:{NUM}))*$")
PATH_RE = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9\s.,eE+-]*$")
TRANSFORM_RE = re.compile(r"^(?:\s*(?:translate|rotate|scale|matrix)\([\-\d\s.,eE+]*\))+$")
AT_RE = re.compile(r"^[^\s:\"'<>]+:\d+(?:-\d+)?$")
REF_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,48}$")  # figure parts are prefixed `<figure>--<part>` by figures.fill
STYLE_PROPS = {"fill", "stroke", "stroke-width", "stroke-dasharray", "opacity", "color", "background", "text-anchor", "font-weight", "font-style", "font-size"}


def token(v: str) -> bool:
    """One value of a style or paint attribute: a number, a keyword or one of the page's colors."""
    m = VAR.match(v)
    return bool(m and m.group(1) in COLORS) or v in KEYWORD or bool(NUM_RE.match(v))


def paint(v: str) -> bool:
    m = VAR.match(v)
    return v in ("none", "currentColor", "transparent") or bool(m and m.group(1) in COLORS)


def keyword(*words):
    return lambda v: v in words


# attribute (as the parser lowercases it) -> (name written out, accepts the value)
ATTRS = {
    "viewbox": ("viewBox", lambda v: bool(LIST_RE.match(v))), "preserveaspectratio": ("preserveAspectRatio", keyword("xMidYMid meet", "none")),
    **{a: (a, lambda v: bool(NUM_RE.match(v))) for a in ("x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "stroke-width", "opacity", "font-size")},
    "d": ("d", lambda v: bool(PATH_RE.match(v))), "points": ("points", lambda v: bool(LIST_RE.match(v))),
    "transform": ("transform", lambda v: bool(TRANSFORM_RE.match(v))), "stroke-dasharray": ("stroke-dasharray", lambda v: bool(LIST_RE.match(v)) or v == "none"),
    "fill": ("fill", paint), "stroke": ("stroke", paint),
    "marker-end": ("marker-end", lambda v: v in MARKERS), "marker-start": ("marker-start", lambda v: v in MARKERS),
    "stroke-linecap": ("stroke-linecap", keyword("round", "butt", "square")), "stroke-linejoin": ("stroke-linejoin", keyword("round", "miter", "bevel")),
    "text-anchor": ("text-anchor", keyword("start", "middle", "end")), "dominant-baseline": ("dominant-baseline", keyword("middle", "central", "hanging", "auto")),
    "font-weight": ("font-weight", lambda v: v in ("normal", "bold") or bool(re.match(r"^[1-9]00$", v))),
    "font-family": ("font-family", lambda v: v == "inherit"),
    "colspan": ("colspan", lambda v: v.isdigit() and int(v) < 20), "rowspan": ("rowspan", lambda v: v.isdigit() and int(v) < 20),
    "open": ("open", lambda v: v == ""), "role": ("role", keyword("img")),
    "data-at": ("data-at", lambda v: bool(AT_RE.match(v))), "data-ref": ("data-ref", lambda v: bool(REF_RE.match(v))),
    "data-step": ("data-step", lambda v: v == ""), "data-decide": ("data-decide", lambda v: v == ""),
    "data-kind": ("data-kind", keyword("map", "timeline", "sequence", "lifecycle", "decision", "data", "before-after")),
    "data-figure": ("data-figure", lambda v: bool(REF_RE.match(v))),
}


class Node:
    __slots__ = ("tag", "attrs", "kids")

    def __init__(self, tag: str | None, attrs: dict | None = None):
        self.tag, self.attrs, self.kids = tag, attrs or {}, []


def clean_style(value: str, notes: set[str]) -> str:
    out = []
    for decl in value.split(";"):
        if not decl.strip():
            continue
        prop, _, val = decl.partition(":")
        prop, parts = prop.strip().lower(), val.split()
        if prop == "font-family" and parts == ["inherit"]:
            continue  # SVG text inherits the page's font already
        if prop == "font-size" and len(parts) == 1 and re.match(r"^\d+(?:\.\d+)?$", parts[0]):
            parts = [parts[0] + "px"]  # a bare number is what SVG means and CSS drops: viewBox units, written as px
        if prop in STYLE_PROPS and parts and all(token(p) for p in parts):
            out.append(f"{prop}:{' '.join(parts)}")
        else:
            notes.add(f"style `{decl.strip()[:40]}` removed (only the page's colors and plain numbers are allowed)")
    return ";".join(out)


def clean_attrs(tag: str, attrs: list, notes: set[str]) -> dict:
    out: dict[str, str] = {}
    for name, value in attrs:
        name, value = name.lower(), (value or "").strip()
        if name == "class":
            keep = [c for c in value.split() if c in CLASSES]
            if len(keep) != len(value.split()):
                notes.add(f"class `{value[:40]}` has names the page does not know")
            if keep:
                out["class"] = " ".join(keep)
        elif name == "style":
            if s := clean_style(value, notes):
                out["style"] = s
        elif name in ATTRS and ATTRS[name][1](value):
            out[ATTRS[name][0]] = value
        elif name in HARMLESS:
            continue  # boilerplate an SVG editor writes; dropping it changes nothing on the page
        else:
            notes.add(f"attribute `{name}` on <{tag}> removed")
    return out


class Reader(HTMLParser):
    def __init__(self, notes: set[str]):
        super().__init__(convert_charrefs=True)
        self.root = Node(None)
        self.open = [self.root]
        self.notes, self.skip, self.skipped = notes, 0, ""

    def start(self, tag: str, attrs: list, empty: bool) -> None:
        if self.skip:
            self.skip += tag == self.skipped and not empty
            return
        if tag in DROP:
            self.notes.add(f"<{tag}> removed")
            if not empty and tag not in VOID:
                self.skip, self.skipped = 1, tag
            return
        if tag not in ALLOWED or len(self.open) > MAX_DEPTH:
            self.notes.add(f"<{tag}> is not allowed; its text was kept")
            return
        node = Node(tag, clean_attrs(tag, attrs, self.notes))
        self.open[-1].kids.append(node)
        if not empty and tag not in VOID:
            self.open.append(node)

    def handle_starttag(self, tag, attrs):
        self.start(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self.start(tag, attrs, True)

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skipped:
                self.skip -= 1
            return
        for i in range(len(self.open) - 1, 0, -1):
            if self.open[i].tag == tag:
                del self.open[i:]
                return

    def handle_data(self, data):
        if not self.skip and data:
            kids = self.open[-1].kids
            if kids and isinstance(kids[-1], str):
                kids[-1] += data
            else:
                kids.append(data)


def parse(fragment: str, notes: set[str] | None = None) -> list:
    """The allowed part of an HTML fragment, as a list of nodes and strings."""
    reader = Reader(notes if notes is not None else set())
    reader.feed(fragment)
    reader.close()
    return reader.root.kids


def render(nodes: list) -> str:
    out = []
    for n in nodes:
        if isinstance(n, str):
            out.append(html.escape(n, quote=False))
            continue
        attrs = "".join(f' {k}="{html.escape(v, quote=True)}"' if v != "" else f" {k}" for k, v in n.attrs.items())
        out.append(f"<{n.tag}{attrs}>" if n.tag in VOID else f"<{n.tag}{attrs}>{render(n.kids)}</{n.tag}>")
    return "".join(out)


def text(nodes) -> str:
    """The words in a node or a list of nodes, as one line."""
    if isinstance(nodes, Node):
        nodes = [nodes]
    # Inline tags join their text as is; any other tag (a cell, an item, a paragraph) is a word break.
    return " ".join("".join(n if isinstance(n, str) else text(n.kids) if n.tag in INLINE else f" {text(n.kids)} " for n in nodes).split())


def prose_words(nodes: list, inside: bool = False) -> int:
    """Words of running text: tables, pictures and code blocks do not count."""
    n = 0
    for x in nodes:
        if isinstance(x, str):
            n += 0 if inside else len(x.split())
        else:
            n += prose_words(x.kids, inside or x.tag in ("table", "svg", "pre", "figcaption"))
    return n


CHAR_WIDTH = 0.58  # Figtree's average advance per font-size unit, measured in Chrome; capitals and wide glyphs run over


def _num(v, default=0.0) -> float:
    try:
        return float(str(v).removesuffix("px"))
    except (TypeError, ValueError):
        return default


def _font_size(n: Node) -> float:
    m = re.search(r"font-size:([\d.]+)", n.attrs.get("style", ""))
    return _num(m.group(1) if m else n.attrs.get("font-size"), 13.0)


def overflowing_labels(nodes: list) -> list[str]:
    """Picture labels that run past the picture's edge, or out of the box they sit in, estimated from character count:
    the author never sees the page, so a label that does not fit is caught here."""
    out = []
    for svg in (n for n in walk(nodes) if n.tag == "svg"):
        vb = [_num(x) for x in re.split(r"[\s,]+", svg.attrs.get("viewBox", "").strip())] if svg.attrs.get("viewBox") else []
        width = vb[2] if len(vb) == 4 else None
        for g in [svg, *(n for n in walk(svg.kids) if n.tag == "g")]:
            rects = [k for k in g.kids if isinstance(k, Node) and k.tag == "rect"]
            for t in (k for k in g.kids if isinstance(k, Node) and k.tag == "text"):
                label = text(t)
                if not label or "x" not in t.attrs:
                    continue
                w, x = len(label) * CHAR_WIDTH * _font_size(t), _num(t.attrs["x"])
                anchor = t.attrs.get("text-anchor") or (re.search(r"text-anchor:(\w+)", t.attrs.get("style", "")) or [None, "start"])[1]
                left = x - w / 2 if anchor == "middle" else x - w if anchor == "end" else x
                if width is not None and (left < (vb[0] if vb else 0) - 2 or left + w > (vb[0] if vb else 0) + width + 2):
                    out.append(f"{label!r} runs past the picture's edge (about {round(w)} wide at x {round(left)}, viewBox {round(width)})")
                    continue
                y = _num(t.attrs.get("y"))
                for r in rects:
                    rx, ry, rw, rh = (_num(r.attrs.get(k)) for k in ("x", "y", "width", "height"))
                    inside_y = ry <= y <= ry + rh
                    centred = rx <= x <= rx + rw
                    if inside_y and centred and (left < rx - 2 or left + w > rx + rw + 2):
                        out.append(f"{label!r} is about {round(w)} wide in a box {round(rw)} wide")
    return out


def walk(nodes: list):
    for n in nodes:
        if isinstance(n, Node):
            yield n
            yield from walk(n.kids)


def find(nodes: list, ref: str) -> Node | None:
    return next((n for n in walk(nodes) if n.attrs.get("data-ref") == ref), None)


def clean(fragment: str) -> str:
    """A fragment as the page will show it."""
    return render(parse(fragment))
