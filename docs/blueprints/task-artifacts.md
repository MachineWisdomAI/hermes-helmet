# Hermes Helmet task artifacts: a retrospective Blueprint

Hermes Helmet is a product of [Machine Wisdom AI](https://machine-wisdom.ai/), which also publishes this account.

This document describes how Hermes Helmet already moves documents between the first officer and the Hermes worker. It is a retrospective account of the contract at one pinned source revision, written for builders who delegate complex work to coding agents and want to keep control of architecture and authority. It is not a proposal, and it does not make Blueprints a required process.

Hermes Helmet moves documents between the first officer and the worker without a shared folder and without either side reading the other's filesystem. The first officer attaches an input to a Kanban task. The worker attaches its deliverable to the same task. The first officer later retrieves exactly the revision it means to review. Hermes Helmet adds no artifact service, database, queue or scheduler for this. The task's native attachments are the only store, and the deployment's existing worker-runtime transport carries the bytes.

The sections below follow an artifact from ownership, through naming, upload and retrieval, to the failures you can expect and how to recover. The detailed command and setup contract lives in [docs/task-artifacts.md](https://github.com/MachineWisdomAI/hermes-helmet/blob/2188219aeb30447b1a587357d245df39b6acab69/docs/task-artifacts.md) and is not repeated here.

## Who owns what

Ownership is split between two components, and the split is the main design decision.

The coordinator side lives in [`task_artifacts.py`](https://github.com/MachineWisdomAI/hermes-helmet/blob/2188219aeb30447b1a587357d245df39b6acab69/src/hermes_helmet/task_artifacts.py) and runs where the first officer runs. It validates names, references and sizes, computes and checks checksums, and decides whether a retrieval may succeed.

The adapter side owns everything across the Captain and worker boundary: authentication, which identity writes, and the native attachment calls. In this library's protocol, authentication is delegated to the adapter; the coordinator does not handle the worker store's credentials. The coordinator reaches the adapter only through the command prefix in `HERMES_HELMET_WORKER_RUNTIME`, using three verbs: `artifact-upload`, `artifact-list` and `artifact-download`.

Hermes Helmet ships one reference adapter, `helmet-compose`, for a worker in a local Docker Compose project. Inside the container, [`native_artifacts.py`](https://github.com/MachineWisdomAI/hermes-helmet/blob/2188219aeb30447b1a587357d245df39b6acab69/src/hermes_helmet/native_artifacts.py) implements the three verbs on the Hermes runtime's own attachment functions. The 25 MiB cap, file name sanitization, collision-free storage and attachment ids therefore belong to the runtime and are not copied into Hermes Helmet. A deployment with a different topology or identities writes its own adapter to the same three-verb contract, and may delegate the supported verbs to the reference one.

The coordinator does not take the adapter's word for what it stored or returned. It checks every record and every download against the size and checksum it expects, so an adapter that returns the wrong record or bytes fails closed instead of delivering them. This is integrity checking, not a general statement about adapters.

## What a reference names

An artifact is named only by a reference of the form `kanban-artifact:v1:BOARD:TASK:ATTACHMENT_ID:SHA256`. The parser accepts exactly that shape: a lowercase board identifier, a task identifier, a positive integer attachment id and a 64-character lowercase hex checksum. Anything else is rejected with `artifact_ref_malformed`, and the rejection does not echo the offending input.

Each part carries a different guarantee. Board and task say who owns the artifact. The attachment id is the native one, reused rather than reissued. The checksum is the revision, so a review comment or acceptance decision can name one specific file and not "whatever is attached now."

Listing returns these fields along with the file name, stated media type, size, role, the original's name for a companion, who uploaded it and when. The native `stored_path` is a location inside the worker. The coordinator cannot read it, so the record parser drops it, and no result the coordinator prints contains it.

This is why a file path, a URL, another task's attachment or a bare number cannot stand in for an artifact. A reference that belongs to a different board or task than the one the caller names fails with `artifact_board_mismatch` or `artifact_task_mismatch` before any transfer. A listing that contains a row owned by a different task is rejected with `artifact_task_mismatch` too, and the adapter scopes its own lookups to the board and task, so the same attachment id on another task is not found.

## A representative flow

Take a first officer handing a brief to a worker and reviewing the result. The documented interface is:

```sh
helmet artifact upload   BOARD TASK_ID ./brief.docx
helmet artifact list     BOARD TASK_ID
helmet artifact download REFERENCE ./retrieved.docx --board BOARD --task-id TASK_ID
```

### Upload

The coordinator reads the local file. It requires a regular file, refuses symlinks, URLs and directories, and refuses anything over 25 MiB before anything is sent. It states the media type itself, using a fixed table for DOCX, PPTX and XLSX so the result does not depend on the host's MIME database. It computes the SHA-256, then calls the adapter with the size, checksum and name as arguments and the file's bytes on stdin. Bytes never appear in arguments and never travel as base64 in a prompt.

The adapter reads at most one byte past the cap and compares the received bytes with the declared size and checksum. It stores nothing if either differs. Otherwise it writes the attachment through the native store and returns one JSON record. The coordinator validates that record and compares size, checksum and media type with what it sent. If the adapter changed any of them, the upload fails with `artifact_metadata_mismatch`. The observable result is a JSON description of the artifact, including its reference. File contents are never printed.

`native_artifacts.py` records the current runtime profile as the uploader. In the reference Compose adapter, the in-container Hermes identity is pinned to `default` for the artifact verbs and for `kanban`, regardless of any host profile variable, so attachments from that adapter show `default` as the uploader.

### Listing

The worker attaches its deliverable to the same task through the native write path. The first officer lists the task and picks the reference it wants to review. Listing orders by attachment id, oldest first, and can filter by role. The listing itself is checked: every record must belong to the requested task, have a valid name and size, and carry a 64-character lowercase SHA-256. A duplicate id or a record without a valid checksum rejects the whole listing (`artifact_transport_invalid`, `artifact_checksum_missing`), because without a checksum the revision cannot be pinned.

### Download

Retrieval does the following, and every step must pass before bytes are returned:

1. It parses the reference and checks it against the board and task the caller named.
2. It lists the task again. If the attachment is absent, the result is `artifact_not_found`. If it is present with a different checksum, the result is `artifact_revision_mismatch`, so feedback and acceptance cannot silently apply to a superseded file.
3. It requests the bytes over the adapter's stdout and compares their length and SHA-256 with the listing. A mismatch is `artifact_integrity_failed`, and nothing is kept.

The tests in [`test_task_artifacts.py`](https://github.com/MachineWisdomAI/hermes-helmet/blob/2188219aeb30447b1a587357d245df39b6acab69/tests/test_task_artifacts.py) exercise this by truncating, flipping, extending and mistyping a download, and assert that nothing is returned or written to the destination in those cases.

### Publication to the destination

`save_artifact` writes the verified bytes to a temporary file in the destination directory, flushes and syncs it, and then publishes it. The destination must not be a symlink, and an existing file is not overwritten unless `--overwrite` is given.

With `--overwrite`, publication is `os.replace`, which swaps the complete file into place. Without it, the normal path hard-links the complete, synced temporary file to the destination. The link fails rather than replaces, so the destination appears in one step with all its bytes. Those two paths are the atomic ones.

Where the filesystem has no hard links, `_publish_new` falls back to creating the destination exclusively (`O_EXCL`) and writing into it. That fallback still refuses an existing path or symlink and removes its partial file if the write fails, but the file exists while it is being written, so a concurrent reader could see it incomplete. The documentation words atomicity more generally than this fallback delivers. Treat the fallback as safe against clobbering, not against early visibility.

In both no-overwrite paths, the early "does the destination exist" check only saves a pointless transfer. If another process creates a regular file at the destination during the transfer, that file is kept and the download fails with `artifact_destination_exists`. If what appears is a symlink or any other invalid target, it is also left in place, but the failure is `artifact_destination_invalid`. The tests cover a file and a symlink appearing mid-transfer, and the no-hard-link fallback.

## Revisions, and what retrieval does not decide

The SHA-256 is the revision. Upload never replaces an attachment. A changed file is a new attachment with a new id and a new reference, and earlier revisions stay retrievable under their own references. Retrieval is exact: the same reference yields the same bytes or an error, never a newer file. Upload is deliberately not idempotent. Sending the same file twice creates two attachments, and the source does not deduplicate them. A caller who needs to know whether a file is current compares checksums in a listing.

Retrieval integrity answers a narrow question: did the exact bytes arrive intact and land safely. It does not answer whether the work is acceptable. The sources define no acceptance state for an artifact, and a successful download says only that the reviewer now holds the revision the reference named. Whether to accept, request changes or reject remains the first officer's judgment under the authority the Captain delegated. The pinned checksum lets that judgment be tied to one revision, but recording it is outside what these sources describe.

## Originals and companions

The original DOCX or PPTX is the editable artifact. It is stored byte for byte under its own name, and its reference and checksum are unchanged by anything attached beside it.

A rendered preview or extracted text can be attached as a separate record named after its original, for example `brief.docx.preview.png` and `brief.docx.extracted.txt`. Companions are uploaded with `--role preview` or `--role extracted-text` and `--original REFERENCE`. The coordinator checks that the named original is an original on the same task and that its checksum still matches, or the upload fails with `artifact_original_invalid`. A companion cannot be the original of another companion. Listing derives each companion's role and original name from its file name, and `--role original` listings exclude companions.

Two rules keep the link honest:

- The suffixes `.preview.EXT` and `.extracted.txt` are reserved. An ordinary upload with such a name, like `x.docx.preview.png`, fails with `artifact_name_reserved`.
- A companion whose derived file name already exists on the task fails with `artifact_companion_exists`, because native storage would otherwise rename the collision silently and sever the name link. The check compares the exact derived name, not the companion kind. An extracted-text companion always derives `ORIGINAL.extracted.txt`, so a second one for the same original always collides. A preview name includes the preview file's extension, so `brief.docx.preview.png` and `brief.docx.preview.jpg` are different names and the source does not refuse the second.

These roles are a storage and naming convention. The pinned sources establish that companions are stored and linked by name. They contain no renderer or extractor, so nothing should be assumed to produce a preview or extract text automatically. The upload path takes whatever bytes the caller provides, supplying only the derived name and, for extracted text, a `text/plain` media type. Producing the files is the uploader's job. A changed original should be uploaded as a new revision.

## What the Compose adapter supports and refuses

The reference adapter, [`helmet-compose`](https://github.com/MachineWisdomAI/hermes-helmet/blob/2188219aeb30447b1a587357d245df39b6acab69/src/hermes_helmet/compose_runtime.py), supports four verbs: `kanban`, which runs the container's native `hermes kanban` with arguments unchanged, and the three artifact verbs. The ledger, dispatch, repository-preparation and wait verbs belong to a deployment's own adapter. Here they, and any other verb, are refused with exit status 2 and `unsupported_verb`, and Docker is never called.

Several choices keep the Captain and worker boundary visible:

- `COMPOSE_FILE` and `COMPOSE_PROJECT_NAME` must both be set and have no defaults, so the adapter never falls back to another project. Because they are Docker-wide variables, the documentation advises a dedicated shell for the worker project.
- Each call becomes `docker compose -p PROJECT exec -T hermes ...`, with arguments passed as a vector and no shell. The process is then replaced by Docker with `exec`, so stdin and stdout remain the caller's binary streams, the container command's exit status comes back unchanged, and the coordinator's timeout reaches Docker.
- If Docker is missing, the adapter exits 127, which the coordinator reports as an unavailable transport.

Inside the container, `native_artifacts` verifies the declared size and SHA-256 before storing anything, hashes the stored blob when listing, scopes every lookup to the board and task, and writes only a fixed code to stderr. The documentation requires a `hermes` service, the Hermes interpreter at `/opt/hermes/.venv/bin/python` and this package's `src` at `/opt/hermes-helmet/src`. Deployment setup is covered in the product's own documents.

## Material failures and recovery

Every failure is a fixed code with a fixed hint, such as `err:artifact_revision_mismatch: ...`. Surfaced errors do not carry file contents or adapter output, and the adapter's stderr is discarded. The reference adapter also writes only fixed codes there. The fixture adapter in [`artifact_adapter.py`](https://github.com/MachineWisdomAI/hermes-helmet/blob/2188219aeb30447b1a587357d245df39b6acab69/tests/fixtures/artifact_adapter.py) prints a credential-like sentinel on purpose while failing, and the tests assert it never reaches an error message. That demonstrates what the coordinator surfaces. It does not promise that an arbitrary adapter cannot expose credentials through some other channel.

Transport problems are separate from contract problems. A missing runtime setting gives `worker_runtime_unset`. Adapter exit status 126 or 127 means it could not start and surfaces as `artifact_transport_unavailable`. Status 3 maps to `artifact_not_found`, 4 to `artifact_too_large`, 5 to `artifact_integrity_failed`, and any other nonzero status to `artifact_transport_failed`. Unparseable output is `artifact_transport_invalid`.

The invocation deadline, 300 seconds by default, covers the whole local call. The adapter runs in its own process group, and the group is killed on completion, overflow or timeout, so a wrapper's child process cannot hold the output pipe past the deadline. Output over the limit kills the process instead of being buffered. The documentation states the remaining edge: a process that deliberately leaves the group cannot be killed from here, but it still cannot hold the call past the deadline plus a short cleanup allowance.

Recovery follows from the code:

- After `artifact_not_found` or `artifact_revision_mismatch`, list the task and review the current reference. The old reference still names its revision, but a changed file needs a new reference. An absent or foreign reference can also mean the wrong task.
- After `artifact_integrity_failed`, nothing was kept, so there is no partial file to clean up. Download again.
- After `artifact_destination_exists`, choose a new path, or pass `--overwrite` if replacing the file is intended. After `artifact_destination_invalid`, the destination is a symlink, an unwritable path or another target that is not a regular file; choose a different path.
- After `artifact_transport_unavailable`, `artifact_transport_timeout` or `artifact_transport_failed`, the problem is in the adapter or its runtime (the Compose project, Docker or the container), and no bytes were accepted.
- After `artifact_too_large`, the file cannot be a native attachment at that size.

The documentation adds one rule for handoffs: a handoff that declares an artifact the runtime cannot find "must stay retryable rather than complete." The reviewed sources contain no code that enforces this. It is documented intent for the deployment adapter and workflow, not observed behavior.

## Evidence scope

Source pin: [`2188219aeb30447b1a587357d245df39b6acab69`](https://github.com/MachineWisdomAI/hermes-helmet/tree/2188219aeb30447b1a587357d245df39b6acab69). This account is a static reading of the documentation and code at that revision: [`docs/task-artifacts.md`](https://github.com/MachineWisdomAI/hermes-helmet/blob/2188219aeb30447b1a587357d245df39b6acab69/docs/task-artifacts.md), [`task_artifacts.py`](https://github.com/MachineWisdomAI/hermes-helmet/blob/2188219aeb30447b1a587357d245df39b6acab69/src/hermes_helmet/task_artifacts.py), [`native_artifacts.py`](https://github.com/MachineWisdomAI/hermes-helmet/blob/2188219aeb30447b1a587357d245df39b6acab69/src/hermes_helmet/native_artifacts.py) and [`compose_runtime.py`](https://github.com/MachineWisdomAI/hermes-helmet/blob/2188219aeb30447b1a587357d245df39b6acab69/src/hermes_helmet/compose_runtime.py). No test, Kanban board, Docker or worker was run for this account.

The repository tests ([`test_task_artifacts.py`](https://github.com/MachineWisdomAI/hermes-helmet/blob/2188219aeb30447b1a587357d245df39b6acab69/tests/test_task_artifacts.py) and the fixture adapter) were read as static coverage. By their own description they use an in-memory transport and a stand-in adapter over a temporary directory, with synthetic DOCX and PPTX archives. They cover coordinator behavior such as byte-identical round trips, revision checks, destination publication and fixed-code errors, not a live board. The documentation also describes a Compose integration test and a [test Compose file](https://github.com/MachineWisdomAI/hermes-helmet/blob/2188219aeb30447b1a587357d245df39b6acab69/tests/fixtures/compose/artifact-transport.compose.yaml) for it, and a real-host acceptance check on the deployment adapter. The Compose file and the documentation's account were read, not the test or its results, and no claim is made that either passes.

The `helmet artifact` command wiring was not among the sources read. Its flags are taken from the documentation and test names, and the library behavior described above sits underneath it.

For command options and deployment setup, read [docs/task-artifacts.md](https://github.com/MachineWisdomAI/hermes-helmet/blob/2188219aeb30447b1a587357d245df39b6acab69/docs/task-artifacts.md).
