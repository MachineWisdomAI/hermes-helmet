"""Provision and verify role profiles inside a Hermes worker, while drained.

The installer invokes this module as the worker UID. It does not copy profile
history or OAuth grants, and does not start workers or restart the gateway.

An owned stock builder persona is linked to the accepted root company contract
through the same SOUL path as other roles. The replaced persona is backed up
under ``.helmet-private/identity-backups`` and can be restored, while drained,
with ``--recover-identity PROFILE``.
"""
from __future__ import annotations

import hashlib
import json
import io
import logging
import os
import copy
import tempfile
from pathlib import Path
import shutil
import subprocess
from typing import Callable

from .worker_roles import PROFILE_RE, WorkerRoles

HERMES = "hermes"
DESCRIPTIONS = {
    "writing": "Documentation, technical specifications, media, marketing and public writing within the accepted task scope.",
    "reviewer": "Independent pull request review against acceptance criteria and repository rules.",
    "documentation": "Documentation, technical specifications and user-facing instructions.",
    "media": "Media, marketing and public writing within the accepted task scope.",
}


def require_drained(hermes_bin: str = HERMES) -> None:
    from agent.estop import check_paused
    from hermes_cli import kanban_db as kb
    # Fail closed if pause/board APIs are unavailable. Pause persists over restart.
    if not check_paused("kanban", logging.getLogger(__name__)):
        raise RuntimeError("pause Hermes dispatch and drain all running tasks before installing roles")
    for board in kb.list_boards(include_archived=False):
        result = subprocess.run(
            [hermes_bin, "kanban", "--board", board["slug"], "list", "--status", "running", "--json"],
            check=True, capture_output=True, text=True,
        )
        if json.loads(result.stdout):
            raise RuntimeError("running tasks remain; role installation requires a drained worker")


def _yaml_load(text: str):
    try:
        import yaml
    except ModuleNotFoundError as exc:
        if exc.name != "yaml":
            raise
        from ruamel.yaml import YAML
        return YAML(typ="safe").load(text)
    return yaml.safe_load(text)


def _yaml_dump(value: dict) -> str:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        if exc.name != "yaml":
            raise
        from ruamel.yaml import YAML
        serializer = YAML(typ="safe")
        serializer.default_flow_style = False
        serializer.sort_base_mapping_type_on_output = False
        stream = io.StringIO()
        serializer.dump(value, stream)
        return stream.getvalue()
    return yaml.safe_dump(value, sort_keys=False)


_PRIVATE_DIR = ".helmet-private"
_DISCOVERY_NAMES = ("profiles", "skills", "memories", "cron", "sessions", "home", "logs")


def _approved_soul_link(soul: Path, source: Path) -> bool:
    if not soul.is_symlink():
        return False
    try:
        return soul.resolve() == source.resolve()
    except OSError:
        return False


def _owned_stock_persona(path: Path) -> bool:
    """Recognize an unchanged installed stock persona by content and ownership.

    Filename, length, and a shared marker are not ownership. A symlink is never
    owned content: replacing through it could modify another file.
    """
    if path.is_symlink() or not path.is_file():
        return False
    try:
        info = path.stat()
        content = path.read_bytes()
    except OSError:
        return False
    if info.st_uid != os.getuid():
        return False
    try:
        from hermes_cli.default_soul import DEFAULT_SOUL_MD
    except ImportError:
        return False
    return content == DEFAULT_SOUL_MD.encode("utf-8")


def _discovery_roots(root: Path) -> list[Path]:
    roots = [root / name for name in _DISCOVERY_NAMES]
    profiles = root / "profiles"
    if profiles.is_dir() and not profiles.is_symlink():
        for entry in profiles.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                roots.append(entry)
                roots.extend(entry / name for name in _DISCOVERY_NAMES)
    return roots


