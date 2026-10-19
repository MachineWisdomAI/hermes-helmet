"""Native Kanban attachment helper behind the ``artifact-*`` adapter verbs.

This module runs where the native Hermes Kanban store lives (inside the worker
container, under the Hermes interpreter). It implements the three verbs of
``docs/task-artifacts.md`` directly on the runtime's own attachment functions:

    upload   BOARD TASK --name N --media-type M --size S --sha256 H --json
    list     BOARD TASK --json
    download BOARD TASK ATTACHMENT_ID

so the 25 MiB cap, name sanitisation, collision-free durable storage and
attachment ids are the runtime's, not a copy of them. Deployment adapters may
call :func:`main` (or the functions below) and keep their own transport.

Contract, identical to the documented adapter exit statuses:

- ``0`` success; ``3`` board, task or attachment not found (lookups are scoped
  to ``BOARD`` and ``TASK``); ``4`` over the 25 MiB cap; ``5`` declared size or
  SHA-256 differs from the received bytes (nothing is stored); ``2`` malformed
  invocation; ``1`` any other failure.
- Bytes use binary stdin/stdout only. stderr carries a fixed one-line code and
  never file content, names, or paths.

``hermes_cli`` is imported lazily so this module can be imported, and the
argument handling tested, where Hermes is not installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from typing import Any, BinaryIO, Mapping, NoReturn, Sequence

MAX_ARTIFACT_BYTES = 25 * 1024 * 1024

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_TOO_LARGE = 4
EXIT_INTEGRITY = 5

_BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_ACTOR = "default"
_CHUNK = 1024 * 1024


class NativeArtifactError(Exception):
    """A failure that maps to one documented exit status and a fixed code."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_native() -> tuple[Any, Any]:
    """Return the native ``(kanban_db, kanban_db_connect)`` modules."""

    try:
        from hermes_cli import kanban_db, kanban_db_connect
    except ImportError as exc:  # pragma: no cover - exercised only without Hermes
        raise NativeArtifactError(EXIT_FAILED, "native_runtime_unavailable") from exc
    return kanban_db, kanban_db_connect


def _actor() -> str:
    """The Hermes identity writing the attachment (the pinned runtime actor)."""

    try:
        from hermes_cli.profiles import current_profile_name

        return current_profile_name(_DEFAULT_ACTOR) or _DEFAULT_ACTOR
    except Exception:  # noqa: BLE001 - provenance must not block a transfer
        return _DEFAULT_ACTOR


def _check_scope(board: str, task_id: str) -> None:
    if not _BOARD_RE.match(board) or not _TASK_RE.match(task_id):
        raise NativeArtifactError(EXIT_USAGE, "malformed_scope")


class _Store:
    """One board connection with task-scoped helpers."""

    def __init__(self, board: str, task_id: str) -> None:
        _check_scope(board, task_id)
        self.board = board
        self.task_id = task_id
        self.kb, self._kbc = _load_native()
        try:
            exists = self.kb.board_exists(board)
        except ValueError as exc:
            raise NativeArtifactError(EXIT_USAGE, "malformed_scope") from exc
        if not exists:
            raise NativeArtifactError(EXIT_NOT_FOUND, "board_not_found")
        self._conn = self._kbc.connect(board=board)
        try:
            if self.kb.get_task(self._conn, task_id) is None:
                raise NativeArtifactError(EXIT_NOT_FOUND, "task_not_found")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "_Store":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def conn(self) -> Any:
        return self._conn

    def rows(self) -> list[Any]:
        return list(self.kb.list_attachments(self._conn, self.task_id))

    def row(self, attachment_id: int) -> Any:
        for row in self.rows():
            if row.id == attachment_id and row.task_id == self.task_id:
                return row
        raise NativeArtifactError(EXIT_NOT_FOUND, "attachment_not_found")


def _blob(row: Any) -> bytes:
    try:
        with open(row.stored_path, "rb") as handle:
            return handle.read(MAX_ARTIFACT_BYTES + 1)
    except OSError as exc:
        raise NativeArtifactError(EXIT_FAILED, "attachment_unreadable") from exc


def _record(row: Any, data: bytes) -> dict[str, object]:
    """The native attachment row plus the SHA-256 of the stored blob."""

    return {
        "id": row.id,
        "task_id": row.task_id,
        "filename": row.filename,
        "stored_path": row.stored_path,
        "content_type": row.content_type,
        "size": len(data),
        "uploaded_by": row.uploaded_by,
        "created_at": row.created_at,
        "sha256": _sha256(data),
    }


