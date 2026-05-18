#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pyyaml>=6.0",
#   "pdfplumber>=0.10",
# ]
# ///
"""Build an OpenSign-ready template package from a Markdown source.

Outputs two files:

  template.pdf
      Final PDF to upload to OpenSign. Contains the document text plus
      invisible (white-rendered) WMK<key>WMK markers wherever a widget
      should sit — recoverable from the PDF text stream but not visible
      on the page.

  placeholders.json
      Placeholder JSON in OpenSign's contracts_Template shape.

Pipeline:
  1. Substitute $$NAME placeholders from config.yaml.
  2. Replace each ‹w:KEY› anchor with a raw LaTeX zero-width invisible
     marker.
  3. Render one PDF via pandoc + xelatex.
  4. pdfplumber extracts both the (invisible) marker positions and the
     adjacent underscore runs.
  5. Each anchor is matched to its underscore run (same line or just below)
     and the widget's bounding box is MEASURED from the rendered PDF.
  6. Build the placeholders JSON grouped by role from widgets.yaml.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pdfplumber
import yaml

ANCHOR_RE = re.compile(r"‹w:([a-z_]+)›")
WMK_RE = re.compile(r"WMK([a-zA-Z0-9-]+)WMK")
PLACEHOLDER_RE = re.compile(r"\$\$([A-Z][A-Z0-9_]*)")
SIGNATURE_TYPES = {"signature", "initials", "stamp"}

DEFAULT_ROLE_COLOR = "#93a3db"


@dataclass
class Run:
    """A horizontal run of `_` characters on one line."""

    x0: float
    x1: float
    top: float
    bottom: float


@dataclass
class Anchor:
    """A WMK marker found in the rendered PDF, plus the matched underscore
    run and the per-page char list used to measure surrounding text."""

    key: str
    page: int
    x_left: float
    y_top: float
    x_right: float
    run: Run | None
    all_runs: list[Run]
    page_chars: list[dict[str, Any]]


# -- markdown preprocessing -------------------------------------------------


def substitute_config(template: str, config: dict[str, Any]) -> str:
    """Replace `$$NAME` tokens with `config[NAME]` (case-insensitive)."""
    upper = {k.upper(): v for k, v in config.items()}
    missing: set[str] = set()

    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in upper:
            return str(upper[key])
        missing.add(key)
        return m.group(0)

    out = PLACEHOLDER_RE.sub(repl, template)
    if missing:
        print(
            f"warning: unresolved $$ placeholders: {sorted(missing)}",
            file=sys.stderr,
        )
    return out


def make_anchors_invisible(markdown: str) -> str:
    """Replace each `‹w:KEY›` anchor with raw LaTeX that renders the key as a
    WMK<key>WMK marker in white inside a zero-width left-aligned makebox.

    The marker has zero horizontal width and stays on the same line as
    whatever follows. `\\rlap{}` works in plain TeX but pandoc's output
    sometimes drops the underscores onto the next line; `\\makebox[0pt][l]`
    keeps them in-line. Underscores in keys are mapped to hyphens so they
    don't trip LaTeX math mode.
    """

    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        marker = "WMK" + key.replace("_", "-") + "WMK"
        return r"\makebox[0pt][l]{\textcolor{white}{" + marker + "}}"

    return ANCHOR_RE.sub(repl, markdown)


def render_pdf(markdown: str, output: Path) -> None:
    """Render the (preprocessed) markdown to PDF via pandoc + xelatex."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", encoding="utf-8", delete=False
    ) as f:
        f.write(markdown)
        src = Path(f.name)
    try:
        result = subprocess.run(
            [
                "pandoc",
                "--from=markdown",
                "--pdf-engine=xelatex",
                "--output",
                str(output),
                str(src),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            sys.exit(f"pandoc PDF failed:\n{result.stderr}")
    finally:
        src.unlink(missing_ok=True)


# -- PDF extraction ---------------------------------------------------------


def _underscore_runs(chars: list[dict[str, Any]]) -> list[Run]:
    us = [c for c in chars if c["text"] == "_"]
    us.sort(key=lambda c: (round(c["top"], 0), c["x0"]))
    runs: list[Run] = []
    current: Run | None = None
    for c in us:
        if (
            current is not None
            and abs(c["top"] - current.top) < 2
            and c["x0"] - current.x1 < 5
        ):
            current.x1 = c["x1"]
            current.bottom = max(current.bottom, c["bottom"])
        else:
            if current is not None:
                runs.append(current)
            current = Run(x0=c["x0"], x1=c["x1"], top=c["top"], bottom=c["bottom"])
    if current is not None:
        runs.append(current)
    return runs


def _markers(chars: list[dict[str, Any]]) -> dict[str, tuple[float, float, float]]:
    """Find each WMK<key>WMK marker on the page, returning
    {key: (x_left, y_top, x_right)}.

    Some xelatex output uses LIGATURES (single `chars` entries whose `text`
    is multiple characters like "ff", "fi"), so we maintain an index map
    from buf-string position to chars-list index. We also pick the
    leftmost/topmost/rightmost char position because chars can be emitted to
    the PDF stream out of left-to-right order within an `\\makebox`.
    """
    if not chars:
        return {}
    buf_parts: list[str] = []
    idx_map: list[int] = []
    for ci, c in enumerate(chars):
        t = c["text"]
        buf_parts.append(t)
        idx_map.extend([ci] * len(t))
    buf = "".join(buf_parts)
    found: dict[str, tuple[float, float, float]] = {}
    for m in WMK_RE.finditer(buf):
        key = m.group(1).replace("-", "_")
        if key in found:
            continue
        marker_chars = chars[idx_map[m.start()] : idx_map[m.end() - 1] + 1]
        x_left = min(c["x0"] for c in marker_chars)
        x_right = max(c["x1"] for c in marker_chars)
        y_top = min(c["top"] for c in marker_chars)
        found[key] = (x_left, y_top, x_right)
    return found


def _match_run(
    x_left: float, y_top: float, x_right: float, runs: list[Run]
) -> Run | None:
    """Match a marker to its underscore run.

    The marker is rendered with zero horizontal advance (`\\makebox[0pt][l]`),
    so the underscores that follow start at the marker's LEFT x — not its
    right. Prefer a same-line run starting at/after the marker's left;
    otherwise take the closest run within ~60pt below.
    """
    same_line = [
        r for r in runs if abs(r.top - y_top) < 6 and r.x0 >= x_left - 3
    ]
    if same_line:
        return min(same_line, key=lambda r: r.x0)
    below = [r for r in runs if 4 <= (r.top - y_top) < 60]
    if not below:
        return None
    below.sort(key=lambda r: r.top)
    nearest_top = below[0].top
    same_below = [r for r in below if abs(r.top - nearest_top) < 2]
    return min(same_below, key=lambda r: r.x0)


def find_anchors(pdf_path: Path) -> dict[str, Anchor]:
    out: dict[str, Anchor] = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages, start=1):
            chars = page.chars
            runs = _underscore_runs(chars)
            for key, (x_left, y_top, x_right) in _markers(chars).items():
                if key in out:  # first occurrence wins
                    continue
                run = _match_run(x_left, y_top, x_right, runs)
                out[key] = Anchor(
                    key=key,
                    page=page_idx,
                    x_left=x_left,
                    y_top=y_top,
                    x_right=x_right,
                    run=run,
                    all_runs=runs,
                    page_chars=chars,
                )
    return out


# -- field geometry ---------------------------------------------------------


def _is_real_text(c: dict[str, Any]) -> bool:
    """True if the char is a normal visible glyph.

    Excludes underscores, whitespace (space, NBSP, tab), and white-rendered
    text (our invisible WMK markers — `non_stroking_color` is all 1.0 in
    gray or RGB).
    """
    t = c["text"]
    if not t:
        return False
    color = c.get("non_stroking_color")
    if color is not None and all(v >= 0.95 for v in color):
        return False
    return any(ch not in (" ", " ", "\t", "_") for ch in t)


def _field_top(run: Run, page_chars: list[dict[str, Any]]) -> float:
    """The y at which the field begins.

    - INLINE (run shares a baseline with real text): the field is one line
      tall, top is just above the run.
    - BLOCK: walk up to the nearest real-text line and treat the gap as
      drawing space (so a signature with a blank `&nbsp;` line above its
      underscore gets ~28pt of height for free).
    """
    same_line_text = [
        c
        for c in page_chars
        if _is_real_text(c) and abs(c["bottom"] - run.bottom) < 5
    ]
    if same_line_text:
        return run.top - 2
    cutoff = run.top - 200
    prev_text = [
        c
        for c in page_chars
        if _is_real_text(c) and cutoff < c["bottom"] < run.top
    ]
    if not prev_text:
        return run.top - 2
    return max(c["bottom"] for c in prev_text) + 2


def _bottommost_run(
    first: Run, all_runs: list[Run], max_field_height: float
) -> Run:
    """Bottommost run in a consecutive multi-line field (same x, line height
    spacing). For a single-line field returns `first`.
    """
    limit_y = first.top + max_field_height + 8
    candidates = [
        r
        for r in all_runs
        if r.top > first.top + 4
        and r.top <= limit_y
        and abs(r.x0 - first.x0) < 15
    ]
    candidates.sort(key=lambda r: r.top)
    bottom = first
    prev_top = first.top
    for r in candidates:
        if r.top - prev_top > 22:  # gap larger than a line — stop
            break
        bottom = r
        prev_top = r.top
    return bottom


def _is_inline(run: Run, page_chars: list[dict[str, Any]]) -> bool:
    return any(
        _is_real_text(c) and abs(c["bottom"] - run.bottom) < 5
        for c in page_chars
    )


# -- widget assembly --------------------------------------------------------


def build_widget(
    anchor: Anchor, spec: dict[str, Any], defaults: dict[str, Any]
) -> dict[str, Any] | None:
    """Build the OpenSign widget dict for one anchor, or None if the anchor
    couldn't be matched to any underscore run.
    """
    run = anchor.run
    if run is None:
        print(
            f"warning: no underscore run matched anchor {anchor.key!r}",
            file=sys.stderr,
        )
        return None

    bottom_run = _bottommost_run(run, anchor.all_runs, 250)
    field_top = _field_top(run, anchor.page_chars)
    measured_w = run.x1 - run.x0
    measured_h = bottom_run.bottom - field_top + 2

    w = spec.get("w", measured_w)
    h = spec.get("h", measured_h)
    # Inline widgets sit on the surrounding text baseline; block widgets get
    # a small descender overhang.
    bottom_pad = 0 if _is_inline(run, anchor.page_chars) else 2
    x = round(run.x0 + spec.get("x_offset", 0), 2)
    y = round(bottom_run.bottom - h + bottom_pad + spec.get("y_offset", 0), 2)

    required = spec.get("required", defaults.get("required", True))
    options: dict[str, Any] = {
        "name": spec.get("name", anchor.key),
        "status": "required" if required else "optional",
        "response": "",
    }
    if "hint" in spec:
        options["hint"] = spec["hint"]
    if "default" in spec:
        options["defaultValue"] = spec["default"]

    widget: dict[str, Any] = {
        "_page": anchor.page,
        "type": spec["type"],
        "isStamp": False,
        "key": anchor.key,
        "xPosition": x,
        "yPosition": y,
        "Width": w,
        "Height": h,
        "options": options,
    }
    if spec["type"] in SIGNATURE_TYPES:
        widget["signatureType"] = ""
    return widget


def build_placeholders(
    anchors: dict[str, Anchor], widgets_yaml: dict[str, Any]
) -> list[dict[str, Any]]:
    widget_specs: dict[str, dict[str, Any]] = widgets_yaml.get("widgets", {})
    defaults: dict[str, Any] = widgets_yaml.get("defaults", {})
    roles_meta: dict[str, dict[str, Any]] = widgets_yaml.get("roles", {})

    expected = set(widget_specs.keys())
    found = set(anchors.keys())
    if missing := expected - found:
        print(
            f"warning: widgets in yaml but not found in PDF: {sorted(missing)}",
            file=sys.stderr,
        )
    if extra := found - expected:
        print(
            f"warning: anchors in PDF but no widget spec: {sorted(extra)}",
            file=sys.stderr,
        )

    # role -> page -> [widgets]
    by_role: dict[str, dict[int, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for key, spec in widget_specs.items():
        if key not in anchors:
            continue
        widget = build_widget(anchors[key], spec, defaults)
        if widget is None:
            continue
        page = widget.pop("_page")
        role = spec.get("role", "Role 1")
        by_role[role][page].append(widget)

    placeholders: list[dict[str, Any]] = []
    for idx, role in enumerate(sorted(by_role.keys()), start=1):
        meta = roles_meta.get(role, {})
        placeholders.append(
            {
                "signerObjId": "",
                "signerPtr": {},
                "Id": idx * 1000,
                "blockColor": meta.get("color", DEFAULT_ROLE_COLOR),
                "Role": role,
                "email": "",
                "placeHolder": [
                    {"pageNumber": p, "pos": ws}
                    for p, ws in sorted(by_role[role].items())
                ],
            }
        )
    return placeholders


# -- entry point ------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument("--template", type=Path, default=Path("template.md"))
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    p.add_argument("--widgets", type=Path, default=Path("widgets.yaml"))
    p.add_argument("--out-pdf", type=Path, default=Path("template.pdf"))
    p.add_argument("--out-json", type=Path, default=Path("placeholders.json"))
    args = p.parse_args()

    for path in (args.template, args.widgets):
        if not path.exists():
            sys.exit(f"error: not found: {path}")

    template_src = args.template.read_text(encoding="utf-8")
    config = (
        yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
        if args.config.exists()
        else {}
    )
    widgets_yaml = yaml.safe_load(args.widgets.read_text(encoding="utf-8")) or {}

    rendered_md = make_anchors_invisible(substitute_config(template_src, config))
    render_pdf(rendered_md, args.out_pdf)
    anchors = find_anchors(args.out_pdf)

    placeholders = build_placeholders(anchors, widgets_yaml)
    args.out_json.write_text(json.dumps(placeholders, indent=2), encoding="utf-8")

    n_widgets = sum(len(ph["placeHolder"]) for ph in placeholders)
    total_pos = sum(
        sum(len(p["pos"]) for p in ph["placeHolder"]) for ph in placeholders
    )
    print(f"wrote {args.out_pdf}")
    print(f"wrote {args.out_json}")
    print(
        f"  {len(placeholders)} role(s), {n_widgets} page-group(s), {total_pos} widget(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
