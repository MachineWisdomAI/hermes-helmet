# Fresh writing worker with Docker Compose

This page builds a new, isolated Hermes worker that can write DOCX and PPTX files
and render Mermaid diagrams. It uses public sources only. It needs no GitHub
token, policy file, poller, host bind mount or published port, and it never
touches an existing Hermes Helmet deployment.

Your first officer plans and reviews the writing on your host with
[Helmet Write](helmet-write.md). This worker does the writing. The two meet
through native Kanban attachments, reached by the `helmet-compose` adapter
described in [Task artifacts](task-artifacts.md).

This page covers the image, the Compose project, the profile configuration and a
runnable first walkthrough: install the plugin, point your first officer at the
worker, and ask for a DOCX or PPTX. It does not run a model for you. You supply
the provider and credentials.

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
[Task artifacts](task-artifacts.md).

## 8. Install the plugin

The plugin supplies the `hermes-helmet:helmet-write` skill beside
`hermes-helmet:helmet-epic`. Install it once per first-officer host
([all hosts and updates](first-officer-plugins.md)):

```bash
codex plugin marketplace add MachineWisdomAI/hermes-helmet
codex plugin add hermes-helmet@hermes-helmet
# Claude Code instead:
#   claude plugin marketplace add MachineWisdomAI/hermes-helmet
#   claude plugin install hermes-helmet@hermes-helmet
```

Open a **new** session afterwards (Claude Code: `/reload-plugins` also works).
Skills are read when a session starts. After a later update
(`codex plugin marketplace upgrade hermes-helmet`) open a new session again.

If the skill is missing, a standalone copy of `helmet-write` in `~/.codex/skills`
or `~/.claude/skills` can mask the plugin's. Remove only that copy; leave
unrelated skills alone.

## 9. Ask for a document

Invoke the skill by its reference and describe the writing, nothing else:

```text
Use hermes-helmet:helmet-write. Write a Q3 update for ExampleCo Widgets
customers as a Word document: what shipped, the quarter's figures, and how an
order flows, with a diagram. Use the notes in examples/writing/sources.
```

For a deck: "Use hermes-helmet:helmet-write. Make a four-slide Widgets 4
overview deck with speaker notes from examples/writing/sources/product-overview-notes.md."

You do not paste a plan, a handoff, CLI commands or instructions for the
workers. The skill shapes the plan, runs the workflow and reports back. It asks
only for what the assignment lacks, such as the audience or the sources.

What the skill does, and what you can check afterwards:

1. **Readiness.** For every profile the plan names, including any replacement
   writer, it confirms the profile is in `kanban.dispatch_profiles`,
   `helmet write profile-check` is clean and the guidance skill is discoverable.
2. **Assignments.** Each contributor card carries only its assigned sources,
   the neighboring passages it needs and the decisions that apply, not the
   whole source collection. Audience, voice and the guidance pack travel in the
   plan's `guidance`, so the editor receives them too.
3. **Review.** Each submission is reviewed as an exact revision. One round of
   concrete requested changes goes back to the same card and writing profile.
   A raw review comment is never promoted to a requirement: only the decisions
   recorded at acceptance reach the editor.
4. **Assembly.** The editor receives the accepted files and decisions, and its
   completion criterion includes reading the complete assembled artifact for
   continuity and fidelity to those inputs before it submits.
5. **Retrieval.** The first officer reviews the editor's exact revision, then
   retrieves the canonical file with `helmet write canonical` into a local path.
   The file survives the worker's scratch cleanup because native Kanban stages
   it as an attachment when the card is submitted.

The reference plans are `examples/writing/docx-plan.json` (two contributions on
the same `writing` profile, plus the editor) and `examples/writing/pptx-plan.json`
(one contribution plus the editor), with synthetic notes in
`examples/writing/sources/`. They are examples of what the skill produces, not
something you type in. A fresh worker has only the `default` board, and
`helmet write plan` does not create one, so create the board first. The command
is safe to repeat; it reports `already exists` when the board is there. To run
one plan directly:

```bash
helmet-compose kanban boards create writing-demo
helmet write plan writing-demo examples/writing/docx-plan.json
```

To use the `default` board instead, name `default` in place of `writing-demo`.

### Capacity and the shared profile

Writers and the editor use one `writing` profile. Two writers run at the same
time only when native capacity allows it (`kanban.max_in_progress_per_profile`
at least 2, step 3). With capacity 1 the second card waits and nothing is wrong.
Each card still has its own session and its own workspace.

### Reading the result

Report two kinds of finding separately:

- **Structural checks**: the file opens; required text and table values are
  present; for a deck, slide order, slide text and speaker notes; the diagram
  image is embedded; the `.mmd`, `.svg` and `.png` are separate attachments and
  the DOCX or PPTX is the one primary submission.
- **Source-based editorial assessment**: your own reading of the final artifact
  against the sources and accepted decisions, for fidelity and continuity.

Source size, elapsed time and word counts are descriptive. They are not targets.
Structural checks do not establish pagination or slide layout.

## Limits

| Area | Supported | Not supported |
| --- | --- | --- |
| Formats | `.docx` and `.pptx`, built with the image's `docx` and `powerpoint` skills | Other formats; PDF output |
| Diagrams | Mermaid source to `.svg` and `.png`, embedded as PNG | Excalidraw, other diagram languages, hand-drawn images |
| Attachments | Files up to 25 MiB each | Larger files |
| Dispatch | The fixed allowlist `[writing]` | A profile you add later, until you add it yourself |
| Reset | Fresh-only: a disposable pre-use project | Reconfiguring a deployment that has tasks |
| Office preview | Optional, later, as `preview` attachments | Page and slide rendering is not installed |

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
