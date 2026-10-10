#!/usr/bin/env python3
"""Structural packaging for the first-officer Claude/Codex plugin."""

from __future__ import annotations

import json
import re
import tempfile
import tomllib
import unittest
from pathlib import Path

from hermes_helmet import public_surface


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ("setup-helmet", "helmet-issue", "helmet-epic", "observe-chat", "helmet-review")
PLUGIN_NAME = "hermes-helmet"
MARKETPLACE_NAME = "hermes-helmet"
INSTALL_GUIDE = "docs/first-officer-plugins.md"


def _load_json(relative: str) -> dict:
    path = ROOT / relative
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_inside(root: Path, relative: str) -> Path:
    if not relative.startswith("./") or ".." in Path(relative).parts:
        raise AssertionError(f"plugin path escapes root: {relative}")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise AssertionError(f"plugin path escapes root: {relative}") from exc
    return resolved


class FirstOfficerPluginTests(unittest.TestCase):
    def test_manifests_share_identity_and_version(self) -> None:
        claude = _load_json(".claude-plugin/plugin.json")
        marketplace = _load_json(".claude-plugin/marketplace.json")
        codex = _load_json(".codex-plugin/plugin.json")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertEqual(claude["name"], PLUGIN_NAME)
        self.assertEqual(codex["name"], PLUGIN_NAME)
        self.assertEqual(claude["version"], codex["version"])
        self.assertRegex(
            claude["version"],
            r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$",
        )
        package_version = tomllib.loads(pyproject)["project"]["version"]
        plugin_version = re.sub(
            r"^(\d+\.\d+\.\d+)(a|b|rc)(\d+)$",
            lambda match: match[1] + "-" + {"a": "alpha", "b": "beta", "rc": "rc"}[match[2]] + "." + match[3],
            package_version,
        )
        self.assertEqual(claude["version"], plugin_version)
        self.assertNotEqual(claude["version"], "0.1.0rc1")
        self.assertNotEqual(claude["version"], "0.1.0-rc.3")
        self.assertEqual(claude["license"], "Apache-2.0")
        self.assertEqual(codex["license"], "Apache-2.0")
        self.assertEqual(claude["author"]["name"], "Machine Wisdom")
        self.assertEqual(codex["author"]["name"], "Machine Wisdom")
        homepage = "https://github.com/MachineWisdomAI/hermes-helmet"
        self.assertEqual(claude["homepage"], homepage)
        self.assertEqual(claude["repository"], homepage)
        self.assertEqual(codex["homepage"], homepage)
        self.assertEqual(codex["repository"], homepage)
        self.assertIn("description", claude)
        self.assertTrue(claude["description"].strip())
        self.assertTrue(codex["description"].strip())

        self.assertEqual(marketplace["name"], MARKETPLACE_NAME)
        self.assertEqual(len(marketplace["plugins"]), 1)
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], PLUGIN_NAME)
        self.assertEqual(entry["source"], "./")
        self.assertFalse((ROOT / ".codex-plugin" / "marketplace.json").exists())
        self.assertFalse((ROOT / ".agents" / "plugins" / "marketplace.json").exists())

    def test_codex_manifest_points_at_root_skills(self) -> None:
        codex = _load_json(".codex-plugin/plugin.json")
        skills_path = _resolve_inside(ROOT, codex["skills"])
        self.assertEqual(skills_path, (ROOT / "skills").resolve())
        interface = codex["interface"]
        self.assertEqual(interface["displayName"], "Hermes Helmet")
        self.assertEqual(interface["developerName"], "Machine Wisdom")
        self.assertTrue(interface["shortDescription"].strip())
        self.assertTrue(interface["longDescription"].strip())
        capabilities = interface["capabilities"]
        self.assertIsInstance(capabilities, list)
        self.assertGreaterEqual(len(capabilities), 1)
        self.assertLessEqual(len(capabilities), 20)
        for capability in capabilities:
            self.assertIsInstance(capability, str)
            self.assertTrue(capability.strip())
            self.assertNotIn("\n", capability)
            self.assertLessEqual(len(capability), 120)
        prompts = interface["defaultPrompt"]
        self.assertIsInstance(prompts, list)
        self.assertGreaterEqual(len(prompts), 1)
        self.assertLessEqual(len(prompts), 3)
        normalized = []
        for prompt in prompts:
            self.assertIsInstance(prompt, str)
            self.assertTrue(prompt.strip())
            self.assertNotIn("\n", prompt)
            self.assertLessEqual(len(prompt), 128)
            collapsed = " ".join(prompt.split())
            self.assertNotIn(collapsed, normalized)
            normalized.append(collapsed)

    def test_plugin_root_exposes_canonical_skills_in_cache_layout(self) -> None:
        marketplace = _load_json(".claude-plugin/marketplace.json")
        plugin_root = _resolve_inside(ROOT, marketplace["plugins"][0]["source"])
        self.assertEqual(plugin_root, ROOT.resolve())
        claude = _load_json(".claude-plugin/plugin.json")
        with tempfile.TemporaryDirectory() as directory:
            cache = (
                Path(directory)
                / MARKETPLACE_NAME
                / PLUGIN_NAME
                / claude["version"]
            )
            for relative in (
                ".claude-plugin/plugin.json",
                ".claude-plugin/marketplace.json",
                ".codex-plugin/plugin.json",
            ):
                dest = cache / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes((ROOT / relative).read_bytes())
            for skill in SKILLS:
                source = ROOT / "skills" / skill
                dest = cache / "skills" / skill
                for path in source.rglob("*"):
                    if not path.is_file():
                        continue
                    target = dest / path.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(path.read_bytes())
            for skill in SKILLS:
                skill_md = cache / "skills" / skill / "SKILL.md"
                self.assertTrue(skill_md.is_file(), skill)
                self.assertGreater(skill_md.stat().st_size, 0, skill)
            reference = cache / "skills" / "helmet-issue" / "references" / "delivery.md"
            self.assertTrue(reference.is_file())
            self.assertGreater(reference.stat().st_size, 0)

    def test_plugin_trees_do_not_duplicate_skills(self) -> None:
        nested = [
            path
            for path in (ROOT / ".claude-plugin").rglob("SKILL.md")
            if path.is_file()
        ] + [
            path
            for path in (ROOT / ".codex-plugin").rglob("SKILL.md")
            if path.is_file()
        ]
        self.assertEqual(nested, [])
        for skill in SKILLS:
            canonical = ROOT / "skills" / skill / "SKILL.md"
            self.assertTrue(canonical.is_file(), skill)
            extra = [
                path
                for path in ROOT.rglob(f"{skill}/SKILL.md")
                if path.is_file()
                and "bundled_skills" not in path.parts
                and path != canonical
            ]
            self.assertEqual(extra, [], extra)

    def test_codex_plugin_loads_captains_bridge_from_repository_source(self) -> None:
        codex = _load_json(".codex-plugin/plugin.json")
        claude = _load_json(".claude-plugin/plugin.json")
        config_path = _resolve_inside(ROOT, codex["mcpServers"])
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(list(config["mcpServers"]), ["hermes_helmet_captains_bridge"])
        server = config["mcpServers"]["hermes_helmet_captains_bridge"]
        script = _resolve_inside(ROOT, server["args"][-1])
        self.assertEqual(script, (ROOT / "mcp" / "captains-bridge" / "server.py").resolve())
        self.assertTrue(script.is_file())
        self.assertEqual(server["cwd"], ".")
        self.assertEqual(set(server["env_vars"]), {"CODEX_HOME", "HOME", "PATH"})
        text = config_path.read_text(encoding="utf-8")
        for marker in ("/Users/", "/home/", "/opt/data", "spike", "marketplace"):
            self.assertNotIn(marker, text)
        # Claude plugin behavior is unchanged: no MCP server is loaded there.
        self.assertNotIn("mcpServers", claude)
        self.assertFalse((ROOT / "prototypes").exists())

    def test_claude_manifest_declares_the_bridge_mod(self) -> None:
        claude = _load_json(".claude-plugin/plugin.json")
        codex = _load_json(".codex-plugin/plugin.json")
        self.assertIn("Captain's Bridge", claude["description"])
        types_path = _resolve_inside(ROOT, claude["types"])
        self.assertEqual(types_path, (ROOT / "types" / "index.d.ts").resolve())
        self.assertTrue(types_path.name.endswith(".d.ts"))
        contract = types_path.read_text(encoding="utf-8")
        self.assertIn("declare module 'claude-code'", contract)
        self.assertIn("interface PluginState", contract)
        self.assertIn("export type BridgeState", contract)
        for field in ("binding", "records", "walkthrough", "request", "view", "pending"):
            self.assertRegex(contract, rf"(?m)^\s+{field}: BridgeState\['{field}'\]")
        for key in ("sessionId", "transcriptPath", "fingerprint", "generation", "retain"):
            self.assertIn(key, contract)
        for status in ("idle", "preparing", "failed", "cancelled", "superseded", "timed-out"):
            self.assertIn(f"'{status}'", contract)
        # A self-contained, types-only contract (Claude Code enforces this too).
        self.assertNotRegex(contract, r"(?m)^\s*(import|export \{|export \*|require)\b")
        self.assertNotIn("/// <reference", contract)
        self.assertEqual(
            claude["userConfig"]["helmetCommand"]["default"], "helmet"
        )
        defaults = {key: value["default"] for key, value in claude["userConfig"].items()}
        self.assertEqual(
            defaults,
            {
                "helmetCommand": "helmet",
                "explanationModel": "sonnet",
                "explanationTimeoutSeconds": 180,
                "showMeCommand": "show-me",
                "retroCommand": "retro",
            },
        )
        self.assertIn("absolute path", claude["userConfig"]["helmetCommand"]["description"])
        # Codex packaging is untouched by the Claude mod.
        for key in ("hooks", "modules", "types", "userConfig"):
            self.assertNotIn(key, codex)
        self.assertEqual(codex["skills"], "./skills/")
        self.assertEqual(codex["mcpServers"], "./codex.mcp.json")

    def test_hooks_modules_entry_resolves_inside_plugin_root(self) -> None:
        hooks_json = ROOT / "hooks" / "hooks.json"
        config = json.loads(hooks_json.read_text(encoding="utf-8"))
        self.assertEqual(list(config), ["modules"])
        modules = config["modules"]
        self.assertEqual(len(modules), 1, "Claude Code allows one hooks module per plugin")
        module = modules[0]
        # Relative to hooks.json; it must stay inside the plugin root.
        self.assertTrue(module.startswith("./"), module)
        self.assertNotIn("..", Path(module).parts)
        resolved = (hooks_json.parent / module).resolve()
        resolved.relative_to(ROOT.resolve())
        self.assertEqual(resolved, (ROOT / "hooks" / "register.tsx").resolve())
        self.assertTrue(resolved.is_file())
        source = resolved.read_text(encoding="utf-8")
        self.assertRegex(source, r"export const register: Register")
        for call in ("captains-bridge", "immediate: true", "$.ui.open"):
            self.assertIn(call, source)
        self.assertIn("This conversation changed. Run /captains-bridge to open the Bridge for it.", source)
        self.assertNotIn("setInterval", source)
        self.assertNotIn("$.clock.every", source)
        self.assertNotIn("$.prompt", source)
        self.assertNotIn("$.session.send", source)
        # Every file the module imports from the plugin stays in the plugin.
        for target in re.findall(r"from '(\.[^']*)'", source):
            candidate = (resolved.parent / target).resolve()
            candidate.relative_to(ROOT.resolve())

    def test_mod_path_confinement_rejects_escapes(self) -> None:
        for bad in ("../outside.d.ts", "./../outside.d.ts", "/abs/outside.d.ts", "types/x.d.ts"):
            with self.assertRaises(AssertionError, msg=bad):
                _resolve_inside(ROOT, bad)
        _resolve_inside(ROOT, "./types/index.d.ts")

    def test_marketplace_still_lists_exactly_one_root_plugin(self) -> None:
        marketplace = _load_json(".claude-plugin/marketplace.json")
        self.assertEqual(len(marketplace["plugins"]), 1)
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["source"], "./")
        self.assertEqual(entry["name"], PLUGIN_NAME)
        self.assertNotIn("hooks", entry)
        self.assertNotIn("strict", entry)

    def test_mod_files_ship_in_the_plugin_cache_layout(self) -> None:
        claude = _load_json(".claude-plugin/plugin.json")
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / MARKETPLACE_NAME / PLUGIN_NAME / claude["version"]
            for relative in (
                ".claude-plugin/plugin.json",
                "hooks/hooks.json",
                "hooks/register.tsx",
                "types/index.d.ts",
            ):
                dest = cache / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes((ROOT / relative).read_bytes())
            manifest = json.loads((cache / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
            self.assertTrue(_resolve_inside(cache, manifest["types"]).is_file())
            hooks = json.loads((cache / "hooks/hooks.json").read_text(encoding="utf-8"))
            self.assertTrue((cache / "hooks" / hooks["modules"][0]).resolve().is_file())
            # The skills are plain files and need no mod to load.
            for skill in SKILLS:
                self.assertTrue((ROOT / "skills" / skill / "SKILL.md").is_file(), skill)

    def test_skills_do_not_depend_on_the_mod(self) -> None:
        # With mods disabled by policy only hooks/ is skipped; skills load from skills/.
        for skill in SKILLS:
            text = (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
            self.assertNotIn("hooks/register", text, skill)

    def test_mod_files_are_in_the_public_boundary_scan(self) -> None:
        self.assertIn("hooks", public_surface.SCAN_ROOTS)
        self.assertIn("types", public_surface.SCAN_ROOTS)
        self.assertEqual(public_surface.scan_public_surface(ROOT), [])
        verify = (ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8")
        for required in (
            "hooks/hooks.json",
            "hooks/register.tsx",
            "hooks/register.test.tsx",
            "hooks/bridge.test.tsx",
            "hooks/actions.test.tsx",
            "types/index.d.ts",
        ):
            self.assertIn(required, verify)

    def test_plugin_and_tests_use_only_synthetic_fixtures(self) -> None:
        for relative in (
            "hooks/register.tsx",
            "hooks/register.test.tsx",
            "hooks/bridge.test.tsx",
            "hooks/actions.test.tsx",
            "types/index.d.ts",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            for marker in ("/Users/", "/home/", "/opt/data", ".claude/projects"):
                self.assertNotIn(marker, text, f"{relative}: {marker}")

    def test_install_guide_documents_the_bridge_mod(self) -> None:
        guide = " ".join((ROOT / INSTALL_GUIDE).read_text(encoding="utf-8").split())
        for phrase in (
            "/captains-bridge",
            "2.1.293",
            "claude plugin update hermes-helmet@hermes-helmet",
            "/reload-plugins",
            "helmetCommand",
            "explanationModel",
            "explanationTimeoutSeconds",
            "showMeCommand",
            "retroCommand",
            "organization policy",
            "This conversation changed. Run /captains-bridge to open the Bridge for it.",
        ):
            self.assertIn(phrase, guide)

    def test_public_surface_scan_covers_plugin_manifests(self) -> None:
        self.assertIn(".claude-plugin", public_surface.SCAN_ROOTS)
        self.assertIn(".codex-plugin", public_surface.SCAN_ROOTS)
        verify = (ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8")
        self.assertIn(".claude-plugin/plugin.json", verify)
        self.assertIn(".claude-plugin/marketplace.json", verify)
        self.assertIn(".codex-plugin/plugin.json", verify)
        self.assertIn(INSTALL_GUIDE, verify)

    def test_verify_runs_captains_bridge_checks(self) -> None:
        verify = (ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8")
        self.assertIn("cd mcp/captains-bridge", verify)
        for check in ("test_view.cjs", "test_delivery.cjs", "test_actions.cjs"):
            self.assertIn(check, verify)
            self.assertTrue((ROOT / "mcp" / "captains-bridge" / check).is_file())

    def test_install_guide_is_linked_and_names_real_commands(self) -> None:
        guide = (ROOT / INSTALL_GUIDE).read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        quickstart = (ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")
        llms = (ROOT / "llms.txt").read_text(encoding="utf-8")
        self.assertIn(INSTALL_GUIDE, readme)
        self.assertIn("first-officer-plugins.md", quickstart)
        self.assertIn("first-officer-plugins.md", llms)
        for command in (
            "claude plugin marketplace add MachineWisdomAI/hermes-helmet",
            "claude plugin install hermes-helmet@hermes-helmet",
            "/hermes-helmet:helmet-issue",
            "codex plugin marketplace add MachineWisdomAI/hermes-helmet",
            "codex plugin add hermes-helmet@hermes-helmet",
        ):
            self.assertIn(command, guide)


if __name__ == "__main__":
    unittest.main()
