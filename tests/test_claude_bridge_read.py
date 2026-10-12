#!/usr/bin/env python3
"""``helmet bridge read``: Claude Code session records through the CLI seam.

All transcripts are synthetic and generated here. Tests drive argv, stdout and
the exit code of ``hermes_helmet.cli.main``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import uuid as uuidlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hermes_helmet import __version__  # noqa: E402
from hermes_helmet import cli  # noqa: E402

SESSION = "11111111-2222-4333-8444-555555555555"
VERSION = "2.1.296"


def make_uuid(n: int) -> str:
    return str(uuidlib.UUID(int=0x4F1C2A9E << 96 | n))


class Transcript:
    """Builds synthetic Claude Code JSONL records with a running clock."""

    def __init__(self, session: str = SESSION, version: str = VERSION) -> None:
        self.session = session
        self.version = version
        self.counter = 0
        self.last: str | None = None
        self.lines: list[str] = []

    def _base(self, rtype: str, **extra) -> dict:
        self.counter += 1
        uid = make_uuid(self.counter)
        record = {
            "type": rtype,
            "uuid": uid,
            "parentUuid": self.last,
            "sessionId": self.session,
            "version": self.version,
            "timestamp": f"2026-10-09T16:{self.counter // 60:02d}:{self.counter % 60:02d}Z",
        }
        record.update(extra)
        self.last = uid
        return record

    def add(self, record: dict) -> dict:
        self.lines.append(json.dumps(record))
        return record

    def user(self, text, **extra):
        return self.add(self._base("user", message={"role": "user", "content": text}, **extra))

    def assistant(self, blocks):
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        return self.add(self._base("assistant", message={"role": "assistant", "content": blocks}))

    def tool_use(self, tool_use_id, name, tool_input):
        return self.assistant([{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}])

    def tool_result(self, tool_use_id, text, is_error=False, **extra):
        return self.add(
            self._base(
                "user",
                message={
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tool_use_id, "content": text, "is_error": is_error}
                    ],
                },
                **extra,
            )
        )

    def text(self, trailing_newline=True, partial: str = "") -> str:
        body = "\n".join(self.lines)
        return body + ("\n" if trailing_newline else "") + partial


class BridgeReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "claude-home"
        self.projects = self.home / "projects"
        self.project = self.projects / "-synthetic-project"
        self.project.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    # helpers ---------------------------------------------------------
    def write(self, transcript: Transcript, *, project: Path | None = None, session: str = SESSION, **kw) -> Path:
        path = (project or self.project) / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(transcript.text(**kw), encoding="utf-8")
        return path

    def run_cli(self, *args: str) -> tuple[int, dict]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["bridge", "read", *args])
        self.assertEqual(err.getvalue(), "")
        return code, json.loads(out.getvalue())

    def read_ok(self, *args: str) -> dict:
        code, payload = self.run_cli("--session", SESSION, *args)
        self.assertEqual(code, 0, payload)
        return payload

    def assert_error(self, payload: dict, code: str) -> str:
        self.assertEqual(payload["schema"], "hermes-helmet.bridge.records/1")
        self.assertEqual(payload["error"]["code"], code)
        self.assertNotIn("records", payload)
        return payload["error"]["message"]

    def basic(self) -> Transcript:
        t = Transcript()
        t.user("Please fix the parser.")
        t.assistant([
            {"type": "thinking", "thinking": "SECRET-REASONING-ONE", "signature": "x"},
            {"type": "text", "text": "I will run the tests."},
        ])
        t.tool_use("toolu_1", "Bash", {"command": "pytest"})
        t.tool_result("toolu_1", "3 passed")
        t.assistant("Done.")
        return t

    # documented output -------------------------------------------------
    def test_documented_output_shape(self) -> None:
        self.write(self.basic())
        payload = self.read_ok()
        self.assertEqual(payload["schema"], "hermes-helmet.bridge.records/1")
        self.assertEqual(payload["helmetVersion"], __version__)
        self.assertEqual(payload["sessionId"], SESSION)
        self.assertRegex(payload["readAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(payload["fingerprint"], f"5:{make_uuid(5).replace('-', '')}")
        self.assertEqual(payload["warnings"], [])
        self.assertEqual(
            payload["coverage"],
            {"recordsTotal": 5, "recordsIncluded": 5, "bytesOmitted": 0,
             "unsupportedSkipped": 0, "pendingTail": False},
        )
        records = payload["records"]
        self.assertEqual(
            [(r["role"], r["origin"]) for r in records],
            [("user", "person"), ("assistant", "assistant"), ("assistant", "assistant"),
             ("tool_result", "assistant"), ("assistant", "assistant")],
        )
        keys = {"ref", "parentRef", "agentId", "timestamp", "role", "origin", "text",
                "tool", "toolUseId", "isError", "truncated"}
        for record in records:
            self.assertEqual(set(record), keys)
            self.assertRegex(record["ref"], r"^[0-9a-f]{32}$")
        refs = [r["ref"] for r in records]
        self.assertEqual(len(set(refs)), len(refs))
        self.assertIsNone(records[0]["parentRef"])
        self.assertEqual(records[1]["parentRef"], refs[0])
        call, result = records[2], records[3]
        self.assertEqual((call["tool"], call["toolUseId"]), ("Bash", "toolu_1"))
        self.assertEqual(json.loads(call["text"]), {"command": "pytest"})
        self.assertEqual((result["tool"], result["toolUseId"], result["text"]), ("Bash", "toolu_1", "3 passed"))
        self.assertFalse(result["isError"])
        self.assertEqual(records[0]["timestamp"], "2026-10-09T16:00:01Z")

    def test_tool_error_flag(self) -> None:
        t = Transcript()
        t.user("go")
        t.tool_use("toolu_9", "Bash", {"command": "false"})
        t.tool_result("toolu_9", "boom", is_error=True)
        self.write(t)
        self.assertTrue(self.read_ok()["records"][-1]["isError"])

    def test_thinking_is_absent(self) -> None:
        t = self.basic()
        t.assistant([{"type": "redacted_thinking", "data": "SECRET-REASONING-TWO"}])
        self.write(t)
        raw = json.dumps(self.read_ok())
        self.assertNotIn("SECRET-REASONING", raw)
        self.assertNotIn("thinking", raw)

    # subagents ----------------------------------------------------------
    def test_subagent_transcripts_carry_agent_ids_and_parents(self) -> None:
        t = Transcript()
        t.user("delegate")
        t.tool_use("toolu_task", "Task", {"prompt": "look"})
        t.tool_result("toolu_task", "found it", toolUseResult={"agentId": "a1b2c3"})
        self.write(t)
        sub = Transcript()
        sub.counter = 100
        sub.user("look", isSidechain=True, agentId="a1b2c3", parentUuid=None)
        sub.assistant("Searching.")
        sub_lines = [json.loads(line) for line in sub.lines]
        for line in sub_lines:
            line["isSidechain"] = True
            line["agentId"] = "a1b2c3"
        sub_lines[0]["parentUuid"] = None
        folder = self.project / SESSION / "subagents"
        folder.mkdir(parents=True)
        (folder / "agent-a1b2c3.jsonl").write_text(
            "\n".join(json.dumps(line) for line in sub_lines) + "\n", encoding="utf-8"
        )
        payload = self.read_ok()
        agents = [r for r in payload["records"] if r["agentId"]]
        self.assertEqual(len(agents), 2)
        self.assertEqual({r["agentId"] for r in agents}, {"a1b2c3"})
        self.assertEqual({r["origin"] for r in agents}, {"agent"})
        task_call = next(r for r in payload["records"] if r["tool"] == "Task" and not r["agentId"])
        self.assertEqual(agents[0]["parentRef"], task_call["ref"])
        self.assertEqual(agents[1]["parentRef"], agents[0]["ref"])
        self.assertEqual(payload["coverage"]["recordsTotal"], 5)

    # compaction ---------------------------------------------------------
    def test_records_on_both_sides_of_compaction_are_kept(self) -> None:
        t = Transcript()
        t.user("before compaction")
        t.assistant("early answer")
        t.add(t._base("system", subtype="compact_boundary", content="Conversation compacted",
                      compactMetadata={"trigger": "auto"}))
        t.user("This session is being continued from a previous conversation...", isCompactSummary=True)
        t.assistant("late answer")
        self.write(t)
        payload = self.read_ok()
        texts = [r["text"] for r in payload["records"]]
        self.assertIn("before compaction", texts)
        self.assertIn("early answer", texts)
        self.assertIn("late answer", texts)
        self.assertIn("Conversation compacted", texts)
        summary = next(r for r in payload["records"] if r["text"].startswith("This session is being"))
        self.assertEqual(summary["origin"], "meta")
        self.assertEqual(payload["coverage"]["recordsTotal"], 5)

    # partial tail -------------------------------------------------------
    def test_incomplete_final_line_is_ignored(self) -> None:
        self.write(self.basic(), partial='{"type":"assistant","uuid":"abc')
        payload = self.read_ok()
        self.assertTrue(payload["coverage"]["pendingTail"])
        self.assertEqual(payload["coverage"]["recordsTotal"], 5)
        self.assertTrue(any(w["code"] == "pending-tail" for w in payload["warnings"]))

    def test_complete_unterminated_final_line_is_read(self) -> None:
        self.write(self.basic(), trailing_newline=False)
        payload = self.read_ok()
        self.assertFalse(payload["coverage"]["pendingTail"])
        self.assertEqual(payload["coverage"]["recordsTotal"], 5)

    def test_corrupt_middle_line_is_unsupported_format(self) -> None:
        t = self.basic()
        t.lines.insert(2, "{not json")
        self.write(t)
        code, payload = self.run_cli("--session", SESSION)
        self.assertEqual(code, 2)
        self.assert_error(payload, "unsupported-format")

    # origins ------------------------------------------------------------
    def test_injected_user_records_are_not_person(self) -> None:
        t = Transcript()
        t.user("typed by the Captain")
        t.user("<system-reminder>hook says hi</system-reminder>")
        t.user("injected by a plugin", origin={"kind": "plugin"})
        t.user("hook output", origin={"kind": "hook"})
        t.user("caveat text", isMeta=True)
        t.user("<command-name>/clear</command-name>")
        t.user("explicit human", origin={"kind": "human"})
        t.assistant("ok")
        self.write(t)
        origins = [r["origin"] for r in self.read_ok()["records"]]
        self.assertEqual(origins, ["person", "hook", "plugin", "hook", "meta", "meta", "person", "assistant"])

    # identity -----------------------------------------------------------
    def test_session_mismatch_inside_file(self) -> None:
        t = self.basic()
        t.session = "99999999-2222-4333-8444-555555555555"
        t.user("someone else")
        self.write(t)
        code, payload = self.run_cli("--session", SESSION)
        self.assertEqual(code, 2)
        message = self.assert_error(payload, "session-mismatch")
        self.assertIn(SESSION, message)

    def test_session_mismatch_with_explicit_transcript(self) -> None:
        other = "99999999-2222-4333-8444-555555555555"
        path = self.write(Transcript(session=other), session=other)
        t = Transcript(session=other)
        t.user("x")
        path.write_text(t.text(), encoding="utf-8")
        code, payload = self.run_cli("--session", SESSION, "--transcript", str(path))
        self.assertEqual(code, 2)
        self.assert_error(payload, "session-mismatch")

    def test_zero_matches_is_session_not_found_even_with_newer_unrelated_file(self) -> None:
        other = "99999999-2222-4333-8444-555555555555"
        t = Transcript(session=other)
        t.user("newer and unrelated")
        self.write(t, session=other)
        code, payload = self.run_cli("--session", SESSION)
        self.assertEqual(code, 2)
        self.assert_error(payload, "session-not-found")

    def test_missing_projects_directory_is_session_not_found(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home / "absent")}):
            code, payload = self.run_cli("--session", SESSION)
        self.assertEqual(code, 2)
        self.assert_error(payload, "session-not-found")

    def test_two_exact_matches_is_ambiguous(self) -> None:
        self.write(self.basic())
        self.write(self.basic(), project=self.projects / "-another-project")
        code, payload = self.run_cli("--session", SESSION)
        self.assertEqual(code, 2)
        self.assert_error(payload, "ambiguous-session")

    def test_explicit_transcript_resolves_ambiguity(self) -> None:
        chosen = self.write(self.basic())
        self.write(self.basic(), project=self.projects / "-another-project")
        payload = self.read_ok("--transcript", str(chosen))
        self.assertEqual(payload["coverage"]["recordsTotal"], 5)

    def test_exact_filename_only_and_newer_file_not_chosen(self) -> None:
        self.write(self.basic())
        newer = Transcript()
        newer.user("a decoy that names the session in its content: " + SESSION)
        decoy = self.project / f"{SESSION}-copy.jsonl"
        decoy.write_text(newer.text(), encoding="utf-8")
        os.utime(decoy, (4102444800, 4102444800))
        payload = self.read_ok()
        self.assertEqual(payload["records"][0]["text"], "Please fix the parser.")

    def test_missing_explicit_transcript_is_not_found(self) -> None:
        code, payload = self.run_cli("--session", SESSION, "--transcript", str(self.home / "nope.jsonl"))
        self.assertEqual(code, 2)
        self.assert_error(payload, "session-not-found")

    def test_unreadable_transcript(self) -> None:
        path = self.write(self.basic())
        real_open = open

        def fake_open(file, *args, **kwargs):
            if str(file) == str(path):
                raise PermissionError(13, "Permission denied")
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", fake_open):
            code, payload = self.run_cli("--session", SESSION, "--transcript", str(path))
        self.assertEqual(code, 2)
        self.assert_error(payload, "unreadable")

    # format support -----------------------------------------------------
    def test_unsupported_version_names_the_version(self) -> None:
        for bad in ("1.0.12", "9.9.9"):
            with self.subTest(version=bad):
                self.write(self._versioned(bad))
                code, payload = self.run_cli("--session", SESSION)
                self.assertEqual(code, 2)
                self.assertIn(bad, self.assert_error(payload, "unsupported-format"))

    def _versioned(self, version: str) -> Transcript:
        t = Transcript(version=version)
        t.user("hello")
        return t

    def test_missing_version_is_unsupported(self) -> None:
        t = Transcript()
        record = t.user("hello")
        del record["version"]
        t.lines = [json.dumps(record)]
        self.write(t)
        code, payload = self.run_cli("--session", SESSION)
        self.assertEqual(code, 2)
        self.assert_error(payload, "unsupported-format")

    def test_malformed_core_records_are_unsupported(self) -> None:
        cases = {
            "no-message": lambda r: r.pop("message"),
            "bad-content": lambda r: r["message"].update(content=42),
            "bad-uuid": lambda r: r.update(uuid=7),
            "bad-block": lambda r: r["message"].update(content=[7]),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                t = Transcript()
                record = t.user("hello")
                mutate(record)
                t.lines = [json.dumps(record)]
                self.write(t)
                code, payload = self.run_cli("--session", SESSION)
                self.assertEqual(code, 2)
                self.assert_error(payload, "unsupported-format")

    def test_unknown_noncore_types_are_skipped_and_counted(self) -> None:
        t = self.basic()
        t.add({"type": "hologram", "sessionId": SESSION, "version": VERSION})
        t.add({"type": "teleport", "sessionId": SESSION})
        t.add({"type": "file-history-snapshot", "messageId": "m", "snapshot": {}})
        self.write(t)
        payload = self.read_ok()
        self.assertEqual(payload["coverage"]["unsupportedSkipped"], 2)
        self.assertEqual(payload["coverage"]["recordsTotal"], 5)
        warning = next(w for w in payload["warnings"] if w["code"] == "unsupported-records-skipped")
        self.assertIn("hologram", warning["message"])

    def test_noncore_record_with_foreign_session_is_mismatch(self) -> None:
        t = self.basic()
        t.add({"type": "summary", "sessionId": "other-session", "summary": "x"})
        self.write(t)
        code, payload = self.run_cli("--session", SESSION)
        self.assertEqual(code, 2)
        self.assert_error(payload, "session-mismatch")

    def test_empty_transcript_is_never_empty_success(self) -> None:
        for body in ("", "\n"):
            with self.subTest(body=body):
                (self.project / f"{SESSION}.jsonl").write_text(body, encoding="utf-8")
                code, payload = self.run_cli("--session", SESSION)
                self.assertEqual(code, 2)
                self.assert_error(payload, "unsupported-format")

    def test_only_noncore_records_is_not_empty_success(self) -> None:
        t = Transcript()
        t.add({"type": "hologram", "sessionId": SESSION})
        self.write(t)
        code, payload = self.run_cli("--session", SESSION)
        self.assertEqual(code, 2)
        self.assert_error(payload, "unsupported-format")

    # budget -------------------------------------------------------------
    def big_transcript(self, results: int, size: int) -> Transcript:
        t = Transcript()
        for index in range(results):
            t.user(f"prompt {index}")
            t.assistant(f"answer {index}")
            t.tool_use(f"toolu_{index}", "Bash", {"command": f"cmd {index}"})
            head, tail = f"HEAD{index}-", f"-TAIL{index}"
            t.tool_result(f"toolu_{index}", head + ("x" * size) + tail)
        return t

    def test_large_transcript_over_4_mib_streams_and_respects_budget(self) -> None:
        t = self.big_transcript(results=24, size=200_000)
        path = self.write(t)
        self.assertGreater(path.stat().st_size, 4 * 1024 * 1024)
        payload = self.read_ok("--max-bytes", "40000")
        cov = payload["coverage"]
        self.assertEqual(cov["recordsTotal"], 24 * 4)
        self.assertLess(cov["recordsIncluded"], cov["recordsTotal"])
        self.assertGreater(cov["bytesOmitted"], 4 * 1024 * 1024 - 100_000)
        prompts = [r["text"] for r in payload["records"] if r["origin"] == "person"]
        answers = [r["text"] for r in payload["records"] if r["role"] == "assistant" and not r["tool"]]
        self.assertEqual(prompts, [f"prompt {i}" for i in range(24)])
        self.assertEqual(answers, [f"answer {i}" for i in range(24)])
        results = [r for r in payload["records"] if r["role"] == "tool_result"]
        self.assertTrue(results)
        self.assertTrue(all(r["truncated"] for r in results))
        # Oldest tool results go first; the retained ones are the newest.
        kept = [int(r["toolUseId"].split("_")[1]) for r in results]
        self.assertEqual(kept, sorted(kept))
        self.assertEqual(kept[-1], 23)
        self.assertLess(kept[0], 23) if len(kept) > 1 else None
        self.assertLessEqual(len(json.dumps(payload).encode("utf-8")), 40000 + 4000)

    def test_excerpt_keeps_head_and_tail_before_dropping(self) -> None:
        t = self.big_transcript(results=3, size=5000)
        self.write(t)
        full = self.read_ok()
        self.assertTrue(all(not r["truncated"] for r in full["records"] if r["role"] == "tool_result"))
        payload = self.read_ok("--max-bytes", "9000")
        self.assertEqual(payload["coverage"]["recordsIncluded"], payload["coverage"]["recordsTotal"])
        results = [r for r in payload["records"] if r["role"] == "tool_result"]
        self.assertEqual(len(results), 3)
        for index, record in enumerate(results):
            self.assertTrue(record["truncated"])
            self.assertTrue(record["text"].startswith(f"HEAD{index}-"))
            self.assertTrue(record["text"].endswith(f"-TAIL{index}"))
        self.assertGreater(payload["coverage"]["bytesOmitted"], 10000)

    def test_protected_text_is_never_deleted_and_conflict_is_reported(self) -> None:
        t = Transcript()
        long_prompt = "P" * 6000
        t.user(long_prompt)
        t.assistant("A" * 6000)
        t.tool_use("toolu_1", "Bash", {"command": "ls"})
        t.tool_result("toolu_1", "R" * 3000)
        self.write(t)
        payload = self.read_ok("--max-bytes", "1000")
        texts = [r["text"] for r in payload["records"]]
        self.assertIn(long_prompt, texts)
        self.assertIn("A" * 6000, texts)
        self.assertFalse([r for r in payload["records"] if r["role"] == "tool_result"])
        self.assertTrue(any(w["code"] == "budget-conflict" for w in payload["warnings"]))
        self.assertEqual(payload["coverage"]["recordsTotal"], 4)

    def test_invalid_max_bytes(self) -> None:
        self.write(self.basic())
        code, payload = self.run_cli("--session", SESSION, "--max-bytes", "0")
        self.assertEqual(code, 2)
        self.assert_error(payload, "unreadable")

    # stable refs ----------------------------------------------------------
    def test_refs_are_stable_after_append(self) -> None:
        t = self.basic()
        path = self.write(t)
        first = self.read_ok()
        t.user("a later prompt")
        t.assistant("a later answer")
        path.write_text(t.text(), encoding="utf-8")
        second = self.read_ok()
        before = [(r["ref"], r["text"], r["parentRef"]) for r in first["records"]]
        after = [(r["ref"], r["text"], r["parentRef"]) for r in second["records"]]
        self.assertEqual(after[: len(before)], before)
        self.assertEqual(second["coverage"]["recordsTotal"], 7)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])

    def refs_of(self, payload: dict) -> dict[str, dict]:
        return {r["text"]: r for r in payload["records"]}

    def test_refs_are_full_32_hex_uuid_derived(self) -> None:
        self.write(self.basic())
        records = self.read_ok()["records"]
        for record in records:
            self.assertRegex(record["ref"], r"^[0-9a-f]{32}$")
        self.assertEqual(records[0]["ref"], make_uuid(1).replace("-", ""))
        self.assertEqual(records[1]["parentRef"], records[0]["ref"])

    def test_ref_is_stable_when_an_appended_uuid_collides_with_its_prefix(self) -> None:
        t = Transcript()
        t.user("single prompt")
        path = self.write(t)
        before = self.read_ok()["records"]
        t.assistant("next answer")  # shares the 4f1c2a9e prefix
        path.write_text(t.text(), encoding="utf-8")
        after = self.read_ok()["records"]
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[1]["parentRef"], before[0]["ref"])
        self.assertEqual(len({r["ref"] for r in after}), 2)

    def test_refs_do_not_depend_on_earlier_or_tied_timestamps(self) -> None:
        t = Transcript()
        t.user("first prompt")
        path = self.write(t)
        before = self.read_ok()["records"][0]
        t.assistant("answer with earlier timestamp")
        t.assistant("answer with tied timestamp")
        records = [json.loads(line) for line in t.lines]
        records[1]["timestamp"] = "2000-01-01T00:00:00Z"
        records[2]["timestamp"] = records[0]["timestamp"]
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        after = self.refs_of(self.read_ok())
        self.assertEqual(after["first prompt"]["ref"], before["ref"])
        self.assertEqual(after["answer with earlier timestamp"]["parentRef"], before["ref"])
        self.assertEqual(
            after["answer with tied timestamp"]["parentRef"],
            after["answer with earlier timestamp"]["ref"],
        )

    def test_refs_are_stable_when_a_partial_tail_completes(self) -> None:
        t = Transcript()
        t.user("kept prompt")
        t.assistant("tail answer")
        complete = t.text()
        first_line, tail_line = complete.rstrip("\n").split("\n")
        path = self.write(t)
        path.write_text(first_line + "\n" + tail_line[:20], encoding="utf-8")
        partial = self.read_ok()
        self.assertTrue(partial["coverage"]["pendingTail"])
        path.write_text(complete, encoding="utf-8")
        done = self.read_ok()
        self.assertEqual(done["records"][0], partial["records"][0])
        self.assertEqual(done["records"][1]["parentRef"], partial["records"][0]["ref"])

    def test_refs_are_stable_when_subagent_records_grow_or_appear(self) -> None:
        t = Transcript()
        t.user("delegate")
        t.tool_use("toolu_task", "Task", {"prompt": "look"})
        t.tool_result("toolu_task", "found it", toolUseResult={"agentId": "a1b2c3"})
        self.write(t)
        before = self.read_ok()["records"]

        def sub_line(uid: str, parent, text: str, stamp: str) -> str:
            return json.dumps({
                "type": "assistant", "uuid": uid, "parentUuid": parent, "sessionId": SESSION,
                "version": VERSION, "timestamp": stamp, "isSidechain": True, "agentId": "a1b2c3",
                "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            })

        folder = self.project / SESSION / "subagents"
        folder.mkdir(parents=True)
        sub = folder / "agent-a1b2c3.jsonl"
        one = make_uuid(900)
        sub.write_text(sub_line(one, None, "sub one", "2000-01-01T00:00:00Z") + "\n", encoding="utf-8")
        discovered = self.read_ok()["records"]
        self.assertEqual([r for r in discovered if not r["agentId"]], before)
        sub_one = next(r for r in discovered if r["agentId"])
        self.assertEqual(sub_one["ref"], one.replace("-", ""))
        task_call = next(r for r in before if r["tool"] == "Task")
        self.assertEqual(sub_one["parentRef"], task_call["ref"])
        sub.write_text(
            sub.read_text(encoding="utf-8")
            + sub_line(make_uuid(901), one, "sub two", "1999-01-01T00:00:00Z") + "\n",
            encoding="utf-8",
        )
        grown = self.refs_of(self.read_ok())
        self.assertEqual(grown["sub one"], sub_one)
        self.assertEqual(grown["sub two"]["parentRef"], sub_one["ref"])

    def test_multiple_blocks_of_one_uuid_have_deterministic_distinct_refs(self) -> None:
        t = Transcript()
        t.user("ask")
        t.assistant([
            {"type": "text", "text": "block one"},
            {"type": "tool_use", "id": "toolu_x", "name": "Bash", "input": {"command": "ls"}},
            {"type": "text", "text": "block three"},
        ])
        path = self.write(t)
        first = self.read_ok()["records"]
        refs = [r["ref"] for r in first]
        self.assertEqual(len(set(refs)), 4)
        for ref in refs:
            self.assertRegex(ref, r"^[0-9a-f]{32}$")
        self.assertEqual(first[1]["ref"], make_uuid(2).replace("-", ""))
        self.assertEqual(first[2]["parentRef"], first[1]["ref"])
        self.assertEqual(first[3]["parentRef"], first[1]["ref"])
        t.user("later")
        path.write_text(t.text(), encoding="utf-8")
        second = self.read_ok()["records"]
        self.assertEqual(second[:4], first)

    # serialized budget ------------------------------------------------------
    def cli_stdout(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["bridge", "read", "--session", SESSION, *args])
        return code, out.getvalue()

    def test_stdout_bytes_stay_within_max_bytes(self) -> None:
        self.write(self.big_transcript(results=20, size=5000))
        code, text = self.cli_stdout("--max-bytes", "40000")
        self.assertEqual(code, 0)
        payload = json.loads(text)
        self.assertLessEqual(len(text.encode("utf-8")), 40000)
        self.assertEqual(payload["warnings"], [])
        prompts = [r["text"] for r in payload["records"] if r["origin"] == "person"]
        self.assertEqual(prompts, [f"prompt {i}" for i in range(20)])

    def test_over_budget_protected_text_still_warns_with_true_size(self) -> None:
        t = Transcript()
        t.user("P" * 6000)
        t.assistant("A" * 6000)
        self.write(t)
        code, text = self.cli_stdout("--max-bytes", "1000")
        payload = json.loads(text)
        self.assertEqual([w["code"] for w in payload["warnings"]], ["budget-conflict"])
        self.assertGreater(len(text.encode("utf-8")), 12000)

    def test_truncated_tool_input_reports_omitted_bytes(self) -> None:
        t = Transcript()
        t.user("go")
        t.tool_use("toolu_1", "Write", {"content": "x" * 10000})
        self.write(t)
        payload = self.read_ok()
        record = [r for r in payload["records"] if r["tool"] == "Write"][0]
        self.assertTrue(record["truncated"])
        self.assertGreater(payload["coverage"]["bytesOmitted"], 7000)

    # fail closed ------------------------------------------------------------
    def test_non_string_core_text_is_unsupported_format(self) -> None:
        for name, rtype in (("assistant", "assistant"), ("user", "user")):
            with self.subTest(name):
                t = Transcript()
                if rtype == "assistant":
                    t.assistant([{"type": "text", "text": {"unexpected": "object"}}])
                else:
                    t.add(t._base("user", message={"role": "user", "content": [{"type": "text", "text": {"unexpected": "object"}}]}))
                self.write(t)
                code, payload = self.run_cli("--session", SESSION)
                self.assertEqual(code, 2)
                self.assert_error(payload, "unsupported-format")

    def test_unsupported_version_on_skipped_record_is_unsupported_format(self) -> None:
        t = Transcript()
        t.user("hello")
        t.add({"type": "future-thing", "sessionId": SESSION, "version": "9.9.9"})
        self.write(t)
        code, payload = self.run_cli("--session", SESSION)
        self.assertEqual(code, 2)
        self.assertIn("9.9.9", self.assert_error(payload, "unsupported-format"))

    def test_versionless_metadata_and_unknown_types_are_still_skipped(self) -> None:
        t = Transcript()
        t.user("hello")
        t.add({"type": "summary", "summary": "x"})
        t.add({"type": "future-thing", "sessionId": SESSION})
        t.add({"type": "future-thing", "sessionId": SESSION, "version": VERSION})
        self.write(t)
        payload = self.read_ok()
        self.assertEqual(payload["coverage"]["unsupportedSkipped"], 2)

    # read-only ------------------------------------------------------------
    def snapshot(self) -> dict[str, tuple[int, int, bytes | None]]:
        state = {}
        for path in sorted(Path(self._tmp.name).rglob("*")):
            stat = path.stat()
            state[str(path)] = (stat.st_size, stat.st_mtime_ns, path.read_bytes() if path.is_file() else None)
        return state

    def test_reader_leaves_the_fixture_directory_unchanged(self) -> None:
        self.write(self.basic())
        folder = self.project / SESSION / "subagents"
        folder.mkdir(parents=True)
        sub = Transcript()
        sub.user("sub", agentId="zz", isSidechain=True)
        (folder / "agent-zz.jsonl").write_text(sub.text(), encoding="utf-8")
        before = self.snapshot()
        self.read_ok()
        self.run_cli("--session", "no-such-session")
        self.assertEqual(self.snapshot(), before)

    def test_reader_opens_only_for_reading(self) -> None:
        self.write(self.basic())
        modes = []
        real_open = open

        def spy(file, mode="r", *args, **kwargs):
            modes.append(mode)
            return real_open(file, mode, *args, **kwargs)

        with mock.patch("builtins.open", spy):
            self.read_ok()
        self.assertTrue(modes)
        self.assertTrue(all(set(m) <= {"r", "b"} for m in modes), modes)

    def test_cli_requires_session(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(["bridge", "read"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