def _outside_discovery(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
    except OSError:
        return False
    if resolved == root_resolved or (resolved.name == "SOUL.md" and resolved.parent == root_resolved):
        return False
    for base in _discovery_roots(root):
        try:
            resolved.relative_to(base.resolve())
        except (OSError, ValueError):
            continue
        return False
    return True


def _backup_dir(root: Path, profile: str) -> Path:
    if not PROFILE_RE.fullmatch(profile):
        raise RuntimeError("refusing unsafe profile name")
    destination = root / _PRIVATE_DIR / "identity-backups" / profile
    for directory in (root / _PRIVATE_DIR, root / _PRIVATE_DIR / "identity-backups", destination):
        if directory.is_symlink():
            raise RuntimeError("refusing symlink identity backup directory")
    if not _outside_discovery(destination, root):
        raise RuntimeError("identity backup would land inside a discovery path")
    return destination


def _atomic_private_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"refusing symlink backup: {path.name}")
    temporary = path.with_name(path.name + ".new")
    if temporary.exists() or temporary.is_symlink():
        raise RuntimeError(f"refusing occupied temporary path: {temporary.name}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    os.chmod(path, 0o600)


def _backup_owned_identity(root: Path, profile: str, soul: Path) -> Path:
    """Copy a replaced persona outside profile, skill, and memory discovery."""
    if soul.is_symlink() or not soul.is_file():
        raise RuntimeError("refusing to back up an unowned identity")
    data = soul.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    destination = _backup_dir(root, profile)
    private = root / _PRIVATE_DIR
    if private.is_symlink() or (private / "identity-backups").is_symlink():
        raise RuntimeError("refusing symlink private backup directory")
    private.mkdir(exist_ok=True)
    os.chmod(private, 0o700)
    backups = private / "identity-backups"
    backups.mkdir(exist_ok=True)
    os.chmod(backups, 0o700)
    destination.mkdir(exist_ok=True)
    os.chmod(destination, 0o700)
    backup = destination / f"{digest}.md"
    if backup.exists() or backup.is_symlink():
        if backup.is_symlink() or not backup.is_file() or backup.read_bytes() != data:
            raise RuntimeError("identity backup path is unsafe")
    else:
        _atomic_private_write(backup, data)
    if backup.name == "SOUL.md" or not _outside_discovery(backup, root):
        raise RuntimeError("identity backup is inside a discovery path")
    receipt = {
        "schema": 1,
        "profile": profile,
        "digest": "sha256:" + digest,
        "backup": backup.relative_to(root).as_posix(),
        "replaced": f"profiles/{profile}/SOUL.md",
    }
    _atomic_private_write(destination / "receipt.json", (json.dumps(receipt, indent=2) + "\n").encode())
    return backup


def recover_owned_identity(root: Path, profile: str) -> None:
    """Restore a backed-up persona. Refuses custom content and unexpected symlinks."""
    destination = _backup_dir(root, profile)
    receipt_path = destination / "receipt.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise RuntimeError("no owned identity receipt")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("unreadable owned identity receipt") from exc
    if not isinstance(receipt, dict) or receipt.get("schema") != 1 or receipt.get("profile") != profile:
        raise RuntimeError("unexpected owned identity receipt")
    recorded = receipt.get("backup")
    if not isinstance(recorded, str) or Path(recorded).is_absolute():
        raise RuntimeError("identity backup path is not relative")
    backup = root / recorded
    try:
        resolved = backup.resolve()
        # The backup must stay inside this profile's private backup directory.
        resolved.relative_to(destination.resolve())
    except (OSError, ValueError) as exc:
        raise RuntimeError("identity backup escapes the private backup directory") from exc
    if backup.is_symlink() or not backup.is_file():
        raise RuntimeError("identity backup is not a private regular file")
    if not _outside_discovery(resolved, root):
        raise RuntimeError("identity backup is not a private regular file")
    data = backup.read_bytes()
    if "sha256:" + hashlib.sha256(data).hexdigest() != receipt.get("digest"):
        raise RuntimeError("identity backup digest mismatch")
    home = root / "profiles" / profile
    if home.is_symlink() or not home.is_dir():
        raise RuntimeError("profile home must be a regular directory")
    soul = home / "SOUL.md"
    source = root / "SOUL.md"
    if soul.is_symlink():
        if not _approved_soul_link(soul, source):
            raise RuntimeError(f"refusing to replace an unexpected SOUL symlink for {profile}")
        soul.unlink()
    elif soul.exists():
        current = soul.read_bytes()
        source_bytes = source.read_bytes() if source.is_file() and not source.is_symlink() else None
        if current != data and current != source_bytes:
            raise RuntimeError(f"refusing to overwrite a custom persona for {profile}")
        soul.unlink()
    _atomic_private_write(soul, data)


def _load_effective_identity(home: Path) -> str | None:
    """Identity Hermes will inject for this home, via the installed loader when present."""
    try:
        from agent.prompt_builder import load_soul_md
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    except ImportError:
        return _read_profile_identity(home)
    # The loader ensures the ambient home before reading home_override. Point
    # that ambient home at a scratch directory so verification does not seed
    # or rewrite the profile being checked.
    with tempfile.TemporaryDirectory() as scratch:
        token = set_hermes_home_override(scratch)
        try:
            return load_soul_md(home_override=home)
        finally:
            reset_hermes_home_override(token)


def _read_profile_identity(home: Path) -> str | None:
    soul = home / "SOUL.md"
    if not soul.exists():
        return None
    try:
        text = soul.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    text = text.strip()
    return text or None


def _company_identity(root: Path) -> str:
    source = root / "SOUL.md"
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("action required: company contract is missing")
    loaded = _load_effective_identity(root)
    if not loaded:
        raise RuntimeError("action required: company contract is empty")
    return loaded


def _require_delivered_identity(root: Path, home: Path, contract: str) -> None:
    soul = home / "SOUL.md"
    source = root / "SOUL.md"
    if soul.is_symlink() and not _approved_soul_link(soul, source):
        raise RuntimeError(f"action required: unexpected SOUL symlink for {home.name}")
    loaded = _load_effective_identity(home)
    if not loaded or loaded != contract or loaded.count(contract) != 1:
        raise RuntimeError(f"action required: company contract missing or stale for {home.name}")


def _route_stock_builder_contract(root: Path, home: Path, link: Callable[[Path, Path], None],
                                  snapshots: dict[Path, tuple[bytes, int] | None]) -> None:
    """Link an owned stock builder persona to the accepted contract. Leave custom bytes alone."""
    soul = home / "SOUL.md"
    source = root / "SOUL.md"
    if _approved_soul_link(soul, source):
        return
    if soul.is_symlink() or (soul.exists() and not _owned_stock_persona(soul)):
        return
    if not source.exists():
        return
    if soul.exists():
        _backup_owned_identity(root, home.name, soul)
        if soul not in snapshots:
            snapshots[soul] = (soul.read_bytes(), soul.stat().st_mode & 0o777)
        soul.unlink()
    link(source, soul)


def _read_config(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"expected a regular config: {path}")
    value = _yaml_load(path.read_text())
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid config mapping: {path}")
    return value


def verify(root: Path, roles: WorkerRoles, *, provider: str, model: str, turns: int) -> None:
    paths = [root] + [root / "profiles" / name for _, name in roles.profiles]
    for home in paths:
        config = _read_config(home / "config.yaml")
        kanban = config.get("kanban", {})
        if (kanban.get("max_in_progress"), kanban.get("max_in_progress_per_profile")) != (
            roles.max_in_progress, roles.max_in_progress_per_profile
        ):
            raise RuntimeError(f"worker capacity drift: {home.name}")
        if home == root:
            continue
        if config.get("model", {}).get("provider") != provider or config.get("model", {}).get("default") != model:
            raise RuntimeError(f"worker model drift: {home.name}")
        if config.get("agent", {}).get("max_turns") != turns:
            raise RuntimeError(f"worker budget drift: {home.name}")
        role = next(role for role, name in roles.profiles if name == home.name)
        effort = dict(roles.reasoning_effort).get(role)
        if effort is not None and config.get("agent", {}).get("reasoning_overrides", {}).get(model) != effort:
            raise RuntimeError(f"worker reasoning effort drift: {home.name}")
        if kanban.get("decompose") is not False or kanban.get("dispatch_in_gateway") is not False:
            raise RuntimeError(f"worker dispatcher/decomposition drift: {home.name}")
        if config.get("delegation", {}).get("max_concurrent_children") != 1:
            raise RuntimeError(f"worker delegation drift: {home.name}")
        if not (home / "home").is_dir() or not (home / "skills").is_dir():
            raise RuntimeError(f"worker profile incomplete: {home.name}")
    contract = _company_identity(root)
    for _, name in roles.profiles:
        _require_delivered_identity(root, root / "profiles" / name, contract)


def create_profile(name: str, role: str, hermes_bin: str = HERMES) -> None:
    subprocess.run([hermes_bin, "profile", "create", name, "--no-alias", "--no-skills",
                    "--description", DESCRIPTIONS[role]], check=True, capture_output=True, text=True)


def provision(root: Path, roles: WorkerRoles, *, provider: str, model: str, turns: int,
              create: Callable[[str, str], None] = create_profile,
              guard: Callable[[], None] = require_drained,
              runtime_check: Callable[..., None] | None = None) -> None:
    """Transactional leaf updates; preserve unrelated profile state on reinstallation."""
    guard()
    builder = root / "profiles" / roles.profile("builder")
    template = _read_config(builder / "config.yaml")
    snapshots: dict[Path, tuple[bytes, int] | None] = {}
    created_profiles: list[Path] = []
    created_links: list[Path] = []

    def write(path: Path, content: bytes) -> None:
        if path.is_symlink():
            raise RuntimeError(f"refusing symlink config: {path}")
        if path not in snapshots:
            snapshots[path] = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        # Files are small; replace atomically so readers never see partial YAML.
        tmp = path.with_name(path.name + ".role-new")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def link(source: Path, target: Path) -> None:
        if target.exists() or target.is_symlink():
            return  # Preserve custom profile tools/skills; never replace user data.
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source)
        created_links.append(target)

    try:
        for role, name in roles.profiles:
            home = root / "profiles" / name
            if home.is_symlink():
                raise RuntimeError("profile home must not be a symlink")
            if not home.exists():
                created_profiles.append(home)
                create(name, role)
            config = _read_config(home / "config.yaml") if (home / "config.yaml").exists() else {}
            if role != "builder":
                existing_overrides = copy.deepcopy(config.get("agent", {}).get("reasoning_overrides", {}))
                # Use company-approved configuration, but retain unrelated role-specific keys.
                for key in ("model", "agent", "fallback_providers", "provider_routing", "providers"):
                    if key in template:
                        config[key] = copy.deepcopy(template[key])
                if existing_overrides:
                    config.setdefault("agent", {}).setdefault("reasoning_overrides", {}).update(existing_overrides)
                for filename in (".env", "home/.gitconfig"):
                    source = builder / filename
                    if source.is_file() and not source.is_symlink():
                        write(home / filename, source.read_bytes())
                # Company contract and installed skills track the existing approved sources.
                soul = home / "SOUL.md"
                if not (soul.is_symlink() and soul.resolve() == (root / "SOUL.md").resolve()):
                    if soul.is_symlink():
                        raise RuntimeError("unexpected role SOUL symlink")
                    snapshots[soul] = (soul.read_bytes(), soul.stat().st_mode & 0o777) if soul.exists() else None
                    soul.unlink(missing_ok=True)
                    link(root / "SOUL.md", soul)
                for skill_root in (root / "skills", builder / "skills"):
                    if skill_root.is_dir():
                        for skill in skill_root.iterdir():
                            if not skill.name.startswith(".") and skill.is_dir():
                                link(skill, home / "skills" / skill.name)
                for relative in ("bin", "home/.local/bin"):
                    tools = builder / relative
                    if tools.is_dir():
                        for tool in tools.iterdir():
                            if tool.is_file() and os.access(tool, os.X_OK):
                                link(tool.resolve(), home / relative / tool.name)
            else:
                _route_stock_builder_contract(root, home, link, snapshots)
            config.setdefault("model", {}).update(provider=provider, default=model)
            config.setdefault("agent", {})["max_turns"] = turns
            effort = dict(roles.reasoning_effort).get(role)
            if effort is not None:
                config["agent"].setdefault("reasoning_overrides", {})[model] = effort
            config.setdefault("kanban", {}).update(
                max_in_progress=roles.max_in_progress,
                max_in_progress_per_profile=roles.max_in_progress_per_profile,
                decompose=False, dispatch_in_gateway=False,
            )
            config.setdefault("delegation", {})["max_concurrent_children"] = 1
            write(home / "config.yaml", _yaml_dump(config).encode())
        config = _read_config(root / "config.yaml")
        config.setdefault("kanban", {}).update(max_in_progress=roles.max_in_progress,
                                               max_in_progress_per_profile=roles.max_in_progress_per_profile)
        write(root / "config.yaml", _yaml_dump(config).encode())
        verify(root, roles, provider=provider, model=model, turns=turns)
        if runtime_check is not None:
            runtime_check(root, roles)
    except BaseException:
        for link_path in reversed(created_links):
            link_path.unlink(missing_ok=True)
        for path, old in snapshots.items():
            if old is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(old[0])
                path.chmod(old[1])
        for home in reversed(created_profiles):
            if home.exists():
                shutil.rmtree(home)
        raise


