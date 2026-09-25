# Hermes Helmet diagrams

## Delegation and identity

[View SVG](delegation-and-identity.svg) · [Download PNG](delegation-and-identity.png) · [Edit Mermaid](delegation-and-identity.mmd) · [Edit Excalidraw](delegation-and-identity.excalidraw)

You agree on a written plan with your coding agent, then delegate authority to it as first officer. It assigns work to Hermes, reviews the returned pull request, directs any repairs, and handles the authorized merge. Hermes implements under its own identity; the first officer uses yours.

The [operating model](../docs/captain-and-crew.md) defines those responsibilities. The [README walkthrough](../README.md#how-the-work-moves) explains how GitHub, the poller, Kanban, and persistent state carry the work between them.

## Editing and rendering

`delegation-and-identity.mmd` is the maintained source. Update it and `manifest.json` together, then regenerate the SVG, PNG, and Excalidraw files. The Excalidraw file contains editable shapes and connectors and opens with **File → Open** in Excalidraw. Reconcile any Excalidraw changes back into Mermaid before the next source render.

The optional `render.cjs` authoring helper uses Node, Playwright, Chrome, and the offline GStack diagram bundle. It is not a product runtime or build dependency. With those tools installed:

```sh
PLAYWRIGHT_PATH=/path/to/node_modules/playwright \
DIAGRAM_BUNDLE=/path/to/gstack/lib/diagram-render/dist/diagram-render.html \
CHROME_PATH=/path/to/chrome \
node diagrams/render.cjs
```

The renderer blocks HTTP requests, preserves a transparent background, includes SVG title/description text, and exports a 2400-pixel PNG. It handles the bundle's prefixed group IDs and rejects flattened Excalidraw exports. `render-inventory.json` records dimensions and bundle versions.

Choose details that explain delegation, responsibility, or completion. Keep labels short and name the actors in text. Color reinforces those labels: amber for the Captain, violet for the first officer, teal for Hermes, and gray for systems and records. Keep titles and explanatory captions outside the diagram. Inspect the PNG and README-sized SVG after changes; check arrow direction, identity boundaries, overlapping labels, and mobile readability.
