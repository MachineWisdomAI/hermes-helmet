#!/usr/bin/env python3
"""Coordinator artifact handoff through native Kanban attachments.

Two isolated fixtures prove the contract without a real Hermes install:

- ``FakeTransport`` implements the ``ArtifactTransport`` protocol in memory and
  can tamper with listings or bytes.
- ``tests/fixtures/artifact_adapter.py`` implements the three ``artifact-*``
  worker-runtime verbs over a temporary directory with native-attachment
  semantics, and is driven through the real subprocess transport and CLI.

Neither touches a real board, Docker, or credentials. Real-host acceptance
differs and is described in docs/task-artifacts.md.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Callable
import unittest
import zipfile
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hermes_helmet import cli, task_artifacts as ta  # noqa: E402

ADAPTER = ROOT / "tests" / "fixtures" / "artifact_adapter.py"
DOC = ROOT / "docs" / "task-artifacts.md"
DOCX_TYPE = ta.OFFICE_MEDIA_TYPES[".docx"]
PPTX_TYPE = ta.OFFICE_MEDIA_TYPES[".pptx"]
SENTINEL = "credential-sentinel-7f3a"
BOARD = "default"


def _zip_bytes(members: list[tuple[str, bytes]]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members:
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 2, 3, 4, 6))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    return stream.getvalue()


# Binary-hostile bytes: NULs, high bytes, CR/LF pairs, and an invalid UTF-8 run.
BINARY_ASSET = bytes(range(256)) * 8 + b"\x00\r\n\xff\xfe\x80"


def make_docx(title: str = "ExampleCo Quarterly Overview") -> bytes:
    return _zip_bytes(
        [
            ("[Content_Types].xml", b"<Types/>"),
            ("word/document.xml", f"<w:document>{title}</w:document>".encode()),
            ("word/styles.xml", b"<w:styles><w:style w:styleId='Heading1'/></w:styles>"),
            ("word/media/logo.png", BINARY_ASSET),
        ]
    )


def make_pptx() -> bytes:
    return _zip_bytes(
        [
            ("[Content_Types].xml", b"<Types/>"),
            ("ppt/presentation.xml", b"<p:sldIdLst><p:sldId id='1'/><p:sldId id='2'/></p:sldIdLst>"),
            ("ppt/slides/slide1.xml", b"<p:sld>ExampleCo kickoff</p:sld>"),
            ("ppt/slides/slide2.xml", b"<p:sld>ExampleCo roadmap</p:sld>"),
            ("ppt/notesSlides/notesSlide1.xml", b"<p:notes>Speaker notes</p:notes>"),
            ("ppt/media/image1.png", BINARY_ASSET),
        ]
    )


class FakeTransport:
    """In-memory ``ArtifactTransport`` with native-like rows and tamper hooks."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], list[dict]] = {}
        self.blobs: dict[int, bytes] = {}
        self.next_id = 1
        self.calls: list[str] = []
        self.listing_hook: Callable[[list[dict]], list] | None = None
        self.download_hook: Callable[[bytes], object] | None = None

    def task(self, board: str, task_id: str) -> None:
        self.rows.setdefault((board, task_id), [])

    def upload(self, board, task_id, *, name, media_type, data, sha256):  # type: ignore[no-untyped-def]
        self.calls.append("upload")
        rows = self.rows[(board, task_id)]
        stored = name
        count = 1
        while any(row["filename"] == stored for row in rows):
            stem, dot, ext = name.partition(".")
            stored = f"{stem} ({count}){dot}{ext}"
            count += 1
        row = {
            "id": self.next_id,
            "task_id": task_id,
            "filename": stored,
            "stored_path": f"/worker-private/attachments/{task_id}/{stored}",
            "content_type": media_type,
            "size": len(data),
            "uploaded_by": "coordinator",
            "created_at": 1_700_000_000 + self.next_id,
        }
        self.blobs[self.next_id] = bytes(data)
        self.next_id += 1
        rows.append(row)
        return {**row, "sha256": hashlib.sha256(data).hexdigest()}

    def list_attachments(self, board, task_id):  # type: ignore[no-untyped-def]
        self.calls.append("list")
        if (board, task_id) not in self.rows:
            raise ta.TaskArtifactError("artifact_not_found")
        out = [
            {**row, "sha256": hashlib.sha256(self.blobs[row["id"]]).hexdigest()}
            for row in self.rows[(board, task_id)]
        ]
        return self.listing_hook(out) if self.listing_hook else out

    def download(self, board, task_id, attachment_id):  # type: ignore[no-untyped-def]
        self.calls.append("download")
        if not any(row["id"] == attachment_id for row in self.rows.get((board, task_id), [])):
            raise ta.TaskArtifactError("artifact_not_found")
        data = self.blobs[attachment_id]
        return self.download_hook(data) if self.download_hook else data


class InMemoryHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = FakeTransport()
        self.transport.task(BOARD, "t_input")
        self.transport.task(BOARD, "t_other")
        self.transport.task("review", "t_input")

    def upload(self, name: str, data: bytes, task: str = "t_input", **kwargs):  # type: ignore[no-untyped-def]
        return ta.upload_artifact_bytes(
            self.transport, board=BOARD, task_id=task, name=name, data=data, **kwargs
        )

    def test_docx_and_pptx_round_trip_byte_identical_with_format_metadata(self) -> None:
        docx, pptx = make_docx(), make_pptx()
        doc_ref = self.upload("brief.docx", docx)
        deck_ref = self.upload("deck.pptx", pptx)

        self.assertEqual((doc_ref.media_type, deck_ref.media_type), (DOCX_TYPE, PPTX_TYPE))
        self.assertEqual((doc_ref.size, deck_ref.size), (len(docx), len(pptx)))
        self.assertEqual(doc_ref.sha256, hashlib.sha256(docx).hexdigest())

        listed = ta.list_artifacts(self.transport, board=BOARD, task_id="t_input")
        self.assertEqual([r.filename for r in listed], ["brief.docx", "deck.pptx"])
        self.assertEqual(listed, [doc_ref, deck_ref])
        for ref in listed:
            self.assertEqual((ref.board, ref.task_id), (BOARD, "t_input"))
            self.assertEqual(ref.role, ta.ROLE_ORIGINAL)

        got_docx = ta.fetch_artifact(self.transport, doc_ref.token, board=BOARD, task_id="t_input")
        got_pptx = ta.fetch_artifact(self.transport, deck_ref)
        self.assertEqual(got_docx.data, docx)
        self.assertEqual(got_pptx.data, pptx)
        # Editable structure survives: members, slide order, notes, assets.
        archive = zipfile.ZipFile(io.BytesIO(got_pptx.data))
        self.assertIsNone(archive.testzip())
        self.assertEqual(archive.namelist(), zipfile.ZipFile(io.BytesIO(pptx)).namelist())
        self.assertEqual(archive.read("ppt/media/image1.png"), BINARY_ASSET)
        self.assertIn(b"Speaker notes", archive.read("ppt/notesSlides/notesSlide1.xml"))

    def test_reference_is_identity_and_revision_without_worker_path(self) -> None:
        ref = self.upload("brief.docx", make_docx())
        self.assertEqual(
            ref.token,
            f"kanban-artifact:v1:{BOARD}:t_input:{ref.attachment_id}:{ref.sha256}",
        )
        parsed = ta.parse_artifact_reference(ref.token)
        self.assertEqual(
            (parsed.board, parsed.task_id, parsed.attachment_id, parsed.sha256),
            (BOARD, "t_input", ref.attachment_id, ref.sha256),
        )
        public = json.dumps(ref.to_public_dict(), sort_keys=True)
        self.assertNotIn("stored_path", public)
        self.assertNotIn("worker-private", public)

    def test_revisions_are_new_artifacts_and_prior_revisions_stay_retrievable(self) -> None:
        first = self.upload("brief.docx", make_docx("first"))
        second = self.upload("brief.docx", make_docx("second"))
        self.assertNotEqual(first.attachment_id, second.attachment_id)
        self.assertNotEqual(first.sha256, second.sha256)
        self.assertEqual(second.filename, "brief (1).docx")
        self.assertEqual(ta.fetch_artifact(self.transport, first).data, make_docx("first"))
        self.assertEqual(ta.fetch_artifact(self.transport, second).data, make_docx("second"))

    def test_changed_attachment_is_not_accepted_as_the_named_revision(self) -> None:
        ref = self.upload("brief.docx", make_docx("examined"))
        self.transport.blobs[ref.attachment_id] = make_docx("silently replaced")
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            ta.fetch_artifact(self.transport, ref)
        self.assertEqual(ctx.exception.code, "artifact_revision_mismatch")

    def test_preview_and_extracted_text_stay_separate_from_the_original(self) -> None:
        docx = make_docx()
        original = self.upload("brief.docx", docx)
        preview = self.upload(
            "render.png", BINARY_ASSET, role=ta.ROLE_PREVIEW, original=original
        )
        text = self.upload(
            "ignored.txt", b"ExampleCo Quarterly Overview", role=ta.ROLE_EXTRACTED_TEXT,
            original=original.token,
        )
        self.assertEqual(preview.filename, "brief.docx.preview.png")
        self.assertEqual(preview.media_type, "image/png")
        self.assertEqual(text.filename, "brief.docx.extracted.txt")
        self.assertEqual(text.media_type, "text/plain")
        self.assertEqual((preview.role, text.role), (ta.ROLE_PREVIEW, ta.ROLE_EXTRACTED_TEXT))
        self.assertEqual((preview.companion_of, text.companion_of), ("brief.docx",) * 2)
        originals = ta.list_artifacts(
            self.transport, board=BOARD, task_id="t_input", role=ta.ROLE_ORIGINAL
        )
        self.assertEqual([r.filename for r in originals], ["brief.docx"])
        self.assertEqual(originals[0].sha256, original.sha256)
        self.assertEqual(ta.fetch_artifact(self.transport, original).data, docx)

    def test_companion_misuse_is_rejected(self) -> None:
        original = self.upload("brief.docx", make_docx())
        foreign = ta.upload_artifact_bytes(
            self.transport, board=BOARD, task_id="t_other", name="x.docx", data=make_docx("x")
        )
        cases = {
            "artifact_original_required": dict(role=ta.ROLE_PREVIEW),
            "artifact_original_invalid": dict(role=ta.ROLE_PREVIEW, original=foreign),
            "artifact_role_invalid": dict(original=original),
            "artifact_name_reserved": None,
        }
        for code, kwargs in cases.items():
            with self.subTest(code=code), self.assertRaises(ta.TaskArtifactError) as ctx:
                if kwargs is None:
                    self.upload("brief.docx.preview.png", b"x")
                else:
                    self.upload("p.png", b"x", **kwargs)
            self.assertEqual(ctx.exception.code, code)
        stale = ta.ArtifactRef(
            BOARD, "t_input", original.attachment_id, "brief.docx", DOCX_TYPE,
            original.size, "0" * 64,
        )
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            self.upload("p.png", b"x", role=ta.ROLE_PREVIEW, original=stale)
        self.assertEqual(ctx.exception.code, "artifact_original_invalid")
        self.upload("p.png", b"x", role=ta.ROLE_PREVIEW, original=original)
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            self.upload("p.png", b"y", role=ta.ROLE_PREVIEW, original=original)
        self.assertEqual(ctx.exception.code, "artifact_companion_exists")
        # A companion cannot itself be an original for another companion.
        companion = ta.list_artifacts(self.transport, board=BOARD, task_id="t_input")[-1]
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            self.upload("q.png", b"z", role=ta.ROLE_PREVIEW, original=companion)
        self.assertEqual(ctx.exception.code, "artifact_original_invalid")

    def test_reference_must_be_a_task_owned_reference(self) -> None:
        digest = "a" * 64
        bad = [
            "/opt/data/kanban/attachments/t_input/brief.docx",
            "../../etc/passwd",
            "C:\\Users\\someone\\brief.docx",
            "file:///etc/passwd",
            "https://example.com/brief.docx",
            "brief.docx",
            "42",
            "",
            f"kanban-artifact:v2:{BOARD}:t_input:1:{digest}",
            f"kanban-artifact:v1:{BOARD}:t_input:0:{digest}",
            f"kanban-artifact:v1:{BOARD}:t_input:-1:{digest}",
            f"kanban-artifact:v1:{BOARD}:t_input:1:{'A' * 64}",
            f"kanban-artifact:v1:{BOARD}:t_input:1:{digest[:-1]}",
            f"kanban-artifact:v1:{BOARD}:t_input/../x:1:{digest}",
            f"kanban-artifact:v1:{BOARD}:t_input:1:{digest}\nextra",
            f"kanban-artifact:v1:{BOARD}:t_input:1:{digest}:/etc/passwd",
            None,
            42,
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ta.TaskArtifactError) as ctx:
                ta.parse_artifact_reference(value)  # type: ignore[arg-type]
            self.assertEqual(ctx.exception.code, "artifact_ref_malformed")
            self.assertNotIn("passwd", str(ctx.exception))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.docx"
            with self.assertRaises(ta.TaskArtifactError):
                ta.save_artifact(self.transport, "/etc/passwd", target)
            self.assertFalse(target.exists())
        self.assertEqual(self.transport.calls, [])

    def test_cross_task_and_cross_board_references_fail(self) -> None:
        ref = self.upload("brief.docx", make_docx())
        for kwargs, code in (
            ({"task_id": "t_other"}, "artifact_task_mismatch"),
            ({"board": "review"}, "artifact_board_mismatch"),
        ):
            with self.subTest(**kwargs), self.assertRaises(ta.TaskArtifactError) as ctx:
                ta.fetch_artifact(self.transport, ref, **kwargs)
            self.assertEqual(ctx.exception.code, code)
        # The same attachment id minted for a different task is not owned there.
        forged = ta.ArtifactRef(
            BOARD, "t_other", ref.attachment_id, ref.filename, ref.media_type, ref.size, ref.sha256
        )
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            ta.fetch_artifact(self.transport, forged)
        self.assertEqual(ctx.exception.code, "artifact_not_found")
        self.assertNotIn("download", self.transport.calls)

    def test_foreign_rows_in_a_listing_are_rejected(self) -> None:
        self.upload("brief.docx", make_docx())

        def foreign(rows):  # type: ignore[no-untyped-def]
            return [{**row, "task_id": "t_other"} for row in rows]

        self.transport.listing_hook = foreign
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            ta.list_artifacts(self.transport, board=BOARD, task_id="t_input")
        self.assertEqual(ctx.exception.code, "artifact_task_mismatch")

    def test_missing_unlisted_and_unknown_tasks_fail_usefully(self) -> None:
        ref = self.upload("brief.docx", make_docx())
        missing = ta.ArtifactRef(
            BOARD, "t_input", ref.attachment_id + 99, "x.docx", DOCX_TYPE, 1, ref.sha256
        )
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            ta.fetch_artifact(self.transport, missing)
        self.assertEqual(ctx.exception.code, "artifact_not_found")
        self.assertIn("list the task's artifacts", str(ctx.exception))
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            ta.list_artifacts(self.transport, board=BOARD, task_id="t_unknown")
        self.assertEqual(ctx.exception.code, "artifact_not_found")

    def test_malformed_adapter_records_are_rejected(self) -> None:
        self.upload("brief.docx", make_docx())
        good = self.transport.list_attachments(BOARD, "t_input")[0]
        variants = {
            "artifact_transport_invalid": [
                {**good, "id": "1"},
                {**good, "id": True},
                {**good, "id": 0},
                {**good, "filename": "../brief.docx"},
                {**good, "filename": ".hidden.docx"},
                {**good, "size": -1},
                {**good, "size": "9"},
                {**good, "created_at": "now"},
                "not-a-record",
            ],
            "artifact_checksum_missing": [
                {k: v for k, v in good.items() if k != "sha256"},
                {**good, "sha256": "short"},
                {**good, "sha256": None},
            ],
            "artifact_too_large": [{**good, "size": ta.MAX_ARTIFACT_BYTES + 1}],
        }
        for code, records in variants.items():
            for record in records:
                with self.subTest(code=code, record=record):
                    self.transport.listing_hook = lambda _rows, r=record: [r]
                    with self.assertRaises(ta.TaskArtifactError) as ctx:
                        ta.list_artifacts(self.transport, board=BOARD, task_id="t_input")
                    self.assertEqual(ctx.exception.code, code)
        self.transport.listing_hook = lambda rows: rows + rows
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            ta.list_artifacts(self.transport, board=BOARD, task_id="t_input")
        self.assertEqual(ctx.exception.code, "artifact_transport_invalid")

    def test_corrupted_or_truncated_download_is_never_returned_or_saved(self) -> None:
        ref = self.upload("brief.docx", make_docx())
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "brief.docx"
            for hook in (
                lambda data: data[:-1],
                lambda data: data[:10] + bytes([data[10] ^ 0xFF]) + data[11:],
                lambda data: data + b"x",
                lambda data: "text",
            ):
                self.transport.download_hook = hook
                with self.assertRaises(ta.TaskArtifactError) as ctx:
                    ta.save_artifact(self.transport, ref, target)
                self.assertIn(
                    ctx.exception.code, {"artifact_integrity_failed", "artifact_transport_invalid"}
                )
                self.assertEqual(os.listdir(tmp), [])

    def test_oversized_inputs_never_reach_the_transport(self) -> None:
        cap = ta.MAX_ARTIFACT_BYTES
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            self.upload("big.docx", b"\x00" * (cap + 1))
        self.assertEqual(ctx.exception.code, "artifact_too_large")
        self.assertIn("25 MiB", str(ctx.exception))
        with tempfile.TemporaryDirectory() as tmp:
            sparse = Path(tmp) / "big.pptx"
            with sparse.open("wb") as stream:
                stream.truncate(cap + 1)
            with self.assertRaises(ta.TaskArtifactError) as ctx:
                ta.upload_artifact(self.transport, board=BOARD, task_id="t_input", path=sparse)
            self.assertEqual(ctx.exception.code, "artifact_too_large")
        self.assertEqual(self.transport.calls, [])
        exact = self.upload("exact.docx", b"\x01" * cap)
        self.assertEqual(exact.size, cap)

    def test_local_sources_must_be_plain_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real.docx"
            real.write_bytes(make_docx())
            link = Path(tmp) / "link.docx"
            link.symlink_to(real)
            fifo = Path(tmp) / "pipe.docx"
            os.mkfifo(fifo)
            sources = [
                Path(tmp) / "absent.docx",
                Path(tmp),
                link,
                fifo,
                "https://example.com/brief.docx",
                "file:///etc/passwd",
                "s3://bucket/brief.docx",
            ]
            for source in sources:
                with self.subTest(source=str(source)), self.assertRaises(
                    ta.TaskArtifactError
                ) as ctx:
                    ta.upload_artifact(
                        self.transport, board=BOARD, task_id="t_input", path=source
                    )
                self.assertEqual(ctx.exception.code, "artifact_source_invalid")
            ref = ta.upload_artifact(
                self.transport, board=BOARD, task_id="t_input", path=real, name="brief.docx"
            )
            self.assertEqual(ref.media_type, DOCX_TYPE)
        self.assertEqual(self.transport.calls, ["upload"])

    def test_invalid_names_media_types_and_identifiers_fail_before_transfer(self) -> None:
        bad_names = ["", " ", ".hidden", "../x.docx", "a/b.docx", "a\\b.docx", "a\x00b", "x" * 201, 5]
        for name in bad_names:
            with self.subTest(name=name), self.assertRaises(ta.TaskArtifactError) as ctx:
                self.upload(name, b"x")  # type: ignore[arg-type]
            self.assertEqual(ctx.exception.code, "artifact_name_invalid")
        for media in ("docx", "text/", "a b/c", "text/plain\nX: y"):
            with self.subTest(media=media), self.assertRaises(ta.TaskArtifactError) as ctx:
                self.upload("a.docx", b"x", media_type=media)
            self.assertEqual(ctx.exception.code, "artifact_media_type_invalid")
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            self.upload("a.docx", b"x", role="draft")
        self.assertEqual(ctx.exception.code, "artifact_role_invalid")
        for board, task in (("../x", "t_input"), ("Default", "t_input"), (BOARD, "t/../x"), (BOARD, "")):
            with self.subTest(board=board, task=task), self.assertRaises(ta.TaskArtifactError):
                ta.list_artifacts(self.transport, board=board, task_id=task)
        self.assertEqual(self.transport.calls, [])

    def test_adapter_that_alters_metadata_is_detected(self) -> None:
        real_upload = self.transport.upload

        def lying(board, task_id, **kwargs):  # type: ignore[no-untyped-def]
            record = dict(real_upload(board, task_id, **kwargs))
            record["content_type"] = "application/octet-stream"
            return record

        self.transport.upload = lying  # type: ignore[method-assign]
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            self.upload("brief.docx", make_docx())
        self.assertEqual(ctx.exception.code, "artifact_metadata_mismatch")

    def test_save_refuses_unsafe_destinations_and_never_overwrites_by_default(self) -> None:
        ref = self.upload("brief.docx", make_docx())
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "existing.docx"
            existing.write_bytes(b"keep me")
            with self.assertRaises(ta.TaskArtifactError) as ctx:
                ta.save_artifact(self.transport, ref, existing)
            self.assertEqual(ctx.exception.code, "artifact_destination_exists")
            self.assertEqual(existing.read_bytes(), b"keep me")
            victim = Path(tmp) / "victim.docx"
            victim.write_bytes(b"victim")
            link = Path(tmp) / "link.docx"
            link.symlink_to(victim)
            for destination in (link, Path(tmp) / "missing-dir" / "x.docx", Path(tmp)):
                with self.subTest(destination=str(destination)), self.assertRaises(
                    ta.TaskArtifactError
                ) as ctx:
                    ta.save_artifact(self.transport, ref, destination, overwrite=True)
                self.assertEqual(ctx.exception.code, "artifact_destination_invalid")
            self.assertEqual(victim.read_bytes(), b"victim")
            saved = ta.save_artifact(self.transport, ref, existing, overwrite=True)
            self.assertEqual(existing.read_bytes(), make_docx())
            self.assertEqual(saved, ref)
            self.assertEqual(
                sorted(os.listdir(tmp)), ["existing.docx", "link.docx", "victim.docx"]
            )

    def test_media_type_does_not_depend_on_the_host_mimetype_table(self) -> None:
        with mock.patch.object(ta.mimetypes, "guess_type", return_value=(None, None)):
            self.assertEqual(ta.media_type_for("a.DOCX"), DOCX_TYPE)
            self.assertEqual(ta.media_type_for("a.pptx"), PPTX_TYPE)
            self.assertEqual(ta.media_type_for("a.unknown"), ta.DEFAULT_MEDIA_TYPE)


