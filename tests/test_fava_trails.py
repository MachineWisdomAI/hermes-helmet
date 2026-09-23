#!/usr/bin/env python3
"""Hermetic tests for optional FAVA Trails company-brain integration (H7)."""

from __future__ import annotations

import io
import json
import os
from dataclasses import replace
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from hermes_helmet.authority import load_authority
from hermes_helmet import cli as helmet_cli
from hermes_helmet import fava_trails as ft, public_surface


FIXTURE_POLICY = (
    Path(__file__).resolve().parents[1] / "config/fixtures/exampleco/policy.json"
)
FIXTURE_FAVA = (
    Path(__file__).resolve().parents[1]
    / "config/fixtures/exampleco/fava-trails/company.template.json"
)


def _enabled_policy_path(directory: Path) -> Path:
    raw = json.loads(FIXTURE_POLICY.read_text(encoding="utf-8"))
    raw["integrations"]["fava_trails"] = True
    path = directory / "policy.json"
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return path


class FavaTemplateTests(unittest.TestCase):
    def test_company_template_uses_placeholders_not_private_names(self) -> None:
        policy = load_authority(FIXTURE_POLICY)
        template = ft.company_template_from_policy(policy)
        payload = template.to_public_dict()
        text = json.dumps(payload)
        self.assertEqual(payload["company_slug"], "exampleco")
        self.assertEqual(payload["company_scope"], "exampleco/engineering")
        self.assertEqual(payload["license"], "Apache-2.0")
        self.assertEqual(payload["fava_pin"], ft.FAVA_PIN)
        self.assertIn("captain", payload["principals"])  # type: ignore[operator]
        self.assertIn("executor", payload["principals"])  # type: ignore[operator]
        for forbidden in (
            "time" + "left--",
            "yia-" + "mw-agent",
            "wisdom" + "helm-builder",
            "Machine" + "WisdomAI/www",
            "xai-" + "oauth",
            "grok-4" + ".5",
        ):
            self.assertNotIn(forbidden, text)
        canonical = payload["canonical"]
        assert isinstance(canonical, dict)
        self.assertIn(ft.FAVA_PIN, str(canonical["install"]))
        self.assertIn("governed-recall", str(canonical["governed_recall"]))

    def test_fixture_template_loads(self) -> None:
        template = ft.load_company_template(FIXTURE_FAVA)
        self.assertEqual(template.company_scope, "exampleco/engineering")
        self.assertNotEqual(
            template.principals["captain"], template.principals["executor"]
        )

    def test_principal_templates_are_ordinary_and_distinct(self) -> None:
        template = ft.default_company_template()
        ids = set()
        for principal in ("captain", "executor"):
            payload = ft.example_principal_payload(template, principal)
            self.assertEqual(payload["role"], "ordinary")
            self.assertFalse(payload["operator"])
            ids.add(str(payload["agent_id"]))
            text = json.dumps(payload)
            self.assertNotIn("sk-or-v1-", text)
            self.assertNotIn("OPENROUTER_API_KEY=", text)
        self.assertEqual(len(ids), 2)

    def test_every_template_field_rejects_secret_shaped_material(self) -> None:
        template = ft.default_company_template()
        secret = "sk-" + "examplecredential123456789"
        variants = (
            replace(template, company_slug=secret),
            replace(template, company_scope=f"exampleco/{secret}"),
            replace(template, principals={**template.principals, "captain": secret}),
            replace(template, principals={**template.principals, secret: "ordinary-agent"}),
            replace(template, trust_gate=secret),
            replace(template, license_id=secret),
            replace(template, fava_pin=secret),
            replace(template, fava_version_hint=secret),
        )
        for candidate in variants:
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(ft.FavaTrailsError, "secret"):
                    ft.render_company_template(candidate)


