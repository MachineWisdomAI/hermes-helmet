#!/usr/bin/env python3
"""Coordinator artifact handoff through native Kanban task attachments.

A coordinator (for example a first officer on the Captain host) must be able to
put input documents on a Kanban task, list the artifact references a task
carries, and retrieve the exact bytes of a deliverable that a worker attached,
without moving files by hand and without treating a worker-side absolute path
as something the coordinator can read.

This module is the portable, coordinator-side half of that handoff. It adds no
service, artifact database, queue, or scheduler. Native task attachments remain
the only store; the deployment adapter behind the existing
``HERMES_HELMET_WORKER_RUNTIME`` seam owns how bytes cross the Captain/worker
boundary. The contract that adapter must satisfy is documented in
``docs/task-artifacts.md``.

Guarantees enforced here, independent of the adapter:

- A reference carries board, task, native attachment id, and the SHA-256 of the
  exact revision. Retrieval verifies size and checksum, so feedback and
  acceptance can name one revision and a changed file is never mistaken for it.
- References are task-owned. Local paths, URLs, and other tasks' attachments
  cannot stand in for one, and a worker ``stored_path`` is never retained.
- Bytes travel over stdin/stdout of the adapter process: never base64 in a
  prompt, never argv, never a diagnostic. Errors are fixed codes with fixed
  hints; they never echo file contents, adapter output, or credentials.
- The runtime's documented 25 MiB per-file cap is checked before and after
  transfer.
- Uploads never replace or convert an original. A revision is a new attachment;
  previews and extracted text are separate, name-linked attachments.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading
from typing import Callable, Mapping, Protocol, Sequence

from hermes_helmet.helmet_issue import DEFAULT_WORKER_RUNTIME, parse_worker_runtime


# Native ``KANBAN_ATTACHMENT_MAX_BYTES``: one cap for dashboard, tools and CLI.
MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
MAX_LISTING_BYTES = 4 * 1024 * 1024
DEFAULT_TRANSPORT_TIMEOUT = 300.0

REFERENCE_PREFIX = "kanban-artifact:v1:"

ROLE_ORIGINAL = "original"
ROLE_PREVIEW = "preview"
ROLE_EXTRACTED_TEXT = "extracted-text"
ROLES = (ROLE_ORIGINAL, ROLE_PREVIEW, ROLE_EXTRACTED_TEXT)

# Documented adapter exit statuses. Anything else non-zero means "unavailable".
EXIT_NOT_FOUND = 3
EXIT_TOO_LARGE = 4
EXIT_INTEGRITY = 5
# Shell/launcher statuses for "not executable" and "command not found". Some
# interpreters raise OSError for a missing adapter; others (and wrappers such as
# a container exec) report these statuses instead. Both mean unavailable.
EXIT_UNAVAILABLE = frozenset({126, 127})

# Native mimetypes can miss Office types (``.pptx`` is ``None`` on some hosts);
# the coordinator always states the type instead of relying on a guess.
OFFICE_MEDIA_TYPES: Mapping[str, str] = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
DEFAULT_MEDIA_TYPE = "application/octet-stream"

_BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = r"[A-Za-z0-9!#$&^_.+-]{1,127}"
_MEDIA_TYPE_RE = re.compile(rf"^{_TOKEN}/{_TOKEN}$")
_REFERENCE_RE = re.compile(
    r"^kanban-artifact:v1:"
    r"(?P<board>[a-z0-9][a-z0-9_-]{0,63}):"
    r"(?P<task>[A-Za-z0-9][A-Za-z0-9_-]{0,63}):"
    r"(?P<id>[1-9][0-9]{0,17}):"
    r"(?P<sha256>[0-9a-f]{64})$"
)
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_COMPANION_RE = re.compile(
    r"^(?P<base>.+)\.(?:preview\.(?P<ext>[A-Za-z0-9]{1,8})|extracted\.txt)$"
)
_EXTENSION_RE = re.compile(r"^[A-Za-z0-9]{1,8}$")

_HINTS: Mapping[str, str] = {
    "worker_runtime_unset": (
        "configure HERMES_HELMET_WORKER_RUNTIME or --worker-runtime with an "
        "adapter that supports artifact-upload, artifact-list and artifact-download"
    ),
    "artifact_ref_malformed": (
        "expected kanban-artifact:v1:BOARD:TASK:ATTACHMENT_ID:SHA256 from a list "
        "or upload result; file paths, URLs and worker paths are not artifact references"
    ),
    "artifact_board_mismatch": "the reference belongs to a different board than the one requested",
    "artifact_task_mismatch": "the artifact is owned by a different task than the one requested",
    "artifact_not_found": (
        "no such artifact on that task; list the task's artifacts and use a current reference"
    ),
    "artifact_revision_mismatch": (
        "the attachment no longer has the checksum named by the reference; "
        "list the task's artifacts and review the current revision"
    ),
    "artifact_integrity_failed": (
        "received bytes do not match the declared size and SHA-256; nothing was kept"
    ),
    "artifact_too_large": "artifacts are limited to 25 MiB per file by the Kanban runtime",
    "artifact_source_invalid": (
        "the source must be an existing regular local file (not a symlink, URL or directory)"
    ),
    "artifact_name_invalid": (
        "artifact names must be plain file names without directories, control "
        "characters or a leading dot, at most 200 characters"
    ),
    "artifact_name_reserved": (
        "names ending in .preview.EXT or .extracted.txt are reserved for companions "
        "of an original; upload with --role and --original, or rename the original"
    ),
    "artifact_companion_exists": (
        "that original already has this companion; upload the changed file as a "
        "new original revision instead"
    ),
    "artifact_media_type_invalid": "the media type must look like type/subtype",
    "artifact_role_invalid": "role must be original, preview or extracted-text",
    "artifact_original_required": (
        "a preview or extracted-text companion must name the original artifact it belongs to"
    ),
    "artifact_original_invalid": (
        "the named original must be an original artifact on the same task"
    ),
    "artifact_metadata_mismatch": (
        "the adapter did not preserve the uploaded media type, size or checksum"
    ),
    "artifact_destination_invalid": (
        "the destination must be a new file path in an existing directory, not a symlink"
    ),
    "artifact_destination_exists": "the destination exists; choose a new path or pass overwrite",
    "artifact_transport_unavailable": "the worker transport could not be started",
    "artifact_transport_timeout": "the worker transport did not finish in time",
    "artifact_transport_failed": "the worker transport reported a failure",
    "artifact_transport_invalid": "the worker transport broke the artifact contract",
    "artifact_checksum_missing": (
        "the adapter listed an artifact without a SHA-256; the revision cannot be pinned"
    ),
}


class TaskArtifactError(RuntimeError):
    """Fixed-code failure that never carries file content or adapter output."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        suffix = f" ({detail})" if detail else ""
        super().__init__(f"err:{code}: {_HINTS.get(code, code)}{suffix}")


