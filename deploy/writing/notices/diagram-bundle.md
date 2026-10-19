# Diagram render bundle: provenance and notices

The writing image's `diagram` skill runs the offline render bundle built by
[garrytan/gstack](https://github.com/garrytan/gstack). Hermes Helmet does not
commit that bundle. The Docker build fetches it at the pinned commit, verifies
the SHA-256, and stages it with `deploy/writing/stage-diagram.py`.

| Item | Value |
| --- | --- |
| Upstream | https://github.com/garrytan/gstack |
| Pinned commit | `47d639abffa882b8cbb641d7d1dedf08ecada9ec` (gstack `VERSION` 1.91.101.0) |
| Bundle | `lib/diagram-render/dist/diagram-render.html`, 7,884,089 bytes |
| Bundle SHA-256 | `ec759ca41239ff888acdd0654c3f6ef505e3314b87c6e5de11903f804fc8a859` |
| Build tool | bun 1.4.2, recorded in the bundle's `BUILD_INFO.json` |
| Hermes Helmet license for its own adapter | Apache-2.0 |
| gstack license | MIT, Copyright (c) 2026 Garry Tan (`gstack-LICENSE` beside this file) |

## Bundled dependencies

Versions come from the pinned bundle's `BUILD_INFO.json` (`deps`), checked
against each package's published license field on npm. `stage-diagram.py` fails
the build if the bundle's `deps` differ from `deploy/writing/bundle-deps.json`,
so this table is re-checked whenever the pin changes.

| Package | Version | License |
| --- | --- | --- |
| mermaid | 11.16.1 | MIT |
| @excalidraw/excalidraw | 0.18.1 | MIT |
| @excalidraw/mermaid-to-excalidraw | 2.2.2 | MIT |
| react | 18.3.1 | MIT |
| react-dom | 18.3.1 | MIT |

`gstack-THIRD-PARTY-LICENSES.md` is gstack's own notice file, copied unchanged.
Its version table is older than the bundle (it lists mermaid 11.12.2, for
example). The table above, which is checked against the bundle, is authoritative.
Transitive dependencies are those resolved by gstack's `bun.lock` at the pinned
commit, which is where the upstream license obligations for them are tracked.

## Fonts

The bundle embeds the Excalidraw font data, including Excalifont, licensed under
the SIL Open Font License 1.1 per the excalidraw repository and the upstream
notice. This image only renders Mermaid to SVG and PNG, and does not use the
Excalidraw export path. The SVG text uses the system font stacks configured by
the bundle (`Helvetica`, `Liberation Sans`, `Arial`, `trebuchet ms`).

## Supported surface

Mermaid source in, SVG and PNG out, offline. Excalidraw scene conversion and
output are intentionally not exposed by the adapter.
