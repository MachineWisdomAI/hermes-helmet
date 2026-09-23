#!/usr/bin/env python3
"""Install portable Hermes Helmet Captain skills into host skill directories.

Supported targets (static validation covers all three; runtime dogfood is Codex):

- codex:   ~/.codex/skills/<name>/SKILL.md
- claude:  ~/.claude/skills/<name>/SKILL.md
- hermes:  ~/.hermes/skills/hermes-helmet/<name>/SKILL.md

This path is an explicit setup action for Captain/orchestrator skills only.
Executor company packs use ``company_skills`` and never write these host-global
trees at runtime.

Skill assets ship inside the installed package under ``bundled_skills/`` so
hosts without a source checkout still work after ``pip install hermes-helmet``.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import tempfile
from dataclasses import dataclass
from typing import Sequence


PACKAGE_DIR = Path(__file__).resolve().parent
# Prefer package-bundled skills (installed wheel/sdist); fall back to repo tree.
_BUNDLED = PACKAGE_DIR / "bundled_skills"
_REPO_SKILLS = PACKAGE_DIR.parents[1] / "skills"
SKILLS_SOURCE = _BUNDLED if _BUNDLED.is_dir() else _REPO_SKILLS

DEFAULT_TARGETS = frozenset({"codex", "claude", "hermes"})

# Relative destinations under $HOME (or --prefix).
TARGET_RELATIVE = {
    "codex": Path(".codex/skills"),
    "claude": Path(".claude/skills"),
    "hermes": Path(".hermes/skills/hermes-helmet"),
}


class InstallError(RuntimeError):
    """Skill installation cannot complete safely."""


@dataclass(frozen=True)
class InstallReport:
    installed: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    skipped_hosts: tuple[str, ...] = ()


_LAST_REPORT = InstallReport()


def last_install_report() -> InstallReport:
    return _LAST_REPORT


def default_skills_source() -> Path:
    if _BUNDLED.is_dir() and any(_BUNDLED.iterdir()):
        return _BUNDLED
    if _REPO_SKILLS.is_dir():
        return _REPO_SKILLS
    raise InstallError(
        f"no bundled skills found under {_BUNDLED} or {_REPO_SKILLS}; "
        "install hermes-helmet with package data or run from a full checkout"
    )


def iter_bundled_skills(source_root: Path | None = None) -> list[Path]:
    root = source_root if source_root is not None else default_skills_source()
    if not root.is_dir():
        raise InstallError(f"skills source directory missing: {root}")
    skills: list[Path] = []
    for path in sorted(root.iterdir()):
        skill_md = path / "SKILL.md"
        if path.is_dir() and skill_md.is_file():
            skills.append(path)
    if not skills:
        raise InstallError(f"no bundled skills found under {root}")
    return skills


def resolve_target_dir(target: str, *, prefix: Path | None = None) -> Path:
    if target not in TARGET_RELATIVE:
        raise InstallError(f"unsupported install target: {target}")
    root = prefix if prefix is not None else Path.home()
    return root / TARGET_RELATIVE[target]


def validate_managed_path(
    root: Path,
    path: Path,
    *,
    final_directory: bool | None = None,
) -> None:
    """Validate each existing component below an owner-controlled boundary."""

    try:
        relative = path.relative_to(root)
    except ValueError:
        raise InstallError("managed path escapes the selected home") from None
    components = (root, *(root / Path(*relative.parts[:index]) for index in range(1, len(relative.parts) + 1)))
    for index, component in enumerate(components):
        try:
            info = component.lstat()
        except FileNotFoundError:
            return
        label = "." if component == root else str(component.relative_to(root))
        if stat.S_ISLNK(info.st_mode):
            raise InstallError(f"managed path component must not be a symlink: {label}")
        if info.st_uid != os.getuid():
            raise InstallError(f"managed path component must be owned by the current user: {label}")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise InstallError(f"managed path component must not be group- or other-writable: {label}")
        final = index == len(components) - 1
        if not final and not stat.S_ISDIR(info.st_mode):
            raise InstallError(f"managed path ancestor must be a directory: {label}")
        if final_directory is True and final and not stat.S_ISDIR(info.st_mode):
            raise InstallError(f"managed path must be a directory: {label}")
        if final_directory is False and final and not stat.S_ISREG(info.st_mode):
            raise InstallError(f"managed path must be a regular file: {label}")


def validate_skill_destinations(
    *,
    targets: Sequence[str],
    prefix: Path,
    skills: Sequence[Path],
) -> None:
    """Fail closed on redirected or locally writable skill destinations."""

    validate_managed_path(prefix, prefix, final_directory=True)
    for target in targets:
        dest_parent = resolve_target_dir(target, prefix=prefix)
        validate_managed_path(prefix, dest_parent, final_directory=True)
        for skill_dir in skills:
            dest = dest_parent / skill_dir.name
            validate_managed_path(prefix, dest, final_directory=True)
            validate_managed_path(prefix, dest / "SKILL.md", final_directory=False)


def validate_skill_tree(skill_dir: Path) -> None:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file() or skill_md.stat().st_size < 1:
        raise InstallError(f"skill missing SKILL.md: {skill_dir}")
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise InstallError(f"SKILL.md must start with YAML frontmatter: {skill_md}")
    if "\n---\n" not in text[3:]:
        raise InstallError(f"SKILL.md frontmatter is not closed: {skill_md}")
    # Minimal required keys without requiring PyYAML.
    head = text.split("\n---\n", 1)[0]
    if "name:" not in head or "description:" not in head:
        raise InstallError(f"SKILL.md frontmatter requires name and description: {skill_md}")
    # No secrets in skill text.
    lowered = text.casefold()
    for needle in ("ghp_", "github_pat_", "sk-", "xai-"):
        if needle in lowered:
            raise InstallError(f"skill appears to contain a secret-shaped token: {skill_md}")
    # Portable entrypoints must not require a checkout-local module path alone.
    if "python -m hermes_helmet" in text and "hermes-helmet" not in text:
        raise InstallError(
            f"skill must document the hermes-helmet console script for portability: {skill_md}"
        )


def install_one(
    skill_dir: Path,
    dest_parent: Path,
    *,
    dry_run: bool = False,
) -> Path:
    validate_skill_tree(skill_dir)
    dest = dest_parent / skill_dir.name
    if dry_run:
        return dest
    dest_parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if dest.exists():
        raise InstallError(f"destination already exists: {dest}")
    staging_root = Path(tempfile.mkdtemp(prefix=f".{skill_dir.name}-", dir=dest_parent))
    staging = staging_root / skill_dir.name
    try:
        shutil.copytree(skill_dir, staging)
        os.replace(staging, dest)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    # Ensure the installed tree is world-readable but not secret-bearing.
    for path in [dest, *dest.rglob("*")]:
        if path.is_dir():
            os.chmod(path, 0o755)
        elif path.is_file():
            os.chmod(path, 0o644)
    return dest


def install_skills(
    *,
    targets: Sequence[str] | None = None,
    prefix: Path | None = None,
    dry_run: bool = False,
    source_root: Path | None = None,
) -> list[str]:
    global _LAST_REPORT
    selected = list(targets) if targets else sorted(DEFAULT_TARGETS)
    unknown = [item for item in selected if item not in DEFAULT_TARGETS]
    if unknown:
        raise InstallError(f"unsupported install targets: {', '.join(unknown)}")
    skills = iter_bundled_skills(source_root)
    root = prefix if prefix is not None else Path.home()
    validate_skill_destinations(targets=selected, prefix=root, skills=skills)
    installed: list[str] = []
    conflicts: list[str] = []
    planned: list[tuple[str, Path, Path]] = []
    for target in selected:
        dest_parent = resolve_target_dir(target, prefix=prefix)
        for skill_dir in skills:
            validate_skill_tree(skill_dir)
            dest = dest_parent / skill_dir.name
            if dest.exists() or dest.is_symlink():
                source_text = (skill_dir / "SKILL.md").read_bytes()
                installed_file = dest / "SKILL.md"
                if (
                    not dest.is_symlink()
                    and dest.is_dir()
                    and installed_file.is_file()
                    and not installed_file.is_symlink()
                    and installed_file.read_bytes() == source_text
                ):
                    installed.append(f"{target}:{dest}")
                    continue
                conflicts.append(f"{target}:{dest}")
            else:
                planned.append((target, skill_dir, dest))
    if conflicts:
        _LAST_REPORT = InstallReport(
            installed=tuple(installed),
            conflicts=tuple(conflicts),
            skipped_hosts=tuple(selected),
        )
        return installed
    if dry_run:
        installed.extend(f"{target}:{dest}" for target, _skill, dest in planned)
        _LAST_REPORT = InstallReport(installed=tuple(installed))
        return installed

    root.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=".hermes-helmet-skills-", dir=root))
    staged: list[tuple[str, Path, Path]] = []
    created: list[Path] = []
    created_parents: list[Path] = []
    try:
        for index, (target, skill_dir, dest) in enumerate(planned):
            staging = staging_root / f"{index}-{target}-{skill_dir.name}"
            shutil.copytree(skill_dir, staging)
            staged.append((target, staging, dest))
        for target, staging, dest in staged:
            if not dest.parent.exists():
                dest.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                created_parents.append(dest.parent)
            os.replace(staging, dest)
            created.append(dest)
            for path in [dest, *dest.rglob("*")]:
                os.chmod(path, 0o755 if path.is_dir() else 0o644)
            installed.append(f"{target}:{dest}")
    except Exception as exc:  # noqa: BLE001 - transaction must roll back every failure
        for dest in reversed(created):
            shutil.rmtree(dest, ignore_errors=True)
        for parent in reversed(created_parents):
            current = parent
            while current != root and current.is_dir():
                try:
                    current.rmdir()
                except OSError:
                    break
                current = current.parent
        _LAST_REPORT = InstallReport()
        raise InstallError("skill installation failed; all new destinations rolled back") from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    _LAST_REPORT = InstallReport(
        installed=tuple(installed),
    )
    return installed


def preflight_skills(
    *,
    targets: Sequence[str] | None = None,
    prefix: Path | None = None,
    source_root: Path | None = None,
) -> InstallReport:
    """Inspect every selected destination without creating or changing paths."""

    selected = list(targets) if targets else sorted(DEFAULT_TARGETS)
    unknown = [item for item in selected if item not in DEFAULT_TARGETS]
    if unknown:
        raise InstallError(f"unsupported install targets: {', '.join(unknown)}")
    skills = iter_bundled_skills(source_root)
    root = prefix if prefix is not None else Path.home()
    validate_skill_destinations(targets=selected, prefix=root, skills=skills)
    installed: list[str] = []
    conflicts: list[str] = []
    for target in selected:
        dest_parent = resolve_target_dir(target, prefix=prefix)
        for skill_dir in skills:
            validate_skill_tree(skill_dir)
            dest = dest_parent / skill_dir.name
            if not dest.exists() and not dest.is_symlink():
                continue
            installed_file = dest / "SKILL.md"
            if (
                not dest.is_symlink()
                and dest.is_dir()
                and installed_file.is_file()
                and not installed_file.is_symlink()
                and installed_file.read_bytes() == (skill_dir / "SKILL.md").read_bytes()
            ):
                installed.append(f"{target}:{dest}")
            else:
                conflicts.append(f"{target}:{dest}")
    return InstallReport(
        installed=tuple(installed),
        conflicts=tuple(conflicts),
        skipped_hosts=tuple(selected) if conflicts else (),
    )


def static_validate_all_targets(source_root: Path | None = None) -> list[str]:
    """Validate bundled skills and that each target path formula is defined."""

    messages: list[str] = []
    skills = iter_bundled_skills(source_root)
    for skill_dir in skills:
        validate_skill_tree(skill_dir)
        messages.append(f"ok skill {skill_dir.name}")
    for target in sorted(DEFAULT_TARGETS):
        path = resolve_target_dir(
            target,
            prefix=Path(tempfile.gettempdir()) / "hermes-helmet-skill-validate",
        )
        messages.append(f"ok target {target} -> {path}")
    return messages
