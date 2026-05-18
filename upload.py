#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.27",
#   "pyyaml>=6.0",
#   "python-dotenv>=1.0",
# ]
# ///
"""Upload a built template package to OpenSign.

Reads OPENSIGN_BASE_URL and OPENSIGN_API_TOKEN from .env. Builds the
`POST /createtemplate` body for OpenSign's v1.2 REST API:

    {
      "file": "<base64 PDF>",
      "title": "...",
      "signers": [{"role": "...", "signer_role": "signer", "widgets": [...]}],
      "prefill": {"widgets": [...]}
    }

Signer widgets come from placeholders.json. Widgets whose yaml spec has
`prefill: true` are routed to `prefill.widgets` instead of a signer.

Usage:
    uv run upload.py --title "My Document"           # POST to sandbox
    uv run upload.py --title "My Document" --prod    # POST to prod creds
    uv run upload.py --dry-run                       # print body, don't POST
    uv run upload.py --probe                         # auth check via /templatelist
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent

DEFAULT_PDF = Path("template.pdf")
DEFAULT_PLACEHOLDERS = Path("placeholders.json")
DEFAULT_WIDGETS_YAML = Path("widgets.yaml")


def env(key: str, *, required: bool = True) -> str:
    val = os.environ.get(key)
    if required and not val:
        sys.exit(f"error: {key} missing from environment / .env")
    return val or ""


def prefill_keys(widgets_yaml: dict[str, Any]) -> set[str]:
    return {
        key
        for key, spec in widgets_yaml.get("widgets", {}).items()
        if spec.get("prefill")
    }


def widget_to_api(internal: dict[str, Any]) -> dict[str, Any]:
    """Convert one entry from placeholders.json's `pos` to the createtemplate
    widget shape (flat x/y/w/h, with `options` rebuilt for OpenSign).
    """
    opts = internal.get("options", {}) or {}
    widget_type = internal["type"]
    api_opts: dict[str, Any] = {
        "name": opts.get("name", internal.get("key", "")),
        "required": opts.get("status", "required") != "optional",
        # OpenSign's `options.fontsize` (lowercase 's'); default 12.
        "fontsize": opts.get("fontsize", 10),
    }
    if hint := opts.get("hint"):
        api_opts["hint"] = hint
    default_value = opts.get("defaultValue")
    if widget_type == "date":
        # Date widget needs a fuller set of options per the createtemplate
        # docs. Yaml's friendly `default: today` maps to `signing_date: true`
        # so OpenSign auto-stamps the date the signer signs.
        api_opts["format"] = opts.get("format", "dd-mm-yyyy")
        api_opts["color"] = opts.get("color", "black")
        api_opts["min_date"] = opts.get("min_date", "")
        api_opts["max_date"] = opts.get("max_date", "")
        api_opts["readonly"] = opts.get("readonly", False)
        if default_value == "today":
            api_opts["signing_date"] = True
        else:
            api_opts["signing_date"] = False
            if default_value:
                api_opts["default"] = default_value
    elif default_value is not None:
        api_opts["default"] = default_value
    return {
        "type": widget_type,
        "page": internal["_page"] if "_page" in internal else internal.get("page"),
        "x": internal["xPosition"],
        "y": internal["yPosition"],
        "w": internal["Width"],
        "h": internal["Height"],
        "options": api_opts,
    }


def build_body(
    placeholders: list[dict[str, Any]],
    prefill: set[str],
    title: str,
    pdf_b64: str,
) -> dict[str, Any]:
    signers: dict[str, list[dict[str, Any]]] = {}
    prefill_widgets: list[dict[str, Any]] = []

    for ph in placeholders:
        role = ph.get("Role", "Role 1")
        for page in ph.get("placeHolder", []):
            page_num = page["pageNumber"]
            for w in page.get("pos", []):
                w_with_page = dict(w)
                w_with_page["_page"] = page_num
                api_widget = widget_to_api(w_with_page)
                key = w.get("key", "")
                if key in prefill:
                    prefill_widgets.append(api_widget)
                else:
                    signers.setdefault(role, []).append(api_widget)

    body: dict[str, Any] = {
        "title": title,
        "file": pdf_b64,
        "signers": [
            {"role": role, "signer_role": "signer", "widgets": widgets}
            for role, widgets in sorted(signers.items())
        ],
    }
    if prefill_widgets:
        body["prefill"] = {"widgets": prefill_widgets}
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--placeholders", type=Path, default=DEFAULT_PLACEHOLDERS)
    parser.add_argument("--widgets", type=Path, default=DEFAULT_WIDGETS_YAML)
    parser.add_argument("--title", required=False, help="Template title in OpenSign")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument(
        "--prod",
        action="store_true",
        help="Use OPENSIGN_PROD_BASE_URL + OPENSIGN_PROD_API_TOKEN",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    load_dotenv(HERE / ".env")
    if args.prod:
        base_url = env("OPENSIGN_PROD_BASE_URL").rstrip("/")
        token = env("OPENSIGN_PROD_API_TOKEN")
        print(f"[prod] target: {base_url}")
    else:
        base_url = env("OPENSIGN_BASE_URL").rstrip("/")
        token = env("OPENSIGN_API_TOKEN")

    headers = {"x-api-token": token, "Accept": "application/json"}
    client = httpx.Client(base_url=base_url, headers=headers, timeout=60.0)

    if args.probe:
        r = client.get("/templatelist")
        print(f"GET /templatelist -> {r.status_code}")
        print(f"  {r.text[:400]}")
        return 0 if r.is_success else 1

    for p in (args.pdf, args.placeholders, args.widgets):
        if not p.exists():
            sys.exit(f"error: not found: {p}")

    if not args.title and not args.dry_run:
        sys.exit("error: --title is required (or pass --dry-run)")

    pdf_b64 = base64.b64encode(args.pdf.read_bytes()).decode()
    placeholders = json.loads(args.placeholders.read_text())
    widgets_yaml = yaml.safe_load(args.widgets.read_text()) or {}

    body = build_body(
        placeholders,
        prefill_keys(widgets_yaml),
        args.title or "(untitled — dry run)",
        pdf_b64,
    )

    if args.dry_run or args.verbose:
        shadow = {**body, "file": f"<base64 {len(pdf_b64)} chars>"}
        print(json.dumps(shadow, indent=2))

    if args.dry_run:
        return 0

    resp = client.post("/createtemplate", json=body)
    print(f"\nPOST /createtemplate -> {resp.status_code}")
    print(resp.text[:1200])
    return 0 if resp.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
