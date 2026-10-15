from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hermes_helmet import provider_selection as p
from hermes_helmet.cli import main


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.selection = p.configure(["sonnet=anthropic:claude-sonnet-test", "grok=provider-two:grok-test",
                                      "rescue=openrouter:vendor/model"])

    def test_ordered_auto_chain_and_manual_pin_then_restore(self):
        primary, backups = p.effective(self.selection)
        self.assertEqual(primary["name"], "sonnet")
        self.assertEqual([r["provider"] for r in backups], ["provider-two", "openrouter"])
        primary, backups = p.effective(p.choose(self.selection, "grok"))
        self.assertEqual(primary["provider"], "provider-two")
        self.assertEqual(backups, [])
        self.assertEqual(p.effective(p.choose(p.choose(self.selection, "grok"), "auto")), p.effective(self.selection))

    def test_manual_config_removes_legacy_routes_and_preserves_unrelated_settings(self):
        before = {"model": {"provider": "old", "default": "old"},
                  "fallback_model": {"provider": "unselected", "model": "old"},
                  "auxiliary": {"vision": {"model": "vision-model"}},
                  "agent": {"reasoning_overrides": {"other-model": "low"}}}
        frozen = copy.deepcopy(before)
        auto = p.project_config(before, self.selection, effort="high")
        self.assertEqual(before, frozen)
        self.assertNotIn("fallback_model", auto)
        self.assertNotIn("api_key", auto["model"])
        self.assertNotIn("base_url", auto["model"])
        self.assertEqual(auto["auxiliary"], before["auxiliary"])
        self.assertEqual(auto["agent"]["reasoning_overrides"]["grok-test"], "high")
        self.assertEqual(auto["agent"]["reasoning_overrides"]["other-model"], "low")
        manual = p.project_config(auto, p.choose(self.selection, "grok"))
        self.assertEqual(manual["fallback_providers"], [])
        self.assertNotIn("fallback_model", manual)

    def test_profile_endpoint_or_credential_override_is_preserved_and_rejected(self):
        config = {"model": {"base_url": "https://old.example/v1", "api_key": "old-secret"}}
        with self.assertRaisesRegex(p.ProviderSelectionError, "migrate"):
            p.project_config(config, self.selection)
        self.assertEqual(config["model"]["api_key"], "old-secret")

    def test_flat_hermes_model_config_can_switch(self):
        config = p.project_config({"model": "old-model"}, self.selection)
        self.assertEqual(config["model"]["default"], "claude-sonnet-test")

    def test_policy_projection_only_changes_executor(self):
        policy = {"inference_provider": "old", "inference_model": "old-model",
                  "repositories": [{"slug": "Example/repo"}], "github_identity": "company-worker",
                  "worker_roles": {"max_in_progress": 12}, "merge_mode": "captain"}
        projected = p.project_policy(policy, self.selection)
        self.assertEqual(projected["inference_provider"], "anthropic")
        for key in ("repositories", "github_identity", "worker_roles", "merge_mode"):
            self.assertEqual(projected[key], policy[key])
        self.assertEqual(policy["inference_provider"], "old")

    def test_reject_endpoint_bound_executor(self):
        with self.assertRaisesRegex(p.ProviderSelectionError, "endpoint-bound"):
            p.project_policy({"model_lanes": {"hermes_executor": {"base_url": "https://old.example"}}}, self.selection)

    def test_persistence_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "providers.json"
            self.assertIsNone(p.read_selection(path))
            p.write_selection(path, self.selection)
            self.assertEqual(p.read_selection(path), self.selection)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            link = Path(tmp) / "link"
            link.symlink_to(path)
            with self.assertRaises(p.ProviderSelectionError):
                p.write_selection(link, self.selection)
            with self.assertRaises(p.ProviderSelectionError):
                p.read_selection(link)

    def test_invalid_or_secret_bearing_routes_fail_closed(self):
        for specs in (["broken"], ["a=provider:model", "a=other:model"],
                      ["a=provider:model", "b=provider:model"], ["auto=p:m"], ["a=p:$(secret)"]):
            with self.subTest(specs=specs), self.assertRaises(p.ProviderSelectionError):
                p.configure(specs)
        for selected in ([], {}, "missing"):
            with self.subTest(selected=selected), self.assertRaises(p.ProviderSelectionError):
                p.validate({**self.selection, "selected": selected})
        invalid = copy.deepcopy(self.selection)
        invalid["routes"][0]["api_key"] = "secret"
        with self.assertRaises(p.ProviderSelectionError):
            p.validate(invalid)

    def test_cli_forwards_quoted_transport_and_selection_without_a_shell(self):
        result = subprocess.CompletedProcess([], 0, '{"selection":null}', '')
        with patch("hermes_helmet.provider_selection.subprocess.run", return_value=result) as run, \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["providers", "configure", "--route", "one=p:m",
                                   "--worker-runtime", 'python3 "/some path/runtime.py"']), 0)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["python3", "/some path/runtime.py", "providers"])
        self.assertEqual(json.loads(argv[3])["selection"]["routes"][0]["name"], "one")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_failed_transport_never_reports_success_or_echoes_credentials(self):
        result = subprocess.CompletedProcess([], 1, '', 'secret=do-not-echo')
        with patch("hermes_helmet.provider_selection.subprocess.run", return_value=result), \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(main(["providers", "auto", "--worker-runtime", "runtime"]), 1)
        self.assertNotIn("do-not-echo", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