def read_bounded(stream: BinaryIO, limit: int = MAX_ARTIFACT_BYTES) -> bytes:
    """Read at most ``limit + 1`` bytes so over-cap input is detectable."""

    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = stream.read(min(_CHUNK, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def upload(
    board: str,
    task_id: str,
    *,
    name: str,
    media_type: str,
    size: int,
    sha256: str,
    stream: BinaryIO,
) -> dict[str, object]:
    """Verify then store one attachment; nothing is stored on any failure."""

    if not _SHA256_RE.match(sha256 or "") or size < 0 or not media_type:
        raise NativeArtifactError(EXIT_USAGE, "malformed_upload")
    with _Store(board, task_id) as store:
        data = read_bounded(stream)
        if len(data) > MAX_ARTIFACT_BYTES:
            raise NativeArtifactError(EXIT_TOO_LARGE, "artifact_too_large")
        if size != len(data) or sha256 != _sha256(data):
            raise NativeArtifactError(EXIT_INTEGRITY, "integrity_mismatch")
        try:
            attachment_id = store.kb.store_attachment_bytes(
                store.conn,
                task_id,
                name,
                data,
                content_type=media_type,
                uploaded_by=_actor(),
                board=board,
                max_bytes=MAX_ARTIFACT_BYTES,
            )
        except store.kb.AttachmentTooLarge as exc:
            raise NativeArtifactError(EXIT_TOO_LARGE, "artifact_too_large") from exc
        except ValueError as exc:
            raise NativeArtifactError(EXIT_USAGE, "malformed_upload") from exc
        return _record(store.row(int(attachment_id)), data)


def list_records(board: str, task_id: str) -> list[dict[str, object]]:
    with _Store(board, task_id) as store:
        return [_record(row, data) for row in store.rows() for data in (_blob(row),)]


def read_attachment(board: str, task_id: str, attachment_id: int) -> bytes:
    with _Store(board, task_id) as store:
        return _blob(store.row(attachment_id))


class _Parser(argparse.ArgumentParser):
    """argparse that never prints an argument value; failure is one fixed code."""

    def error(self, message: str) -> "NoReturn":  # type: ignore[override]
        raise NativeArtifactError(EXIT_USAGE, "malformed_invocation")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="native_artifacts", add_help=False)
    sub = parser.add_subparsers(
        dest="verb", required=True, parser_class=_Parser
    )
    up = sub.add_parser("artifact-upload", aliases=["upload"])
    up.add_argument("board")
    up.add_argument("task_id")
    up.add_argument("--name", required=True)
    up.add_argument("--media-type", required=True)
    up.add_argument("--size", required=True, type=int)
    up.add_argument("--sha256", required=True)
    up.add_argument("--json", action="store_true")
    ls = sub.add_parser("artifact-list", aliases=["list"])
    ls.add_argument("board")
    ls.add_argument("task_id")
    ls.add_argument("--json", action="store_true")
    dl = sub.add_parser("artifact-download", aliases=["download"])
    dl.add_argument("board")
    dl.add_argument("task_id")
    dl.add_argument("attachment_id", type=int)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    stderr: Any = None,
) -> int:
    """Run one verb; return its documented exit status."""

    in_stream: BinaryIO = stdin if stdin is not None else sys.stdin.buffer
    out_stream: BinaryIO = stdout if stdout is not None else sys.stdout.buffer
    err_stream: Any = stderr if stderr is not None else sys.stderr
    try:
        args = _parser().parse_args(list(argv) if argv is not None else None)
    except NativeArtifactError as exc:
        err_stream.write(f"native_artifacts: {exc.code}\n")
        return exc.status
    verb = args.verb.removeprefix("artifact-")
    try:
        if verb == "upload":
            payload: Mapping[str, object] | list[dict[str, object]] = upload(
                args.board,
                args.task_id,
                name=args.name,
                media_type=args.media_type,
                size=args.size,
                sha256=args.sha256,
                stream=in_stream,
            )
            out_stream.write(json.dumps(payload).encode("utf-8"))
        elif verb == "list":
            payload = list_records(args.board, args.task_id)
            out_stream.write(json.dumps(payload).encode("utf-8"))
        else:
            out_stream.write(read_attachment(args.board, args.task_id, args.attachment_id))
        out_stream.flush()
        return EXIT_OK
    except NativeArtifactError as exc:
        err_stream.write(f"native_artifacts: {exc.code}\n")
        return exc.status
    except Exception:  # noqa: BLE001 - never echo content or paths
        err_stream.write("native_artifacts: failed\n")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
