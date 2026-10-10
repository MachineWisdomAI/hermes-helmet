#!/usr/bin/env python3
"""Pinned Claude Code CLI and Bridge mod call-allowlist tests (synthetic only)."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hermes_helmet.claude_mod_toolchain import (
    ALLOWED_CALLS,
    ARTIFACTS,
    CLAUDE_CODE_VERSION,
    MINIMUM_CLAUDE_CODE_VERSION,
    ClaudeModToolchainError,
    artifact_platform,
    calls_from_validation,
    check_mod_calls,
    forbidden_calls,
    install,
    require_minimum,
    require_version,
    require_version_output,
    validate_binary,
    verify_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
ISSUE_ALLOWLIST = (
    "$.session.id, $.session.version, $.process.run, $.model.complete, "
    "$.command.register, $.command.list, $.command.run, $.ui.open, $.ui.resolve, "
    "$.ui.status, $.ui.toast, $.state.get, $.state.set, $.clock.after"
)


def _report(*calls_lines: str, success: bool = True) -> str:
    return json.dumps(
        {
            "success": success,
            "contents": [
                {
                    "type": "hooks",
                    "file": "hooks/hooks.json",
                    "errors": [],
                    "notes": ["./register.tsx hooks: session.start", *calls_lines],
                }
            ],
        }
    )


class PinTests(unittest.TestCase):
    def test_pin_is_exact_and_not_below_the_issue_minimum(self) -> None:
        self.assertRegex(CLAUDE_CODE_VERSION, r"^\d+\.\d+\.\d+$")
        self.assertEqual(MINIMUM_CLAUDE_CODE_VERSION, "2.1.293")
        require_minimum(CLAUDE_CODE_VERSION)
        require_minimum("2.1.293")
        with self.assertRaisesRegex(ClaudeModToolchainError, "below the minimum"):
            require_minimum("2.1.292")
        with self.assertRaisesRegex(ClaudeModToolchainError, "unrecognized"):
            require_minimum("latest")

    def test_every_artifact_has_a_sha256_digest(self) -> None:
        self.assertEqual(
            set(ARTIFACTS), {"linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64"}
        )
        for name, digest in ARTIFACTS.items():
            self.assertRegex(digest, r"^[0-9a-f]{64}$", name)

    def test_version_drift_is_rejected(self) -> None:
        require_version(CLAUDE_CODE_VERSION)
        require_version_output(f"{CLAUDE_CODE_VERSION} (Claude Code)\n")
        with self.assertRaisesRegex(ClaudeModToolchainError, "version drift"):
            require_version("2.1.293")
        with self.assertRaisesRegex(ClaudeModToolchainError, "version drift"):
            require_version("latest")
        with self.assertRaisesRegex(ClaudeModToolchainError, "unrecognized"):
            require_version_output(f"prefix {CLAUDE_CODE_VERSION} (Claude Code)\n")
        with self.assertRaisesRegex(ClaudeModToolchainError, "version drift"):
            require_version_output("2.1.2960 (Claude Code)\n")

    def test_invoked_binary_version_is_checked(self) -> None:
        exact = mock.Mock(returncode=0, stdout=f"{CLAUDE_CODE_VERSION} (Claude Code)\n")
        drifted = mock.Mock(returncode=0, stdout="2.1.293 (Claude Code)\n")
        with mock.patch(
            "hermes_helmet.claude_mod_toolchain.subprocess.run", return_value=exact
        ) as run:
            validate_binary(Path("/synthetic/claude"))
        env = run.call_args.kwargs["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["DISABLE_AUTOUPDATER"], "1")
        with mock.patch(
            "hermes_helmet.claude_mod_toolchain.subprocess.run", return_value=drifted
        ), self.assertRaisesRegex(ClaudeModToolchainError, "version drift"):
            validate_binary(Path("/synthetic/claude"))

    def test_checksum_drift_is_rejected(self) -> None:
        payload = b"not-a-claude-binary"
        with self.assertRaisesRegex(ClaudeModToolchainError, "checksum"):
            verify_sha256(payload, ARTIFACTS["linux-x64"])
        verify_sha256(payload, hashlib.sha256(payload).hexdigest())

    def test_install_verifies_before_atomic_replacement(self) -> None:
        payload = b"synthetic-claude-binary"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            ARTIFACTS, {"linux-x64": digest}, clear=True
        ), mock.patch(
            "hermes_helmet.claude_mod_toolchain.artifact_platform", return_value="linux-x64"
        ), mock.patch(
            "hermes_helmet.claude_mod_toolchain.request.urlopen"
        ) as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = payload
            destination = install(bin_dir=Path(directory))
            self.assertEqual(destination.read_bytes(), b"synthetic-claude-binary")
            self.assertTrue(destination.stat().st_mode & 0o111)
            self.assertIn(
                f"/claude-code-releases/{CLAUDE_CODE_VERSION}/linux-x64/claude",
                urlopen.call_args.args[0],
            )
            ARTIFACTS["linux-x64"] = "0" * 64
            destination.unlink()
            with self.assertRaisesRegex(ClaudeModToolchainError, "checksum"):
                install(bin_dir=Path(directory))
            self.assertFalse(destination.exists())
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), [])

    def test_platforms_cover_ci_runners(self) -> None:
        self.assertEqual(artifact_platform(system="Linux", machine="x86_64"), "linux-x64")
        self.assertEqual(artifact_platform(system="Linux", machine="aarch64"), "linux-arm64")
        self.assertEqual(artifact_platform(system="Darwin", machine="arm64"), "darwin-arm64")
        self.assertEqual(artifact_platform(system="Darwin", machine="x86_64"), "darwin-x64")
        with self.assertRaisesRegex(ClaudeModToolchainError, "unsupported"):
            artifact_platform(system="Windows", machine="AMD64")


class CallAllowlistTests(unittest.TestCase):
    def test_allowlist_is_exactly_the_accepted_issue_list(self) -> None:
        self.assertEqual(ALLOWED_CALLS, frozenset(ISSUE_ALLOWLIST.split(", ")))
        self.assertEqual(len(ALLOWED_CALLS), 14)

    def test_calls_within_the_allowlist_pass(self) -> None:
        calls = check_mod_calls(
            _report("./register.tsx calls: $.command.register, $.session.id, $.ui.open")
        )
        self.assertEqual(calls, ["$.command.register", "$.session.id", "$.ui.open"])

    def test_helper_annotations_are_dropped_but_every_call_is_checked(self) -> None:
        calls = check_mod_calls(
            _report(
                "./register.tsx calls: $.command.list (via discoverCommands, runAction), "
                "$.command.run (via runAction), $.ui.open"
            )
        )
        self.assertEqual(calls, ["$.command.list", "$.command.run", "$.ui.open"])
        with self.assertRaisesRegex(ClaudeModToolchainError, r"\$\.fs\.write"):
            check_mod_calls(_report("./register.tsx calls: $.fs.write (via helper), $.ui.open"))
        for text in (
            "./register.tsx calls: $.ui.open (via )",
            "./register.tsx calls: $.ui.open (via a b)",
            "./register.tsx calls: $.ui.open (also $.fs.write)",
            "./register.tsx calls: $.ui.open (via a), ",
        ):
            with self.subTest(text=text), self.assertRaises(ClaudeModToolchainError):
                calls_from_validation(_report(text))

    def test_forbidden_calls_fail_closed(self) -> None:
        for call in (
            "$.fs.write",
            "$.agent.spawn",
            "$.prompt.submit",
            "$.prompt.fill",
            "$.session.send",
            "$.http.fetch",
            "$.tool.call",
            "$.process.spawn",
            "$.store.set",
        ):
            with self.subTest(call=call), self.assertRaisesRegex(
                ClaudeModToolchainError, "outside the read-only allowlist"
            ):
                check_mod_calls(_report(f"./register.tsx calls: $.ui.open, {call}"))
        self.assertEqual(
            forbidden_calls({"m": ["$.ui.open", "$.fs.write"]}), ["$.fs.write"]
        )

    def test_unparseable_validation_output_fails_closed(self) -> None:
        for text in (
            "",
            "not json",
            "[]",
            json.dumps({"success": False, "contents": []}),
            json.dumps({"success": True}),
            json.dumps({"success": True, "contents": []}),
            _report("./register.tsx hooks: session.start"),
            _report("./register.tsx calls: ui.open"),
            _report("./register.tsx calls: "),
            json.dumps(
                {"success": True, "contents": [{"type": "hooks", "notes": "calls: $.ui.open"}]}
            ),
        ):
            with self.subTest(text=text[:40]), self.assertRaises(ClaudeModToolchainError):
                calls_from_validation(text)

    def test_every_module_in_the_report_is_checked(self) -> None:
        report = json.dumps(
            {
                "success": True,
                "contents": [
                    {"type": "hooks", "notes": ["./a.ts calls: $.ui.open"]},
                    {"type": "hooks", "notes": ["./b.ts calls: $.fs.write"]},
                ],
            }
        )
        with self.assertRaisesRegex(ClaudeModToolchainError, r"\$\.fs\.write"):
            check_mod_calls(report)

    def test_shipped_module_source_names_no_forbidden_call(self) -> None:
        # Static belt for hosts without the CLI; CI's real validate is the authority.
        source = (ROOT / "hooks" / "register.tsx").read_text(encoding="utf-8")
        used = set(re.findall(r"\$\.[a-z]+\.[A-Za-z]+(?=\()", source))
        self.assertEqual(sorted(used - ALLOWED_CALLS), [])


class WorkflowWiringTests(unittest.TestCase):
    def test_workflow_installs_and_checks_the_exact_cli_before_verify(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text(encoding="utf-8")
        install_at = workflow.index("hermes_helmet.claude_mod_toolchain --bin-dir")
        check_at = workflow.index("hermes_helmet.claude_mod_toolchain --check-binary")
        verify_at = workflow.index("run: scripts/verify.sh")
        self.assertLess(install_at, check_at)
        self.assertLess(check_at, verify_at)
        self.assertNotIn("npm install", workflow)
        self.assertNotIn("curl", workflow)
        self.assertNotIn("claude.ai/install.sh", workflow)

    def test_verify_runs_real_validate_and_test_and_requires_cli_in_ci(self) -> None:
        verify = (ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8")
        self.assertIn("claude_mod_toolchain", verify)
        self.assertIn("--check-mod", verify)
        self.assertIn('-n "${CI:-}"', verify)
        self.assertIn("Claude Code CLI is required in CI", verify)

    def test_no_account_credentials_reach_the_mod_checks(self) -> None:
        source = (ROOT / "src" / "hermes_helmet" / "claude_mod_toolchain.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("ANTHROPIC_", source)
        self.assertIn("CLAUDE_CODE_OAUTH", source)
        workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text(encoding="utf-8")
        self.assertNotIn("secrets.", workflow)


if __name__ == "__main__":
    unittest.main()
