#!/usr/bin/env python3
"""Configure and verify the fresh, isolated ``helmet-writing`` Hermes worker.

Runs inside the writing image's one-off container, before the gateway starts::

    python -m hermes_helmet.writing_setup configure
    python -m hermes_helmet.writing_setup verify

``configure`` shapes a *new* worker home into the supported writing topology:
the native ``default`` coordinator profile plus one ``writing`` profile that
every writer and the editor share (each on its own card and session). It is a
thin, repeatable configuration step, not an installer: no journal, lock,
snapshot, recovery or in-place rollback exists. A failure before the recipe
marker is written means "discard the dedicated Compose project and retry".

Fresh-state rules (the recipe marker is ``/opt/data/.helmet-writing-recipe.json``
holding exactly ``{"recipe": "helmet-writing", "version": 1}``). Before any
mutation, ``configure`` refuses:

- any profile directory other than ``writing`` (the native ``default`` profile
  is the home itself);
- a marker that is not that exact document, or is not a regular file;
- a ``writing`` profile without the marker;
- any native task record on any board, archived boards and archived tasks
  included, or a task store it cannot read.

An absent marker with no extra profile and no tasks is fresh state. A matching
marker allows a repeat run, with identical results and no duplicate profile or
skill release, only while no task exists. There is no active-service detection:
the container has no Docker socket, so run ``configure`` before the service
starts. After work begins, ``verify`` still runs and ``configure`` refuses.

Preserved: the provider, model, credentials and capacity an adopter supplied
through native setup (every key this module does not name). Set here: the
writing memory/context flags, ``session_search`` disabled, the existing
``helmet-writing-context`` plugin enabled, the image's document and diagram
capability directory registered, the neutral guidance pack imported, and the
fixed dispatch allowlist ``kanban.dispatch_profiles: [writing]`` on the root
config. A profile added later is not dispatchable until the adopter adds it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from hermes_helmet import company_skills, writing_context
from hermes_helmet.worker_role_runtime import _yaml_load

RECIPE = "helmet-writing"
RECIPE_VERSION = 1
MARKER_NAME = ".helmet-writing-recipe.json"
MARKER_DOCUMENT = {"recipe": RECIPE, "version": RECIPE_VERSION}
WRITING_PROFILE = "writing"
COORDINATOR_PROFILE = "default"
DISPATCH_PROFILES = [WRITING_PROFILE]
GUIDANCE_SKILL = "example-writing-guidance"
DOCUMENT_SKILLS = ("docx", "powerpoint")
DIAGRAM_SKILL = "diagram"
DEFAULT_HOME = "/opt/data"
DEFAULT_ASSETS = "/opt/hermes-helmet/writing"
DEFAULT_GUIDANCE_PACK = "/opt/hermes-helmet/examples/writing/guidance"
OFFICE_RENDERER_COMMANDS = ("soffice", "libreoffice")
# The image's three interpreters (deploy/writing/install-libs.py installs the
# locked libraries into each). The bundled document skills run under whichever a
# worker's `python` resolves to, so verify exercises all of them.
IMAGE_INTERPRETERS = (
    "/opt/hermes/.venv/bin/python",
    "/usr/local/bin/python3",
    "/usr/bin/python3",
)

# Variables that choose a Hermes identity; native calls here always address the
# profile explicitly instead.
_IDENTITY_ENV = ("HERMES_PROFILE", "HERMES_PROFILE_NAME")


class SetupRefused(Exception):
    """A refusal made before any mutation. ``code`` is stable and documented."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class SetupFailed(Exception):
    """A step failed after mutation began; discard the pre-use project and retry."""


# --------------------------------------------------------------------------- #
# Native Hermes seam
# --------------------------------------------------------------------------- #