@dataclass(frozen=True)
class ArtifactRef:
    """Identity and exact revision of one task-owned attachment.

    ``sha256`` is the revision. ``stored_path`` of the native row is deliberately
    absent: it is a worker path and never a coordinator-readable deliverable.
    """

    board: str
    task_id: str
    attachment_id: int
    filename: str
    media_type: str
    size: int
    sha256: str
    uploaded_by: str | None = None
    created_at: int | None = None

    @property
    def token(self) -> str:
        return (
            f"{REFERENCE_PREFIX}{self.board}:{self.task_id}:"
            f"{self.attachment_id}:{self.sha256}"
        )

    @property
    def role(self) -> str:
        match = _COMPANION_RE.match(self.filename)
        if match is None:
            return ROLE_ORIGINAL
        return ROLE_PREVIEW if match.group("ext") else ROLE_EXTRACTED_TEXT

    @property
    def companion_of(self) -> str | None:
        match = _COMPANION_RE.match(self.filename)
        return match.group("base") if match else None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "reference": self.token,
            "board": self.board,
            "task_id": self.task_id,
            "attachment_id": self.attachment_id,
            "filename": self.filename,
            "media_type": self.media_type,
            "size": self.size,
            "sha256": self.sha256,
            "role": self.role,
            "companion_of": self.companion_of,
            "uploaded_by": self.uploaded_by,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ArtifactLocator:
    board: str
    task_id: str
    attachment_id: int
    sha256: str