def verify_identity(root: Path, roles: WorkerRoles, expected: str, *, gh_bin: str = "gh",
                    git_credential_helper: str | None = None) -> None:
    from hermes_constants import get_subprocess_home
    for _, name in roles.profiles:
        home = root / "profiles" / name
        env = dict(os.environ, HERMES_HOME=str(home), HERMES_PROFILE=name)
        resolved = get_subprocess_home(env)
        if resolved != str(home / "home"):
            raise RuntimeError(f"unexpected subprocess HOME for {name}")
        env["HOME"] = resolved
        env["PATH"] = os.pathsep.join((str(home / "bin"), str(home / "home/.local/bin"), env.get("PATH", "")))
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        result = subprocess.run([gh_bin, "api", "user", "--jq", ".login"],
                                env=env, check=True, capture_output=True, text=True)
        if result.stdout.strip() != expected:
            raise RuntimeError(f"company GitHub identity mismatch for {name}")
        if git_credential_helper is None:
            continue
        helper = subprocess.run(["git", "config", "--global", "--get-all", "credential.https://github.com.helper"],
                                env=env, check=True, capture_output=True, text=True)
        if helper.stdout.strip().splitlines() != [git_credential_helper]:
            raise RuntimeError(f"company Git credential helper mismatch for {name}")


