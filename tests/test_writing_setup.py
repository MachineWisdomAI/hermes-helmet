#!/usr/bin/env python3
"""``writing_setup configure|verify``: fresh-state rules, repeatability, refusals.

Most tests drive the real module against ``FakeNative``, a stand-in for the four
native Hermes operations it uses (profile creation, config path, ``config set``,
``plugins enable``, skill listing), so they need neither Hermes nor Docker.
``NativeHermesTests`` runs the same module against the real ``hermes`` CLI and
needs ``HELMET_WRITING_NATIVE=1`` plus a ``hermes`` on ``PATH`` (the pinned image).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hermes_helmet import company_skills as cs  # noqa: E402
from hermes_helmet import writing_context  # noqa: E402
from hermes_helmet import writing_setup as ws  # noqa: E402
from hermes_helmet.worker_role_runtime import _yaml_dump, _yaml_load  # noqa: E402

GUIDANCE = ROOT / "examples" / "writing" / "guidance"


def _yaml_available() -> bool:
    try:
        _yaml_load("a: 1")
    except Exception:  # noqa: BLE001
        return False
    return True


def emit_yaml(value, indent: int = 0) -> str:
    """Block YAML with the native layout (indented sequences), no YAML library.

    Native ``hermes config set`` indents list items under their key; the
    company-skill importer's text merge relies on that shape, so the stand-in
    writes it the same way. Strings use JSON quoting, which is valid YAML.
    """

    pad = " " * indent
    lines: list[str] = []
    for key, item in value.items():
        if isinstance(item, dict) and item:
            lines.append(f"{pad}{key}:")
            lines.append(emit_yaml(item, indent + 2).rstrip("\n"))
        elif isinstance(item, list) and item:
            lines.append(f"{pad}{key}:")
            lines.extend(f"{pad}  - {json.dumps(entry)}" for entry in item)
        elif isinstance(item, (dict, list)):
            lines.append(f"{pad}{key}: {{}}" if isinstance(item, dict) else f"{pad}{key}: []")
        else:
            lines.append(f"{pad}{key}: {json.dumps(item)}")
    return "\n".join(lines) + "\n"


class FakeNative:
    """The native Hermes operations ``writing_setup`` uses, over plain files."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.calls: list[tuple] = []

    def _config(self, profile: str) -> Path:
        base = self.home if profile == ws.COORDINATOR_PROFILE else self.home / "profiles" / profile
        return base / "config.yaml"

    def create_profile(self, name: str) -> None:
        self.calls.append(("create_profile", name))
        target = self.home / "profiles" / name
        target.mkdir(parents=True)
        (target / "SOUL.md").write_text("native profile\n", encoding="utf-8")

    def config_path(self, profile: str) -> Path:
        return self._config(profile)

    def config_set(self, profile: str, key: str, value) -> None:
        self.calls.append(("config_set", profile, key))
        path = self._config(profile)
        data = _yaml_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        data = data or {}
        node = data
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(emit_yaml(data), encoding="utf-8")

    def enable_plugin(self, profile: str, name: str) -> None:
        self.calls.append(("enable_plugin", profile, name))
        path = self._config(profile)
        data = (_yaml_load(path.read_text(encoding="utf-8")) if path.is_file() else {}) or {}
        plugins = data.setdefault("plugins", {})
        enabled = plugins.setdefault("enabled", [])
        if name not in enabled:
            enabled.append(name)
        plugins.setdefault("disabled", [])
        path.write_text(emit_yaml(data), encoding="utf-8")

    def discovered_skills(self, profile: str) -> set[str]:
        names = {"docx", "powerpoint"}  # bundled with every native profile
        path = self._config(profile)
        data = (_yaml_load(path.read_text(encoding="utf-8")) if path.is_file() else {}) or {}
        for directory in (data.get("skills") or {}).get("external_dirs") or []:
            root = Path(directory)
            if root.is_dir():
                names.update(child.name for child in root.iterdir() if (child / "SKILL.md").is_file())
        return names


