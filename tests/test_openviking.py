#!/usr/bin/env python3
"""Hermetic tests for optional OpenViking shared-context integration (H6)."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unittest
from unittest import mock
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from hermes_helmet.authority import load_authority
from hermes_helmet import cli as helmet_cli
from hermes_helmet import openviking as ov


FIXTURE_POLICY = (
    Path(__file__).resolve().parents[1] / "config/fixtures/exampleco/policy.json"
)


def _enabled_policy_path(directory: Path) -> Path:
    raw = json.loads(FIXTURE_POLICY.read_text(encoding="utf-8"))
    raw["integrations"]["openviking"] = True
    path = directory / "policy.json"
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return path


def _write_counting_transport(service: ov.FakeOpenVikingService):
    """Wrap fake transport so content writes can be counted independently."""

    writes: list[object] = []
    original = service.transport

    def counting(method, url, headers, payload=None):
        if method == "POST" and str(url).endswith("/api/v1/content/write"):
            writes.append(payload)
        return original(method, url, headers, payload)

    return counting, writes


class OpenVikingTemplateTests(unittest.TestCase):
    def test_company_template_uses_placeholders_not_adopter_names(self) -> None:
        policy = load_authority(FIXTURE_POLICY)
        template = ov.company_template_from_policy(policy)
        payload = template.to_public_dict()
        text = json.dumps(payload)
        self.assertEqual(payload["account"], "exampleco")
        self.assertEqual(payload["user"], "company-context")
        self.assertEqual(payload["license"], "AGPL-3.0")
        self.assertEqual(
            payload["peers"],
            {"codex": "codex", "chatgpt": "chatgpt", "hermes": "hermes"},
        )
        for forbidden in (
            "time" + "left--",
            "yia-" + "mw-agent",
            "machinewisdom",
            "MachineWisdom",
            "WISEMACHINE",
        ):
            self.assertNotIn(forbidden, text)

    def test_client_templates_are_distinct_and_role_user(self) -> None:
        template = ov.default_company_template()
        peers = set()
        for client in ("codex", "chatgpt", "hermes"):
            payload = ov.example_client_config_payload(template, client)
            self.assertEqual(payload["required_role"], "user")
            self.assertEqual(payload["account"], "exampleco")
            peers.add(str(payload["actor_peer_id"]))
            self.assertIn("__OPENVIKING_USER_API_KEY__", str(payload["api_key"]))
        self.assertEqual(peers, {"codex", "chatgpt", "hermes"})


class OpenVikingConfigTests(unittest.TestCase):
    def _write_clients(
        self, root: Path, *, key: str = "user-scoped-key-with-enough-length"
    ) -> dict[str, Path]:
        template = ov.default_company_template()
        paths = ov.default_client_config_paths(root)
        for client, path in paths.items():
            ov.write_client_config(
                path, client=client, template=template, api_key=key
            )
        return paths

    def test_write_client_is_owner_only_and_never_prints_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = "super-secret-user-key-abcdef"
            template = ov.default_company_template()
            path = root / "codex.conf"
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                cfg = ov.write_client_config(
                    path, client="codex", template=template, api_key=key
                )
            self.assertEqual(cfg.peer_id, "codex")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(key, stdout.getvalue())
            loaded = ov.load_client_config(path, expected_client="codex")
            self.assertEqual(loaded.api_key, key)

    def test_rejects_world_readable_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._write_clients(Path(directory))
            path = paths["codex"]
            path.chmod(0o644)
            with self.assertRaisesRegex(ov.OpenVikingError, "owner-only"):
                ov.load_client_config(path, expected_client="codex")

    def test_rejects_blank_duplicate_and_mismatched_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._write_clients(root)
            blank = json.loads(paths["codex"].read_text(encoding="utf-8"))
            blank["actor_peer_id"] = ""
            paths["codex"].write_text(json.dumps(blank), encoding="utf-8")
            paths["codex"].chmod(0o600)
            with self.assertRaisesRegex(ov.OpenVikingError, "peer id"):
                ov.load_client_config(paths["codex"], expected_client="codex")

            paths = self._write_clients(root)
            with self.assertRaisesRegex(ov.OpenVikingError, "mismatched client"):
                ov.load_client_config(paths["codex"], expected_client="hermes")

    def test_rejects_root_key_in_client_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._write_clients(Path(directory))
            payload = json.loads(paths["hermes"].read_text(encoding="utf-8"))
            payload["root_api_key"] = "root-key-must-not-live-here-012345"
            paths["hermes"].write_text(json.dumps(payload), encoding="utf-8")
            paths["hermes"].chmod(0o600)
            with self.assertRaisesRegex(ov.OpenVikingError, "root_api_key"):
                ov.load_client_config(paths["hermes"], expected_client="hermes")

    def test_rejects_non_user_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._write_clients(Path(directory))
            payload = json.loads(paths["chatgpt"].read_text(encoding="utf-8"))
            payload["role"] = "admin"
            paths["chatgpt"].write_text(json.dumps(payload), encoding="utf-8")
            paths["chatgpt"].chmod(0o600)
            with self.assertRaisesRegex(ov.OpenVikingError, "USER"):
                ov.load_client_config(paths["chatgpt"], expected_client="chatgpt")


class OpenVikingDoctorTests(unittest.TestCase):
    def test_declined_integration_skips_ok(self) -> None:
        report = ov.doctor(FIXTURE_POLICY)
        self.assertTrue(report["ok"])
        openviking = report["openviking"]
        assert isinstance(openviking, dict)
        self.assertTrue(openviking["skipped"])
        self.assertFalse(openviking["enabled"])

    def test_enabled_doctor_requires_distinct_shared_clients(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = _enabled_policy_path(root)
            template = ov.default_company_template()
            key = "shared-user-key-with-enough-len"
            paths = ov.default_client_config_paths(root / "ov")
            for client, path in paths.items():
                ov.write_client_config(
                    path, client=client, template=template, api_key=key
                )
            service = ov.FakeOpenVikingService()
            service.register_user_key(
                key, account=template.account, user=template.user, role="user"
            )
            report = ov.doctor(
                policy_path,
                client_paths=paths,
                transport=service.transport,
                require_live=True,
            )
            self.assertTrue(report["ok"], report)
            openviking = report["openviking"]
            assert isinstance(openviking, dict)
            self.assertTrue(openviking["ok"])
            peer_ids = {item["peer_id"] for item in openviking["clients"]}
            self.assertEqual(peer_ids, {"codex", "chatgpt", "hermes"})

    def test_doctor_fails_closed_on_duplicate_peers_without_printing_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = _enabled_policy_path(root)
            template = ov.default_company_template()
            key = "shared-user-key-with-enough-len"
            paths = ov.default_client_config_paths(root / "ov")
            for client, path in paths.items():
                ov.write_client_config(
                    path, client=client, template=template, api_key=key
                )
            # Force duplicate peer ids across clients.
            payload = json.loads(paths["chatgpt"].read_text(encoding="utf-8"))
            payload["actor_peer_id"] = "codex"
            paths["chatgpt"].write_text(json.dumps(payload), encoding="utf-8")
            paths["chatgpt"].chmod(0o600)
            report = ov.doctor(policy_path, client_paths=paths)
            self.assertFalse(report["ok"])
            blob = json.dumps(report)
            self.assertNotIn(key, blob)

    def test_doctor_fails_wrong_account_or_user_live(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = _enabled_policy_path(root)
            template = ov.default_company_template()
            key = "shared-user-key-with-enough-len"
            paths = ov.default_client_config_paths(root / "ov")
            for client, path in paths.items():
                ov.write_client_config(
                    path, client=client, template=template, api_key=key
                )
            service = ov.FakeOpenVikingService()
            service.register_user_key(
                key, account="otherco", user="outsider", role="user"
            )
            report = ov.doctor(
                policy_path,
                client_paths=paths,
                transport=service.transport,
                require_live=True,
            )
            self.assertFalse(report["ok"])
            self.assertNotIn(key, json.dumps(report))

    def test_live_doctor_compares_namespace_to_selected_company_template(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = _enabled_policy_path(root)
            key = "shared-user-key-with-enough-len"
            wrong = ov.default_company_template(
                account="otherco",
                user="outsider",
                service_url="http://127.0.0.1:1933",
            )
            paths = ov.default_client_config_paths(root / "ov")
            for client, path in paths.items():
                ov.write_client_config(
                    path, client=client, template=wrong, api_key=key
                )
            service = ov.FakeOpenVikingService()
            service.register_user_key(
                key, account=wrong.account, user=wrong.user, role="user"
            )
            report = ov.doctor(
                policy_path,
                client_paths=paths,
                transport=service.transport,
                require_live=True,
            )
            self.assertFalse(report["ok"], report)
            openviking = report["openviking"]
            assert isinstance(openviking, dict)
            codes = {item["code"]: item for item in openviking["findings"]}
            self.assertFalse(codes["expected-namespace"]["ok"])
            self.assertNotIn(key, json.dumps(report))

            selected = ov.default_company_template(
                account="acmeops",
                user="ops-context",
                service_url="http://127.0.0.1:1933",
            )
            (root / "ov").mkdir(parents=True, exist_ok=True)
            (root / "ov" / "company.template.json").write_text(
                ov.render_company_template(selected), encoding="utf-8"
            )
            matching_paths = ov.default_client_config_paths(root / "ov")
            for client, path in matching_paths.items():
                ov.write_client_config(
                    path, client=client, template=selected, api_key=key
                )
            matching_service = ov.FakeOpenVikingService()
            matching_service.register_user_key(
                key, account=selected.account, user=selected.user, role="user"
            )
            accepted = ov.doctor(
                policy_path,
                client_paths=matching_paths,
                transport=matching_service.transport,
                require_live=True,
            )
            self.assertTrue(accepted["ok"], accepted)
            accepted_ov = accepted["openviking"]
            assert isinstance(accepted_ov, dict)
            accepted_codes = {
                item["code"]: item for item in accepted_ov["findings"]
            }
            self.assertTrue(accepted_codes["expected-namespace"]["ok"])

    def test_chatgpt_operator_gates_fail_closed_when_missing_or_unsafe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = _enabled_policy_path(root)
            template = ov.default_company_template()
            key = "shared-user-key-with-enough-len"
            paths = ov.default_client_config_paths(root / "ov")
            for client, path in paths.items():
                ov.write_client_config(
                    path, client=client, template=template, api_key=key
                )
            chatgpt_payload = json.loads(paths["chatgpt"].read_text(encoding="utf-8"))
            self.assertIn("chatgpt", chatgpt_payload)
            gates = chatgpt_payload["chatgpt"]
            assert isinstance(gates, dict)
            self.assertEqual(gates["connectivity"], "loopback_or_private_only")
            self.assertTrue(gates["intentional_tool_selection"])
            self.assertFalse(gates["transcript_capture"])
            self.assertFalse(gates["auto_scrape"])
            self.assertIs(gates["operator_gates_required"], True)

            chatgpt_payload.pop("chatgpt")
            paths["chatgpt"].write_text(
                json.dumps(chatgpt_payload), encoding="utf-8"
            )
            paths["chatgpt"].chmod(0o600)
            missing = ov.doctor(policy_path, client_paths=paths)
            self.assertFalse(missing["ok"], missing)
            missing_ov = missing["openviking"]
            assert isinstance(missing_ov, dict)
            missing_codes = {item["code"]: item for item in missing_ov["findings"]}
            self.assertFalse(missing_codes["chatgpt-operator-gates"]["ok"])

            for client, path in paths.items():
                ov.write_client_config(
                    path, client=client, template=template, api_key=key
                )
            unsafe = json.loads(paths["chatgpt"].read_text(encoding="utf-8"))
            unsafe["chatgpt"]["transcript_capture"] = True
            unsafe["chatgpt"]["intentional_tool_selection"] = False
            paths["chatgpt"].write_text(json.dumps(unsafe), encoding="utf-8")
            paths["chatgpt"].chmod(0o600)
            captured = ov.doctor(policy_path, client_paths=paths)
            self.assertFalse(captured["ok"], captured)
            captured_ov = captured["openviking"]
            assert isinstance(captured_ov, dict)
            captured_codes = {
                item["code"]: item for item in captured_ov["findings"]
            }
            self.assertFalse(captured_codes["chatgpt-operator-gates"]["ok"])
            self.assertNotIn(key, json.dumps(captured))

    def test_doctor_fails_closed_offline_on_mixed_old_new_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = _enabled_policy_path(root)
            template = ov.default_company_template()
            old_key = "old-user-key-with-enough-length"
            new_key = "new-user-key-with-enough-length"
            paths = ov.default_client_config_paths(root / "ov")
            for client, path in paths.items():
                ov.write_client_config(
                    path,
                    client=client,
                    template=template,
                    api_key=new_key if client == "chatgpt" else old_key,
                )
            report = ov.doctor(policy_path, client_paths=paths)
            self.assertFalse(report["ok"], report)
            openviking = report["openviking"]
            assert isinstance(openviking, dict)
            self.assertEqual(openviking.get("verification_mode"), "offline")
            codes = {item["code"]: item for item in openviking["findings"]}
            self.assertFalse(codes["shared-user-key"]["ok"])
            blob = json.dumps(report)
            self.assertNotIn(old_key, blob)
            self.assertNotIn(new_key, blob)

            service = ov.FakeOpenVikingService()
            service.register_user_key(
                old_key, account=template.account, user=template.user, role="user"
            )
            service.register_user_key(
                new_key, account=template.account, user=template.user, role="user"
            )
            live = ov.doctor(
                policy_path,
                client_paths=paths,
                transport=service.transport,
                require_live=True,
            )
            self.assertFalse(live["ok"], live)
            live_blob = json.dumps(live)
            self.assertNotIn(old_key, live_blob)
            self.assertNotIn(new_key, live_blob)

    def test_chatgpt_operator_gates_require_operator_gates_required_exactly_true(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = _enabled_policy_path(root)
            template = ov.default_company_template()
            key = "shared-user-key-with-enough-len"
            paths = ov.default_client_config_paths(root / "ov")
            for client, path in paths.items():
                ov.write_client_config(
                    path, client=client, template=template, api_key=key
                )
            payload = json.loads(paths["chatgpt"].read_text(encoding="utf-8"))
            self.assertIs(payload["chatgpt"]["operator_gates_required"], True)
            for invalid in (False, None, "true", 1):
                payload["chatgpt"]["operator_gates_required"] = invalid
                paths["chatgpt"].write_text(json.dumps(payload), encoding="utf-8")
                paths["chatgpt"].chmod(0o600)
                report = ov.doctor(policy_path, client_paths=paths)
                self.assertFalse(report["ok"], report)
                openviking = report["openviking"]
                assert isinstance(openviking, dict)
                codes = {item["code"]: item for item in openviking["findings"]}
                self.assertFalse(codes["chatgpt-operator-gates"]["ok"])
                self.assertNotIn(key, json.dumps(report))


class OpenVikingMarkerTests(unittest.TestCase):
    def test_cross_client_markers_search_recall_read_and_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = ov.default_company_template()
            key = "shared-user-key-with-enough-len"
            paths = ov.default_client_config_paths(root)
            configs = {}
            for client, path in paths.items():
                configs[client] = ov.write_client_config(
                    path, client=client, template=template, api_key=key
                )
            service = ov.FakeOpenVikingService()
            service.register_user_key(
                key, account=template.account, user=template.user, role="user"
            )
            result = ov.cross_client_marker_roundtrip(
                configs, transport=service.transport
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["transport"], "fake")
            self.assertEqual(result["verification_scope"], "synthetic_non_acceptance")
            self.assertFalse(result["acceptance"])
            self.assertIn("synthetic", str(result.get("acceptance_note") or "").casefold())
            markers = result["markers"]
            assert isinstance(markers, dict)
            for client, meta in markers.items():
                view = meta["origin_view"]
                self.assertEqual(view["originating_client"], client)
                self.assertEqual(view["request_header"], ov.ACTOR_PEER_HEADER)
                self.assertFalse(view["stored_jsonl_actor_peer_field"])
                self.assertEqual(view["write_uri_namespace"], "common_user_memory")
                self.assertEqual(
                    view["supported_origin_evidence"],
                    "request_headers_and_client_config_view",
                )
                self.assertIn(f"/{ov.SHARED_MEMORY_TYPE}/", str(meta["uri"]))
                self.assertIn("/memories/", str(meta["uri"]))
                self.assertNotIn("/peers/", str(meta["uri"]))
            self.assertTrue(result["peer_tree_isolation"])
            self.assertEqual(result["shared_namespace"], ov.SHARED_MEMORY_ROOT)
            self.assertEqual(result["intentional_path"], "content_write_common_events_memory")
            limitation = str(result.get("limitation") or "")
            self.assertNotIn("stdio MCP", limitation)
            self.assertNotIn("mcp-stdio", limitation)
            self.assertIn("openviking-memory", limitation)
            self.assertIn("native", limitation.casefold())
            # Request headers captured for each client include actor peer.
            for client in configs:
                headers = service.last_headers[client]
                self.assertEqual(headers[ov.ACTOR_PEER_HEADER], client)
                write_headers = result["write_headers"]
                assert isinstance(write_headers, dict)
                client_headers = write_headers[client]
                assert isinstance(client_headers, dict)
                self.assertEqual(client_headers[ov.ACTOR_PEER_HEADER], client)

    def test_foreign_tenant_cannot_access_shared_markers(self) -> None:
        template = ov.default_company_template()
        key = "shared-user-key-with-enough-len"
        foreign_key = "foreign-user-key-with-enough-len"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ov.default_client_config_paths(root)
            configs = {
                client: ov.write_client_config(
                    path, client=client, template=template, api_key=key
                )
                for client, path in paths.items()
            }
            service = ov.FakeOpenVikingService()
            service.register_user_key(
                key, account=template.account, user=template.user, role="user"
            )
            service.register_user_key(
                foreign_key, account="otherco", user="outsider", role="user"
            )
            ov.cross_client_marker_roundtrip(configs, transport=service.transport)
            foreign = ov.ClientConfig(
                client="codex",
                service_url=template.service_url,
                account="otherco",
                user="outsider",
                peer_id="codex",
                api_key=foreign_key,
            )
            hits = ov.search_markers(
                foreign, "helmet-ov-marker-codex-unique", transport=service.transport
            )
            self.assertEqual(hits, [])

    def test_recall_survives_service_restart_snapshot(self) -> None:
        template = ov.default_company_template()
        key = "shared-user-key-with-enough-len"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configs = {
                client: ov.write_client_config(
                    path,
                    client=client,
                    template=template,
                    api_key=key,
                )
                for client, path in ov.default_client_config_paths(root).items()
            }
            service = ov.FakeOpenVikingService()
            service.register_user_key(
                key, account=template.account, user=template.user, role="user"
            )
            proof = ov.cross_client_marker_roundtrip(
                configs, transport=service.transport
            )
            self.assertTrue(proof["ok"])
            self.assertEqual(proof["transport"], "fake")
            self.assertEqual(proof["verification_scope"], "synthetic_non_acceptance")
            self.assertFalse(proof["acceptance"])
            snap = service.snapshot()
            restarted = ov.FakeOpenVikingService()
            restarted.restore(snap)
            for client, config in configs.items():
                token = f"helmet-ov-marker-{client}-unique"
                recalled = ov.recall_markers(
                    configs["hermes"], token, transport=restarted.transport
                )
                self.assertTrue(recalled, client)


class OpenVikingCliTests(unittest.TestCase):
    def test_cli_doctor_skipped_when_declined(self) -> None:
        code = helmet_cli.main(
            ["doctor", "--config", str(FIXTURE_POLICY), "--json"]
        )
        self.assertEqual(code, 0)

    def test_cli_template_and_setup_omit_secrets(self) -> None:
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            code = helmet_cli.main(
                [
                    "openviking",
                    "template",
                    "--config",
                    str(FIXTURE_POLICY),
                    "--client",
                    "chatgpt",
                ]
            )
        self.assertEqual(code, 0)
        out = stdout.getvalue()
        self.assertIn("AGPL-3.0", out)
        self.assertIn("actor_peer_id", out)
        self.assertIn("chatgpt", out)
        self.assertNotRegex(out, r"[0-9a-fA-F]{32}")

    def test_cli_write_client_never_echoes_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "user.key"
            key = "cli-user-key-with-enough-length"
            key_file.write_text(key + "\n", encoding="utf-8")
            key_file.chmod(0o600)
            output = root / "hermes.conf"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
                code = helmet_cli.main(
                    [
                        "openviking",
                        "write-client",
                        "--config",
                        str(FIXTURE_POLICY),
                        "--client",
                        "hermes",
                        "--api-key-file",
                        str(key_file),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(code, 0)
            displayed = stdout.getvalue() + stderr.getvalue()
            self.assertNotIn(key, displayed)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_cli_write_client_rejects_world_readable_key_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "user.key"
            key = "cli-user-key-with-enough-length"
            key_file.write_text(key + "\n", encoding="utf-8")
            key_file.chmod(0o644)
            output = root / "hermes.conf"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
                code = helmet_cli.main(
                    [
                        "openviking",
                        "write-client",
                        "--config",
                        str(FIXTURE_POLICY),
                        "--client",
                        "hermes",
                        "--api-key-file",
                        str(key_file),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(code, 1)
            displayed = stdout.getvalue() + stderr.getvalue()
            self.assertNotIn(key, displayed)
            self.assertFalse(output.exists())

    def test_redact_secrets_strips_known_values(self) -> None:
        secret = "abcd1234secretvalue"
        text = ov.redact_secrets(f"api_key={secret} used detail={secret}", secret)
        self.assertNotIn(secret, text)
        self.assertIn("[REDACTED]", text)

    def test_public_view_rejects_credential_url_userinfo(self) -> None:
        with self.assertRaisesRegex(ov.OpenVikingError, "credentials"):
            ov.validate_service_url("http://user:secret-key@127.0.0.1:1933")

    def test_private_host_validation_is_address_based(self) -> None:
        self.assertTrue(ov.is_private_or_loopback_host("127.0.0.1"))
        self.assertTrue(ov.is_private_or_loopback_host("192.168.20.4"))
        self.assertTrue(ov.is_private_or_loopback_host("10.0.0.5"))
        self.assertFalse(ov.is_private_or_loopback_host("10.attacker.example"))
        self.assertFalse(ov.is_private_or_loopback_host("example.com"))

    def test_http_transport_redacts_echoed_key_and_blocks_cross_origin_redirect(self) -> None:
        secret = "synthetic-live-key-abcdef012345"
        body = json.dumps({"detail": f"bad key {secret}"}).encode("utf-8")
        error = urlerror.HTTPError(
            url="http://127.0.0.1:1933/health",
            code=401,
            msg="unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(body),
        )
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=error):
            with self.assertRaises(ov.OpenVikingError) as raised:
                ov.http_transport(
                    "GET",
                    "http://127.0.0.1:1933/health",
                    {
                        ov.API_KEY_HEADER: secret,
                        "Authorization": f"Bearer {secret}",
                    },
                    None,
                    known_secrets=(secret,),
                )
        self.assertNotIn(secret, str(raised.exception))

        handler = ov._OriginBoundRedirectHandler()
        req = urlrequest.Request(
            "http://127.0.0.1:1933/health",
            headers={ov.API_KEY_HEADER: secret, "Authorization": f"Bearer {secret}"},
        )
        with self.assertRaisesRegex(ov.OpenVikingError, "cross-origin redirect"):
            handler.redirect_request(
                req,
                fp=io.BytesIO(b""),
                code=302,
                msg="Found",
                headers=urlrequest.Request("http://127.0.0.1/").headers,
                newurl="http://10.0.0.9/health",
            )


class OpenVikingOptionalRuntimeTests(unittest.TestCase):
    def test_import_and_authority_work_without_openviking_enabled(self) -> None:
        policy = load_authority(FIXTURE_POLICY)
        self.assertFalse(policy.integrations.openviking)
        # doctor skip path already covered; ensure setup messages still render.
        messages = ov.setup_messages(ov.company_template_from_policy(policy))
        self.assertTrue(any("AGPL-3.0" in line for line in messages))
        self.assertTrue(any("FAVA" in line for line in messages))
        self.assertTrue(any("declined or unavailable" in line for line in messages))
        self.assertTrue(any("shared-all" in line for line in messages))
        self.assertTrue(any("Captain/operator" in line for line in messages))
        self.assertTrue(any("Rotate by rewriting every owner-only client config" in line for line in messages))

    def test_chatgpt_checklist_has_operator_gates(self) -> None:
        checklist = ov.chatgpt_operator_checklist()
        blob = " ".join(checklist).casefold()
        self.assertIn("tunnel", blob)
        self.assertIn("intentional", blob)
        self.assertIn("transcript", blob)
        self.assertIn("loopback", blob)

    def test_custom_peer_ids_keep_fixed_client_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw = json.loads(FIXTURE_POLICY.read_text(encoding="utf-8"))
            raw["integrations"]["openviking"] = True
            raw["openviking_peers"] = [
                {"id": "peer-codex-a"},
                {"id": "peer-chatgpt-b"},
                {"id": "peer-hermes-c"},
            ]
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            policy = load_authority(path)
            template = ov.company_template_from_policy(policy)
            self.assertEqual(
                dict(template.peers),
                {
                    "codex": "peer-codex-a",
                    "chatgpt": "peer-chatgpt-b",
                    "hermes": "peer-hermes-c",
                },
            )
            for client in ("codex", "chatgpt", "hermes"):
                payload = ov.example_client_config_payload(template, client)
                self.assertEqual(payload["client"], client)
                self.assertEqual(payload["actor_peer_id"], template.peers[client])

    def test_doctor_accepts_shared_custom_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = _enabled_policy_path(root)
            template = ov.default_company_template(
                account="acmeops",
                user="ops-context",
                service_url="http://127.0.0.1:1933",
            )
            key = "shared-user-key-with-enough-len"
            ov_root = root / "ov"
            ov_root.mkdir(parents=True, exist_ok=True)
            (ov_root / "company.template.json").write_text(
                ov.render_company_template(template), encoding="utf-8"
            )
            paths = ov.default_client_config_paths(ov_root)
            for client, path in paths.items():
                ov.write_client_config(
                    path, client=client, template=template, api_key=key
                )
            report = ov.doctor(policy_path, client_paths=paths)
            self.assertTrue(report["ok"], report)
            openviking = report["openviking"]
            assert isinstance(openviking, dict)
            self.assertEqual(openviking["verification_mode"], "offline")
            codes = {item["code"]: item for item in openviking["findings"]}
            self.assertTrue(codes["expected-namespace"]["ok"], codes["expected-namespace"])
            self.assertIn("identity-verification", codes)
            self.assertIn("offline", codes["identity-verification"]["message"])
            self.assertNotIn("live-identity", codes)

            explicit = ov.doctor(
                policy_path,
                client_paths=paths,
                expected_template=template,
            )
            self.assertTrue(explicit["ok"], explicit)

    def test_offline_doctor_rejects_namespace_mismatch_against_selected_template(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = _enabled_policy_path(root)
            wrong = ov.default_company_template(
                account="otherco",
                user="outsider",
                service_url="http://127.0.0.1:1933",
            )
            key = "shared-user-key-with-enough-len"
            paths = ov.default_client_config_paths(root / "ov")
            for client, path in paths.items():
                ov.write_client_config(
                    path, client=client, template=wrong, api_key=key
                )
            report = ov.doctor(policy_path, client_paths=paths)
            self.assertFalse(report["ok"], report)
            openviking = report["openviking"]
            assert isinstance(openviking, dict)
            self.assertEqual(openviking["verification_mode"], "offline")
            codes = {item["code"]: item for item in openviking["findings"]}
            self.assertTrue(codes["shared-namespace"]["ok"])
            self.assertFalse(codes["expected-namespace"]["ok"])
            self.assertIn("identity-verification", codes)
            self.assertNotIn("live-identity", codes)
            self.assertNotIn(key, json.dumps(report))

    def test_doctor_rejects_hermes_contradiction_and_public_dns_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = _enabled_policy_path(root)
            template = ov.default_company_template()
            key = "shared-user-key-with-enough-len"
            paths = ov.default_client_config_paths(root / "ov")
            for client, path in paths.items():
                ov.write_client_config(
                    path, client=client, template=template, api_key=key
                )
            payload = json.loads(paths["hermes"].read_text(encoding="utf-8"))
            payload["actor_peer_id"] = "hermes"
            payload["OPENVIKING_AGENT"] = "codex"
            paths["hermes"].write_text(json.dumps(payload), encoding="utf-8")
            paths["hermes"].chmod(0o600)
            report = ov.doctor(policy_path, client_paths=paths)
            self.assertFalse(report["ok"])
            self.assertNotIn(key, json.dumps(report))

            paths = ov.default_client_config_paths(root / "ov2")
            for client, path in paths.items():
                ov.write_client_config(
                    path, client=client, template=template, api_key=key
                )
            payload = json.loads(paths["chatgpt"].read_text(encoding="utf-8"))
            payload["url"] = "http://10.attacker.example:1933"
            paths["chatgpt"].write_text(json.dumps(payload), encoding="utf-8")
            paths["chatgpt"].chmod(0o600)
            report = ov.doctor(policy_path, client_paths=paths)
            self.assertFalse(report["ok"])
            blob = json.dumps(report)
            self.assertIn("chatgpt-connectivity", blob)
            self.assertNotIn(key, blob)

    def test_peer_tree_is_isolated_while_common_memory_is_shared(self) -> None:
        template = ov.default_company_template()
        key = "shared-user-key-with-enough-len"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configs = {
                client: ov.write_client_config(
                    path, client=client, template=template, api_key=key
                )
                for client, path in ov.default_client_config_paths(root).items()
            }
            service = ov.FakeOpenVikingService()
            service.register_user_key(
                key, account=template.account, user=template.user, role="user"
            )
            private_uri, _ = ov.write_synthetic_marker(
                configs["hermes"],
                "peer-private-hermes-only",
                transport=service.transport,
                shared=False,
            )
            self.assertIn("/peers/hermes/", private_uri)
            with self.assertRaisesRegex(ov.OpenVikingError, "403|another peer"):
                ov.read_marker(configs["codex"], private_uri, transport=service.transport)
            shared = ov.cross_client_marker_roundtrip(
                configs, transport=service.transport
            )
            self.assertTrue(shared["ok"])

    def test_rejects_unknown_query_name_bearing_known_key(self) -> None:
        key = "shared-user-key-with-enough-len"
        with self.assertRaisesRegex(ov.OpenVikingError, "secrets on the URL"):
            ov.validate_service_url(
                f"http://127.0.0.1:1933?q={key}",
                known_secrets=(key,),
            )
        with self.assertRaisesRegex(ov.OpenVikingError, "secrets on the URL"):
            ov.http_transport(
                "GET",
                f"http://127.0.0.1:1933/health?q={key}",
                {ov.API_KEY_HEADER: key, "Authorization": f"Bearer {key}"},
                None,
                known_secrets=(key,),
            )
        # Public views strip query entirely even if validation was bypassed.
        view = ov.ClientConfig(
            client="codex",
            service_url=f"http://127.0.0.1:1933?q={key}",
            account="exampleco",
            user="company-context",
            peer_id="codex",
            api_key=key,
        ).public_view()
        self.assertNotIn(key, json.dumps(view))
        self.assertNotIn("?", str(view["service_url"]))

    def test_mixed_peer_list_reserves_fixed_client_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw = json.loads(FIXTURE_POLICY.read_text(encoding="utf-8"))
            raw["integrations"]["openviking"] = True
            raw["openviking_peers"] = [
                {"id": "chatgpt"},
                {"id": "desktop-codex"},
                {"id": "hermes"},
            ]
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            policy = load_authority(path)
            template = ov.company_template_from_policy(policy)
            self.assertEqual(
                dict(template.peers),
                {
                    "codex": "desktop-codex",
                    "chatgpt": "chatgpt",
                    "hermes": "hermes",
                },
            )

    def test_adopter_company_template_roundtrip_to_write_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = _enabled_policy_path(root)
            policy = load_authority(policy_path)
            template = ov.resolve_company_template(
                policy,
                account="acmeops",
                user="ops-context",
                service_url="http://127.0.0.1:1933",
            )
            company_path = root / "company.template.json"
            company_path.write_text(ov.render_company_template(template), encoding="utf-8")
            loaded = ov.load_company_template(company_path)
            self.assertEqual(loaded.account, "acmeops")
            self.assertEqual(loaded.user, "ops-context")
            key = "shared-user-key-with-enough-len"
            cfg = ov.write_client_config(
                root / "clients" / "codex" / "ovcli.conf",
                client="codex",
                template=loaded,
                api_key=key,
            )
            self.assertEqual(cfg.account, "acmeops")
            self.assertEqual(cfg.user, "ops-context")

    def test_rejects_hermes_conflicting_credential_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ov.default_client_config_paths(Path(directory))
            template = ov.default_company_template()
            key = "shared-user-key-with-enough-len"
            other = "other-user-key-with-enough-len"
            ov.write_client_config(
                paths["hermes"], client="hermes", template=template, api_key=key
            )
            payload = json.loads(paths["hermes"].read_text(encoding="utf-8"))
            payload["OPENVIKING_API_KEY"] = other
            paths["hermes"].write_text(json.dumps(payload), encoding="utf-8")
            paths["hermes"].chmod(0o600)
            with self.assertRaisesRegex(ov.OpenVikingError, "credential aliases"):
                ov.load_client_config(paths["hermes"], expected_client="hermes")
            payload["OPENVIKING_API_KEY"] = key
            payload["hermes"] = {
                "OPENVIKING_API_KEY": other,
                "OPENVIKING_AGENT": "hermes",
                "OPENVIKING_ENDPOINT": template.service_url,
            }
            paths["hermes"].write_text(json.dumps(payload), encoding="utf-8")
            paths["hermes"].chmod(0o600)
            with self.assertRaisesRegex(ov.OpenVikingError, "credential aliases"):
                ov.load_client_config(paths["hermes"], expected_client="hermes")

    def test_decodes_real_search_and_recall_envelopes(self) -> None:
        search_hits = ov._decode_search_result(
            {
                "memories": [{"uri": "viking://~/memories/events/a.md", "abstract": "token-a"}],
                "resources": [],
                "skills": [],
                "total": 1,
            }
        )
        self.assertEqual(len(search_hits), 1)
        self.assertIn("token-a", str(search_hits[0].get("content")))
        recall_hits = ov._decode_recall_result(
            {
                "entries": [
                    {
                        "uri": "viking://~/memories/events/a.md",
                        "content": "token-a",
                        "type": "events",
                    }
                ],
                "rendered": "token-a",
                "stats": {"count": 1},
            }
        )
        self.assertEqual(len(recall_hits), 1)
        self.assertIn("token-a", str(recall_hits[0].get("content")))

    def test_cli_shared_proof_exposes_intentional_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = ov.default_company_template()
            key = "shared-user-key-with-enough-len"
            paths = ov.default_client_config_paths(root / "ov")
            for client, path in paths.items():
                ov.write_client_config(
                    path, client=client, template=template, api_key=key
                )
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                code = helmet_cli.main(
                    [
                        "openviking",
                        "shared-proof",
                        "--openviking-root",
                        str(root / "ov"),
                        "--transport",
                        "fake",
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["shared_memory_type"], "events")
            self.assertEqual(payload["transport"], "fake")
            self.assertEqual(payload["verification_scope"], "synthetic_non_acceptance")
            self.assertFalse(payload["acceptance"])
            self.assertIn("synthetic", str(payload.get("acceptance_note") or "").casefold())
            self.assertIn("non-acceptance", str(payload.get("acceptance_note") or "").casefold())
            self.assertNotIn(key, stdout.getvalue())

    def test_rejects_ordinarily_urlencoded_known_key(self) -> None:
        # Opaque key with +/= must not bypass admission via urlencode.
        key = "syn+key/with=paddingAA"
        encoded = __import__("urllib.parse", fromlist=["urlencode"]).urlencode({"q": key})
        self.assertNotEqual(key, encoded.split("=", 1)[-1])
        with self.assertRaisesRegex(ov.OpenVikingError, "secrets on the URL"):
            ov.validate_service_url(
                f"http://127.0.0.1:1933?{encoded}",
                known_secrets=(key,),
            )
        with self.assertRaisesRegex(ov.OpenVikingError, "secrets on the URL"):
            ov.http_transport(
                "GET",
                f"http://127.0.0.1:1933/health?{encoded}",
                {ov.API_KEY_HEADER: key, "Authorization": f"Bearer {key}"},
                None,
                known_secrets=(key,),
            )
        quote = urlparse.quote
        upper_encoded = quote(key, safe="")
        # Independent hex-letter case at each escape (2^3=8 for + / =).
        escape_matches = list(re.finditer(r"%([0-9A-Fa-f]{2})", upper_encoded))
        self.assertGreaterEqual(len(escape_matches), 3)
        letter_escapes = [
            match for match in escape_matches if any(ch.isalpha() for ch in match.group(1))
        ]
        self.assertGreaterEqual(len(letter_escapes), 3)
        focus = letter_escapes[:3]
        mixed_spellings: list[str] = []
        for mask in range(1 << len(focus)):
            chars = list(upper_encoded)
            for bit, match in enumerate(focus):
                hex_digits = match.group(1)
                cased = hex_digits.lower() if mask & (1 << bit) else hex_digits.upper()
                start = match.start() + 1
                chars[start : start + 2] = list(cased)
            mixed_spellings.append("".join(chars))
        self.assertEqual(len(set(mixed_spellings)), 8)
        for spelling in mixed_spellings:
            detail = f"HTTP 401 detail bad key {spelling}"
            redacted = ov.redact_secrets(detail, key)
            self.assertNotIn(key, redacted, spelling)
            self.assertNotIn(spelling, redacted, spelling)
            decoded_spelling = urlparse.unquote(spelling)
            self.assertNotIn(decoded_spelling, redacted)
            self.assertIn("[REDACTED]", redacted)
            self.assertEqual(decoded_spelling, key)
        # Exact plaintext still redacts.
        plain = ov.redact_secrets(f"HTTP 401 detail bad key {key}", key)
        self.assertNotIn(key, plain)
        self.assertIn("[REDACTED]", plain)
        # Plaintext key matching remains case-sensitive.
        mixed = key[:3].swapcase() + key[3:]
        if mixed != key:
            still = ov.redact_secrets(f"detail={mixed}", key)
            self.assertIn(mixed, still)

    def test_shared_proof_rejects_collapsed_peer_ids_before_write(self) -> None:
        template = ov.default_company_template()
        key = "shared-user-key-with-enough-len"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ov.default_client_config_paths(root)
            configs = {
                client: ov.write_client_config(
                    path, client=client, template=template, api_key=key
                )
                for client, path in paths.items()
            }
            # Collapse all peers to the same provenance id.
            collapsed = {
                name: ov.ClientConfig(
                    client=cfg.client,
                    service_url=cfg.service_url,
                    account=cfg.account,
                    user=cfg.user,
                    peer_id="shared-peer",
                    api_key=cfg.api_key,
                    path=cfg.path,
                )
                for name, cfg in configs.items()
            }
            service = ov.FakeOpenVikingService()
            service.register_user_key(
                key, account=template.account, user=template.user, role="user"
            )
            with self.assertRaisesRegex(ov.OpenVikingError, "pairwise-distinct"):
                ov.cross_client_marker_roundtrip(
                    collapsed, transport=service.transport
                )
            self.assertEqual(len(service.records), 0)

            # CLI path also fails closed before mutation.
            for client, path in paths.items():
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["actor_peer_id"] = "shared-peer"
                if client == "hermes":
                    payload["OPENVIKING_AGENT"] = "shared-peer"
                    nested = payload.get("hermes")
                    if isinstance(nested, dict):
                        nested["OPENVIKING_AGENT"] = "shared-peer"
                path.write_text(json.dumps(payload), encoding="utf-8")
                path.chmod(0o600)
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr), mock.patch("sys.stdout", io.StringIO()):
                code = helmet_cli.main(
                    [
                        "openviking",
                        "shared-proof",
                        "--openviking-root",
                        str(root),
                        "--transport",
                        "fake",
                    ]
                )
            self.assertEqual(code, 1)
            self.assertNotIn(key, stderr.getvalue())

    def test_shared_proof_rejects_forbidden_identity_before_write(self) -> None:
        template = ov.default_company_template()
        key = "shared-user-key-with-enough-len"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configs = {
                client: ov.write_client_config(
                    path, client=client, template=template, api_key=key
                )
                for client, path in ov.default_client_config_paths(root).items()
            }
            writes: list[object] = []
            for role in ("root", "admin"):
                service = ov.FakeOpenVikingService()
                service.register_user_key(
                    key, account=template.account, user=template.user, role=role
                )
                original = service.transport

                def counting(method, url, headers, payload=None, _original=original):
                    if method == "POST" and str(url).endswith("/api/v1/content/write"):
                        writes.append(payload)
                    return _original(method, url, headers, payload)

                writes.clear()
                with self.assertRaisesRegex(ov.OpenVikingError, "USER"):
                    ov.cross_client_marker_roundtrip(configs, transport=counting)
                self.assertEqual(writes, [], role)
                self.assertEqual(len(service.records), 0, role)

            wrong = ov.FakeOpenVikingService()
            wrong.register_user_key(
                key, account="otherco", user="outsider", role="user"
            )
            original_wrong = wrong.transport
            wrong_writes: list[object] = []

            def counting_wrong(method, url, headers, payload=None):
                if method == "POST" and str(url).endswith("/api/v1/content/write"):
                    wrong_writes.append(payload)
                return original_wrong(method, url, headers, payload)

            with self.assertRaisesRegex(ov.OpenVikingError, "wrong-account|wrong-user"):
                ov.cross_client_marker_roundtrip(configs, transport=counting_wrong)
            self.assertEqual(wrong_writes, [])
            self.assertEqual(len(wrong.records), 0)

    def test_write_marker_rejects_forbidden_identity_before_write(self) -> None:
        template = ov.default_company_template()
        key = "shared-user-key-with-enough-len"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = ov.write_client_config(
                ov.default_client_config_paths(root)["codex"],
                client="codex",
                template=template,
                api_key=key,
            )
            for role in ("root", "admin"):
                service = ov.FakeOpenVikingService()
                service.register_user_key(
                    key, account=template.account, user=template.user, role=role
                )
                counting, writes = _write_counting_transport(service)
                with self.assertRaisesRegex(ov.OpenVikingError, "USER"):
                    ov.intentional_shared_marker_write(
                        config, "should-not-write", transport=counting
                    )
                self.assertEqual(writes, [], role)
                self.assertEqual(len(service.records), 0, role)

            wrong = ov.FakeOpenVikingService()
            wrong.register_user_key(
                key, account="otherco", user="outsider", role="user"
            )
            counting_wrong, wrong_writes = _write_counting_transport(wrong)
            with self.assertRaisesRegex(ov.OpenVikingError, "wrong-account|wrong-user"):
                ov.intentional_shared_marker_write(
                    config, "should-not-write", transport=counting_wrong
                )
            self.assertEqual(wrong_writes, [])
            self.assertEqual(len(wrong.records), 0)

            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                code = helmet_cli.main(
                    [
                        "openviking",
                        "write-marker",
                        "--client",
                        "codex",
                        "--client-config",
                        str(config.path),
                        "--text",
                        "cli-demo-marker",
                        "--transport",
                        "fake",
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["transport"], "fake")
            self.assertEqual(payload["verification_scope"], "synthetic_non_acceptance")
            self.assertFalse(payload["acceptance"])
            self.assertIn(
                "synthetic", str(payload.get("acceptance_note") or "").casefold()
            )
            self.assertIn(
                "non-acceptance", str(payload.get("acceptance_note") or "").casefold()
            )
            self.assertNotIn(key, stdout.getvalue())

    def test_write_marker_rejects_unsafe_chatgpt_gates_before_probe_or_write(
        self,
    ) -> None:
        template = ov.default_company_template()
        key = "shared-user-key-with-enough-len"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = ov.default_client_config_paths(root)["chatgpt"]

            def assert_zero_probe_and_write(mutator, pattern: str) -> None:
                ov.write_client_config(
                    path, client="chatgpt", template=template, api_key=key
                )
                payload = json.loads(path.read_text(encoding="utf-8"))
                mutator(payload)
                path.write_text(json.dumps(payload), encoding="utf-8")
                path.chmod(0o600)
                config = ov.load_client_config(path, expected_client="chatgpt")
                service = ov.FakeOpenVikingService()
                service.register_user_key(
                    key, account=template.account, user=template.user, role="user"
                )
                original = service.transport
                probes: list[str] = []
                writes: list[object] = []

                def counting(method, url, headers, payload=None):
                    if method == "GET" and str(url).endswith("/health"):
                        probes.append(str(url))
                    if method == "POST" and str(url).endswith("/api/v1/content/write"):
                        writes.append(payload)
                    return original(method, url, headers, payload)

                with self.assertRaisesRegex(ov.OpenVikingError, pattern):
                    ov.intentional_shared_marker_write(
                        config, "should-not-write", transport=counting
                    )
                self.assertEqual(probes, [])
                self.assertEqual(writes, [])
                self.assertEqual(len(service.records), 0)

            assert_zero_probe_and_write(lambda raw: raw.pop("chatgpt"), "operator gates")
            assert_zero_probe_and_write(
                lambda raw: raw["chatgpt"].__setitem__("transcript_capture", True),
                "transcript",
            )
            assert_zero_probe_and_write(
                lambda raw: raw["chatgpt"].__setitem__(
                    "operator_gates_required", False
                ),
                "operator_gates_required",
            )

            ov.write_client_config(
                path, client="chatgpt", template=template, api_key=key
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["chatgpt"]["transcript_capture"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            path.chmod(0o600)
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr), mock.patch(
                "sys.stdout", io.StringIO()
            ):
                code = helmet_cli.main(
                    [
                        "openviking",
                        "write-marker",
                        "--client",
                        "chatgpt",
                        "--client-config",
                        str(path),
                        "--text",
                        "cli-should-not-write",
                        "--transport",
                        "fake",
                    ]
                )
            self.assertEqual(code, 1)
            self.assertNotIn(key, stderr.getvalue())

    def test_shared_proof_rejects_unsafe_chatgpt_gates_before_write(self) -> None:
        template = ov.default_company_template()
        key = "shared-user-key-with-enough-len"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ov.default_client_config_paths(root)

            def reload_configs() -> dict[str, ov.ClientConfig]:
                return {
                    client: ov.load_client_config(path, expected_client=client)
                    for client, path in paths.items()
                }

            for client, path in paths.items():
                ov.write_client_config(
                    path, client=client, template=template, api_key=key
                )

            def assert_zero_write(mutator, pattern: str) -> None:
                for client, path in paths.items():
                    ov.write_client_config(
                        path, client=client, template=template, api_key=key
                    )
                payload = json.loads(paths["chatgpt"].read_text(encoding="utf-8"))
                mutator(payload)
                paths["chatgpt"].write_text(json.dumps(payload), encoding="utf-8")
                paths["chatgpt"].chmod(0o600)
                configs = reload_configs()
                service = ov.FakeOpenVikingService()
                service.register_user_key(
                    key, account=template.account, user=template.user, role="user"
                )
                counting, writes = _write_counting_transport(service)
                with self.assertRaisesRegex(ov.OpenVikingError, pattern):
                    ov.cross_client_marker_roundtrip(configs, transport=counting)
                self.assertEqual(writes, [])
                self.assertEqual(len(service.records), 0)

            assert_zero_write(lambda raw: raw.pop("chatgpt"), "operator gates")
            assert_zero_write(
                lambda raw: raw["chatgpt"].__setitem__("transcript_capture", True),
                "transcript",
            )
            assert_zero_write(
                lambda raw: raw["chatgpt"].__setitem__(
                    "intentional_tool_selection", False
                ),
                "intentional",
            )
            assert_zero_write(
                lambda raw: raw["chatgpt"].__setitem__(
                    "operator_gates_required", False
                ),
                "operator_gates_required",
            )

            stderr = io.StringIO()
            payload = json.loads(paths["chatgpt"].read_text(encoding="utf-8"))
            payload["chatgpt"]["transcript_capture"] = True
            paths["chatgpt"].write_text(json.dumps(payload), encoding="utf-8")
            paths["chatgpt"].chmod(0o600)
            with mock.patch("sys.stderr", stderr), mock.patch(
                "sys.stdout", io.StringIO()
            ):
                code = helmet_cli.main(
                    [
                        "openviking",
                        "shared-proof",
                        "--openviking-root",
                        str(root),
                        "--transport",
                        "fake",
                    ]
                )
            self.assertEqual(code, 1)
            self.assertNotIn(key, stderr.getvalue())

    def test_shared_proof_rejects_mixed_old_new_keys_before_write(self) -> None:
        template = ov.default_company_template()
        old_key = "old-user-key-with-enough-length"
        new_key = "new-user-key-with-enough-length"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ov.default_client_config_paths(root)
            configs = {
                client: ov.write_client_config(
                    path,
                    client=client,
                    template=template,
                    api_key=new_key if client == "chatgpt" else old_key,
                )
                for client, path in paths.items()
            }
            service = ov.FakeOpenVikingService()
            service.register_user_key(
                old_key, account=template.account, user=template.user, role="user"
            )
            service.register_user_key(
                new_key, account=template.account, user=template.user, role="user"
            )
            counting, writes = _write_counting_transport(service)
            with self.assertRaisesRegex(ov.OpenVikingError, "USER key"):
                ov.cross_client_marker_roundtrip(configs, transport=counting)
            self.assertEqual(writes, [])
            self.assertEqual(len(service.records), 0)

            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr), mock.patch(
                "sys.stdout", io.StringIO()
            ):
                code = helmet_cli.main(
                    [
                        "openviking",
                        "shared-proof",
                        "--openviking-root",
                        str(root),
                        "--transport",
                        "fake",
                    ]
                )
            self.assertEqual(code, 1)
            err = stderr.getvalue()
            self.assertNotIn(old_key, err)
            self.assertNotIn(new_key, err)

    def test_unique_uri_retry_and_bounded_index_wait(self) -> None:
        template = ov.default_company_template()
        key = "shared-user-key-with-enough-len"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configs = {
                client: ov.write_client_config(
                    path, client=client, template=template, api_key=key
                )
                for client, path in ov.default_client_config_paths(root).items()
            }
            service = ov.FakeOpenVikingService(index_delay_s=0.15)
            service.register_user_key(
                key, account=template.account, user=template.user, role="user"
            )
            # First pass waits for indexing; second pass must not fail on create URI.
            first = ov.cross_client_marker_roundtrip(
                configs, transport=service.transport, index_timeout_s=2.0
            )
            self.assertTrue(first["ok"])
            second = ov.cross_client_marker_roundtrip(
                configs, transport=service.transport, index_timeout_s=2.0
            )
            self.assertTrue(second["ok"])
            self.assertNotEqual(first["proof_run_id"], second["proof_run_id"])
            # Deterministic same-run_id create collision falls back to replace.
            cfg = configs["codex"]
            uri1, _ = ov.write_synthetic_marker(
                cfg, "same-text-marker", transport=service.transport, run_id="fixed-run"
            )
            uri2, _ = ov.write_synthetic_marker(
                cfg, "same-text-marker", transport=service.transport, run_id="fixed-run"
            )
            self.assertEqual(uri1, uri2)

    def test_shared_uris_use_home_alias_not_uidless_user(self) -> None:
        uri = ov.common_user_memory_uri(relative_path="events/helmet-markers/demo.md")
        self.assertTrue(uri.startswith("viking://~/memories/"))
        self.assertFalse(ov.is_uidless_current_user_uri(uri))
        self.assertTrue(ov.is_uidless_current_user_uri("viking://user/memories/events/x.md"))
        peer = ov.peer_memory_uri("hermes", relative_path="events/x.md")
        self.assertTrue(peer.startswith("viking://~/peers/hermes/memories/"))

    def test_fake_service_rejects_uidless_user_memory_write(self) -> None:
        service = ov.FakeOpenVikingService()
        key = "shared-user-key-with-enough-len"
        service.register_user_key(key, account="exampleco", user="company-context", role="user")
        headers = {
            "X-API-Key": key,
            "Authorization": f"Bearer {key}",
            ov.ACTOR_PEER_HEADER: "codex",
        }
        with self.assertRaisesRegex(ov.OpenVikingError, "uid-less"):
            service.transport(
                "POST",
                "http://127.0.0.1:1933/api/v1/content/write",
                headers,
                {
                    "uri": "viking://user/memories/events/forbidden.md",
                    "content": "nope",
                    "mode": "create",
                },
            )
        self.assertEqual(len(service.records), 0)

    def test_cli_has_no_helmet_owned_stdio_mcp(self) -> None:
        parser = helmet_cli.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["openviking", "mcp-stdio"])
        args = parser.parse_args(
            ["openviking", "shared-proof", "--openviking-root", "/tmp"]
        )
        self.assertEqual(args.openviking_command, "shared-proof")

    def test_codex_template_points_at_official_plugin(self) -> None:
        payload = ov.example_client_config_payload(ov.default_company_template(), "codex")
        plugin = payload["official_plugin"]
        self.assertEqual(plugin["name"], "openviking-memory")
        self.assertEqual(plugin["harness"], "codex")
        self.assertNotIn("codex_mcp", payload)
        chatgpt = ov.example_client_config_payload(ov.default_company_template(), "chatgpt")
        self.assertTrue(chatgpt["chatgpt"]["operator_gates_required"])
        self.assertFalse(chatgpt["chatgpt"]["transcript_capture"])
        self.assertEqual(chatgpt["chatgpt"]["official_claude_plugin"], "openviking-memory")



if __name__ == "__main__":
    unittest.main()
