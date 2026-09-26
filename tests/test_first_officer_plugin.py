#!/usr/bin/env python3
"""Structural packaging for the first-officer Claude/Codex plugin."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hermes_helmet import public_surface


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ("setup-helmet", "helmet-issue", "helmet-epic")
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
        self.assertIn('version = "0.1.0rc1"', pyproject)
        self.assertNotEqual(claude["version"], "0.1.0rc1")
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

    def test_public_surface_scan_covers_plugin_manifests(self) -> None:
        self.assertIn(".claude-plugin", public_surface.SCAN_ROOTS)
        self.assertIn(".codex-plugin", public_surface.SCAN_ROOTS)
        verify = (ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8")
        self.assertIn(".claude-plugin/plugin.json", verify)
        self.assertIn(".claude-plugin/marketplace.json", verify)
        self.assertIn(".codex-plugin/plugin.json", verify)
        self.assertIn(INSTALL_GUIDE, verify)

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