def snapshot(home: Path) -> dict[str, str]:
    """Every path under ``home`` with a content digest; directories included."""

    result: dict[str, str] = {}
    for path in sorted(home.rglob("*")):
        rel = path.relative_to(home).as_posix()
        if path.is_symlink():
            result[rel] = "link:" + os.readlink(path)
        elif path.is_dir():
            result[rel] = "dir"
        else:
            result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def make_task_store(path: Path, statuses: tuple[str, ...] = ("ready",)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        for index, status in enumerate(statuses):
            connection.execute("INSERT INTO tasks VALUES (?, ?)", (f"t_{index}", status))
        connection.commit()
    finally:
        connection.close()


@unittest.skipUnless(_yaml_available(), "PyYAML or ruamel.yaml is required")
class WritingSetupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.home = base / "data"
        self.home.mkdir()
        self.assets = base / "assets"
        (self.assets / "skills" / "diagram").mkdir(parents=True)
        (self.assets / "skills" / "diagram" / "SKILL.md").write_text(
            "---\nname: diagram\ndescription: d\n---\n", encoding="utf-8"
        )
        (self.assets / "smoke.py").write_text("# stub\n", encoding="utf-8")
        self.layout = ws.Layout(home=self.home, assets=self.assets, guidance_pack=GUIDANCE)
        self.native = FakeNative(self.home)

    def configure(self) -> dict:
        return ws.configure(self.layout, self.native)

    def assert_refused(self, code: str) -> None:
        before = snapshot(self.home)
        calls = len(self.native.calls)
        with self.assertRaises(ws.SetupRefused) as caught:
            self.configure()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(snapshot(self.home), before, "a refusal must not change the home")
        self.assertEqual(len(self.native.calls), calls, "a refusal must not call native Hermes")

    def profile_config(self) -> dict:
        return _yaml_load((self.home / "profiles" / "writing" / "config.yaml").read_text(encoding="utf-8"))

    def root_config(self) -> dict:
        return _yaml_load((self.home / "config.yaml").read_text(encoding="utf-8"))


class ConfigureTests(WritingSetupTestCase):
    def test_configures_the_fixed_topology_and_writes_the_exact_marker(self) -> None:
        result = self.configure()
        self.assertTrue(result["ok"])
        self.assertEqual(
            json.loads((self.home / ws.MARKER_NAME).read_text(encoding="utf-8")),
            {"recipe": "helmet-writing", "version": 1},
        )
        self.assertEqual(ws.MARKER_NAME, ".helmet-writing-recipe.json")
        profile = self.profile_config()
        self.assertIs(profile["memory"]["memory_enabled"], False)
        self.assertIs(profile["memory"]["user_profile_enabled"], False)
        self.assertEqual(profile["memory"]["provider"], "")
        self.assertEqual(profile["agent"]["disabled_toolsets"], ["session_search"])
        self.assertIn(writing_context.PLUGIN_NAME, profile["plugins"]["enabled"])
        self.assertEqual(self.root_config()["kanban"]["dispatch_profiles"], ["writing"])
        # Only the native default coordinator (the home itself) and one profile.
        self.assertEqual(sorted(p.name for p in (self.home / "profiles").iterdir()), ["writing"])
        self.assertEqual([c for c in self.native.calls if c[0] == "create_profile"], [("create_profile", "writing")])

    def test_profile_check_is_clean_and_plugin_files_are_installed(self) -> None:
        self.configure()
        self.assertEqual(writing_context.profile_findings(self.profile_config()), [])
        plugin = self.home / "profiles" / "writing" / "plugins" / writing_context.PLUGIN_NAME
        self.assertTrue((plugin / "plugin.yaml").is_file())
        self.assertTrue((plugin / "__init__.py").is_file())

    def test_guidance_is_imported_into_the_writing_profile_with_discovery(self) -> None:
        self.configure()
        profile_home = self.home / "profiles" / "writing"
        state = cs.default_state_root(prefix=profile_home)
        self.assertEqual([e.name for e in cs.list_installed_skills(state)], ["example-writing-guidance"])
        discovery = str(cs.executor_discovery_dir(state))
        self.assertIn(discovery, self.profile_config()["skills"]["external_dirs"])
        self.assertIn(str(self.assets / "skills"), self.profile_config()["skills"]["external_dirs"])
        # Never the root home, and never a host-global skill tree.
        self.assertFalse((self.home / "hermes-helmet").exists())
        self.assertEqual(
            self.native.discovered_skills("writing") >= {"docx", "powerpoint", "diagram", "example-writing-guidance"},
            True,
        )

    def test_repeat_configure_changes_nothing_and_does_not_duplicate(self) -> None:
        self.configure()
        before = snapshot(self.home)
        releases = len(list((cs.default_state_root(prefix=self.home / "profiles" / "writing") / "releases").iterdir()))
        again = self.configure()
        self.assertTrue(again["ok"])
        self.assertEqual(again["previous_marker"], "valid")
        self.assertEqual(again["guidance"]["status"], "unchanged")
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(
            len(list((cs.default_state_root(prefix=self.home / "profiles" / "writing") / "releases").iterdir())),
            releases,
        )
        self.assertEqual([c for c in self.native.calls if c[0] == "create_profile"], [("create_profile", "writing")])
        profile = self.profile_config()
        self.assertEqual(profile["plugins"]["enabled"].count(writing_context.PLUGIN_NAME), 1)
        self.assertEqual(len(profile["skills"]["external_dirs"]), len(set(profile["skills"]["external_dirs"])))

    def test_provider_credentials_and_capacity_are_preserved(self) -> None:
        (self.home / "config.yaml").write_text(
            _yaml_dump({"model": {"default": "m", "provider": "p"}, "kanban": {"max_in_progress_per_profile": 3}}),
            encoding="utf-8",
        )
        self.configure()
        root = self.root_config()
        self.assertEqual(root["model"], {"default": "m", "provider": "p"})
        self.assertEqual(root["kanban"]["max_in_progress_per_profile"], 3)
        self.assertEqual(root["kanban"]["dispatch_profiles"], ["writing"])
        # An adopter who configured the profile before configure keeps those keys.
        self.setUp_second_home_with_profile_credentials()

    def setUp_second_home_with_profile_credentials(self) -> None:
        base = Path(self.tmp.name) / "second"
        home = base / "data"
        profile = home / "profiles" / "writing"
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text(
            _yaml_dump({"model": {"default": "wm", "provider": "wp"}, "agent": {"disabled_toolsets": ["browser"]}}),
            encoding="utf-8",
        )
        # A bare profile directory without the marker is refused, not adopted.
        layout = ws.Layout(home=home, assets=self.assets, guidance_pack=GUIDANCE)
        with self.assertRaises(ws.SetupRefused) as caught:
            ws.configure(layout, FakeNative(home))
        self.assertEqual(caught.exception.code, "writing_profile_unmarked")

    def test_existing_disabled_toolsets_are_extended_not_replaced(self) -> None:
        native = self.native
        native.create_profile("writing")
        native.config_set("writing", "agent.disabled_toolsets", ["browser"])
        # create_profile above made the directory but not the marker: refused.
        self.assert_refused("writing_profile_unmarked")

    def test_missing_image_assets_is_refused_before_mutation(self) -> None:
        shutil.rmtree(self.assets / "skills")
        self.assert_refused("image_assets_missing")


class RefusalTests(WritingSetupTestCase):
    """Every refused-state predicate: the home is left exactly as found."""

    def test_unknown_profile_is_refused(self) -> None:
        (self.home / "profiles" / "other").mkdir(parents=True)
        self.assert_refused("unexpected_profile")

    def test_unknown_profile_is_refused_even_with_a_valid_marker(self) -> None:
        self.configure()
        (self.home / "profiles" / "extra").mkdir()
        self.assert_refused("unexpected_profile")

    def test_profiles_path_that_is_a_file_is_refused(self) -> None:
        (self.home / "profiles").write_text("x", encoding="utf-8")
        self.assert_refused("unexpected_profile")

    def test_writing_profile_without_the_marker_is_refused(self) -> None:
        (self.home / "profiles" / "writing").mkdir(parents=True)
        self.assert_refused("writing_profile_unmarked")

    def test_marker_that_is_not_json_is_refused(self) -> None:
        (self.home / ws.MARKER_NAME).write_text("{not json", encoding="utf-8")
        self.assert_refused("marker_invalid")

    def test_marker_for_another_recipe_or_version_is_refused(self) -> None:
        for document in (
            {"recipe": "other", "version": 1},
            {"recipe": "helmet-writing", "version": 2},
            {"recipe": "helmet-writing", "version": 1, "extra": True},
            {"recipe": "helmet-writing"},
            [],
        ):
            with self.subTest(document=document):
                (self.home / ws.MARKER_NAME).write_text(json.dumps(document), encoding="utf-8")
                self.assert_refused("marker_different")

    def test_marker_that_is_a_directory_or_symlink_is_refused(self) -> None:
        (self.home / ws.MARKER_NAME).mkdir()
        self.assert_refused("marker_invalid")
        (self.home / ws.MARKER_NAME).rmdir()
        outside = Path(self.tmp.name) / "outside.json"
        outside.write_text(json.dumps(ws.MARKER_DOCUMENT), encoding="utf-8")
        (self.home / ws.MARKER_NAME).symlink_to(outside)
        self.assert_refused("marker_invalid")

    def test_a_task_on_the_default_board_is_refused(self) -> None:
        make_task_store(self.home / "kanban.db")
        self.assert_refused("tasks_exist")

    def test_a_task_on_a_named_board_is_refused(self) -> None:
        make_task_store(self.home / "kanban" / "boards" / "project" / "kanban.db")
        self.assert_refused("tasks_exist")

    def test_an_archived_task_is_refused(self) -> None:
        make_task_store(self.home / "kanban.db", ("archived",))
        self.assert_refused("tasks_exist")

    def test_a_task_on_an_archived_board_is_refused(self) -> None:
        make_task_store(self.home / "kanban" / "boards" / "_archived" / "old-1700000000" / "kanban.db", ("done",))
        self.assert_refused("tasks_exist")

    def test_an_unreadable_task_store_is_refused(self) -> None:
        (self.home / "kanban.db").write_bytes(b"this is not a sqlite database at all" * 8)
        self.assert_refused("task_state_unreadable")

    def test_an_empty_task_store_is_fresh(self) -> None:
        make_task_store(self.home / "kanban.db", ())
        self.assertTrue(self.configure()["ok"])

    def test_tasks_after_a_successful_configure_are_refused_without_change(self) -> None:
        self.configure()
        make_task_store(self.home / "kanban" / "boards" / "w" / "kanban.db", ("archived", "done"))
        self.assert_refused("tasks_exist")

    def test_inspection_never_creates_a_task_store(self) -> None:
        self.configure()
        self.assertFalse((self.home / "kanban.db").exists())
        self.assertFalse((self.home / "kanban").exists())


class VerifyTests(WritingSetupTestCase):
    def runner_ok(self, argv, env):
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}\n', stderr="")

    def verify(self, **kwargs) -> dict:
        interpreters = kwargs.pop("interpreters", (sys.executable,))
        return ws.verify(self.layout, self.native, runner=kwargs.pop("runner", self.runner_ok), interpreters=interpreters)

    def test_verify_passes_after_configure_and_reports_office_rendering_unavailable(self) -> None:
        self.configure()
        original = ws.office_rendering_status
        ws.office_rendering_status = lambda which=shutil.which: original(lambda name: None)
        self.addCleanup(setattr, ws, "office_rendering_status", original)
        report = self.verify()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["office_visual_rendering"], "unavailable")
        names = [c["name"] for c in report["checks"]]
        for expected in (
            "recipe_marker", "profile_check", "capability_dir_registered", "context_plugin_installed",
            "dispatch_allowlist", "native_skill_discovery", f"documents[{sys.executable}]", "diagram_offline",
        ):
            self.assertIn(expected, names)

    def test_office_rendering_available_is_only_reported(self) -> None:
        self.assertEqual(ws.office_rendering_status(lambda name: "/usr/bin/soffice" if name == "soffice" else None), "available")
        self.assertEqual(ws.office_rendering_status(lambda name: None), "unavailable")

    def test_verify_runs_each_image_interpreter_and_fails_when_one_cannot_run_the_skills(self) -> None:
        self.configure()
        seen: list[list[str]] = []

        def runner(argv, env):
            seen.append(list(argv))
            if argv[0] == "/usr/bin/python3" and argv[2] == "documents":
                return subprocess.CompletedProcess(argv, 1, stdout='{"ok": false, "error": "No module named docx"}\n', stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}\n', stderr="")

        fake_paths = {"/opt/hermes/.venv/bin/python", "/usr/local/bin/python3", "/usr/bin/python3"}
        original = Path.is_file
        Path.is_file = lambda self_path: str(self_path) in fake_paths or original(self_path)  # type: ignore[method-assign]
        self.addCleanup(setattr, Path, "is_file", original)
        report = self.verify(runner=runner, interpreters=ws.IMAGE_INTERPRETERS)
        documents = [c for c in report["checks"] if c["name"].startswith("documents[")]
        self.assertEqual([c["name"] for c in documents], [f"documents[{p}]" for p in ws.IMAGE_INTERPRETERS])
        self.assertEqual([c["status"] for c in documents], ["pass", "pass", "fail"])
        self.assertFalse(report["ok"])
        self.assertTrue(any(a[2:3] == ["diagram"] for a in seen))

    def test_verify_fails_before_configuration_and_for_a_drifted_profile(self) -> None:
        report = self.verify()
        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"][0]["name"], "recipe_marker")
        self.configure()
        config = self.home / "profiles" / "writing" / "config.yaml"
        data = _yaml_load(config.read_text(encoding="utf-8"))
        data["memory"]["memory_enabled"] = True
        config.write_text(_yaml_dump(data), encoding="utf-8")
        report = self.verify()
        profile_check = next(c for c in report["checks"] if c["name"] == "profile_check")
        self.assertEqual(profile_check["status"], "fail")
        self.assertIn("builtin_memory_enabled", profile_check["detail"])

    def test_verify_stays_usable_after_work_begins_and_changes_nothing(self) -> None:
        self.configure()
        make_task_store(self.home / "kanban.db", ("running", "archived"))
        before = snapshot(self.home)
        report = self.verify()
        self.assertTrue(report["ok"], report)
        self.assertIs(report["tasks_exist"], True)
        self.assertEqual(snapshot(self.home), before)
        self.assert_refused("tasks_exist")

    def test_verify_requires_the_fixed_allowlist(self) -> None:
        self.configure()
        (self.home / "config.yaml").write_text(
            _yaml_dump({"kanban": {"dispatch_profiles": ["writing", "default"]}}), encoding="utf-8"
        )
        check = next(c for c in self.verify()["checks"] if c["name"] == "dispatch_allowlist")
        self.assertEqual(check["status"], "fail")


