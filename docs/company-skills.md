# Company skill ecosystem

Hermes Helmet keeps skill import **optional** and **adopter-controlled**. There
are two separate boundaries:

| Boundary | What | Install path | Writes where |
| --- | --- | --- | --- |
| Captain / orchestrator | Bundled skills `helmet-issue`, `helmet-epic`, `setup-helmet` | Explicit setup only: `helmet setup` / `helmet install-skills` | Host agent skill dirs (Codex / Claude Code / Hermes) under an optional `--prefix` |
| Executor / company pack | Adopter-owned skills (for example `repo-bootstrap`) | Optional runtime/setup import: `helmet import-company-skills` | **Hermes-owned** persistent state only (`$HERMES_HOME/hermes-helmet/company-skills` by default) |

Executor packs **cannot** grant review or merge authority. Reserved Captain
skill names (`helmet-issue`, `helmet-epic`, `setup-helmet`) are rejected if they
appear in a company pack allowlist.

Runtime import **never** writes to host-global:

- `~/.codex/skills`
- `~/.claude/skills`
- `~/.hermes/skills`

## Skill source and distribution

Edit portable Captain skills only in this repository's `skills/` tree, including
linked references. The Python build copies those assets into
`hermes_helmet/bundled_skills/` in the wheel. Source and editable installs use
`skills/` directly. The source distribution carries that same tree, and the
release test builds a wheel from it and compares every packaged asset.

An adopter's installer may copy this tree into its host skill directories or
package it for another client. Those copies are generated distribution artifacts;
changes belong here. A private toolkit is never required to install or use the
portable skills. Company-specific instructions and deployment runbooks remain in
the adopter's repository. Captain skills stay outside executor company packs.

## Authority policy (optional)

Extend the version-2 authority document:

```json
"skills": {
  "company_pack": {
    "source": "/opt/data/company-skills-pack",
    "allowlist": ["repo-bootstrap"]
  }
}
```

- Omit `skills` entirely for minimum startup: import status is **`skipped`**.
- `source` must be an absolute path.
- Only names listed in `allowlist` are candidates; everything else in the pack
  is ignored.
- Empty allowlist, missing source directory, or empty pack **preserve** the last
  known-good import (no mutation).
- Invalid names, path traversal, escaping symlinks, unreadable trees, missing
  `SKILL.md`, secret-shaped tokens, and Captain-authority claims are **rejected
  before mutation**.

## Import lifecycle

1. Validate the complete candidate (every allowlisted skill) up front.
2. Stage a full release under `…/company-skills/releases/<id>/`.
3. Atomically point `…/company-skills/active` at that release.
4. Register `…/company-skills/active/skills` in Hermes
   `skills.external_dirs` (supported executor discovery boundary) without
   writing host-global Codex/Claude/Hermes skill trees.
5. On failure after partial work, restore the previous active release when
   possible; never leave a half-applied allowlist as current.

The container `deploy/hermes/runtime-entrypoint.sh` attempts the same optional
import on every startup **after** scrubbing raw `GH_TOKEN`/`GITHUB_TOKEN` and
**before** `exec /init …`. Skip, preserve, and reject remain non-blocking.
Set `HERMES_HELMET_SKIP_COMPANY_SKILLS=1` to disable the hook.

Commands:

```sh
# Optional executor import (safe when pack is unset → status=skipped)
PYTHONPATH=src python3 -m hermes_helmet.cli import-company-skills \
  --config config/policy.json \
  --json

# Deterministic catalog for setup-helmet
PYTHONPATH=src python3 -m hermes_helmet.cli skill-catalog \
  --config config/policy.json \
  --json

# Captain skills: explicit setup only (separate boundary)
PYTHONPATH=src python3 -m hermes_helmet.cli install-skills --target codex
```

Poller install also attempts company import by default and prints
`company skill import: status=…` without blocking core startup on skip/preserve/
reject. Use `--skip-company-skills` to disable.

## Creating a company-owned skill pack

Adopters own a pack directory **outside** this repository (or under a private
overlay). Do **not** copy private `mw-*` content into public packs.

Recommended layout:

```text
company-skills-pack/
  repo-bootstrap/
    SKILL.md
  optional-other-skill/
    SKILL.md
```

Each skill directory must contain a `SKILL.md` with YAML frontmatter `name` and
`description`. Keep examples free of credentials, PATs, provider keys, and
real adopter identities.

### Repository-bootstrap skill

A good first company skill is **repository bootstrap**:

- create or adopt a repository under the configured GitHub owner allowlist
- seed portable instruction files from **company-owned** templates
- establish branch and pull-request conventions
- optionally install local git guards
- never merge, force-push, or impersonate the Captain

See the public fixture:

`config/fixtures/exampleco/company-skills/repo-bootstrap/SKILL.md`

Point `skills.company_pack.source` at a directory with the same shape, and list
exact skill directory names in `allowlist`.

## Recommended upstream additions (not bundled)

Setup may **recommend** operator-approved upstream skills as **pinned,
provenance-documented, explicit installations**. They are **not** runtime
dependencies of Hermes Helmet, are **not** shipped in this repository, and must
**never** be installed silently.

| Upstream | Role | Guidance |
| --- | --- | --- |
| Matt Pocock agent skills | General coding-agent workflows | Pin a specific git commit or release tag; record provenance in the adopter overlay; install only after Captain approval |
| gstack | Stacked-diff / PR workflow helpers | Same: pin, document source URL and revision, explicit opt-in only |

Do not add these packages to `pyproject.toml`, container images, or default
compose mounts. `setup-helmet` surfaces them as optional recommendations with
pin and source URL fields, not as automatic imports.

## setup-helmet consumption

`helmet skill-catalog --json` returns a stable document:

- `status` — `skipped` / `imported` / …
- `role_boundary` — `captain_setup_vs_executor_import`
- `skills[]` — entries with `name`, `role` (`captain` or `executor`), `source`
- `allowlist`, `source`, `state_root`, `active_release`, `message`
- `discovery_path` — active executor skills dir registered for Hermes discovery
- `generated_at` — deterministic: active release `imported_at` (or empty when
  nothing is active). Identical inputs yield identical catalog JSON.

Captain `source=bundled` entries come only from real package assets under
`bundled_skills/` (`helmet-issue`, `helmet-epic`, and `setup-helmet`). Reserved
names that are not shipped are not advertised as available.

`import-company-skills --json` returns the import result plus nested
`validation` and `catalog` objects for guided setup UIs.

## Safety checklist

- [ ] No pack configured → startup succeeds, status `skipped`
- [ ] Valid allowlisted pack → Hermes-owned state, survives restart
- [ ] Missing/empty source → last known-good preserved
- [ ] Bad names / traversal / symlinks / missing `SKILL.md` → rejected, no mutation
- [ ] Captain install path remains explicit and separate
- [ ] Errors and fixtures contain no credentials or private skill content
