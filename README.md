# opensign-template-md

Build [OpenSign](https://www.opensignlabs.com/) signing templates from a
plain Markdown document.

Write your contract in Markdown, mark each fillable field with a `‹w:KEY›`
anchor, render it through `pandoc` + `xelatex`, and the build script
produces:

- a clean PDF ready to upload to OpenSign,
- a `placeholders.json` describing every widget's position, size, type and
  role.

`upload.py` then POSTs both to OpenSign's `/createtemplate` endpoint.

## Why

OpenSign's UI for placing widgets onto a PDF is drag-and-drop. If you
ever touch the source document — fix a typo, reword a clause, add a
section — every widget reverts to drag-and-drop hell.

This tool keeps the source in Markdown, in your repo, alongside the
widget specs in YAML. Re-render any time; widget positions track the
text automatically.

## How it works

1. **Substitute** `$$NAME` tokens in your Markdown from `config.yaml`.
2. **Replace** each `‹w:KEY›` anchor with raw LaTeX that prints a
   `WMK<key>WMK` marker in white inside a zero-width box — invisible
   on the page but recoverable from the PDF text stream.
3. **Render** one PDF with `pandoc --pdf-engine=xelatex`.
4. **Find** every marker via `pdfplumber`. Match each one to its
   adjacent underscore field (`\_______`) — same line for inline
   fields, just below for label-style fields.
5. **Measure** the widget's bounding box from the rendered PDF: width
   = underscore-line extent; height = from the previous label or
   drawing-space line down to the bottommost underscore in a
   multi-line field.
6. **Build** `placeholders.json` grouped by signing role from
   `widgets.yaml`.

The output PDF looks identical to one with no anchors — the markers
take zero horizontal width and are rendered in white.

## Requirements

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) (the scripts are PEP-723 inline)
- `pandoc` + a TeX distribution with `xelatex` (e.g. TeX Live, MacTeX)
- An OpenSign account (a free sandbox account is enough to test)

## Quickstart

```bash
# 1. Clone & copy the example into the working directory
git clone https://github.com/robtaylor/opensign-template-md.git
cd opensign-template-md
cp examples/template.md template.md
cp examples/widgets.yaml widgets.yaml
cp examples/config.yaml config.yaml
cp examples/.env.example .env

# 2. Edit .env and paste in your OpenSign test API token
$EDITOR .env

# 3. Build
uv run build.py
# wrote template.pdf
# wrote placeholders.json
#   2 role(s), 4 page-group(s), 10 widget(s)

# 4. Upload to OpenSign sandbox
uv run upload.py --title "Mutual NDA"
```

The template should now show in your OpenSign sandbox dashboard with
every widget positioned on its underscore field.

## Markdown anchors

Each fillable field has a `‹w:KEY›` anchor in the markdown source. The
build script supports two layouts:

### Inline fields

The anchor sits mid-sentence, immediately before a run of `\_` characters
that visually marks the field:

```markdown
Effective on ‹w:start_date›\_\_\_\_\_\_\_\_\_\_ this agreement...
```

The widget overlays the underscore run on the same line.

### Block fields (label + line below)

The label is on one paragraph, the anchor + underscores form the next
paragraph:

```markdown
Name:

‹w:client_name›\_______________________________________________________________________________
```

Place the anchor right before the underscores. With `\rlap`-style zero
width, the marker shares the same x as the first underscore.

### Multi-line fields

Stack additional `\_____` lines below the anchored line:

```markdown
Registered office address:

‹w:client_address›\_______________________________________________________________________________

\_______________________________________________________________________________

\_______________________________________________________________________________
```

The build script walks downward from the matched run looking for
consecutive runs at the same x; the widget covers all of them.

### Signature blocks (drawing space above the line)

Insert one or more `&nbsp;` paragraphs between the label and the
underscore line — they reserve vertical space that the build script
folds into the widget's height:

```markdown
Signature:

&nbsp;

‹w:consultant_signature›\_______________________________________________________________________________
```

## Configuration

### `widgets.yaml`

Maps each anchor key to its OpenSign widget type, signing role, and
display name. The script measures width and height from the PDF — only
set `w`/`h` explicitly when you need to override the measurement.

```yaml
widgets:
  client_name:
    type: company
    role: Client            # the Client signer fills this in
    name: Client Name

  client_signature:
    type: signature
    role: Client
    name: Client Signature

  agreement_date:
    type: date
    role: Consultant
    prefill: true           # the sender fills this when sending the template
    name: Agreement Date
```

**Roles.** Each widget has a `role:` that names the signing party
responsible for filling it in. The build script groups widgets by role
into OpenSign's `Placeholders` array; OpenSign then routes the signing
request to the right person per role. You can declare any role names
you like; the YAML also accepts an optional `roles:` block to set the
UI block colour:

```yaml
roles:
  Client:
    color: "#93a3db"
  Consultant:
    color: "#dba593"
```

**Prefill (sender-filled) widgets.** Set `prefill: true` to route a
widget into the `prefill.widgets` array instead of a signer's widget
list. The sender fills these in when preparing the document to send.
The `role:` on a prefill widget is unused (but harmless to leave in).

**Auto-filled signing date.** Add `default: today` to a `date` widget
to have OpenSign stamp in the date that signer actually signs. The
upload script translates this into the `signing_date: true` option
that OpenSign expects:

```yaml
client_signed_date:
  type: date
  role: Client
  default: today          # auto-fills with the date the Client signs
  name: Client Date Signed
```

**Other per-widget options:** `hint` (placeholder text), `default`
(default value), `required: false`, `w` / `h` (override measured
size), `x_offset` / `y_offset` (fine-tune position).

See [`examples/widgets.yaml`](examples/widgets.yaml) for a full
worked example.

### `config.yaml`

Optional. Provides values for `$$NAME` tokens in the markdown.

### `.env`

```env
OPENSIGN_BASE_URL=https://sandbox.opensignlabs.com/api/v1.2
OPENSIGN_API_TOKEN=test.xxxxxxxxxxxxxxxxxxxxxx

# Optional, for `upload.py --prod`
OPENSIGN_PROD_BASE_URL=https://app.opensignlabs.com/api/v1.2
OPENSIGN_PROD_API_TOKEN=opensign.xxxxxxxxxxxxxxxxxxxxxx
```

The sandbox token is free with any OpenSign account; the production
token requires a paid plan.

## Scripts

| Script | What it does |
|---|---|
| `build.py` | Substitute → render → find anchors → write `template.pdf` and `placeholders.json` |
| `upload.py` | Base64-encode the PDF, POST to OpenSign's `/createtemplate` |

Both accept `--help`. Useful flags:

- `upload.py --dry-run` — print the JSON body instead of POSTing
- `upload.py --probe` — auth check via `GET /templatelist`
- `upload.py --prod` — use the `_PROD_` environment variables

## Notes & known limits

- OpenSign textboxes are single-line by default. The signer has to
  toggle multi-line mode in the UI to enter newlines — there's no API
  parameter for it ([OpenSignLabs/OpenSign#2107](https://github.com/OpenSignLabs/OpenSign/issues/2107)).
- OpenSign uses lowercase `fontsize` in the widget options (not
  camelCase). The script sets it to 10 by default.
- The build script renders one PDF and never strips the markers. The
  markers stay in the PDF text stream but are invisible (white text in
  a zero-width box).

## License

MIT — see `LICENSE`.