class HermesNative:
    """The few native Hermes operations this recipe needs.

    Profile creation, path resolution and skill discovery go through the
    ``hermes`` CLI the image already ships, so this module never re-implements
    profile layout. Tests substitute a stand-in with the same four methods.
    """

    def __init__(self, home: Path, hermes: str = "hermes") -> None:
        self.home = Path(home)
        self.hermes = hermes

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _IDENTITY_ENV}
        env.update({"HERMES_HOME": str(self.home), "HOME": str(self.home), "COLUMNS": "240"})
        for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
            env.pop(name, None)
        return env

    def _run(self, *args: str) -> str:
        try:
            result = subprocess.run(
                [self.hermes, *args],
                env=self._env(),
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SetupFailed(f"native hermes call failed: {args[0]} ({type(exc).__name__})") from exc
        if result.returncode != 0:
            raise SetupFailed(f"native hermes call failed: {' '.join(args[:2])} exited {result.returncode}")
        return result.stdout

    def create_profile(self, name: str) -> None:
        self._run("profile", "create", name, "--no-alias")

    def config_set(self, profile: str, key: str, value: Any) -> None:
        """Native ``config set``: keeps comments, ordering and every unnamed key."""

        scope = [] if profile == COORDINATOR_PROFILE else ["-p", profile]
        # Native ``config set`` parses its value as YAML: lists and booleans need
        # their literal form, but a string (including the empty one) must pass raw.
        text = value if isinstance(value, str) else json.dumps(value)
        self._run(*scope, "config", "set", key, text)

    def enable_plugin(self, profile: str, name: str) -> None:
        scope = [] if profile == COORDINATOR_PROFILE else ["-p", profile]
        self._run(*scope, "plugins", "enable", name)

    def config_path(self, profile: str) -> Path:
        """The profile's native ``config.yaml`` path (``default`` is the home)."""

        args = ["config", "path"] if profile == COORDINATOR_PROFILE else ["-p", profile, "config", "path"]
        text = self._run(*args).strip().splitlines()
        if not text:
            raise SetupFailed("native config path was empty")
        return Path(text[-1].strip())

    def discovered_skills(self, profile: str) -> set[str]:
        """Names the profile's native discovery lists as enabled."""

        text = self._run("-p", profile, "skills", "list", "--enabled-only")
        names: set[str] = set()
        for line in text.splitlines():
            cells = [cell.strip() for cell in re.split(r"[│|]", line) if cell.strip()]
            if cells:
                names.add(cells[0])
        return names


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Layout:
    """Resolved paths for one worker home and the image assets."""

    home: Path
    assets: Path
    guidance_pack: Path

    @property
    def marker(self) -> Path:
        return self.home / MARKER_NAME

    @property
    def profiles(self) -> Path:
        return self.home / "profiles"

    @property
    def capability_skills(self) -> Path:
        return self.assets / "skills"

    @property
    def smoke(self) -> Path:
        return self.assets / "smoke.py"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = _yaml_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same refusal
        raise SetupRefused("config_unreadable", f"{path.name} could not be parsed") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SetupRefused("config_unreadable", f"{path.name} is not a mapping")
    return value


def _section(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key)
    if not isinstance(value, dict):
        value = {}
        config[key] = value
    return value


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _with_member(values: Any, member: str) -> list[str]:
    items = _string_list(values)
    return items if member in items else [*items, member]


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Fresh-state predicates (all read-only)
# --------------------------------------------------------------------------- #


def read_marker(layout: Layout) -> str:
    """``absent`` | ``valid``; raises :class:`SetupRefused` for an invalid marker."""

    marker = layout.marker
    if marker.is_symlink() or (marker.exists() and not marker.is_file()):
        raise SetupRefused("marker_invalid", f"{MARKER_NAME} is not a regular file")
    if not marker.exists():
        return "absent"
    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SetupRefused("marker_invalid", f"{MARKER_NAME} is not valid JSON") from exc
    if document != MARKER_DOCUMENT:
        raise SetupRefused("marker_different", f"{MARKER_NAME} belongs to a different recipe or version")
    return "valid"


def extra_profiles(layout: Layout) -> list[str]:
    """Entries under ``profiles/`` other than ``writing`` (``default`` is the home)."""

    if not layout.profiles.exists():
        return []
    if layout.profiles.is_symlink() or not layout.profiles.is_dir():
        return ["profiles"]
    return sorted(entry.name for entry in layout.profiles.iterdir() if entry.name != WRITING_PROFILE)


def _task_databases(home: Path) -> list[Path]:
    found: list[Path] = []
    default_db = home / "kanban.db"
    if default_db.exists():
        found.append(default_db)
    boards = home / "kanban" / "boards"
    if boards.is_dir():
        # Every board, archived boards (``_archived/<slug>-<ts>``) included.
        found.extend(sorted(p for p in boards.rglob("kanban.db") if p.is_file()))
    return found


def task_count(layout: Layout) -> int:
    """Native task rows on every board, archived tasks included.

    Opened read-only, so inspection never creates or alters a store. A store
    that cannot be read is itself a refusal: the state is not known to be empty.
    """

    total = 0
    for database in _task_databases(layout.home):
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error as exc:
            raise SetupRefused("task_state_unreadable", "a Kanban store could not be opened") from exc
        try:
            has_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
            ).fetchone()
            if has_table:
                total += int(connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        except sqlite3.Error as exc:
            raise SetupRefused("task_state_unreadable", "a Kanban store could not be read") from exc
        finally:
            connection.close()
    return total


def assess_fresh(layout: Layout) -> str:
    """Return the marker state, or raise the first refusal. Mutates nothing."""

    marker_state = read_marker(layout)
    extras = extra_profiles(layout)
    if extras:
        raise SetupRefused(
            "unexpected_profile",
            "only the native default profile and 'writing' are supported; found: " + ", ".join(extras),
        )
    writing_dir = layout.profiles / WRITING_PROFILE
    if (writing_dir.exists() or writing_dir.is_symlink()) and marker_state == "absent":
        raise SetupRefused(
            "writing_profile_unmarked",
            "a 'writing' profile exists without this recipe's marker; use a new project",
        )
    if task_count(layout) > 0:
        raise SetupRefused(
            "tasks_exist",
            "native task records exist (archived tasks count); configure must run "
            "before work begins. Use a new project for a changed setup",
        )
    return marker_state


# --------------------------------------------------------------------------- #
# configure
# --------------------------------------------------------------------------- #


def _lookup(config: Mapping[str, Any], dotted: str) -> Any:
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _set_if_changed(native: Any, profile: str, config: Mapping[str, Any], key: str, value: Any) -> None:
    """Native ``config set`` only when the stored value differs, so a repeat run
    writes nothing (and leaves no extra native config backup behind)."""

    if _lookup(config, key) != value:
        native.config_set(profile, key, value)


def _apply_profile_config(native: Any, config: Mapping[str, Any], capability_skills: Path) -> None:
    """Set only the writing keys, through native ``config set`` (comments and every
    other key, including provider, credentials and capacity, are left as found)."""

    for key, value in (
        ("memory.memory_enabled", False),
        ("memory.user_profile_enabled", False),
        ("memory.provider", ""),
        (
            "agent.disabled_toolsets",
            _with_member(_lookup(config, "agent.disabled_toolsets"), writing_context.RETRIEVAL_TOOLSET),
        ),
        (
            "skills.external_dirs",
            _with_member(_lookup(config, "skills.external_dirs"), str(capability_skills)),
        ),
    ):
        _set_if_changed(native, WRITING_PROFILE, config, key, value)
    enabled = _string_list(_lookup(config, "plugins.enabled"))
    disabled = _string_list(_lookup(config, "plugins.disabled"))
    if writing_context.PLUGIN_NAME not in enabled or writing_context.PLUGIN_NAME in disabled:
        native.enable_plugin(WRITING_PROFILE, writing_context.PLUGIN_NAME)


def _import_guidance(layout: Layout, profile_home: Path, config_path: Path) -> dict[str, Any]:
    """Import the neutral pack into the writing profile, once, with discovery registered."""

    source = layout.guidance_pack
    pack_skill = source / GUIDANCE_SKILL
    if not (pack_skill / "SKILL.md").is_file():
        raise SetupFailed(f"guidance pack is missing {GUIDANCE_SKILL}/SKILL.md in the image")
    state_root = company_skills.default_state_root(prefix=profile_home)
    installed = company_skills.list_installed_skills(state_root)
    active = state_root / "active" / company_skills.SKILLS_DIR_NAME / GUIDANCE_SKILL
    unchanged = (
        any(entry.name == GUIDANCE_SKILL for entry in installed)
        and active.is_dir()
        and _tree_digest(active) == _tree_digest(pack_skill)
    )
    if unchanged:
        # A repeat run must not stack a second release; only confirm registration.
        discovery = company_skills.register_executor_discovery(
            state_root, hermes_home=profile_home, config_path=config_path
        )
        if discovery is None:
            raise SetupFailed("guidance discovery registration could not be verified")
        return {"status": "unchanged", "discovery_path": str(discovery)}
    result = company_skills.import_company_skills(
        source=source,
        allowlist=[GUIDANCE_SKILL],
        state_root=state_root,
        hermes_home=profile_home,
        hermes_config_path=config_path,
        register_discovery=True,
    )
    if result.status != company_skills.STATUS_IMPORTED or GUIDANCE_SKILL not in result.installed_names:
        raise SetupFailed(f"guidance import was not successful: {result.status}: {result.message}")
    if not result.discovery_path or "discovery registration failed" in result.message:
        raise SetupFailed("guidance discovery registration was not successful")
    return {"status": "imported", "discovery_path": result.discovery_path}


def configure(layout: Layout, native: Any) -> dict[str, Any]:
    """Shape a fresh worker home. Every refusal precedes the first mutation."""

    state = assess_fresh(layout)
    if not layout.capability_skills.joinpath(DIAGRAM_SKILL, "SKILL.md").is_file():
        raise SetupRefused("image_assets_missing", "the writing image's diagram skill is not present")

    layout.home.mkdir(parents=True, exist_ok=True)
    if not (layout.profiles / WRITING_PROFILE).is_dir():
        native.create_profile(WRITING_PROFILE)
    profile_config = Path(native.config_path(WRITING_PROFILE))
    profile_home = profile_config.parent
    root_config = Path(native.config_path(COORDINATOR_PROFILE))

    writing_context.write_plugin(profile_home / "plugins")

    _apply_profile_config(native, _read_yaml(profile_config), layout.capability_skills)
    _set_if_changed(
        native,
        COORDINATOR_PROFILE,
        _read_yaml(root_config),
        "kanban.dispatch_profiles",
        list(DISPATCH_PROFILES),
    )

    guidance = _import_guidance(layout, profile_home, profile_config)

    layout.marker.write_text(json.dumps(MARKER_DOCUMENT, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "action": "configure",
        "marker": MARKER_NAME,
        "previous_marker": state,
        "coordinator": COORDINATOR_PROFILE,
        "worker": WRITING_PROFILE,
        "dispatch_profiles": list(DISPATCH_PROFILES),
        "guidance": guidance,
        "next": "verify capabilities, then start the service",
    }


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #

Runner = Callable[[Sequence[str], Mapping[str, str]], "subprocess.CompletedProcess[str]"]


def _default_runner(argv: Sequence[str], env: Mapping[str, str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), env=dict(env), capture_output=True, text=True, timeout=600, check=False)


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "status": "pass" if ok else "fail", "detail": detail}


def office_rendering_status(which: Callable[[str], str | None] = shutil.which) -> str:
    """Office page/slide rendering is optional in v1 and never fails verification."""

    return "available" if any(which(name) for name in OFFICE_RENDERER_COMMANDS) else "unavailable"


def verify(
    layout: Layout,
    native: Any,
    *,
    runner: Runner = _default_runner,
    interpreters: Sequence[str] = IMAGE_INTERPRETERS,
) -> dict[str, Any]:
    """Check the configured worker. Valid before and after work begins; read-only
    apart from the throwaway files the capability smoke writes under a temp dir."""

    checks: list[dict[str, Any]] = []

    try:
        marker_state = read_marker(layout)
    except SetupRefused as exc:
        marker_state = "invalid"
        checks.append(_check("recipe_marker", False, exc.message))
    else:
        checks.append(_check("recipe_marker", marker_state == "valid", "present" if marker_state == "valid" else "absent"))

    profile_home: Path | None = None
    if marker_state == "valid":
        try:
            profile_config = Path(native.config_path(WRITING_PROFILE))
            profile_home = profile_config.parent
            config = _read_yaml(profile_config)
            findings = writing_context.profile_findings(config)
            checks.append(
                _check("profile_check", not findings, ", ".join(item["code"] for item in findings) or "fresh context ready")
            )
            external = _string_list(_section(dict(config), "skills").get("external_dirs"))
            checks.append(
                _check("capability_dir_registered", str(layout.capability_skills) in external, str(layout.capability_skills))
            )
            plugin = profile_home / "plugins" / writing_context.PLUGIN_NAME / "__init__.py"
            checks.append(_check("context_plugin_installed", plugin.is_file(), writing_context.PLUGIN_NAME))

            root = _read_yaml(Path(native.config_path(COORDINATOR_PROFILE)))
            allow = _string_list(_section(dict(root), "kanban").get("dispatch_profiles"))
            checks.append(
                _check(
                    "dispatch_allowlist",
                    WRITING_PROFILE in allow and COORDINATOR_PROFILE not in allow,
                    json.dumps(allow),
                )
            )

            wanted = {*DOCUMENT_SKILLS, DIAGRAM_SKILL, GUIDANCE_SKILL}
            seen = set(native.discovered_skills(WRITING_PROFILE))
            missing = sorted(wanted - seen)
            checks.append(_check("native_skill_discovery", not missing, "missing: " + ", ".join(missing) if missing else "all required skills discovered"))
        except (SetupFailed, SetupRefused) as exc:
            checks.append(_check("profile_state", False, str(exc)))

    checks.extend(_capability_checks(layout, profile_home, runner, interpreters))
    return {
        "ok": all(item["status"] == "pass" for item in checks),
        "action": "verify",
        "checks": checks,
        "tasks_exist": _safe_task_count(layout),
        "office_visual_rendering": office_rendering_status(),
        "note": "Office visual rendering is optional in v1; structural checks do not establish pagination or slide layout.",
    }


def _safe_task_count(layout: Layout) -> bool | None:
    try:
        return task_count(layout) > 0
    except SetupRefused:
        return None


def _capability_checks(
    layout: Layout,
    profile_home: Path | None,
    runner: Runner,
    interpreters: Sequence[str],
) -> list[dict[str, Any]]:
    env = {k: v for k, v in os.environ.items() if k not in _IDENTITY_ENV}
    skills_root = (profile_home / "skills" / "productivity") if profile_home else None
    results: list[dict[str, Any]] = []
    if not layout.smoke.is_file():
        return [_check("capability_smoke", False, "smoke.py is not present in the image")]
    present = [item for item in interpreters if Path(item).is_file()]
    if not present:
        return [_check("documents", False, "no image interpreter was found")]
    for interpreter in present:
        argv = [interpreter, str(layout.smoke), "documents"]
        if skills_root is not None and skills_root.is_dir():
            argv += ["--skills-root", str(skills_root)]
        completed = _run_capability(runner, argv, env)
        results.append(_check(f"documents[{interpreter}]", completed[0], completed[1]))
    completed = _run_capability(runner, [present[0], str(layout.smoke), "diagram", "--assets", str(layout.assets)], env)
    results.append(_check("diagram_offline", completed[0], completed[1]))
    return results


def _run_capability(runner: Runner, argv: Sequence[str], env: Mapping[str, str]) -> tuple[bool, str]:
    try:
        completed = runner(argv, env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, type(exc).__name__
    lines = (completed.stdout or "").strip().splitlines() or (completed.stderr or "").strip().splitlines()
    detail = lines[-1] if lines else ""
    return completed.returncode == 0, detail[:300]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_layout(args: argparse.Namespace) -> Layout:
    return Layout(
        home=Path(args.home or os.environ.get("HERMES_HOME") or DEFAULT_HOME),
        assets=Path(args.assets or os.environ.get("HELMET_WRITING_ASSETS") or DEFAULT_ASSETS),
        guidance_pack=Path(args.guidance_pack or os.environ.get("HELMET_WRITING_GUIDANCE") or DEFAULT_GUIDANCE_PACK),
    )


def main(argv: Sequence[str] | None = None, *, native: Any = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hermes_helmet.writing_setup",
        description="Configure or verify the fresh Helmet writing worker (see the module docstring).",
    )
    parser.add_argument("action", choices=("configure", "verify"))
    parser.add_argument("--home", help="worker HERMES_HOME (default: $HERMES_HOME or /opt/data)")
    parser.add_argument("--assets", help="writing image asset directory")
    parser.add_argument("--guidance-pack", dest="guidance_pack", help="neutral guidance pack source directory")
    args = parser.parse_args(list(argv) if argv is not None else None)
    layout = build_layout(args)
    backend = native or HermesNative(layout.home)
    try:
        payload = configure(layout, backend) if args.action == "configure" else verify(layout, backend)
    except SetupRefused as exc:
        sys.stdout.write(json.dumps({"ok": False, "refused": exc.code, "message": exc.message, "changed": False}, indent=2) + "\n")
        return 1
    except SetupFailed as exc:
        sys.stdout.write(
            json.dumps(
                {
                    "ok": False,
                    "failed": str(exc),
                    "hint": "if the recipe marker was not written, discard the dedicated pre-use project and retry",
                },
                indent=2,
            )
            + "\n"
        )
        return 1
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
