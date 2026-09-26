#!/usr/bin/env python3
"""H10 public configuration, privacy, and documentation boundary."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hermes_helmet import authority, doctor as helmet_doctor, preflight, public_surface
from hermes_helmet.company_skills import STATUS_SKIPPED, validate_company_pack
from hermes_helmet.fava_trails import doctor as fava_doctor
from hermes_helmet.model_lanes import doctor_model_lanes
from hermes_helmet.openviking import doctor as openviking_doctor


ROOT = Path(__file__).resolve().parents[1]
EXAMPLECO = ROOT / "config" / "fixtures" / "exampleco" / "policy.json"

REQUIRED_PUBLIC_FILES = (
    "README.md",
    "LICENSE",
    "NOTICE",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "AGENTS.md",
    "CLAUDE.md",
    "docs/captain-and-crew.md",
    "docs/authority-schema.md",
    "docs/private-overlay.md",
    "docs/quickstart.md",
    "docs/openviking.md",
    "docs/first-officer-plugins.md",
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    ".codex-plugin/plugin.json",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/bug.md",
    ".github/ISSUE_TEMPLATE/feature.md",
)

FORBIDDEN_PUBLIC_MARKERS = (
    "time" + "left--",
    "yia-" + "mw-agent",
    "wisdom" + "helm-builder",
    "xai-" + "oauth",
    "grok-4" + ".5",
)


class PublicBoundaryTests(unittest.TestCase):
    def test_required_public_documents_exist(self) -> None:
        missing = [name for name in REQUIRED_PUBLIC_FILES if not (ROOT / name).is_file()]
        self.assertEqual(missing, [])
        for name in REQUIRED_PUBLIC_FILES:
            self.assertGreater((ROOT / name).stat().st_size, 0, name)

    def test_public_surface_scan_is_clean(self) -> None:
        violations = public_surface.scan_public_surface(ROOT)
        self.assertEqual(violations, [])
        verify = (ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8")
        self.assertIn("scan_public_surface", verify)
        self.assertTrue(
            public_surface.has_forbidden_public_marker("owner=" + "yia-" + "mw-agent")
        )
        self.assertTrue(
            public_surface.has_forbidden_public_marker("login=" + "time" + "left--")
        )
        self.assertFalse(
            public_surface.has_forbidden_public_marker(
                "https://github.com/MachineWisdomAI/hermes-helmet"
            )
        )

    def test_fava_provenance_allowlist_rejects_suffix_smuggling(self) -> None:
        repository = "Machine" + "WisdomAI/fava-" + "trails"
        pin = public_surface.FAVA_PIN
        readme = f"https://github.com/{repository}/blob/{pin}/README.md"
        worker = "yia-" + "mw-agent"
        owner = "time" + "left--"
        private_path = "/opt" + "/data/profiles/worker"
        self.assertFalse(public_surface.has_forbidden_public_marker(readme))
        for smuggled in (
            f"{readme}?owner={worker}",
            f"{readme}#owner={worker}",
            f"{readme}?checkout={private_path}",
            f"https://github.com/{repository}/tree/{pin}/{owner}",
            f"https://github.com/{repository}/tree/{pin}{private_path}",
        ):
            with self.subTest(smuggled=smuggled):
                self.assertTrue(public_surface.has_forbidden_public_marker(smuggled))

    def test_scanner_has_no_module_or_assertion_line_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            marker = "yia-" + "mw-agent"
            (tests / "test_authority.py").write_text(
                f'self.assertNotIn("safe", blob)  # {marker}\n',
                encoding="utf-8",
            )
            with mock.patch.object(public_surface, "SCAN_ROOTS", ("tests",)):
                findings = public_surface.scan_public_surface(root)
        self.assertEqual(len(findings), 1)
        self.assertIn("tests/test_authority.py:1:", findings[0])

    def test_scanner_rejects_quoted_and_tuple_forbidden_values(self) -> None:
        owner = "time" + "left--"
        worker = "yia-" + "mw-agent"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_model_lanes.py").write_text(
                f'    "{owner}",\nWORKER_LOGINS = ("{worker}",)\n',
                encoding="utf-8",
            )
            with mock.patch.object(public_surface, "SCAN_ROOTS", ("tests",)):
                findings = public_surface.scan_public_surface(root)
        self.assertEqual(len(findings), 2)
        self.assertTrue(
            all(item.startswith("tests/test_model_lanes.py:") for item in findings)
        )

    def test_scanner_fails_closed_on_unreadable_text_without_error_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "invalid.txt"
            invalid.write_bytes(b"\xff\xfe")
            with mock.patch.object(public_surface, "SCAN_ROOTS", ("invalid.txt",)):
                findings = public_surface.scan_public_surface(root)
            self.assertEqual(findings, ["invalid.txt:unreadable-text"])

            denied = root / "denied.txt"
            denied.write_text("ordinary text\n", encoding="utf-8")
            original_read_text = Path.read_text

            def fail_read(path: Path, *args: object, **kwargs: object) -> str:
                if path == denied:
                    raise OSError("sensitive filesystem detail")
                return original_read_text(path, *args, **kwargs)  # type: ignore[arg-type]

            with (
                mock.patch.object(public_surface, "SCAN_ROOTS", ("denied.txt",)),
                mock.patch.object(Path, "read_text", fail_read),
            ):
                findings = public_surface.scan_public_surface(root)
        self.assertEqual(findings, ["denied.txt:unreadable-text"])
        self.assertNotIn("sensitive", findings[0])

    def test_python_requires_311_and_temp_paths_are_platform_dependent(self) -> None:
        self.assertGreaterEqual(sys.version_info[:2], (3, 11))
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('requires-python = ">=3.11"', pyproject)
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertIn("3.11", contributing)
        install_skills = (
            ROOT / "src" / "hermes_helmet" / "install_skills.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn('Path("/tmp/', install_skills)
        self.assertIn("tempfile.gettempdir()", install_skills)
        prefix = Path(tempfile.gettempdir()) / "hermes-helmet-skill-validate"
        from hermes_helmet.install_skills import resolve_target_dir

        resolved = resolve_target_dir("hermes", prefix=prefix)
        self.assertTrue(str(resolved).startswith(str(prefix)))

    def test_exampleco_fixture_is_internally_consistent(self) -> None:
        policy = authority.load_authority(EXAMPLECO)
        contract = authority.render_crew_contract(policy)
        task = authority.render_issue_task_body(
            issue_url="https://github.com/example-org/demo-repo/issues/1",
            issue_body="Harden the public boundary",
            policy=policy,
        )
        checks = preflight.assert_worker_ready(
            policy, preflight.StaticIdentityProbe("example-agent")
        )
        self.assertEqual(checks, ("github-worker-identity",))
        with self.assertRaises(preflight.PreflightError):
            preflight.assert_worker_ready(
                policy, preflight.StaticIdentityProbe("example-captain")
            )
        with self.assertRaises(preflight.PreflightError):
            preflight.assert_worker_ready(policy, preflight.StaticIdentityProbe(""))
        with self.assertRaises(preflight.PreflightError):
            preflight.assert_worker_ready(
                policy, preflight.StaticIdentityProbe("unknown-bot")
            )

        for blob in (contract, task, EXAMPLECO.read_text(encoding="utf-8")):
            self.assertIn("example-agent", blob)
            for marker in FORBIDDEN_PUBLIC_MARKERS:
                self.assertNotIn(marker, blob)

        ov = openviking_doctor(EXAMPLECO)
        self.assertTrue(ov["ok"])
        self.assertTrue(ov["openviking"]["skipped"])
        fava = fava_doctor(EXAMPLECO)
        self.assertTrue(fava["ok"])
        self.assertTrue(fava["fava_trails"]["skipped"])
        lanes = doctor_model_lanes(policy)
        self.assertTrue(lanes.ok)
        self.assertTrue(lanes.skipped_optional)

        docs = (ROOT / "docs" / "captain-and-crew.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        schema = (ROOT / "docs" / "authority-schema.md").read_text(encoding="utf-8")
        for blob in (docs, readme, schema):
            self.assertIn("ExampleCo", blob)
            self.assertIn("example-captain", blob)
            self.assertIn("example-agent", blob)
            for marker in FORBIDDEN_PUBLIC_MARKERS:
                self.assertNotIn(marker, blob)

    def test_secrets_stay_out_of_failures_guidance_and_examples(self) -> None:
        secret = "ghp_super_secret_value"
        with self.assertRaises(authority.AuthorityError) as ctx:
            payload = json.loads(EXAMPLECO.read_text(encoding="utf-8"))
            payload["github_token"] = secret
            authority.policy_from_mapping(payload)
        message = str(ctx.exception)
        self.assertIn("github_token", message)
        self.assertNotIn(secret, message)
        examples = [
            ROOT / "config" / "policy.example.json",
            ROOT / "config" / "setup.answers.example.json",
            ROOT / "deploy" / ".env.example",
            ROOT / "docs" / "authority-schema.md",
            ROOT / "docs" / "captain-and-crew.md",
        ]
        for path in examples:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(secret, text)
            self.assertNotIn("sk-live-", text)
            self.assertNotRegex(text, r"ghp_[A-Za-z0-9]{8,}")

    def test_skipped_integrations_are_reported_truthfully(self) -> None:
        policy = authority.load_authority(EXAMPLECO)
        self.assertFalse(policy.skills.configured)
        self.assertFalse(policy.integrations.openviking)
        self.assertFalse(policy.integrations.fava_trails)
        self.assertFalse(policy.integrations.signal)
        skills = helmet_doctor.company_skills_report(policy)
        self.assertTrue(skills["ok"])
        self.assertTrue(skills["skipped"])
        self.assertEqual(skills["status"], STATUS_SKIPPED)
        pack = validate_company_pack(None, ())
        self.assertTrue(pack.ok)
        self.assertEqual(pack.status, STATUS_SKIPPED)
        ov = openviking_doctor(EXAMPLECO)
        self.assertTrue(ov["ok"])
        self.assertTrue(ov["openviking"]["skipped"])
        fava = fava_doctor(EXAMPLECO)
        self.assertTrue(fava["ok"])
        self.assertTrue(fava["fava_trails"]["skipped"])
        signal = helmet_doctor.signal_report(policy)
        self.assertTrue(signal["ok"])
        self.assertTrue(signal["skipped"])
        self.assertFalse(signal["enabled"])
        lanes = doctor_model_lanes(policy).to_public_dict()
        self.assertTrue(lanes["ok"])
        self.assertTrue(lanes["skipped_optional"])

    def test_public_docs_match_behavior_and_keep_private_overlay_optional(self) -> None:
        texts = {
            name: (ROOT / name).read_text(encoding="utf-8")
            for name in (
                "README.md",
                "docs/captain-and-crew.md",
                "docs/quickstart.md",
                "docs/private-overlay.md",
                "docs/openviking.md",
                "NOTICE",
                "CONTRIBUTING.md",
                "SECURITY.md",
                "CODE_OF_CONDUCT.md",
                "CHANGELOG.md",
            )
        }
        readme = texts["README.md"]
        self.assertIn("docs/captain-and-crew.md", readme)
        self.assertIn("CONTRIBUTING.md", readme)
        self.assertIn("SECURITY.md", readme)
        self.assertIn("CODE_OF_CONDUCT.md", readme)
        self.assertIn("CHANGELOG.md", readme)
        self.assertIn("NOTICE", readme)
        self.assertIn("Apache-2.0", readme)
        self.assertIn("AGPL-3.0", readme)
        self.assertIn("AGPL-3.0", texts["docs/openviking.md"])
        self.assertIn("AGPL-3.0", texts["NOTICE"])
        overlay = texts["docs/private-overlay.md"]
        self.assertIn("WisdomHelm", overlay)
        self.assertIn("private overlay", overlay.casefold())
        for name, text in texts.items():
            if name == "docs/private-overlay.md":
                continue
            self.assertNotIn("required WisdomHelm", text)
            self.assertNotIn("WisdomHelm is required", text)
            self.assertNotIn("must install WisdomLoop", text)
        crew = texts["docs/captain-and-crew.md"]
        self.assertIn("Captain", crew)
        self.assertIn("fail closed", crew.casefold())
        self.assertIn("never merge", crew.casefold())
        self.assertIn("six-question", crew.casefold())

    def test_github_templates_are_generic(self) -> None:
        pr = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
        bug = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug.md").read_text(encoding="utf-8")
        feature = (ROOT / ".github" / "ISSUE_TEMPLATE" / "feature.md").read_text(
            encoding="utf-8"
        )
        config = (ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(
            encoding="utf-8"
        )
        for blob in (pr, bug, feature, config):
            for marker in FORBIDDEN_PUBLIC_MARKERS:
                self.assertNotIn(marker, blob)
            self.assertNotIn("WisdomHelm", blob)


if __name__ == "__main__":
    unittest.main()
