# Fresh writing worker with Docker Compose

This page builds a new, isolated Hermes worker that can write DOCX and PPTX files
and render Mermaid diagrams. It uses public sources only. It needs no GitHub
token, policy file, poller, host bind mount or published port, and it never
touches an existing Hermes Helmet deployment.

Your first officer plans and reviews the writing on your host with
[Helmet Write](helmet-write.md). This worker does the writing. The two meet
through native Kanban attachments, reached by the `helmet-compose` adapter
described in [Task artifacts](task-artifacts.md).

This page covers the image, the Compose project and the profile configuration.
It does not run a model for you. You supply the provider and credentials.

## What you get

| Piece | Where it comes from |
| --- | --- |
| Hermes Agent, Node, Chromium | The pinned base image in `deploy/Dockerfile` |
| `docx` and `powerpoint` skills | Bundled with that Hermes release |
| `python-docx`, `python-pptx` and their dependencies | `deploy/writing/requirements.lock`, hash-locked, installed at build time into every image Python |
| `diagram` skill: Mermaid to `.svg` and `.png`, offline | [gstack](https://github.com/garrytan/gstack) render bundle at a pinned commit, plus a small adapter, `deploy/writing/skills/diagram/scripts/render.mjs` |
| Neutral guidance skill `example-writing-guidance` | `examples/writing/guidance/` |
| One profile named `writing` | `python -m hermes_helmet.writing_setup configure` |

Nothing is installed when the worker runs. Office page and slide rendering is not
included. See [Preview and rendering](#preview-and-rendering).

## Requirements

- Docker with the Compose plugin, and a checkout of this repository.
- Network access for the build (base image, the Python wheels, the pinned
  gstack commit). The worker does not need it to render diagrams.
- A model provider account of your own for the worker. Do not reuse your first
  officer's login, sessions or memory.

## 1. Open a dedicated shell

The walkthrough uses Docker's own `COMPOSE_FILE` and `COMPOSE_PROJECT_NAME`
variables. They apply to every `docker compose` command in the shell, so use a
shell you will close when you are finished. Never put these exports in a shell
startup file.

```bash
cd /path/to/hermes-helmet
export COMPOSE_FILE="$PWD/deploy/writing.compose.yaml"
export COMPOSE_PROJECT_NAME=writing-demo   # unique per worker
docker compose config --quiet
```

`docker compose config --quiet` passes with no `HERMES_GITHUB_TOKEN` set: this
recipe reads none. Two shells with different project names resolve to different
volumes, networks and images (`writing-demo_hermes_state`, `writing-demo_default`
and so on). The recipe sets no container, volume or network name of its own.

## 2. Build the image

```bash
docker compose build
```

The build selects the `writing` target of `deploy/Dockerfile`. It fails if:

- the gstack checkout is not exactly the pinned commit, or the render bundle's
  SHA-256 differs from the pin;
- the bundle's dependency list differs from `deploy/writing/bundle-deps.json`,
  the list its notice was checked against;
- a Python library is missing from the lock or fails its hash;
- the bundled `docx` and `powerpoint` skills cannot create, read and edit a file
  under **each** of the image's three Pythons, or the diagram command cannot
  render a fixture offline.

To record the source in the image, pass `HERMES_HELMET_SOURCE_COMMIT` and
`HERMES_HELMET_VERSION` in the shell first. The base image pin is the one in
`deploy/Dockerfile`; override it with `HERMES_BASE_IMAGE`.

## 3. Set up the provider, before the service starts

The worker's home is the project's own volume, `/opt/data`. Run Hermes's native
setup inside a one-off container to choose a provider and add the worker's own
credentials:

```bash
docker compose run --rm --no-deps -it --entrypoint hermes hermes setup
```

Use credentials created for this worker. Do not copy files from another Hermes
home. `writing_setup` never edits or removes the provider, model, credentials or
capacity settings you choose here.

Set native worker capacity if you want writers to overlap, for example in the
root config: `hermes config set kanban.max_in_progress_per_profile 2` (run the
same way). It is yours to choose; the recipe leaves it as found.

## 4. Configure the profile

```bash
docker compose run --rm --no-deps --entrypoint python hermes \
    -m hermes_helmet.writing_setup configure
```

Run this **before** the service has ever started. The one-off container has no
Docker socket, so the command does not try to detect a running service. If you
configure while a service is running or after work exists, the next steps below
apply.

It builds this topology and nothing else:

- the native `default` profile, the coordinator. It receives review requests and
  never runs a model worker;
- one native profile, `writing`, for every writer and the editor. Each works its
  own card in its own session, so a coordinator plan names `writing` for the
  writers and the editor, and a different profile for the coordinator
  ([Helmet Write](helmet-write.md));
- in `writing`: built-in memory, user profile and any external memory provider
  off; `session_search` disabled through `agent.disabled_toolsets`; the
  `helmet-writing-context` plugin installed and enabled; the image's capability
  directory registered with native skill discovery;
- the neutral guidance pack imported into the `writing` profile's own state
  directory through the company-skill importer, with discovery registration
  required to succeed;
- the root config's `kanban.dispatch_profiles` set to `["writing"]`;
- `/opt/data/.helmet-writing-recipe.json` containing exactly
  `{"recipe": "helmet-writing", "version": 1}`, written last.

The dispatch allowlist is fixed. A profile you add later is **not** dispatched
until you add its name to `kanban.dispatch_profiles` yourself. That is an
adopter configuration change, outside this recipe.

Configure is repeatable while no task exists: the same inputs give the same
settings, with no duplicate profile, plugin entry, discovery path or skill
release.

### When configure refuses

It reads state first and changes nothing when it refuses. It prints a JSON object
with a stable `refused` code and `"changed": false`.

| `refused` | State found |
| --- | --- |
| `unexpected_profile` | A profile other than `writing` under `profiles/` |
| `marker_invalid` | The marker is not a regular file, or not valid JSON |
| `marker_different` | The marker is JSON for another recipe or version, or has other keys |
| `writing_profile_unmarked` | A `writing` profile exists without the marker |
| `tasks_exist` | Any native task on any board, including archived tasks and archived boards |
| `task_state_unreadable` | A Kanban store exists but cannot be read, so it is not known to be empty |
| `image_assets_missing` | The image does not carry the diagram skill (wrong target) |

No journal, lock, snapshot or in-place rollback exists. If configure fails
**before** it writes the marker, remove the disposable project and retry (see
[Reset](#reset)). After tasks exist, `verify` still works but `configure` will
not change the deployment. Use a new project for a changed setup.

## 5. Verify capabilities

```bash
docker compose run --rm --no-deps --entrypoint python hermes \
    -m hermes_helmet.writing_setup verify
```

It prints one JSON report and exits nonzero on any failed check. It checks the
marker, `profile-check`'s settings, the plugin files, the capability directory,
the fixed dispatch allowlist, native skill discovery of `docx`, `powerpoint`,
`diagram` and `example-writing-guidance`, the document skills under each image
Python, and an offline diagram render. It also reports `office_visual_rendering`.
That value is informational: `unavailable` does not fail verification.

Do not start work until `verify` passes. It keeps working after work begins and
changes nothing.

## 6. Start the worker

```bash
docker compose up -d
```

This starts the native gateway and its dispatcher. Only `writing` can be
claimed. Watch it with `docker compose logs -f hermes`.

## 7. Point your first officer at it

On the first officer's host, in the same dedicated shell:

```bash
export HERMES_HELMET_HERMES=helmet-compose
export HERMES_HELMET_WORKER_RUNTIME=helmet-compose
```

`helmet-compose` requires both `COMPOSE_FILE` and `COMPOSE_PROJECT_NAME`. It
supports native `kanban ...` and the three `artifact-*` verbs only. See
[Task artifacts](task-artifacts.md). The Helmet Write plugin invocation and a
worked end-to-end example are a separate step and not part of this page.

## Diagrams

Inside a writing task, the worker follows the `diagram` skill:

```bash
node "$DIAGRAM_DIR/scripts/render.mjs" --check
node "$DIAGRAM_DIR/scripts/render.mjs" flow.mmd --out-dir out --slug flow
```

It keeps `flow.mmd` and writes `flow.svg` and `flow.png`. Only Mermaid source,
SVG and PNG are supported. A syntax error stops the command before anything is
written. The renderer starts the image's Chromium with every non-loopback host
blocked, so it works offline.

## Preview and rendering

Page and slide rendering of DOCX and PPTX (for example through LibreOffice) is
not installed and not required. `verify` reports it as `unavailable`. Structural
checks do not establish pagination or slide layout, so read the produced files.

Previews can be added later without changing review or canonical retrieval: use
the existing `preview` and `extracted-text` attachment roles beside the
`original` ([Task artifacts](task-artifacts.md)).

## Reset

A failed or abandoned **pre-use** project is disposable. In the same dedicated
shell, remove only this project's resources:

```bash
docker compose down --volumes
```

Read the project and volume it names first. Volume deletion is destructive and
cannot be undone.

Once writing work exists, download any artifact you need **before** removing a
volume. A plain `docker compose down` stops the service and keeps the volume.
Never run a broad prune, and never touch another project's resources. Source
rollback of this recipe is an ordinary revert of the pull request that changed
it.

When you are done, close the dedicated shell so `COMPOSE_FILE` and
`COMPOSE_PROJECT_NAME` do not leak into other work.

## Adding your own guidance

The example pack is neutral. For your own house style, put a pack of skills in a
directory and import it with the [company-skill importer](company-skills.md) into
the `writing` profile's state directory, not into a shared host directory. Name
the skills in a card's `guidance` so writers and the editor load them.