class FavaConfigTests(unittest.TestCase):
    def test_write_principal_is_owner_only_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = ft.default_company_template()
            data_repo = root / "data"
            data_repo.mkdir()
            path = root / "captain.json"
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                cfg = ft.write_principal_config(
                    path,
                    principal="captain",
                    template=template,
                    data_repo=str(data_repo),
                )
            self.assertEqual(cfg.principal, "captain")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("sk-", stdout.getvalue())
            loaded = ft.load_principal_config(path, expected_principal="captain")
            self.assertEqual(loaded.agent_id, cfg.agent_id)
            self.assertFalse(loaded.operator)

    def test_rejects_world_readable_and_operator_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = ft.default_company_template()
            data_repo = root / "data"
            data_repo.mkdir()
            path = root / "executor.json"
            ft.write_principal_config(
                path,
                principal="executor",
                template=template,
                data_repo=str(data_repo),
            )
            path.chmod(0o644)
            with self.assertRaisesRegex(ft.FavaTrailsError, "owner-only"):
                ft.load_principal_config(path, expected_principal="executor")

            path.chmod(0o600)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["operator"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaisesRegex(ft.FavaTrailsError, "operator"):
                ft.load_principal_config(path, expected_principal="executor")

    def test_rejects_secret_values_and_placeholder_data_repo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = ft.default_company_template()
            path = root / "captain.json"
            with self.assertRaisesRegex(ft.FavaTrailsError, "data_repo"):
                # write succeeds with placeholder only if we bypass write helper
                payload = ft.example_principal_payload(template, "captain")
                ft.write_owner_only_json(path, payload)
                ft.load_principal_config(path, expected_principal="captain")

            data_repo = root / "data"
            data_repo.mkdir()
            ft.write_principal_config(
                path,
                principal="captain",
                template=template,
                data_repo=str(data_repo),
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["openrouter_api_key"] = "sk-or-v1-not-a-real-secret-value-123456"
            path.write_text(json.dumps(payload), encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaisesRegex(ft.FavaTrailsError, "secret"):
                ft.load_principal_config(path, expected_principal="captain")

    def test_scaffold_refuses_nonempty_and_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = ft.default_company_template()
            target = root / "data"
            result = ft.prepare_data_repo_scaffold(target, template)
            self.assertTrue(Path(str(result["config"])).is_file())
            text = Path(str(result["config"])).read_text()
            self.assertIn("trails_dir", text)
            self.assertIn(f"name: {template.company_scope}", text)
            with self.assertRaisesRegex(ft.FavaTrailsError, "already present|non-empty"):
                ft.prepare_data_repo_scaffold(target, template)

    def test_rejects_credential_bearing_remote_before_render(self) -> None:
        with self.assertRaisesRegex(ft.FavaTrailsError, "credential"):
            ft.validate_remote_url(
                "https://example-user:s3cret-token@example.invalid/company-data.git"
            )
        with self.assertRaisesRegex(ft.FavaTrailsError, "credential"):
            ft.validate_remote_url("https://token-only@example.invalid/company-data.git")
        # Ordinary credential-free remotes still pass.
        self.assertTrue(
            ft.validate_remote_url("https://github.com/YOUR-ORG/fava-trails-data.git").startswith(
                "https://"
            )
        )
        self.assertTrue(ft.validate_remote_url("git@github.com:YOUR-ORG/fava-trails-data.git"))

    def test_template_and_setup_reject_credential_remote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = "https://example-user:s3cret-token@example.invalid/company-data.git"
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                rc = helmet_cli.main(
                    [
                        "fava",
                        "template",
                        "--config",
                        str(FIXTURE_POLICY),
                        "--remote-url",
                        bad,
                    ]
                )
            self.assertEqual(rc, 1)
            self.assertNotIn("s3cret-token", stdout.getvalue())
            self.assertNotIn(bad, stdout.getvalue())

            rc = helmet_cli.main(
                [
                    "fava",
                    "setup",
                    "--config",
                    str(FIXTURE_POLICY),
                    "--fava-root",
                    str(root),
                    "--write-templates",
                    "--remote-url",
                    bad,
                ]
            )
            self.assertEqual(rc, 1)
            self.assertFalse((root / "company.template.json").exists())

    def test_write_principal_refuses_symlink_tmp_and_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = ft.default_company_template()
            data_repo = root / "data"
            data_repo.mkdir()
            victim = root / "unrelated-settings.json"
            victim.write_text('{"keep": true}\n', encoding="utf-8")
            path = root / "captain.json"
            # Predictable temp-name symlink must not be followed/truncated.
            tmp_alias = path.with_name(path.name + ".tmp")
            tmp_alias.symlink_to(victim)
            # New writer uses unique mkstemp names, so pre-existing alias is left alone.
            cfg = ft.write_principal_config(
                path,
                principal="captain",
                template=template,
                data_repo=str(data_repo),
            )
            self.assertEqual(cfg.principal, "captain")
            self.assertEqual(victim.read_text(encoding="utf-8"), '{"keep": true}\n')
            self.assertTrue(path.is_file())
            self.assertFalse(path.is_symlink())

            # Destination path that is already a symlink is rejected without clobber.
            linked = root / "linked-principal.json"
            linked.symlink_to(victim)
            with self.assertRaisesRegex(ft.FavaTrailsError, "symlink"):
                ft.write_principal_config(
                    linked,
                    principal="executor",
                    template=template,
                    data_repo=str(data_repo),
                )
            self.assertEqual(victim.read_text(encoding="utf-8"), '{"keep": true}\n')
            self.assertTrue(linked.is_symlink())

    def test_write_principal_preserves_existing_parent_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / "shared-config"
            shared.mkdir(mode=0o755)
            os.chmod(shared, 0o755)
            before = shared.stat().st_mode & 0o777
            self.assertEqual(before, 0o755)
            template = ft.default_company_template()
            data_repo = root / "data"
            data_repo.mkdir()
            # Rejected symlink write must not chmod the shared parent.
            linked = shared / "captain.json"
            victim = root / "unrelated.json"
            victim.write_text("{}\n", encoding="utf-8")
            linked.symlink_to(victim)
            with self.assertRaisesRegex(ft.FavaTrailsError, "symlink"):
                ft.write_principal_config(
                    linked,
                    principal="captain",
                    template=template,
                    data_repo=str(data_repo),
                )
            self.assertEqual(shared.stat().st_mode & 0o777, 0o755)
            # Successful write also preserves the pre-existing parent mode.
            ok_path = shared / "executor.json"
            ft.write_principal_config(
                ok_path,
                principal="executor",
                template=template,
                data_repo=str(data_repo),
            )
            self.assertEqual(shared.stat().st_mode & 0o777, 0o755)
            self.assertEqual(ok_path.stat().st_mode & 0o777, 0o600)

    def test_rejects_credential_data_repo_and_secret_env_before_render(self) -> None:
        template = ft.default_company_template()
        bad_repo = "https://example-user:s3cret-token@example.invalid/company-data.git"
        with self.assertRaisesRegex(ft.FavaTrailsError, "credential"):
            ft.example_principal_payload(template, "captain", data_repo=bad_repo)
        with self.assertRaisesRegex(ft.FavaTrailsError, "credential|filesystem path"):
            ft.render_mcp_registration_example(template, "captain", data_repo=bad_repo)
        with self.assertRaisesRegex(ft.FavaTrailsError, "secret|environment variable"):
            ft.validate_env_var_name("sk-or-v1-example-secret-value-not-an-env")
        # Ordinary credential-free path + env name still pass.
        payload = ft.example_principal_payload(
            template, "captain", data_repo="/var/company/fava-data"
        )
        self.assertEqual(payload["data_repo"], "/var/company/fava-data")
        self.assertEqual(
            ft.validate_env_var_name("OPENROUTER_API_KEY"), "OPENROUTER_API_KEY"
        )

    def test_template_cli_rejects_credential_data_repo_override(self) -> None:
        bad = "https://example-user:s3cret-token@example.invalid/company-data.git"
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            rc = helmet_cli.main(
                [
                    "fava",
                    "template",
                    "--config",
                    str(FIXTURE_POLICY),
                    "--principal",
                    "captain",
                    "--data-repo",
                    bad,
                ]
            )
        self.assertEqual(rc, 1)
        self.assertNotIn("s3cret-token", stdout.getvalue())
        self.assertNotIn(bad, stdout.getvalue())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rc = helmet_cli.main(
                [
                    "fava",
                    "setup",
                    "--config",
                    str(FIXTURE_POLICY),
                    "--fava-root",
                    str(root),
                    "--write-templates",
                    "--data-repo",
                    bad,
                ]
            )
            self.assertEqual(rc, 1)
            self.assertFalse((root / "templates" / "captain.principal.json.example").exists())

    def test_generated_config_validates_against_fava_schema(self) -> None:
        fava = ft.try_import_fava()
        self.assertIsNotNone(fava)
        assert fava is not None
        template = ft.default_company_template()
        text = ft.render_data_repo_config_yaml(template)
        import yaml  # type: ignore

        parsed = yaml.safe_load(text)
        cfg = fava["GlobalConfig"].model_validate(parsed)
        self.assertIn(template.company_scope, cfg.trails)
        self.assertEqual(cfg.trails[template.company_scope].name, template.company_scope)

    def test_surface_guard_strips_pin_token_not_whole_line(self) -> None:
        """Allowed pin URL must not conceal a private marker on the same line."""
        repository = "Machine" + "WisdomAI/fava-" + "trails"
        pin_url = (
            f"https://github.com/{repository}/blob/{ft.FAVA_PIN}/README.md"
        )
        private_worker = "yia-" + "mw-agent"
        private_owner = "time" + "left--"
        private_path = "/opt" + "/data/profiles/worker"
        self.assertFalse(public_surface.has_forbidden_public_marker(pin_url))
        for dirty in (
            f"{pin_url} wisdom" + "helm-builder default",
            f"{pin_url}?owner={private_worker}",
            f"{pin_url}#owner={private_worker}",
            f"{pin_url}?checkout={private_path}",
            f"https://github.com/{repository}/tree/{ft.FAVA_PIN}/{private_owner}",
        ):
            with self.subTest(dirty=dirty):
                self.assertTrue(public_surface.has_forbidden_public_marker(dirty))


class FavaDoctorTests(unittest.TestCase):
    def test_declined_integration_skips_ok(self) -> None:
        report = ft.doctor(FIXTURE_POLICY)
        self.assertTrue(report["ok"])
        fava = report["fava_trails"]
        assert isinstance(fava, dict)
        self.assertTrue(fava["skipped"])
        self.assertFalse(fava["enabled"])

    def test_enabled_doctor_requires_principals_and_data_repo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = _enabled_policy_path(root)
            report = ft.doctor(policy_path)
            self.assertFalse(report["ok"])

            template = ft.default_company_template()
            data_repo = root / "fava-data"
            ft.prepare_data_repo_scaffold(data_repo, template)
            paths = ft.default_principal_paths(root / "fava")
            for name, path in paths.items():
                ft.write_principal_config(
                    path,
                    principal=name,
                    template=template,
                    data_repo=str(data_repo),
                )
            report = ft.doctor(policy_path, principal_paths=paths)
            self.assertTrue(report["ok"], report)
            fava = report["fava_trails"]
            assert isinstance(fava, dict)
            self.assertTrue(fava["ok"])
            self.assertEqual(fava["verification_mode"], "offline")
            blob = json.dumps(report)
            self.assertNotIn("sk-or-v1-", blob)

    def test_doctor_fails_closed_on_duplicate_agent_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = _enabled_policy_path(root)
            template = ft.default_company_template()
            data_repo = root / "fava-data"
            ft.prepare_data_repo_scaffold(data_repo, template)
            paths = ft.default_principal_paths(root / "fava")
            for name, path in paths.items():
                ft.write_principal_config(
                    path,
                    principal=name,
                    template=template,
                    data_repo=str(data_repo),
                )
            payload = json.loads(paths["executor"].read_text(encoding="utf-8"))
            payload["agent_id"] = payload and paths["captain"].read_text()
            # Force same agent id as captain.
            cap = json.loads(paths["captain"].read_text(encoding="utf-8"))
            payload["agent_id"] = cap["agent_id"]
            paths["executor"].write_text(json.dumps(payload), encoding="utf-8")
            paths["executor"].chmod(0o600)
            report = ft.doctor(policy_path, principal_paths=paths)
            self.assertFalse(report["ok"])


class FavaLifecycleTests(unittest.TestCase):
    def test_governed_lifecycle_example_ok(self) -> None:
        result = ft.run_governed_lifecycle_example()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result.get("engine"), "accepted-fava-trail-manager")
        steps = {item["step"]: item for item in result["steps"]}  # type: ignore[index]
        self.assertTrue(steps["draft-isolation"]["ok"])
        self.assertTrue(steps["reject-fail-closed"]["ok"])
        self.assertTrue(steps["frozen-approved"]["ok"])
        self.assertTrue(steps["approved-shared-recall"]["ok"])
        self.assertTrue(steps["supersede"]["ok"])
        self.assertTrue(steps["explicit-openviking-promotion"]["ok"])
        self.assertIn("canonical", result)
        self.assertIn(ft.FAVA_PIN, json.dumps(result["canonical"]))
        # Selected root must be canonicalized for env + JJ backend agreement.
        data_repo = Path(str(result["data_repo"]))
        self.assertEqual(str(data_repo), str(data_repo.resolve()))

    def test_lifecycle_normalizes_explicit_data_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="helmet-fava-explicit-") as directory:
            root = Path(directory) / "data"
            result = ft.run_governed_lifecycle_example(data_repo=root, keep_data_repo=True)
            self.assertTrue(result["ok"], result)
            self.assertEqual(Path(str(result["data_repo"])), root.resolve())

    def test_fake_engine_isolation_path_still_available(self) -> None:
        result = ft.run_governed_lifecycle_example(engine=ft.FakeFavaEngine())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result.get("engine"), "fake-fava-lifecycle")

    def test_authoring_isolation_and_history_gate(self) -> None:
        engine = ft.FakeFavaEngine()
        engine.register_principal("cap-a", role="ordinary")
        engine.register_principal("exec-b", role="ordinary")
        draft = engine.save_draft(
            agent_id="cap-a",
            scope="exampleco/engineering",
            content="private draft",
        )
        with self.assertRaisesRegex(ft.FavaTrailsError, "configured principal"):
            engine.propose(draft.thought_id, agent_id="exec-b")
        with self.assertRaisesRegex(ft.FavaTrailsError, "operator"):
            engine.recall(
                scope="exampleco/engineering",
                mode="history",
                agent_id="exec-b",
                include_superseded=True,
            )


