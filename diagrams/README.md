# Hermes Helmet diagrams

## Identity and runtime

[View SVG](identity-and-runtime.svg) · [Download PNG](identity-and-runtime.png) · [Edit Mermaid](identity-and-runtime.mmd) · [Edit Excalidraw](identity-and-runtime.excalidraw)

The Captain and first officer share the Captain identity. GitHub connects the first officer to the separately identified Hermes worker in Docker Compose. Solid arrows carry work and feedback. Dotted lines connect Kanban and the worker to persistent state at `/opt/data`.

The Captain agrees on a written plan before delegating authority. The first officer assigns, reviews, and handles authorized merges; Hermes implements and repairs. The [operating model](../docs/captain-and-crew.md) explains the responsibilities. The Compose box groups the Hermes runtime and its persistent volume, as configured in [`deploy/compose.yaml`](../deploy/compose.yaml).

## Editing and rendering

`identity-and-runtime.mmd` is the maintained source. Update it and `manifest.json` together, then regenerate the SVG, PNG, and Excalidraw files. The Excalidraw file contains editable shapes and connectors and opens with **File → Open** in Excalidraw. Reconcile any Excalidraw changes back into Mermaid before the next source render.

The optional `render.cjs` authoring helper uses Node, Playwright, Chrome, and the offline GStack diagram bundle. It is not a product runtime or build dependency. With those tools installed:

```sh
PLAYWRIGHT_PATH=/path/to/node_modules/playwright \
DIAGRAM_BUNDLE=/path/to/gstack/lib/diagram-render/dist/diagram-render.html \
CHROME_PATH=/path/to/chrome \
node diagrams/render.cjs
```

The renderer blocks HTTP requests, preserves a transparent background, includes SVG title/description text, and exports a 2400-pixel PNG. It handles the bundle's prefixed group IDs and rejects flattened Excalidraw exports. `render-inventory.json` records dimensions and bundle versions.

Keep labels short and name the actors in text. Color reinforces those labels: amber for the Captain, violet for the first officer, teal for Hermes, and gray for systems and records. Keep titles and explanatory captions outside the diagram. Inspect the PNG and README-sized SVG after changes; check arrow direction, identity boundaries, overlapping labels, and mobile readability.
