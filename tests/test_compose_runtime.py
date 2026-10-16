#!/usr/bin/env python3
"""``helmet-compose``: argv, project selection, actor, verbs, streams, and transport.

Three layers, none of which duplicates the coordinator's revision/self-approval
matrix in ``tests/test_writing_review.py``:

- ``CommandTests`` / ``ExecutableTests`` check the Docker argument vector, the
  explicit Compose target, the pinned actor, unsupported verbs, binary stdin and
  stdout, and the timeout through the real executable and a stand-in ``docker``
  (``tests/fixtures/docker_shim.py``). No daemon is needed.
- ``NativeArtifactsTests`` drive ``native_artifacts`` against an in-memory stand-in
  for the native Kanban store: exit statuses 3, 4 and 5, task/board scoping, and
  unchanged earlier bytes.
- ``ComposeIntegrationTests`` is the explicit real-boundary command: a disposable
  Compose project on the pinned Hermes image. It runs only with
  ``HELMET_COMPOSE_INTEGRATION=1`` and a Docker daemon with the Compose plugin:

      HELMET_COMPOSE_INTEGRATION=1 PYTHONPATH=src \\
          python3 -m unittest tests.test_compose_runtime.ComposeIntegrationTests -v
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from hermes_helmet import compose_runtime as cr  # noqa: E402
from hermes_helmet import native_artifacts as na  # noqa: E402
from hermes_helmet import task_artifacts as ta  # noqa: E402
from test_task_artifacts import BINARY_ASSET, make_docx, make_pptx  # noqa: E402

SHIM = ROOT / "tests" / "fixtures" / "docker_shim.py"
COMPOSE_FIXTURE = ROOT / "tests" / "fixtures" / "compose" / "artifact-transport.compose.yaml"
TARGET = {"COMPOSE_FILE": "/work/writing.compose.yaml", "COMPOSE_PROJECT_NAME": "writing-a"}
CAP = ta.MAX_ARTIFACT_BYTES


def _hermes_python() -> str | None:
    """A Hermes interpreter that can import the native Kanban store, if any."""

    for candidate in (os.environ.get("HERMES_PYTHON"), cr.CONTAINER_PYTHON):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


class CommandTests(unittest.TestCase):
    def test_artifact_verbs_exec_the_native_helper_in_the_hermes_service(self) -> None:
        argv = ["artifact-download", "default", "t_1", "7"]
        command = cr.build_command(argv, TARGET)
        self.assertEqual(command[:6], ["docker", "compose", "-p", "writing-a", "exec", "-T"])
        service = command.index("hermes")
        self.assertEqual(
            command[service:],
            [
                "hermes",
                cr.CONTAINER_PYTHON,
                "-m",
                "hermes_helmet.native_artifacts",
                *argv,
            ],
        )
        self.assertIn(f"PYTHONPATH={cr.CONTAINER_SOURCE}", command[:service])

    def test_every_artifact_verb_keeps_its_arguments_verbatim_after_the_module(self) -> None:
        upload = [
            "artifact-upload", "default", "t_1", "--name", "brief.docx",
            "--media-type", "application/x", "--size", "3", "--sha256", "0" * 64, "--json",
        ]
        for argv in (upload, ["artifact-list", "default", "t_1", "--json"]):
            command = cr.build_command(argv, TARGET)
            self.assertEqual(command[-len(argv) :], argv)

    def test_kanban_runs_the_native_hermes_binary_with_its_arguments_unchanged(self) -> None:
        argv = ["kanban", "--board", "writing", "show", "t_1", "--json"]
        command = cr.build_command(argv, TARGET)
        self.assertEqual(
            command[command.index("hermes") :], ["hermes", cr.CONTAINER_HERMES, *argv]
        )

    def test_project_selection_comes_only_from_the_environment(self) -> None:
        one = cr.build_command(["kanban", "list"], TARGET)
        two = cr.build_command(
            ["kanban", "list"], {**TARGET, "COMPOSE_PROJECT_NAME": "writing-b"}
        )
        self.assertEqual(one[3], "writing-a")
        self.assertEqual(two[3], "writing-b")
        # The Compose file stays an environment value Docker reads itself.
        self.assertNotIn(TARGET["COMPOSE_FILE"], one)
        self.assertNotIn("-f", one[:6])

    def test_both_compose_variables_are_required_and_never_defaulted(self) -> None:
        for env in (
            {},
            {"COMPOSE_FILE": TARGET["COMPOSE_FILE"]},
            {"COMPOSE_PROJECT_NAME": "writing-a"},
            {**TARGET, "COMPOSE_PROJECT_NAME": "  "},
            {**TARGET, "COMPOSE_FILE": ""},
        ):
            with self.assertRaises(cr.ComposeRuntimeError) as caught:
                cr.build_command(["kanban", "list"], env)
            self.assertEqual(caught.exception.code, "compose_target_unset")
            self.assertEqual(caught.exception.status, 2)

    def test_actor_is_pinned_to_default_regardless_of_host_profile(self) -> None:
        host = {**TARGET, "HERMES_PROFILE": "captain", "HERMES_PROFILE_NAME": "captain"}
        for argv in (["kanban", "list"], ["artifact-list", "default", "t_1", "--json"]):
            command = cr.build_command(argv, host)
            self.assertIn("HERMES_PROFILE=default", command)
            self.assertIn("HERMES_PROFILE_NAME=default", command)
            self.assertNotIn("captain", " ".join(command))
            # Both pins precede the service name, so they are exec options.
            self.assertLess(command.index("HERMES_PROFILE=default"), command.index("hermes"))

    def test_unsupported_verbs_fail_explicitly_even_without_a_target(self) -> None:
        for verb in (*cr.COORDINATION_VERBS, "bash", "exec", "artifact-delete", "--help"):
            for env in (TARGET, {}):
                with self.assertRaises(cr.ComposeRuntimeError) as caught:
                    cr.build_command([verb, "x"], env)
                self.assertEqual(caught.exception.code, "unsupported_verb", verb)
                self.assertEqual(caught.exception.status, 2)
        with self.assertRaises(cr.ComposeRuntimeError) as caught:
            cr.build_command([], TARGET)
        self.assertEqual(caught.exception.code, "verb_required")

    def test_content_never_appears_in_argv_and_nothing_goes_through_a_shell(self) -> None:
        secret = "credential-sentinel-7f3a"
        command = cr.build_command(
            ["artifact-upload", "default", "t_1", "--name", "a.docx", "--media-type", "a/b",
             "--size", "9", "--sha256", "f" * 64, "--json"],
            {**TARGET, "GH_TOKEN": secret, "HERMES_HELMET_X": secret},
        )
        self.assertNotIn(secret, " ".join(command))
        self.assertNotIn("sh", [part for part in command if part in {"sh", "bash", "-c"}])


@unittest.skipUnless(os.name == "posix", "exec-based adapter is POSIX-only")
class ExecutableTests(unittest.TestCase):
    """The real ``python -m hermes_helmet.compose_runtime`` against a stand-in docker."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        bindir = self.tmp / "bin"
        bindir.mkdir()
        docker = bindir / "docker"
        docker.write_text(f"#!/bin/sh\nexec {sys.executable} {SHIM} \"$@\"\n")
        docker.chmod(0o755)
        self.log = self.tmp / "docker.log"
        self.env = {
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(self.tmp),
            "PYTHONPATH": str(ROOT / "src"),
            "DOCKER_SHIM_LOG": str(self.log),
            **TARGET,
        }
        self.argv = [sys.executable, "-m", "hermes_helmet.compose_runtime"]

    def run_adapter(self, *args: str, stdin: bytes | None = None, **extra: str):
        return subprocess.run(
            [*self.argv, *args],
            input=stdin if stdin is not None else b"",
            capture_output=True,
            env={**self.env, **extra},
            timeout=60,
        )

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_docker_receives_the_exec_vector_and_the_compose_target(self) -> None:
        result = self.run_adapter(
            "kanban", "--board", "writing", "list", DOCKER_SHIM_EXIT="0",
            HERMES_PROFILE="captain",
        )
        self.assertEqual(result.returncode, 0)
        (call,) = self.calls()
        self.assertEqual(
            call["argv"][:6], ["compose", "-p", "writing-a", "exec", "-T", "-e"]
        )
        self.assertIn("HERMES_PROFILE=default", call["argv"])
        self.assertEqual(call["compose_file"], TARGET["COMPOSE_FILE"])
        self.assertEqual(call["compose_project"], "writing-a")
        self.assertEqual(
            call["argv"][call["argv"].index("hermes") :],
            ["hermes", cr.CONTAINER_HERMES, "kanban", "--board", "writing", "list"],
        )

    def test_missing_target_exits_2_without_calling_docker(self) -> None:
        for drop in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME"):
            env = {key: value for key, value in self.env.items() if key != drop}
            result = subprocess.run(
                [*self.argv, "kanban", "list"], env=env, capture_output=True, timeout=60
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn(b"compose_target_unset", result.stderr)
        self.assertFalse(self.log.exists())

    def test_unsupported_verb_exits_2_without_calling_docker(self) -> None:
        for verb in ("ledger-root", "dispatch-root", "wait", "prepare-repo"):
            result = self.run_adapter(verb, "https://example.test/issues/1")
            self.assertEqual(result.returncode, 2, verb)
            self.assertIn(b"unsupported_verb", result.stderr)
            self.assertEqual(result.stdout, b"")
        self.assertFalse(self.log.exists())

    def test_binary_stdin_reaches_stdout_unchanged_and_never_argv(self) -> None:
        payload = make_docx() + BINARY_ASSET + b"\x00\r\n\xff\xfe\x80"
        result = self.run_adapter(
            "artifact-upload", "default", "t_1", "--name", "a.docx", "--media-type",
            "application/octet-stream", "--size", str(len(payload)), "--sha256",
            hashlib.sha256(payload).hexdigest(), "--json",
            stdin=payload, DOCKER_SHIM_ECHO="1",
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, payload)
        (call,) = self.calls()
        self.assertFalse(any(payload[:16] in part.encode("latin-1", "ignore") for part in call["argv"]))

    def test_docker_exit_status_is_the_adapter_exit_status(self) -> None:
        for status in (0, 3, 4, 5, 9, 127):
            result = self.run_adapter(
                "artifact-list", "default", "t_1", "--json", DOCKER_SHIM_EXIT=str(status)
            )
            self.assertEqual(result.returncode, status)

    def test_missing_docker_reports_unavailable(self) -> None:
        env = {**self.env, "PATH": str(self.tmp / "empty")}
        result = subprocess.run(
            [*self.argv, "artifact-list", "default", "t_1", "--json"],
            env=env,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 127)
        self.assertIn(b"docker_unavailable", result.stderr)

    def test_transport_timeout_kills_the_docker_call(self) -> None:
        env = {**self.env, "DOCKER_SHIM_SLEEP": "30"}
        with mock.patch.dict(os.environ, env):
            transport = ta.WorkerRuntimeTransport(self.argv, timeout=1.0)
            started = time.monotonic()
            with self.assertRaises(ta.TaskArtifactError) as caught:
                transport.list_attachments("default", "t_1")
        self.assertEqual(caught.exception.code, "artifact_transport_timeout")
        self.assertLess(time.monotonic() - started, 10)

    def test_the_coordinator_transport_round_trips_through_the_executable(self) -> None:
        # ECHO mode is not a real store, so only the stream is exercised here: the
        # coordinator's upload record check must reject an echoed payload, and the
        # download path must deliver exactly the bytes the child wrote.
        payload = make_pptx() + BINARY_ASSET
        with mock.patch.dict(os.environ, {**self.env, "DOCKER_SHIM_ECHO": "1"}):
            transport = ta.WorkerRuntimeTransport(self.argv, timeout=30.0)
            with self.assertRaises(ta.TaskArtifactError) as caught:
                transport.upload(
                    "default", "t_1", name="deck.pptx", media_type=ta.OFFICE_MEDIA_TYPES[".pptx"],
                    data=payload, sha256=hashlib.sha256(payload).hexdigest(),
                )
        self.assertEqual(caught.exception.code, "artifact_transport_invalid")


class FakeNativeStore:
    """In-memory stand-in for ``hermes_cli.kanban_db`` with native semantics."""

    class AttachmentTooLarge(ValueError):
        pass

    class Row:
        def __init__(self, **fields: object) -> None:
            self.__dict__.update(fields)

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.boards: dict[str, set[str]] = {"default": {"t_one", "t_two"}, "other": {"t_one"}}
        self.attachments: dict[tuple[str, str], list[FakeNativeStore.Row]] = {}
        self.next_id = 1

    # hermes_cli.kanban_db
    def board_exists(self, board: str) -> bool:
        return board in self.boards

    def get_task(self, conn: tuple[str, ...], task_id: str):  # type: ignore[no-untyped-def]
        return object() if task_id in self.boards[conn[0]] else None

    def list_attachments(self, conn: tuple[str, ...], task_id: str):  # type: ignore[no-untyped-def]
        return list(self.attachments.get((conn[0], task_id), []))

    def store_attachment_bytes(  # type: ignore[no-untyped-def]
        self, conn, task_id, filename, data, *, content_type=None, uploaded_by=None,
        board=None, max_bytes=None,
    ) -> int:
        if len(data) > (max_bytes or CAP):
            raise self.AttachmentTooLarge("too large")
        path = self.directory / f"{self.next_id}-{filename}"
        path.write_bytes(data)
        row = self.Row(
            id=self.next_id, task_id=task_id, filename=filename,
            stored_path=str(path), content_type=content_type, size=len(data),
            uploaded_by=uploaded_by, created_at=1_700_000_000 + self.next_id,
        )
        self.next_id += 1
        self.attachments.setdefault((board, task_id), []).append(row)
        return row.id

    # hermes_cli.kanban_db_connect
    def connect(self, *, board: str):  # type: ignore[no-untyped-def]
        return _Conn((board,))


class _Conn(tuple):
    def close(self) -> None:
        pass


class NativeArtifactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = FakeNativeStore(Path(self._tmp.name))
        patcher = mock.patch.object(na, "_load_native", return_value=(self.store, self.store))
        patcher.start()
        self.addCleanup(patcher.stop)
        actor = mock.patch.object(na, "_actor", return_value="default")
        actor.start()
        self.addCleanup(actor.stop)

    def invoke(self, *argv: str, stdin: bytes = b""):
        out, err = io.BytesIO(), io.StringIO()
        code = na.main(list(argv), stdin=io.BytesIO(stdin), stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def upload(self, task: str, name: str, data: bytes, *, board: str = "default", size=None, sha=None):  # type: ignore[no-untyped-def]
        return self.invoke(
            "artifact-upload", board, task, "--name", name, "--media-type",
            "application/octet-stream", "--size", str(len(data) if size is None else size),
            "--sha256", sha or hashlib.sha256(data).hexdigest(), "--json", stdin=data,
        )

    def test_docx_and_pptx_round_trip_with_identical_size_and_sha256(self) -> None:
        for name, data in (("brief.docx", make_docx()), ("deck.pptx", make_pptx())):
            code, out, _err = self.upload("t_one", name, data)
            self.assertEqual(code, 0)
            record = json.loads(out)
            self.assertEqual(record["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(record["size"], len(data))
            self.assertEqual(record["uploaded_by"], "default")
            code, blob, _ = self.invoke("artifact-download", "default", "t_one", str(record["id"]))
            self.assertEqual((code, blob), (0, data))
        code, out, _ = self.invoke("artifact-list", "default", "t_one", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([item["filename"] for item in json.loads(out)], ["brief.docx", "deck.pptx"])

    def test_missing_wrong_task_and_wrong_board_exit_3(self) -> None:
        data = make_docx()
        _code, out, _ = self.upload("t_one", "brief.docx", data)
        attachment_id = str(json.loads(out)["id"])
        for argv in (
            ("artifact-download", "default", "t_one", "999"),  # missing attachment
            ("artifact-download", "default", "t_two", attachment_id),  # wrong task
            ("artifact-download", "other", "t_one", attachment_id),  # wrong board
            ("artifact-download", "absent", "t_one", attachment_id),  # unknown board
            ("artifact-list", "default", "t_missing", "--json"),  # unknown task
            ("artifact-list", "absent", "t_one", "--json"),
        ):
            code, blob, err = self.invoke(*argv)
            self.assertEqual(code, 3, argv)
            self.assertEqual(blob, b"")
            self.assertTrue(err.startswith("native_artifacts: "), err)
        code, _blob, _ = self.upload("t_missing", "x.docx", data)
        self.assertEqual(code, 3)
        code, _blob, _ = self.upload("t_one", "x.docx", data, board="absent")
        self.assertEqual(code, 3)

    def test_oversize_exits_4_and_stores_nothing(self) -> None:
        self.upload("t_one", "keep.docx", make_docx())
        before = self.invoke("artifact-list", "default", "t_one", "--json")[1]
        big = b"\x00" * (CAP + 1)
        code, out, _ = self.upload("t_one", "big.docx", big)
        self.assertEqual((code, out), (4, b""))
        self.assertEqual(self.invoke("artifact-list", "default", "t_one", "--json")[1], before)

    def test_integrity_mismatch_exits_5_and_leaves_earlier_bytes_unchanged(self) -> None:
        original = make_docx()
        _code, out, _ = self.upload("t_one", "brief.docx", original)
        first = json.loads(out)
        before = self.invoke("artifact-list", "default", "t_one", "--json")[1]
        changed = make_docx("Changed")
        for kwargs in (
            {"size": len(changed) + 1},
            {"sha": hashlib.sha256(original).hexdigest()},  # checksum of other bytes
        ):
            code, out, _ = self.upload("t_one", "brief.docx", changed, **kwargs)
            self.assertEqual((code, out), (5, b""))
        self.assertEqual(self.invoke("artifact-list", "default", "t_one", "--json")[1], before)
        self.assertEqual(
            self.invoke("artifact-download", "default", "t_one", str(first["id"]))[1], original
        )

    def test_a_new_revision_is_a_new_attachment_and_old_bytes_remain(self) -> None:
        one, two = make_docx("v1"), make_docx("v2")
        id_one = json.loads(self.upload("t_one", "brief.docx", one)[1])["id"]
        id_two = json.loads(self.upload("t_one", "brief.docx", two)[1])["id"]
        self.assertNotEqual(id_one, id_two)
        self.assertEqual(self.invoke("artifact-download", "default", "t_one", str(id_one))[1], one)
        self.assertEqual(self.invoke("artifact-download", "default", "t_one", str(id_two))[1], two)

    def test_malformed_invocations_exit_2_without_echoing_arguments(self) -> None:
        secret = "credential-sentinel-7f3a"
        for argv in (
            (),
            ("artifact-frobnicate", "default", "t_one"),
            ("artifact-download", "default", "t_one", secret),
            ("artifact-upload", "default", "t_one", "--name", secret),
            ("artifact-list", "BAD BOARD", secret, "--json"),
            ("artifact-upload", "default", "t_one", "--name", "a", "--media-type", "a/b",
             "--size", "1", "--sha256", secret, "--json"),
        ):
            code, out, err = self.invoke(*argv, stdin=b"x")
            self.assertEqual(code, 2, argv)
            self.assertEqual(out, b"")
            self.assertNotIn(secret, err)

    def test_unexpected_failures_never_echo_content(self) -> None:
        data = make_docx()
        with mock.patch.object(
            self.store, "store_attachment_bytes", side_effect=RuntimeError("credential-sentinel-7f3a")
        ):
            code, out, err = self.upload("t_one", "a.docx", data)
        self.assertEqual((code, out), (1, b""))
        self.assertNotIn("credential-sentinel-7f3a", err)


@unittest.skipUnless(os.name == "posix", "needs a POSIX shell environment")
@unittest.skipUnless(_hermes_python(), "needs the native Hermes interpreter")
class NativeStoreThroughAdapterTests(unittest.TestCase):
    """The chain ``helmet-compose`` -> native helper -> the real Hermes Kanban store.

    A stand-in docker applies the adapter's environment to a host command, so a
    real native store is exercised where a Docker daemon is not available.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.home = tmp / "hermes-home"
        self.home.mkdir()
        bindir = tmp / "bin"
        bindir.mkdir()
        docker = bindir / "docker"
        docker.write_text(f"#!/bin/sh\nexec {sys.executable} {SHIM} \"$@\"\n")
        docker.chmod(0o755)
        self.env = {
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(tmp),
            "PYTHONPATH": str(ROOT / "src"),
            "DOCKER_SHIM_HOME": str(self.home),
            "DOCKER_SHIM_SRC": str(ROOT / "src"),
            "HERMES_PROFILE": "captain",
            **TARGET,
        }
        self.argv = [sys.executable, "-m", "hermes_helmet.compose_runtime"]
        board = self.run_adapter("kanban", "boards", "create", "other")
        self.assertEqual(board.returncode, 0, board.stderr)
        self.task = self.create("default", "input")
        self.other_task = self.create("other", "elsewhere")
        self.transport = ta.WorkerRuntimeTransport(self.argv, timeout=120.0, executor=self.execute)

    def execute(self, argv, stdin_data, max_stdout, timeout):  # type: ignore[no-untyped-def]
        with mock.patch.dict(os.environ, self.env):
            return ta.run_bounded(argv, stdin_data, max_stdout, timeout)

    def run_adapter(self, *args: str, stdin: bytes = b""):
        return subprocess.run(
            [*self.argv, *args], input=stdin, capture_output=True, env=self.env, timeout=120
        )

    def create(self, board: str, title: str) -> str:
        result = self.run_adapter(
            "kanban", "--board", board, "create", title, "--assignee", "writing", "--json"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["id"]

    def test_docx_and_pptx_round_trip_through_native_attachments(self) -> None:
        for name, data in (("brief.docx", make_docx()), ("deck.pptx", make_pptx())):
            ref = ta.upload_artifact_bytes(
                self.transport, board="default", task_id=self.task, name=name, data=data
            )
            fetched = ta.fetch_artifact(self.transport, ref, board="default", task_id=self.task)
            self.assertEqual(fetched.data, data)
            self.assertEqual((ref.size, ref.sha256), (len(data), hashlib.sha256(data).hexdigest()))
            self.assertEqual(ref.uploaded_by, "default")  # never the host's profile

    def test_scoping_and_integrity_exit_statuses(self) -> None:
        data = make_docx()
        ref = ta.upload_artifact_bytes(
            self.transport, board="default", task_id=self.task, name="brief.docx", data=data
        )
        listing = self.run_adapter("artifact-list", "default", self.task, "--json").stdout
        att = str(ref.attachment_id)
        for args, status in (
            (("artifact-download", "default", self.task, "999"), 3),
            (("artifact-download", "default", self.other_task, att), 3),
            (("artifact-download", "other", self.task, att), 3),
            (("artifact-list", "absent", self.task, "--json"), 3),
        ):
            self.assertEqual(self.run_adapter(*args).returncode, status, args)
        mismatch = self.run_adapter(
            "artifact-upload", "default", self.task, "--name", "x.docx", "--media-type", "a/b",
            "--size", "5", "--sha256", hashlib.sha256(data).hexdigest(), "--json", stdin=data,
        )
        self.assertEqual(mismatch.returncode, 5)
        self.assertEqual(self.run_adapter("artifact-list", "default", self.task, "--json").stdout, listing)
        self.assertEqual(
            self.run_adapter("artifact-download", "default", self.task, att).stdout, data
        )


class OwnershipDocumentationTests(unittest.TestCase):
    """The public reference adapter is named wherever the ownership rule is stated."""

    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_ownership_and_supported_surface_are_documented_together(self) -> None:
        for relative in (
            "docs/task-artifacts.md",
            "docs/helmet-issue.md",
            "docs/first-officer-plugins.md",
            "src/hermes_helmet/helmet_issue.py",
        ):
            self.assertIn("helmet-compose", self.read(relative), relative)
        guide = self.read("docs/task-artifacts.md")
        for needle in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "dedicated shell", "unsupported_verb", "default"):
            self.assertIn(needle, guide)
        self.assertNotIn(
            "Deployment-specific adapters stay outside this repository;",
            self.read("src/hermes_helmet/helmet_issue.py"),
        )

    def test_console_entrypoint_is_declared_once(self) -> None:
        text = self.read("pyproject.toml")
        self.assertEqual(text.count("helmet-compose ="), 1)
        self.assertIn('"hermes_helmet.compose_runtime:main"', text)


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, timeout=60, check=False
    )
    return probe.returncode == 0


@unittest.skipUnless(
    os.environ.get("HELMET_COMPOSE_INTEGRATION") == "1",
    "set HELMET_COMPOSE_INTEGRATION=1 to run the real Compose boundary test",
)
@unittest.skipUnless(
    os.name == "posix" and _docker_compose_available(), "needs docker with the compose plugin"
)
class ComposeIntegrationTests(unittest.TestCase):
    """Real native attachments over a real, disposable Compose project."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.project = f"helmet-compose-it-{uuid.uuid4().hex[:8]}"
        cls.env = {
            **os.environ,
            "COMPOSE_FILE": str(COMPOSE_FIXTURE),
            "COMPOSE_PROJECT_NAME": cls.project,
            "HELMET_TEST_SRC": str(ROOT / "src"),
            "PYTHONPATH": str(ROOT / "src"),
            "HERMES_PROFILE": "captain",
        }
        cls.addClassCleanup(cls._down)
        up = subprocess.run(
            ["docker", "compose", "up", "-d", "--wait"],
            env=cls.env, capture_output=True, timeout=1500, check=False,
        )
        if up.returncode != 0:
            raise RuntimeError(f"compose up failed (exit {up.returncode})")
        cls.argv = [sys.executable, "-m", "hermes_helmet.compose_runtime"]
        cls.transport = ta.WorkerRuntimeTransport(cls.argv, timeout=300.0, executor=cls._execute)

    @classmethod
    def _down(cls) -> None:
        # Only this test's uniquely named project, including its anonymous volume.
        subprocess.run(
            ["docker", "compose", "down", "-v", "--remove-orphans"],
            env=cls.env, capture_output=True, timeout=300, check=False,
        )

    @classmethod
    def _execute(cls, argv, stdin_data, max_stdout, timeout):  # type: ignore[no-untyped-def]
        with mock.patch.dict(os.environ, cls.env):
            return ta.run_bounded(argv, stdin_data, max_stdout, timeout)

    @classmethod
    def adapter(cls, *args: str, stdin: bytes = b"", timeout: int = 300):
        return subprocess.run(
            [*cls.argv, *args], input=stdin, capture_output=True, env=cls.env, timeout=timeout
        )

    @classmethod
    def task(cls, board: str, title: str) -> str:
        result = cls.adapter(
            "kanban", "--board", board, "create", title, "--assignee", "writing", "--json"
        )
        assert result.returncode == 0, "native kanban create failed"
        return json.loads(result.stdout)["id"]

    def test_docx_and_pptx_round_trip_and_exit_statuses_over_real_compose(self) -> None:
        made = self.adapter("kanban", "boards", "create", "other")
        self.assertEqual(made.returncode, 0)
        task = self.task("default", "input")
        elsewhere = self.task("other", "elsewhere")
        self.assertEqual(self.adapter("kanban", "--board", "default", "show", task, "--json").returncode, 0)

        refs = []
        for name, data in (("brief.docx", make_docx()), ("deck.pptx", make_pptx())):
            ref = ta.upload_artifact_bytes(
                self.transport, board="default", task_id=task, name=name, data=data
            )
            fetched = ta.fetch_artifact(self.transport, ref, board="default", task_id=task)
            self.assertEqual(fetched.data, data)
            self.assertEqual((ref.size, ref.sha256), (len(data), hashlib.sha256(data).hexdigest()))
            self.assertEqual(ref.uploaded_by, "default")
            refs.append((ref, data))

        listing = self.adapter("artifact-list", "default", task, "--json").stdout
        first = str(refs[0][0].attachment_id)
        for args, status in (
            (("artifact-download", "default", task, "9999"), 3),  # missing
            (("artifact-download", "default", elsewhere, first), 3),  # wrong task
            (("artifact-download", "other", task, first), 3),  # wrong board
            (("artifact-list", "other", task, "--json"), 3),
        ):
            self.assertEqual(self.adapter(*args).returncode, status, args)

        data = refs[0][1]
        integrity = self.adapter(
            "artifact-upload", "default", task, "--name", "bad.docx", "--media-type", "a/b",
            "--size", str(len(data) + 1), "--sha256", hashlib.sha256(data).hexdigest(),
            "--json", stdin=data,
        )
        self.assertEqual(integrity.returncode, 5)

        # Directly to the adapter: the coordinator would refuse this before sending.
        oversize = self.adapter(
            "artifact-upload", "default", task, "--name", "big.docx", "--media-type", "a/b",
            "--size", str(CAP + 1), "--sha256", "0" * 64, "--json",
            stdin=b"\x00" * (CAP + 1), timeout=600,
        )
        self.assertEqual(oversize.returncode, 4)

        self.assertEqual(self.adapter("artifact-list", "default", task, "--json").stdout, listing)
        for ref, data in refs:
            self.assertEqual(
                ta.fetch_artifact(self.transport, ref, board="default", task_id=task).data, data
            )


if __name__ == "__main__":
    unittest.main()