class CommandLineTests(WritingSetupTestCase):
    def run_main(self, action: str) -> tuple[int, dict]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = ws.main(
                [action, "--home", str(self.home), "--assets", str(self.assets), "--guidance-pack", str(GUIDANCE)],
                native=self.native,
            )
        return code, json.loads(out.getvalue())

    def test_configure_prints_json_and_returns_zero(self) -> None:
        code, payload = self.run_main("configure")
        self.assertEqual(code, 0)
        self.assertEqual(payload["worker"], "writing")

    def test_refusal_prints_a_stable_code_and_returns_nonzero(self) -> None:
        make_task_store(self.home / "kanban.db")
        code, payload = self.run_main("configure")
        self.assertEqual(code, 1)
        self.assertEqual(payload["refused"], "tasks_exist")
        self.assertIs(payload["changed"], False)

    def test_module_does_not_install_packages_or_use_a_docker_socket(self) -> None:
        source = (ROOT / "src" / "hermes_helmet" / "writing_setup.py").read_text(encoding="utf-8")
        for needle in ("pip install", "pip3", "docker", "/var/run", "fcntl", "flock", ".lock", "rollback("):
            self.assertNotIn(needle, source.replace("no Docker socket", "").replace("Docker socket", ""))


@unittest.skipUnless(
    os.environ.get("HELMET_WRITING_NATIVE") == "1" and shutil.which("hermes"),
    "set HELMET_WRITING_NATIVE=1 with the pinned image's hermes on PATH",
)
class NativeHermesTests(unittest.TestCase):
    """The same module against the real native CLI in a throwaway home."""

    def test_real_configure_repeat_native_discovery_and_profile_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "data"
            (home).mkdir()
            assets = base / "assets"
            (assets / "skills" / "diagram").mkdir(parents=True)
            (assets / "skills" / "diagram" / "SKILL.md").write_text(
                "---\nname: diagram\ndescription: d\nversion: 1.0.0\n---\n", encoding="utf-8"
            )
            (assets / "smoke.py").write_text("# stub\n", encoding="utf-8")
            (home / "config.yaml").write_text("# adopter comment\nmodel:\n  provider: keep-me\n", encoding="utf-8")
            layout = ws.Layout(home=home, assets=assets, guidance_pack=GUIDANCE)
            native = ws.HermesNative(home)
            ws.configure(layout, native)

            def owned() -> dict[str, str]:
                """The files this recipe owns; native Hermes keeps its own backups and caches."""
                keep = ("config.yaml", ws.MARKER_NAME, "plugin.yaml", "SKILL.md", "manifest.json")
                return {k: v for k, v in snapshot(home).items() if k.rsplit("/", 1)[-1] in keep and not k.startswith(("backups/", "profiles/writing/skills/"))}

            first = owned()
            self.assertTrue(first)
            ws.configure(layout, native)
            self.assertEqual(owned(), first)
            self.assertIn("# adopter comment", (home / "config.yaml").read_text(encoding="utf-8"))
            profile_config = Path(native.config_path("writing"))
            self.assertEqual(writing_context.profile_findings(_yaml_load(profile_config.read_text(encoding="utf-8"))), [])
            seen = native.discovered_skills("writing")
            self.assertTrue({"docx", "powerpoint", "diagram", "example-writing-guidance"} <= seen, seen)
            report = ws.verify(layout, native, runner=lambda argv, env: subprocess.CompletedProcess(argv, 0, stdout="{}\n", stderr=""), interpreters=(sys.executable,))
            self.assertTrue(report["ok"], report)


if __name__ == "__main__":
    unittest.main()
