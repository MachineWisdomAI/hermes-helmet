"""Provision and verify role profiles inside a Hermes worker, while drained.

The installer invokes this module as the worker UID. It does not copy profile
history or OAuth grants, and does not start workers or restart the gateway.
"""
from __future__ import annotations

import json
import io
import logging
import os
from pathlib import Path
import shutil
import subprocess
from typing import Callable

from .worker_roles import WorkerRoles

HERMES = "hermes"
DESCRIPTIONS = {
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
        if kanban.get("decompose") is not False or kanban.get("dispatch_in_gateway") is not False:
            raise RuntimeError(f"worker dispatcher/decomposition drift: {home.name}")
        if config.get("delegation", {}).get("max_concurrent_children") != 1:
            raise RuntimeError(f"worker delegation drift: {home.name}")
        if not (home / "home").is_dir() or not (home / "skills").is_dir():
            raise RuntimeError(f"worker profile incomplete: {home.name}")


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
                # Use company-approved configuration, but retain unrelated role-specific keys.
                for key in ("model", "agent", "fallback_providers", "provider_routing", "providers"):
                    if key in template:
                        config[key] = template[key]
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
            config.setdefault("model", {}).update(provider=provider, default=model)
            config.setdefault("agent", {})["max_turns"] = turns
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
        f"roles = WorkerRoles({roles.profiles!r}, {roles.max_in_progress!r}, {roles.max_in_progress_per_profile!r}); "
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
    args = parser.parse_args(argv)
    os.environ["HERMES_HOME"] = str(args.state_root)
    policy = load_policy(args.config)
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