class FavaCliTests(unittest.TestCase):
    def test_doctor_cli_skips_when_declined(self) -> None:
        rc = helmet_cli.main(
            ["doctor", "--config", str(FIXTURE_POLICY), "--json"]
        )
        self.assertEqual(rc, 0)

    def test_lifecycle_demo_cli(self) -> None:
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            rc = helmet_cli.main(
                [
                    "fava",
                    "lifecycle-demo",
                    "--config",
                    str(FIXTURE_POLICY),
                    "--json",
                ]
            )
        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])

    def test_setup_write_templates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                rc = helmet_cli.main(
                    [
                        "fava",
                        "setup",
                        "--config",
                        str(FIXTURE_POLICY),
                        "--fava-root",
                        str(root),
                        "--write-templates",
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertTrue((root / "company.template.json").is_file())
            self.assertTrue(
                (root / "templates" / "captain.principal.json.example").is_file()
            )
            text = stdout.getvalue()
            self.assertIn("Canonical install", text)
            self.assertNotIn("sk-or-v1-REAL", text)

    def test_write_principal_cli_redacts_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_repo = root / "data"
            data_repo.mkdir()
            out = root / "principals" / "captain" / "principal.json"
            rc = helmet_cli.main(
                [
                    "fava",
                    "write-principal",
                    "--config",
                    str(FIXTURE_POLICY),
                    "--principal",
                    "captain",
                    "--data-repo",
                    str(data_repo),
                    "--output",
                    str(out),
                ]
            )
            self.assertEqual(rc, 0)
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)


class FavaOptionalRuntimeTests(unittest.TestCase):
    def test_helmet_operates_without_fava_module_side_effects(self) -> None:
        # Doctor on default fixture must succeed with fava skipped.
        report = ft.doctor(FIXTURE_POLICY)
        self.assertTrue(report["ok"])
        # Authority still loads with fava_trails false.
        policy = load_authority(FIXTURE_POLICY)
        self.assertFalse(policy.integrations.fava_trails)

    def test_redact_secrets_strips_credential_shapes(self) -> None:
        text = "OPENROUTER_API_KEY=sk-or-v1-abcdefghijklmnopqrstuvwxyz012345"
        redacted = ft.redact_secrets(text, "sk-or-v1-abcdefghijklmnopqrstuvwxyz012345")
        self.assertNotIn("sk-or-v1-abcdefghijklmnopqrstuvwxyz012345", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_live_probe_handles_missing_cli(self) -> None:
        with mock.patch.object(ft.shutil, "which", return_value=None):
            probe = ft.probe_fava_cli(data_repo="/tmp/does-not-matter")
        self.assertFalse(probe["available"])
        self.assertFalse(probe["ok"])

    def test_live_probe_requires_selected_data_repo(self) -> None:
        with mock.patch.object(ft.shutil, "which", return_value="/usr/bin/fava-trails"):
            probe = ft.probe_fava_cli()
        self.assertTrue(probe["available"])
        self.assertFalse(probe["ok"])
        self.assertIn("selected data repository", str(probe["message"]))

    def test_live_probe_redacts_configured_custom_credential_name(self) -> None:
        import subprocess

        opaque = "opaque-value-without-a-known-token-shape"

        def runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
            self.assertEqual(kwargs["env"]["FAVA_CREDENTIAL"], opaque)
            return subprocess.CompletedProcess(cmd, 1, stdout=opaque, stderr=opaque)

        with mock.patch.object(ft.shutil, "which", return_value="/usr/bin/fava-trails"):
            with mock.patch.dict(os.environ, {"FAVA_CREDENTIAL": opaque}, clear=False):
                probe = ft.probe_fava_cli(
                    data_repo="/selected/company-data",
                    credential_env_name="FAVA_CREDENTIAL",
                    runner=runner,
                )
        self.assertNotIn(opaque, str(probe))
        self.assertIn("[REDACTED]", str(probe))

    def test_live_probe_binds_selected_root_not_ambient(self) -> None:
        import subprocess

        calls: list[dict[str, object]] = []

        def runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
            calls.append({"cmd": cmd, "env": dict(kwargs.get("env") or {}), "cwd": kwargs.get("cwd")})
            return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

        with mock.patch.object(ft.shutil, "which", return_value="/usr/bin/fava-trails"):
            with mock.patch.dict(
                os.environ,
                {
                    "FAVA_TRAILS_DATA_REPO": "/ambient/wrong-root",
                    "FAVA_TRAILS_AGENT_ID": "ambient-agent",
                },
                clear=False,
            ):
                probe = ft.probe_fava_cli(
                    data_repo="/selected/company-data",
                    agent_id="selected-captain",
                    runner=runner,
                )
        self.assertTrue(probe["ok"])
        self.assertEqual(len(calls), 1)
        env = calls[0]["env"]
        assert isinstance(env, dict)
        self.assertEqual(
            env.get("FAVA_TRAILS_DATA_REPO"),
            str(Path("/selected/company-data").resolve()),
        )
        self.assertEqual(env.get("FAVA_TRAILS_AGENT_ID"), "selected-captain")
        self.assertNotEqual(env.get("FAVA_TRAILS_DATA_REPO"), "/ambient/wrong-root")
        self.assertIsNone(env.get("FAVA_TRAILS_OPERATOR"))

    def test_mcp_child_env_does_not_inherit_unrelated_credentials(self) -> None:
        env = ft._bound_env_for_data_repo(
            Path("/selected/company-data"),
            agent_id="selected-captain",
            base={
                "PATH": "/usr/bin",
                "HOME": "/tmp/home",
                "GITHUB_TOKEN": "host-token",
                "AWS_SECRET_ACCESS_KEY": "host-secret",
                "OPENROUTER_API_KEY": "provider-secret",
                "FAVA_TRAILS_DATA_REPO": "/ambient/wrong-root",
            },
        )
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["HOME"], "/tmp/home")
        self.assertEqual(env["FAVA_TRAILS_AGENT_ID"], "selected-captain")
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("OPENROUTER_API_KEY", env)

        selected = ft._bound_env_for_data_repo(
            Path("/selected/company-data"),
            base={"PATH": "/usr/bin", "OPENROUTER_API_KEY": "provider-secret"},
            credential_env_names=("OPENROUTER_API_KEY",),
        )
        self.assertEqual(selected["OPENROUTER_API_KEY"], "provider-secret")

    def test_scope_results_require_ok_status_and_exact_path(self) -> None:
        exact = {"status": "ok", "scopes": [{"path": "exampleco/engineering"}]}
        self.assertEqual(ft._listed_scope_paths(exact), {"exampleco/engineering"})
        self.assertNotIn(
            "exampleco/engineering",
            ft._listed_scope_paths(
                {"status": "ok", "scopes": [{"path": "exampleco/engineering-extra"}]}
            ),
        )
        self.assertEqual(
            ft._listed_scope_paths(
                {"status": "error", "scopes": [{"path": "exampleco/engineering"}]}
            ),
            set(),
        )

    def test_live_doctor_fails_closed_without_mcp_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = _enabled_policy_path(root)
            template = ft.default_company_template()
            data_repo = root / "fava-data"
            ft.prepare_data_repo_scaffold(data_repo, template)
            # Live MCP contact needs a JJ monorepo; init one for readiness realism.
            jj = ft.shutil.which("jj") or str(Path.home() / ".local" / "bin" / "jj")
            if Path(jj).exists():
                import subprocess

                subprocess.run(
                    [jj, "git", "init", "--colocate"],
                    cwd=str(data_repo),
                    check=True,
                    capture_output=True,
                    text=True,
                )
                subprocess.run(
                    [jj, "config", "set", "--repo", "user.name", "Helmet Test"],
                    cwd=str(data_repo),
                    check=True,
                    capture_output=True,
                    text=True,
                )
                subprocess.run(
                    [jj, "config", "set", "--repo", "user.email", "test@example.invalid"],
                    cwd=str(data_repo),
                    check=True,
                    capture_output=True,
                    text=True,
                )
                subprocess.run(
                    [jj, "commit", "-m", "baseline"],
                    cwd=str(data_repo),
                    check=True,
                    capture_output=True,
                    text=True,
                )
            paths = ft.default_principal_paths(root / "fava")
            for name, path in paths.items():
                ft.write_principal_config(
                    path,
                    principal=name,
                    template=template,
                    data_repo=str(data_repo),
                )
            with mock.patch.object(ft, "resolve_fava_server_command", return_value=None):
                report = ft.doctor(policy_path, principal_paths=paths, require_live=True)
            self.assertFalse(report["ok"], report)
            fava = report["fava_trails"]
            assert isinstance(fava, dict)
            self.assertEqual(fava.get("verification_mode"), "live")
            blob = json.dumps(report).casefold()
            # Live mode must fail closed: either MCP contact missing, or FAVA package absent.
            self.assertTrue(
                any(
                    token in blob
                    for token in (
                        "mcp",
                        "connection",
                        "server",
                        "fava",
                        "package",
                        "live principal",
                    )
                ),
                report,
            )

    def test_mcp_contact_reports_connection_attempted(self) -> None:
        cfg = ft.PrincipalConfig(
            principal="captain",
            agent_id="example-captain-fava",
            role="ordinary",
            company_scope="exampleco/engineering",
            data_repo="/tmp/unused",
        )
        with mock.patch.object(ft, "resolve_fava_server_command", return_value=None):
            result = ft.contact_principal_mcp_connection(
                cfg,
                data_repo=Path("/tmp/unused"),
                company_scope="exampleco/engineering",
            )
        self.assertFalse(result["ok"])
        self.assertFalse(result["connection_attempted"])
        self.assertIn("not available", str(result["message"]))


if __name__ == "__main__":
    unittest.main()
