# Task artifacts

Hand documents to a worker and bring its deliverables back through native Kanban
task attachments, without moving files by hand or reading a worker path.

A coordinator (for example a first officer) can:

- **upload** an input file to a task,
- **list** the artifact references a task carries, and
- **download** the exact bytes of one referenced revision.

Hermes Helmet adds no service, artifact database, queue, or scheduler. The task's
native attachments remain the only store, and the deployment's existing
worker-runtime transport (`HERMES_HELMET_WORKER_RUNTIME`) carries the bytes.

```sh
helmet artifact upload  BOARD TASK_ID ./brief.docx
helmet artifact list    BOARD TASK_ID [--role original|preview|extracted-text]
helmet artifact download REFERENCE ./retrieved.docx [--board BOARD] [--task-id TASK_ID]
```

Each command prints JSON describing the artifact and never prints file contents.
Pass `--worker-runtime` to override the environment variable.

## Artifact reference

Every listed artifact carries the identity and exact revision a later review
needs:

| Field | Meaning |
| --- | --- |
| `reference` | `kanban-artifact:v1:BOARD:TASK:ATTACHMENT_ID:SHA256` |
| `board`, `task_id` | Owning board and task |
| `attachment_id` | The native attachment id (reused, not reissued) |
| `filename`, `media_type`, `size` | Stored name, stated media type, bytes |
| `sha256` | Checksum of the exact revision |
| `role`, `companion_of` | `original`, `preview`, or `extracted-text`, and the original's name |
| `uploaded_by`, `created_at` | Native provenance |

The reference is the only thing that names an artifact. It is not a file path,
and the native `stored_path` is a worker location the coordinator cannot read, so
it is dropped from every result. Local paths, URLs, other tasks' attachments, and
bare numbers are rejected with `artifact_ref_malformed`, `artifact_task_mismatch`,
or `artifact_board_mismatch`; they cannot stand in for a task-owned reference.

## Revisions

The checksum is the revision. A download succeeds only if the task still lists the
attachment with the checksum in the reference, and only if the received bytes match
the listed size and SHA-256. A changed or replaced file fails with
`artifact_revision_mismatch`, so feedback and acceptance cannot silently apply to
a superseded revision. Upload never replaces an attachment: a new revision is a
new attachment with a new id, and earlier revisions stay retrievable.

## Original, preview, and extracted text

The original DOCX or PPTX is the editable artifact and is stored byte-for-byte. A
rendered preview or extracted text is a separate attachment named after its
original (`brief.docx.preview.png`, `brief.docx.extracted.txt`), uploaded with
`--role preview|extracted-text --original REFERENCE`. A companion never replaces
or stands in for its original, and `--role original` listings exclude it. Those
two name suffixes are reserved for companions.

## Limits and failures

- Files are limited to 25 MiB, the Kanban runtime's per-file cap. Oversize input is
  refused before anything is sent, and an adapter that still receives too much
  answers with exit status 4.
- Bytes travel on the adapter process's stdin and stdout, never as arguments and
  never as base64 in a prompt. The adapter's stderr is discarded.
- Failures are fixed codes with a fixed hint (`err:CODE: hint`). They never echo
  file contents, adapter output, or credentials.

| Code | Meaning |
| --- | --- |
| `worker_runtime_unset` | No adapter configured |
| `artifact_ref_malformed` | Not a `kanban-artifact:v1:` reference |
| `artifact_board_mismatch`, `artifact_task_mismatch` | Reference or record belongs elsewhere |
| `artifact_not_found` | Task or attachment absent (also adapter exit 3) |
| `artifact_revision_mismatch` | Attachment no longer has the referenced checksum |
| `artifact_integrity_failed` | Received bytes differ from the listed size or SHA-256 (also exit 5); nothing is kept |
| `artifact_checksum_missing` | The adapter listed an artifact without a SHA-256 |
| `artifact_too_large` | Over 25 MiB (also exit 4) |
| `artifact_source_invalid` | Upload source is not an existing regular local file |
| `artifact_name_invalid`, `artifact_name_reserved` | Unsafe name, or a companion-only name |
| `artifact_media_type_invalid` | Not `type/subtype` |
| `artifact_role_invalid`, `artifact_original_required`, `artifact_original_invalid`, `artifact_companion_exists` | Companion misuse |
| `artifact_metadata_mismatch` | The adapter did not preserve the media type, size, checksum or name |
| `artifact_destination_invalid`, `artifact_destination_exists` | Unsafe or occupied download target |
| `artifact_transport_unavailable`, `artifact_transport_timeout`, `artifact_transport_failed`, `artifact_transport_invalid` | The adapter could not run, timed out, failed, or broke this contract |