def drain_check_program(*, hermes_bin: str = "hermes") -> str:
    """Run the same gate before a deployment adapter stages this package."""
    import inspect
    return ("import json, logging, subprocess\nHERMES = 'hermes'\n"
            + inspect.getsource(require_drained) + f"\nrequire_drained({hermes_bin!r})")


def verification_program(policy, *, state_root: str, gh_bin: str = "gh",
                         git_credential_helper: str | None = None) -> str:
    """Verify against accepted policy, rather than live policy drift."""
    roles = policy.worker_roles
    return (
        "from pathlib import Path; "
        "from hermes_helmet.worker_roles import WorkerRoles; "
        "from hermes_helmet.worker_role_runtime import verify, verify_identity; "
        f"roles = WorkerRoles({roles.profiles!r}, {roles.max_in_progress!r}, {roles.max_in_progress_per_profile!r}, {roles.reasoning_effort!r}); "
        f"verify(Path({state_root!r}), roles, provider={policy.inference_provider!r}, "
        f"model={policy.inference_model!r}, turns={policy.worker_max_turns!r}); "
        f"verify_identity(Path({state_root!r}), roles, {policy.worker_github_login!r}, "
        f"gh_bin={gh_bin!r}, git_credential_helper={git_credential_helper!r})"
    )


