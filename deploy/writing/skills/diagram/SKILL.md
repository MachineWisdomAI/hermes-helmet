---
name: diagram
description: "Turn Mermaid source into a retained .mmd plus rendered .svg and .png, fully offline. Use for flowcharts, sequence and architecture diagrams that go into a document or deck."
version: 1.0.0
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [diagram, mermaid, svg, png, flowchart]
    category: creative
---

# diagram: Mermaid source in, rendered diagram out

Every run produces three files with the same content:

| File | Use |
| --- | --- |
| `<slug>.mmd` | Mermaid source. This is the single source of truth. |
| `<slug>.svg` | Vector render. Its labels are real text. |
| `<slug>.png` | Raster render, 1950 px wide by default. Embed this in a DOCX or PPTX. |

The renderer is the offline bundle from the pinned gstack `diagram-render`
build, driven by `scripts/render.mjs` with the image's Node and Chromium. It
needs no network, no CDN, no `bun` and no package installation.
`PROVENANCE.json` records the gstack commit and bundle hash. Only Mermaid
source, SVG and PNG are supported.

## Rules

- Render with the script below. Never hand-draw SVG or PNG and never swap in
  another renderer. A `.mmd` file alone is not a rendered diagram.
- If rendering is impossible, stop and report `BLOCKED` with the exact output of
  the `--check` command. Do not install anything and do not improvise.
- Look at the PNG after rendering (use the image-viewing tool on the file) and
  fix the `.mmd` until labels, arrows and layout read correctly. Re-render from
  the `.mmd`; do not patch the SVG or PNG.

## Step 1: author the Mermaid

- Use `graph LR` for pipelines and flows, `graph TD` for hierarchies.
- Keep node labels short and put detail on edges. Aim for 5 to 15 nodes; split
  larger diagrams into several and say why.
- Pick an output directory inside your task workspace and a kebab-case `<slug>`
  of at most 40 characters.

## Step 2: check, then render

`DIAGRAM_DIR` is this skill's directory: `${HERMES_SKILL_DIR}` when it has been
substituted, otherwise the `diagram` directory under the image's writing assets
(`/opt/hermes-helmet/writing/skills/diagram`).

```bash
node "$DIAGRAM_DIR/scripts/render.mjs" --check
node "$DIAGRAM_DIR/scripts/render.mjs" <outdir>/<slug>.mmd --out-dir <outdir> --slug <slug>
```

`--check` prints one JSON line: `ok`, the `missing` prerequisites by name, the
Node version, the Chrome binary and version, the gstack commit and the bundle
hash. The render prints one `OK <path>` line per file.

- `ERROR: render script did not finish: ... Parse error ...` is a Mermaid syntax
  error. Nothing was written. Fix the `.mmd` and retry; do not hand over a
  broken source file.
- `PAGE_ERRORS=[...]` lists page errors. Read them before trusting the output.
- The renderer's browser blocks every host except the loopback page it serves.

## Step 3: show and deliver

1. View the PNG and confirm it matches the request.
2. Embed the PNG in the document or deck you are producing.
3. Keep the `.mmd` and `.svg` beside it. They are separate files, not the
   primary DOCX or PPTX submission.

## Not supported here

Excalidraw scenes, PDF output, other input formats, and any browser other than
the one `--check` reports.