A download is written to a new file atomically. It never follows a symlink and
does not overwrite an existing file unless `--overwrite` is given. Without
`--overwrite` the guarantee holds at publication, not only before the transfer: the
verified bytes become visible only if nothing exists at the destination at that
moment, so a file another process creates while the artifact is being fetched is
kept and the download fails with `artifact_destination_exists`.

The `artifact_transport_timeout` deadline covers the whole local invocation. The
adapter runs in its own process group; when it finishes, overflows, or times out,
that group is killed, so a wrapper's child (for example a container exec) cannot
outlive the call or keep the output pipe open. A process that deliberately leaves
the group cannot be killed from here, but it still cannot hold the call past the
deadline plus a short cleanup allowance.

## Adapter contract for the deployment

The adapter owns authentication, the Captain/worker boundary, and the native
attachment calls. It needs exactly three verbs. `RUNTIME` is the configured
command prefix. Hermes Helmet ships one reference adapter for a local Docker
Compose worker, [`helmet-compose`](#reference-adapter-helmet-compose); a
deployment with its own topology, identities, or other runtime verbs writes its
own adapter to this contract, and may delegate these three verbs to the public
helper instead of reimplementing them.

| Invocation | stdin | stdout |
| --- | --- | --- |
| `RUNTIME artifact-upload BOARD TASK --name N --media-type M --size S --sha256 H --json` | the exact bytes | one JSON attachment record |
| `RUNTIME artifact-list BOARD TASK --json` | none | a JSON array of attachment records |
| `RUNTIME artifact-download BOARD TASK ATTACHMENT_ID` | none | the exact bytes and nothing else |

An attachment record is the native attachment row plus the checksum:

```json
{
  "id": 41,
  "task_id": "t_0123abcd",
  "filename": "brief.docx",
  "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "size": 18432,
  "sha256": "<64 lowercase hex characters>",
  "uploaded_by": "coordinator",
  "created_at": 1760000000,
  "stored_path": "<worker location; accepted and ignored>"
}
```

The adapter must:

- store uploads through the native attachment write path so the 25 MiB cap, name
  sanitization, durable storage, and attachment id are the runtime's own;
- verify the declared size and SHA-256 before storing (exit 5 on mismatch) and
  compute `sha256` over the stored blob when listing;
- scope every lookup to `BOARD` and `TASK`; the same attachment id on another task
  is not found (exit 3);
- exit `0` on success, `3` not found, `4` over the cap, `5` integrity mismatch, and
  any other status for a failed transport (`126` and `127`, not executable or
  command not found, report it as unavailable);
- keep credentials out of stdout, and not rely on stderr being surfaced.

The coordinator verifies every record and every download again, so a faulty adapter
fails closed instead of delivering the wrong bytes.

## Reference adapter: `helmet-compose`

`helmet-compose` is the public reference adapter for a native Hermes worker in a
dedicated Docker Compose project. It is a single console entrypoint
(`hermes_helmet.compose_runtime`), installed with the package, and it plays two
roles:

```sh
export HERMES_HELMET_HERMES=helmet-compose           # native `hermes kanban ...`
export HERMES_HELMET_WORKER_RUNTIME=helmet-compose   # artifact-upload|list|download
```

| Supported | Behavior |
| --- | --- |
| `helmet-compose kanban ...` | The container's native `hermes kanban ...`, arguments unchanged |
| `helmet-compose artifact-upload\|artifact-list\|artifact-download BOARD TASK ...` | The adapter contract above, served by `hermes_helmet.native_artifacts` |
| `ledger-root`, `ledger-watch`, `dispatch-root`, `prepare-repo`, `wait`, and anything else | Refused with exit `2` and `unsupported_verb`; Docker is not called |

- **Explicit target.** `COMPOSE_FILE` and `COMPOSE_PROJECT_NAME` must both be set.
  Neither has a default, so the adapter never falls back to another project.
  They are Docker-wide variables: every `docker compose` command in that shell
  targets the same project. Use a dedicated shell for the worker project, do not
  export them from a shell startup file, and close the shell when finished. Set
  `HERMES_HELMET_HERMES` and `HERMES_HELMET_WORKER_RUNTIME` in that shell only so
  an existing general worker-runtime configuration is not replaced.
- **Transport.** Each call is `docker compose -p PROJECT exec -T hermes ...` into
  the service named `hermes`, with the argument vector passed without a shell. The
  adapter then hands the process to Docker, so stdin and stdout stay the caller's
  binary streams, the exit status is the container command's (`3`, `4`, `5`
  unchanged), and the coordinator's timeout and process-group kill reach Docker.
  Bytes never appear in arguments.