def main(argv: list[str] | None = None) -> int:
    import argparse
    from .authority import load_policy
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--hermes", default="hermes")
    parser.add_argument("--gh", default="gh")
    parser.add_argument("--git-credential-helper")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true")
    mode.add_argument("--check-drained", action="store_true")
    mode.add_argument("--recover-identity", metavar="PROFILE")
    args = parser.parse_args(argv)
    os.environ["HERMES_HOME"] = str(args.state_root)
    policy = load_policy(args.config)
    if args.recover_identity:
        require_drained(args.hermes)
        recover_owned_identity(args.state_root, args.recover_identity)
        print(f"Restored owned identity for {args.recover_identity}.")
        return 0
    if policy.worker_roles is None:
        return 0
    if args.check_drained:
        require_drained(args.hermes)
        return 0
    kwargs = dict(provider=policy.inference_provider, model=policy.inference_model, turns=policy.worker_max_turns)
    identity = lambda root, roles: verify_identity(root, roles, policy.worker_github_login,
                         gh_bin=args.gh, git_credential_helper=args.git_credential_helper)
    if args.verify:
        verify(args.state_root, policy.worker_roles, **kwargs)
        identity(args.state_root, policy.worker_roles)
    else:
        provision(args.state_root, policy.worker_roles, **kwargs,
                  create=lambda name, role: create_profile(name, role, args.hermes),
                  guard=lambda: require_drained(args.hermes), runtime_check=identity)
    print("Worker role configuration verified; scheduler activation requires a drained gateway restart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