@dataclass(frozen=True)
class FetchedArtifact:
    ref: ArtifactRef
    data: bytes


class ArtifactTransport(Protocol):
    """The whole adapter surface: three operations, bytes in and out.

    Implementations own authentication, the Captain/worker boundary, and the
    native attachment calls. Records are native attachment rows plus ``sha256``
    (see ``docs/task-artifacts.md``).
    """

    def upload(
        self,
        board: str,
        task_id: str,
        *,
        name: str,
        media_type: str,
        data: bytes,
        sha256: str,
    ) -> Mapping[str, object]: ...

    def list_attachments(
        self, board: str, task_id: str
    ) -> Sequence[Mapping[str, object]]: ...

    def download(self, board: str, task_id: str, attachment_id: int) -> bytes: ...


# --------------------------------------------------------------------------
# Subprocess implementation of the transport behind the worker-runtime seam.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecResult:
    returncode: int
    stdout: bytes
    overflow: bool = False
    timed_out: bool = False


Executor = Callable[[Sequence[str], "bytes | None", int, float], ExecResult]


def run_bounded(
    argv: Sequence[str],
    stdin_data: bytes | None,
    max_stdout: int,
    timeout: float,
) -> ExecResult:
    """Run *argv* with binary stdin/stdout, bounded output, and no stderr capture.

    stderr is discarded so an adapter diagnostic can never reach our errors or
    logs. Output beyond *max_stdout* kills the process instead of buffering it.
    """

    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    timed_out = threading.Event()

    def _expire() -> None:
        timed_out.set()
        process.kill()

    def _feed() -> None:
        try:
            assert process.stdin is not None
            process.stdin.write(stdin_data or b"")
        except OSError:
            pass  # The adapter may reject early and close its end.
        finally:
            try:
                assert process.stdin is not None
                process.stdin.close()
            except OSError:
                pass

    timer = threading.Timer(timeout, _expire)
    timer.daemon = True
    timer.start()
    feeder: threading.Thread | None = None
    if stdin_data is not None:
        feeder = threading.Thread(target=_feed, daemon=True)
        feeder.start()
    chunks: list[bytes] = []
    total = 0
    overflow = False
    try:
        assert process.stdout is not None
        while True:
            chunk = process.stdout.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_stdout:
                overflow = True
                process.kill()
                break
            chunks.append(chunk)
        returncode = process.wait()
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
        if feeder is not None:
            feeder.join(timeout=5)
    return ExecResult(returncode, b"".join(chunks), overflow, timed_out.is_set())