class RecordingExecutor:
    def __init__(self, inner=None) -> None:  # type: ignore[no-untyped-def]
        self.inner = inner or ta.run_bounded
        self.calls: list[tuple[list[str], bytes | None]] = []

    def __call__(self, argv, stdin_data, max_stdout, timeout):  # type: ignore[no-untyped-def]
        self.calls.append((list(argv), stdin_data))
        return self.inner(argv, stdin_data, max_stdout, timeout)


class AdapterContractTests(unittest.TestCase):
    """The real subprocess transport and CLI against the isolated native fixture."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "store"
        for board, task in ((BOARD, "t_input"), (BOARD, "t_other"), ("review", "t_input")):
            (self.root / board / task).mkdir(parents=True)
        self.work = Path(self._tmp.name) / "coordinator"
        self.work.mkdir()
        patcher = mock.patch.dict(os.environ, {"ARTIFACT_FIXTURE_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("ARTIFACT_FIXTURE_MODE", None)
        self.runtime = (sys.executable, str(ADAPTER))
        self.executor = RecordingExecutor()
        self.transport = ta.WorkerRuntimeTransport(self.runtime, executor=self.executor)

    def cli(self, *argv: str) -> tuple[int, str, str]:
        arguments = list(argv)
        if "--worker-runtime" not in arguments:
            arguments += ["--worker-runtime", " ".join(self.runtime)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(arguments)
        return code, out.getvalue(), err.getvalue()

    def test_docx_and_pptx_cross_the_process_boundary_byte_for_byte(self) -> None:
        docx, pptx = make_docx(), make_pptx()
        with self.assertNoLogs():
            doc_ref = ta.upload_artifact_bytes(
                self.transport, board=BOARD, task_id="t_input", name="brief.docx", data=docx
            )
            deck_ref = ta.upload_artifact_bytes(
                self.transport, board=BOARD, task_id="t_input", name="deck.pptx", data=pptx
            )
            listed = ta.list_artifacts(self.transport, board=BOARD, task_id="t_input")
        self.assertEqual(listed, [doc_ref, deck_ref])
        self.assertEqual((doc_ref.media_type, deck_ref.media_type), (DOCX_TYPE, PPTX_TYPE))
        self.assertEqual(ta.fetch_artifact(self.transport, doc_ref).data, docx)
        self.assertEqual(ta.fetch_artifact(self.transport, deck_ref).data, pptx)
        self.assertEqual(ta.list_artifacts(self.transport, board=BOARD, task_id="t_other"), [])
        self.assertEqual(
            ta.list_artifacts(self.transport, board="review", task_id="t_input"), []
        )
        # Ids are global in the native store, so a bare id is not ownership.
        other = ta.upload_artifact_bytes(
            self.transport, board=BOARD, task_id="t_other", name="brief.docx", data=make_docx("o")
        )
        self.assertNotEqual(other.attachment_id, doc_ref.attachment_id)
        forged = ta.ArtifactRef(
            BOARD, "t_other", doc_ref.attachment_id, "brief.docx", DOCX_TYPE,
            doc_ref.size, doc_ref.sha256,
        )
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            ta.fetch_artifact(self.transport, forged)
        self.assertEqual(ctx.exception.code, "artifact_not_found")

    def test_bytes_travel_on_stdin_and_stdout_never_in_arguments(self) -> None:
        docx = make_docx(SENTINEL)
        ref = ta.upload_artifact_bytes(
            self.transport, board=BOARD, task_id="t_input", name="brief.docx", data=docx
        )
        ta.fetch_artifact(self.transport, ref)
        verbs = []
        for argv, stdin_data in self.executor.calls:
            verb = argv[len(self.runtime)]
            verbs.append(verb)
            joined = "\x00".join(argv).encode("utf-8", "surrogateescape")
            self.assertNotIn(SENTINEL.encode(), joined)
            self.assertNotIn(docx[:32], joined)
            self.assertNotIn(b"base64", joined)
            self.assertTrue(all(len(part) < 512 for part in argv))
            if verb == "artifact-upload":
                self.assertEqual(stdin_data, docx)
            else:
                self.assertIsNone(stdin_data)
        # upload, then fetch = verify listing + download.
        self.assertEqual(verbs, ["artifact-upload", "artifact-list", "artifact-download"])

    def test_exact_cap_round_trips_and_one_byte_more_is_refused_by_the_adapter(self) -> None:
        cap = ta.MAX_ARTIFACT_BYTES
        data = (b"ExampleCo\x00\xff" * (cap // 11 + 1))[:cap]
        ref = ta.upload_artifact_bytes(
            self.transport, board=BOARD, task_id="t_input", name="exact.pptx", data=data
        )
        self.assertEqual(ref.size, cap)
        self.assertEqual(ta.fetch_artifact(self.transport, ref).data, data)
        # An adapter enforcing the runtime cap reports exit 4 even if the
        # coordinator's own pre-check were bypassed.
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            self.transport.upload(
                BOARD, "t_input", name="over.pptx", media_type=PPTX_TYPE,
                data=b"\x00" * (cap + 1), sha256="0" * 64,
            )
        self.assertEqual(ctx.exception.code, "artifact_too_large")

    def test_adapter_failures_map_to_fixed_errors_without_leaking_output(self) -> None:
        ref = ta.upload_artifact_bytes(
            self.transport, board=BOARD, task_id="t_input", name="brief.docx", data=make_docx()
        )
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            ta.list_artifacts(self.transport, board=BOARD, task_id="t_absent")
        self.assertEqual(ctx.exception.code, "artifact_not_found")
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            self.transport.upload(
                BOARD, "t_input", name="x.docx", media_type=DOCX_TYPE, data=b"abc",
                sha256="0" * 64,
            )
        self.assertEqual(ctx.exception.code, "artifact_integrity_failed")
        with mock.patch.dict(os.environ, {"ARTIFACT_FIXTURE_MODE": "fail"}):
            for call in (
                lambda: ta.list_artifacts(self.transport, board=BOARD, task_id="t_input"),
                lambda: ta.fetch_artifact(self.transport, ref),
                lambda: ta.upload_artifact_bytes(
                    self.transport, board=BOARD, task_id="t_input", name="n.docx", data=b"1"
                ),
            ):
                with self.assertRaises(ta.TaskArtifactError) as ctx:
                    call()
                self.assertEqual(ctx.exception.code, "artifact_transport_failed")
                self.assertNotIn(SENTINEL, str(ctx.exception))
                self.assertNotIn(SENTINEL, repr(ctx.exception))
        with mock.patch.dict(os.environ, {"ARTIFACT_FIXTURE_MODE": "list-other-task"}):
            with self.assertRaises(ta.TaskArtifactError) as ctx:
                ta.list_artifacts(self.transport, board=BOARD, task_id="t_input")
            self.assertEqual(ctx.exception.code, "artifact_task_mismatch")
        with mock.patch.dict(os.environ, {"ARTIFACT_FIXTURE_MODE": "list-no-checksum"}):
            with self.assertRaises(ta.TaskArtifactError) as ctx:
                ta.fetch_artifact(self.transport, ref)
            self.assertEqual(ctx.exception.code, "artifact_checksum_missing")

    def test_unavailable_slow_and_runaway_transports_fail_closed(self) -> None:
        missing = ta.WorkerRuntimeTransport(("/nonexistent/helmet-artifact-adapter",))
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            ta.list_artifacts(missing, board=BOARD, task_id="t_input")
        self.assertEqual(ctx.exception.code, "artifact_transport_unavailable")
        # Interpreters differ: some raise OSError for a missing adapter, others
        # surface a launcher status. Both are "unavailable", never "failed".
        for status in (126, 127):
            launcher = ta.WorkerRuntimeTransport(
                self.runtime,
                executor=lambda *_args, s=status: ta.ExecResult(s, b""),
            )
            with self.assertRaises(ta.TaskArtifactError) as ctx:
                launcher.list_attachments(BOARD, "t_input")
            self.assertEqual(ctx.exception.code, "artifact_transport_unavailable")

        def slow(argv, stdin_data, max_stdout, timeout):  # type: ignore[no-untyped-def]
            return ta.run_bounded(
                [sys.executable, str(ADAPTER), "sleep", "10"], stdin_data, max_stdout, timeout
            )

        sluggish = ta.WorkerRuntimeTransport(self.runtime, timeout=0.3, executor=slow)
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            ta.list_artifacts(sluggish, board=BOARD, task_id="t_input")
        self.assertEqual(ctx.exception.code, "artifact_transport_timeout")

        def runaway(argv, stdin_data, max_stdout, timeout):  # type: ignore[no-untyped-def]
            return ta.run_bounded(
                [sys.executable, str(ADAPTER), "emit", str(max_stdout + 1)],
                stdin_data, max_stdout, timeout,
            )

        flood = ta.WorkerRuntimeTransport(self.runtime, executor=runaway)
        for call, code in (
            (lambda: flood.list_attachments(BOARD, "t_input"), "artifact_transport_invalid"),
            (lambda: flood.download(BOARD, "t_input", 1), "artifact_too_large"),
        ):
            with self.assertRaises(ta.TaskArtifactError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, code)

        def garbage(argv, stdin_data, max_stdout, timeout):  # type: ignore[no-untyped-def]
            return ta.ExecResult(0, b"\xff\xfenot json")

        broken = ta.WorkerRuntimeTransport(self.runtime, executor=garbage)
        with self.assertRaises(ta.TaskArtifactError) as ctx:
            broken.list_attachments(BOARD, "t_input")
        self.assertEqual(ctx.exception.code, "artifact_transport_invalid")

    def test_runtime_must_be_configured(self) -> None:
        with mock.patch.object(ta, "DEFAULT_WORKER_RUNTIME", ()):
            with self.assertRaises(ta.TaskArtifactError) as ctx:
                ta.WorkerRuntimeTransport()
            self.assertEqual(ctx.exception.code, "worker_runtime_unset")
        with mock.patch.object(cli, "DEFAULT_WORKER_RUNTIME", ()), mock.patch.object(
            ta, "DEFAULT_WORKER_RUNTIME", ()
        ):
            code, out, err = self.cli("artifact", "list", BOARD, "t_input", "--worker-runtime", "")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("HERMES_HELMET_WORKER_RUNTIME", err)

    def test_cli_round_trip_never_prints_bytes_paths_or_worker_locations(self) -> None:
        docx = make_docx(SENTINEL)
        source = self.work / "brief.docx"
        source.write_bytes(docx)
        code, out, err = self.cli("artifact", "upload", BOARD, "t_input", str(source))
        self.assertEqual((code, err), (0, ""))
        uploaded = json.loads(out)
        self.assertEqual(uploaded["media_type"], DOCX_TYPE)
        self.assertEqual(uploaded["sha256"], hashlib.sha256(docx).hexdigest())

        code, out, err = self.cli("artifact", "list", BOARD, "t_input")
        self.assertEqual((code, err), (0, ""))
        listing = json.loads(out)
        self.assertEqual([item["reference"] for item in listing], [uploaded["reference"]])

        destination = self.work / "retrieved.docx"
        code, out, err = self.cli(
            "artifact", "download", uploaded["reference"], str(destination),
            "--board", BOARD, "--task-id", "t_input",
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(destination.read_bytes(), docx)
        for text in (out, err):
            self.assertNotIn(SENTINEL, text)
            self.assertNotIn(str(self.root), text)
            self.assertNotIn("stored_path", text)

    def test_cli_failures_exit_nonzero_with_actionable_messages(self) -> None:
        source = self.work / "brief.docx"
        source.write_bytes(make_docx())
        _, out, _ = self.cli("artifact", "upload", BOARD, "t_input", str(source))
        reference = json.loads(out)["reference"]
        target = self.work / "out.docx"
        cases = [
            (("artifact", "download", str(source), str(target)), "artifact_ref_malformed"),
            (("artifact", "download", reference, str(target), "--task-id", "t_other"), "artifact_task_mismatch"),
            (("artifact", "download", reference, str(target), "--board", "review"), "artifact_board_mismatch"),
            (("artifact", "upload", BOARD, "t_input", str(self.work / "absent.docx")), "artifact_source_invalid"),
            (("artifact", "upload", BOARD, "t_input", "https://example.com/a.docx"), "artifact_source_invalid"),
            (("artifact", "upload", BOARD, "t_absent", str(source)), "artifact_not_found"),
            (("artifact", "list", BOARD, "t_absent"), "artifact_not_found"),
        ]
        for argv, code in cases:
            with self.subTest(argv=argv[:2], code=code):
                status, stdout, stderr = self.cli(*argv)
                self.assertEqual(status, 1)
                self.assertEqual(stdout, "")
                self.assertIn(code, stderr)
        self.assertFalse(target.exists())

    def test_cli_companion_upload_keeps_the_original_intact(self) -> None:
        docx = make_docx()
        source = self.work / "brief.docx"
        source.write_bytes(docx)
        text = self.work / "brief.txt"
        text.write_text("ExampleCo Quarterly Overview", encoding="utf-8")
        _, out, _ = self.cli("artifact", "upload", BOARD, "t_input", str(source))
        original = json.loads(out)
        code, out, err = self.cli(
            "artifact", "upload", BOARD, "t_input", str(text),
            "--role", "extracted-text", "--original", original["reference"],
        )
        self.assertEqual((code, err), (0, ""))
        companion = json.loads(out)
        self.assertEqual(companion["filename"], "brief.docx.extracted.txt")
        self.assertEqual(companion["companion_of"], "brief.docx")
        _, out, _ = self.cli("artifact", "list", BOARD, "t_input", "--role", "original")
        self.assertEqual([i["reference"] for i in json.loads(out)], [original["reference"]])
        self.assertEqual(
            ta.fetch_artifact(self.transport, original["reference"]).data, docx
        )


class DocumentationTests(unittest.TestCase):
    def test_contract_document_states_the_narrow_adapter_surface(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        for needle in (
            "artifact-upload",
            "artifact-list",
            "artifact-download",
            "kanban-artifact:v1:",
            "25 MiB",
            "sha256",
            "Real-host acceptance",
            "stored_path",
            "ExampleCo",
        ):
            self.assertIn(needle, text)
        for code in ta._HINTS:  # every documented error code is published
            self.assertIn(code, text, code)
        for forbidden in ("base64 in", "second board"):
            self.assertNotIn(forbidden + " is supported", text)

    def test_contract_is_discoverable_and_verified(self) -> None:
        verify = (ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8")
        self.assertIn("docs/task-artifacts.md", verify)
        self.assertIn("src/hermes_helmet/task_artifacts.py", verify)
        for name in ("README.md", "llms.txt", "docs/helmet-issue.md"):
            self.assertIn(
                "task-artifacts.md", (ROOT / name).read_text(encoding="utf-8"), name
            )


if __name__ == "__main__":
    unittest.main()