- **Actor.** The in-container Hermes identity is pinned to `default`
  (`HERMES_PROFILE` and `HERMES_PROFILE_NAME`), independent of any host profile
  variable, so attachments are recorded as uploaded by `default`.
- **Native store.** `hermes_helmet.native_artifacts` runs inside the container and
  uses the runtime's own attachment functions, so the 25 MiB cap, name
  sanitization, collision-free storage, and attachment ids are native. It verifies
  the declared size and SHA-256 before storing, hashes the stored blob when
  listing, scopes every lookup to board and task, and writes only a fixed code to
  stderr.
- **Container prerequisites.** The service is named `hermes`, runs the Hermes
  image's interpreter at `/opt/hermes/.venv/bin/python`, and has this package's
  `src` at `/opt/hermes-helmet/src` (the Hermes Helmet image already does).

Docker being absent is reported as exit `127`, which the coordinator treats as an
unavailable transport.

## How tests differ from real-host acceptance

The repository tests use an in-memory transport and an isolated fixture adapter
(`tests/fixtures/artifact_adapter.py`) that implements the verbs above over a
temporary directory with native-like attachment semantics. They use synthetic
ExampleCo DOCX and PPTX archives and prove the coordinator behavior: byte-identical
round trips, format metadata, revision checks, error codes, and no content or
credentials in arguments, errors, or output. They do not use a real Kanban board,
Docker, or a worker profile.

`tests/test_compose_runtime.py` covers `helmet-compose` in three layers: argument
vector, project selection, pinned actor, unsupported verbs, binary streams and
timeouts through the real executable and a stand-in `docker`; the exit-status
matrix over a stand-in native store; and, when a Hermes interpreter is present,
the real native Kanban store through the adapter chain. Its explicit integration
command runs a disposable Compose project on the pinned Hermes image with a
minimal idle `hermes` service (not the writing recipe, and no model dispatch):

```sh
HELMET_COMPOSE_INTEGRATION=1 PYTHONPATH=src \
  python3 -m unittest tests.test_compose_runtime.ComposeIntegrationTests -v
```

It round-trips DOCX and PPTX bytes through native attachments, checks exit `3`
for a missing attachment, wrong task, and wrong board, `4` for oversize input, and
`5` for an integrity mismatch (calling the adapter directly, because the
coordinator would refuse those inputs first), and confirms earlier bytes are
unchanged. The unique project and its volume are removed afterwards.

Real-host acceptance is separate and belongs to the deployment adapter. It must run
a real DOCX input through the real boundary, have a worker produce a revision as a
durable attachment that survives scratch cleanup, and retrieve it on the coordinator
with a matching checksum. A container-only path does not satisfy it. A handoff that
declares an artifact the runtime cannot find must stay retryable rather than
complete.

See also [helmet-issue.md](helmet-issue.md) for the worker-runtime transport.