class WorkerRuntimeTransport:
    """Reach the adapter as ``<runtime> artifact-upload|artifact-list|artifact-download``.

    ``artifact-upload BOARD TASK --name N --media-type M --size S --sha256 H --json``
        stdin: the exact bytes. stdout: one JSON attachment record.
    ``artifact-list BOARD TASK --json``
        stdout: a JSON array of attachment records.
    ``artifact-download BOARD TASK ATTACHMENT_ID``
        stdout: the exact bytes and nothing else.

    Exit ``0`` succeeds; ``3`` not found; ``4`` over the 25 MiB cap; ``5`` upload
    integrity mismatch; any other status means the transport is unavailable.
    """

    def __init__(
        self,
        runtime: str | Sequence[str] | None = None,
        *,
        timeout: float = DEFAULT_TRANSPORT_TIMEOUT,
        executor: Executor | None = None,
    ) -> None:
        resolved = parse_worker_runtime(runtime) or DEFAULT_WORKER_RUNTIME
        if not resolved:
            raise TaskArtifactError("worker_runtime_unset")
        self.runtime = tuple(resolved)
        self.timeout = timeout
        self._execute = executor or run_bounded

    def _call(
        self,
        verb_args: Sequence[str],
        *,
        stdin_data: bytes | None = None,
        max_stdout: int,
        oversize_code: str = "artifact_transport_invalid",
    ) -> bytes:
        argv = [*self.runtime, *verb_args]
        try:
            result = self._execute(argv, stdin_data, max_stdout, self.timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise TaskArtifactError("artifact_transport_unavailable") from exc
        if result.timed_out:
            raise TaskArtifactError("artifact_transport_timeout")
        if result.overflow:
            raise TaskArtifactError(oversize_code)
        if result.returncode in EXIT_UNAVAILABLE:
            raise TaskArtifactError("artifact_transport_unavailable")
        if result.returncode == EXIT_NOT_FOUND:
            raise TaskArtifactError("artifact_not_found")
        if result.returncode == EXIT_TOO_LARGE:
            raise TaskArtifactError("artifact_too_large")
        if result.returncode == EXIT_INTEGRITY:
            raise TaskArtifactError("artifact_integrity_failed")
        if result.returncode != 0:
            raise TaskArtifactError(
                "artifact_transport_failed", f"exit {result.returncode}"
            )
        return result.stdout

    @staticmethod
    def _json(payload: bytes) -> object:
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise TaskArtifactError("artifact_transport_invalid") from exc

    def upload(
        self,
        board: str,
        task_id: str,
        *,
        name: str,
        media_type: str,
        data: bytes,
        sha256: str,
    ) -> Mapping[str, object]:
        out = self._call(
            [
                "artifact-upload",
                board,
                task_id,
                "--name",
                name,
                "--media-type",
                media_type,
                "--size",
                str(len(data)),
                "--sha256",
                sha256,
                "--json",
            ],
            stdin_data=data,
            max_stdout=MAX_LISTING_BYTES,
        )
        record = self._json(out)
        if not isinstance(record, dict):
            raise TaskArtifactError("artifact_transport_invalid")
        return record

    def list_attachments(
        self, board: str, task_id: str
    ) -> Sequence[Mapping[str, object]]:
        out = self._call(
            ["artifact-list", board, task_id, "--json"],
            max_stdout=MAX_LISTING_BYTES,
        )
        records = self._json(out)
        if not isinstance(records, list):
            raise TaskArtifactError("artifact_transport_invalid")
        return records

    def download(self, board: str, task_id: str, attachment_id: int) -> bytes:
        return self._call(
            ["artifact-download", board, task_id, str(attachment_id)],
            max_stdout=MAX_ARTIFACT_BYTES,
            oversize_code="artifact_too_large",
        )


# --------------------------------------------------------------------------
# Validation helpers.
# --------------------------------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_board(board: object) -> str:
    if not isinstance(board, str) or _BOARD_RE.fullmatch(board) is None:
        raise TaskArtifactError("artifact_board_mismatch", "invalid board identifier")
    return board


def _require_task(task_id: object) -> str:
    if not isinstance(task_id, str) or _TASK_RE.fullmatch(task_id) is None:
        raise TaskArtifactError("artifact_task_mismatch", "invalid task identifier")
    return task_id


def valid_filename(name: object) -> bool:
    if not isinstance(name, str) or not name or len(name) > 200:
        return False
    if name != name.strip() or name.startswith("."):
        return False
    if "/" in name or "\\" in name:
        return False
    return all(ch.isprintable() for ch in name)


def _require_name(name: object) -> str:
    if not valid_filename(name):
        raise TaskArtifactError("artifact_name_invalid")
    return str(name)


def media_type_for(filename: str) -> str:
    """Stated media type for a file name; Office types never depend on the host."""

    suffix = Path(filename).suffix.lower()
    if suffix in OFFICE_MEDIA_TYPES:
        return OFFICE_MEDIA_TYPES[suffix]
    guessed = mimetypes.guess_type(filename)[0]
    return guessed or DEFAULT_MEDIA_TYPE


def _normalize_media_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.split(";", 1)[0].strip().lower()
    return candidate if _MEDIA_TYPE_RE.fullmatch(candidate) else None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_artifact_reference(reference: "str | ArtifactRef") -> ArtifactLocator:
    """Parse a task-owned reference; anything else is rejected without echo."""

    if isinstance(reference, ArtifactRef):
        reference = reference.token
    if not isinstance(reference, str):
        raise TaskArtifactError("artifact_ref_malformed")
    match = _REFERENCE_RE.fullmatch(reference.strip())
    if match is None:
        raise TaskArtifactError("artifact_ref_malformed")
    return ArtifactLocator(
        board=match.group("board"),
        task_id=match.group("task"),
        attachment_id=int(match.group("id")),
        sha256=match.group("sha256"),
    )


def _ref_from_record(board: str, task_id: str, record: object) -> ArtifactRef:
    """Validate one adapter record; unknown fields (``stored_path``) are dropped."""

    if not isinstance(record, Mapping):
        raise TaskArtifactError("artifact_transport_invalid")
    attachment_id = record.get("id")
    if not _is_int(attachment_id) or int(attachment_id) < 1:  # type: ignore[arg-type]
        raise TaskArtifactError("artifact_transport_invalid")
    owner = record.get("task_id")
    if owner != task_id:
        raise TaskArtifactError("artifact_task_mismatch")
    filename = record.get("filename")
    if not valid_filename(filename):
        raise TaskArtifactError("artifact_transport_invalid")
    size = record.get("size")
    if not _is_int(size) or int(size) < 0:  # type: ignore[arg-type]
        raise TaskArtifactError("artifact_transport_invalid")
    if int(size) > MAX_ARTIFACT_BYTES:  # type: ignore[arg-type]
        raise TaskArtifactError("artifact_too_large")
    checksum = record.get("sha256")
    if not isinstance(checksum, str) or _SHA256_RE.fullmatch(checksum) is None:
        raise TaskArtifactError("artifact_checksum_missing")
    uploaded_by = record.get("uploaded_by")
    if uploaded_by is not None and (
        not isinstance(uploaded_by, str)
        or len(uploaded_by) > 128
        or not uploaded_by.isprintable()
    ):
        raise TaskArtifactError("artifact_transport_invalid")
    created_at = record.get("created_at")
    if created_at is not None and (not _is_int(created_at) or int(created_at) < 0):  # type: ignore[arg-type]
        raise TaskArtifactError("artifact_transport_invalid")
    media_type = _normalize_media_type(record.get("content_type")) or media_type_for(
        str(filename)
    )
    return ArtifactRef(
        board=board,
        task_id=task_id,
        attachment_id=int(attachment_id),  # type: ignore[arg-type]
        filename=str(filename),
        media_type=media_type,
        size=int(size),  # type: ignore[arg-type]
        sha256=str(checksum),
        uploaded_by=uploaded_by,
        created_at=created_at,
    )


# --------------------------------------------------------------------------
# Public operations.
# --------------------------------------------------------------------------


def list_artifacts(
    transport: ArtifactTransport,
    *,
    board: str,
    task_id: str,
    role: str | None = None,
) -> list[ArtifactRef]:
    """Artifact references the task currently carries, oldest first."""

    board = _require_board(board)
    task_id = _require_task(task_id)
    if role is not None and role not in ROLES:
        raise TaskArtifactError("artifact_role_invalid")
    refs: dict[int, ArtifactRef] = {}
    for record in transport.list_attachments(board, task_id):
        ref = _ref_from_record(board, task_id, record)
        if ref.attachment_id in refs:
            raise TaskArtifactError("artifact_transport_invalid")
        refs[ref.attachment_id] = ref
    ordered = sorted(refs.values(), key=lambda item: item.attachment_id)
    if role is not None:
        ordered = [item for item in ordered if item.role == role]
    return ordered


def _companion_name(original: ArtifactRef, role: str, source_name: str) -> str:
    if role == ROLE_EXTRACTED_TEXT:
        return f"{original.filename}.extracted.txt"
    extension = Path(source_name).suffix.lstrip(".")
    if _EXTENSION_RE.fullmatch(extension) is None:
        raise TaskArtifactError("artifact_name_invalid")
    return f"{original.filename}.preview.{extension}"


def upload_artifact_bytes(
    transport: ArtifactTransport,
    *,
    board: str,
    task_id: str,
    name: str,
    data: bytes,
    media_type: str | None = None,
    role: str = ROLE_ORIGINAL,
    original: "str | ArtifactRef | None" = None,
) -> ArtifactRef:
    """Attach *data* to the task as a new, immutable artifact.

    An original keeps its own name and bytes. A preview or extracted-text
    companion is a separate attachment named after its original; it can never
    replace or stand in for it.
    """

    board = _require_board(board)
    task_id = _require_task(task_id)
    name = _require_name(name)
    if role not in ROLES:
        raise TaskArtifactError("artifact_role_invalid")
    if not isinstance(data, (bytes, bytearray)):
        raise TaskArtifactError("artifact_source_invalid")
    data = bytes(data)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise TaskArtifactError("artifact_too_large")
    if role != ROLE_ORIGINAL:
        if original is None:
            raise TaskArtifactError("artifact_original_required")
        locator = parse_artifact_reference(original)
        if locator.board != board or locator.task_id != task_id:
            raise TaskArtifactError("artifact_original_invalid")
        listing = {
            item.attachment_id: item
            for item in list_artifacts(transport, board=board, task_id=task_id)
        }
        parent = listing.get(locator.attachment_id)
        if (
            parent is None
            or parent.sha256 != locator.sha256
            or parent.role != ROLE_ORIGINAL
        ):
            raise TaskArtifactError("artifact_original_invalid")
        source_name = name
        name = _require_name(_companion_name(parent, role, source_name))
        if any(item.filename == name for item in listing.values()):
            # Native storage would silently rename a collision, which would
            # sever the name link to the original.
            raise TaskArtifactError("artifact_companion_exists")
        if media_type is None and role == ROLE_EXTRACTED_TEXT:
            media_type = "text/plain"
    elif original is not None:
        raise TaskArtifactError("artifact_role_invalid")
    elif _COMPANION_RE.match(name):
        # Would be listed as someone's preview/extracted text, not an original.
        raise TaskArtifactError("artifact_name_reserved")
    stated = _normalize_media_type(media_type) if media_type else media_type_for(name)
    if stated is None:
        raise TaskArtifactError("artifact_media_type_invalid")
    checksum = sha256_hex(data)
    record = transport.upload(
        board, task_id, name=name, media_type=stated, data=data, sha256=checksum
    )
    ref = _ref_from_record(board, task_id, record)
    returned_type = (
        _normalize_media_type(record.get("content_type"))
        if isinstance(record, Mapping)
        else None
    )
    if ref.size != len(data) or ref.sha256 != checksum or returned_type != stated:
        raise TaskArtifactError("artifact_metadata_mismatch")
    if role != ROLE_ORIGINAL and ref.filename != name:
        raise TaskArtifactError("artifact_metadata_mismatch")
    return ref


def _read_source(path: str | os.PathLike[str]) -> tuple[str, bytes]:
    text = os.fspath(path)
    if _URL_SCHEME_RE.match(text):
        raise TaskArtifactError("artifact_source_invalid")
    try:
        # O_NONBLOCK so a FIFO or device cannot stall the open; it is rejected
        # by the regular-file check below. It has no effect on regular files.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(text, flags)
    except OSError as exc:
        raise TaskArtifactError("artifact_source_invalid") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise TaskArtifactError("artifact_source_invalid")
        if info.st_size > MAX_ARTIFACT_BYTES:
            raise TaskArtifactError("artifact_too_large")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(MAX_ARTIFACT_BYTES + 1)
    except OSError as exc:
        raise TaskArtifactError("artifact_source_invalid") from exc
    finally:
        os.close(descriptor)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise TaskArtifactError("artifact_too_large")
    return os.path.basename(text), data


def upload_artifact(
    transport: ArtifactTransport,
    *,
    board: str,
    task_id: str,
    path: str | os.PathLike[str],
    name: str | None = None,
    media_type: str | None = None,
    role: str = ROLE_ORIGINAL,
    original: "str | ArtifactRef | None" = None,
) -> ArtifactRef:
    """Upload one local regular file; the file's bytes are never altered."""

    base, data = _read_source(path)
    return upload_artifact_bytes(
        transport,
        board=board,
        task_id=task_id,
        name=name or base,
        data=data,
        media_type=media_type,
        role=role,
        original=original,
    )


def fetch_artifact(
    transport: ArtifactTransport,
    reference: "str | ArtifactRef",
    *,
    board: str | None = None,
    task_id: str | None = None,
) -> FetchedArtifact:
    """Retrieve the exact bytes of the revision a reference names, verified.

    *board* and *task_id* state which task the coordinator is working on; a
    reference owned by another board or task fails instead of being fetched.
    """

    locator = parse_artifact_reference(reference)
    if board is not None and locator.board != _require_board(board):
        raise TaskArtifactError("artifact_board_mismatch")
    if task_id is not None and locator.task_id != _require_task(task_id):
        raise TaskArtifactError("artifact_task_mismatch")
    listing = list_artifacts(transport, board=locator.board, task_id=locator.task_id)
    entry = next(
        (item for item in listing if item.attachment_id == locator.attachment_id), None
    )
    if entry is None:
        raise TaskArtifactError("artifact_not_found")
    if entry.sha256 != locator.sha256:
        raise TaskArtifactError("artifact_revision_mismatch")
    data = transport.download(entry.board, entry.task_id, entry.attachment_id)
    if not isinstance(data, (bytes, bytearray)):
        raise TaskArtifactError("artifact_transport_invalid")
    data = bytes(data)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise TaskArtifactError("artifact_too_large")
    if len(data) != entry.size or sha256_hex(data) != entry.sha256:
        raise TaskArtifactError("artifact_integrity_failed")
    return FetchedArtifact(ref=entry, data=data)


def save_artifact(
    transport: ArtifactTransport,
    reference: "str | ArtifactRef",
    destination: str | os.PathLike[str],
    *,
    board: str | None = None,
    task_id: str | None = None,
    overwrite: bool = False,
) -> ArtifactRef:
    """Verify, then write one artifact atomically to a coordinator-local path."""

    target = Path(destination)
    parent = target.parent if str(target.parent) else Path(".")
    if target.is_symlink() or not parent.is_dir():
        raise TaskArtifactError("artifact_destination_invalid")
    if target.exists() and (not overwrite or not target.is_file()):
        raise TaskArtifactError(
            "artifact_destination_exists"
            if target.is_file()
            else "artifact_destination_invalid"
        )
    fetched = fetch_artifact(transport, reference, board=board, task_id=task_id)
    descriptor, temporary = tempfile.mkstemp(prefix=".artifact-", dir=parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(fetched.data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        raise TaskArtifactError("artifact_destination_invalid") from exc
    finally:
        Path(temporary).unlink(missing_ok=True)
    return fetched.ref
