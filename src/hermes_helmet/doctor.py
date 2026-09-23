#!/usr/bin/env python3
"""Read-only fail-closed diagnostics for a Hermes Helmet installation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess
from typing import Mapping
from urllib.parse import urlparse

from hermes_helmet.authority import Policy, authority_public_dict, load_authority, render_crew_contract
from hermes_helmet.company_skills import STATUS_SKIPPED, validate_company_pack
from hermes_helmet.fava_trails import doctor_fava_trails
from hermes_helmet.install_skills import (
    DEFAULT_TARGETS,
    InstallError,
    iter_bundled_skills,
    resolve_target_dir,
    validate_skill_destinations,
)
from hermes_helmet.model_lanes import ModelLaneError, doctor_model_lanes
from hermes_helmet.openviking import doctor_openviking
from hermes_helmet.setup import (
    GitHub,
    SetupError,
    _validate_helmet_managed_paths,
    _validate_home_boundary,
    _validate_owned_path,
    _verify_github,
    default_github_client,
)


def _section(ok: bool, message: str, **extra: object) -> dict[str, object]:
    return {"ok": ok, "message": message, **extra}


def _file_modes(root: Path, paths: tuple[Path, ...]) -> dict[str, object]:
    findings: list[str] = []
    for directory, label in (
        (root, "Helmet state directory"),
        (root / "secrets", "Helmet secrets directory"),
    ):
        try:
            _validate_owned_path(directory, directory=True, mode=0o700, label=label)
        except SetupError as exc:
            findings.append(str(exc))
    for path in paths:
        try:
            _validate_owned_path(path, directory=False, mode=0o600, label=path.name)
        except SetupError as exc:
            findings.append(str(exc))
    ok = not findings
    return _section(ok, "owner-only configuration files" if ok else "file mode check failed", findings=findings)


def _skills(prefix: Path) -> dict[str, object]:
    missing: list[str] = []
    mismatched: list[str] = []
    for source in iter_bundled_skills():
        expected = (source / "SKILL.md").read_bytes()
        for target in sorted(DEFAULT_TARGETS):
            installed = resolve_target_dir(target, prefix=prefix) / source.name / "SKILL.md"
            if not installed.is_file():
                missing.append(f"{target}:{source.name}")
            elif installed.read_bytes() != expected:
                mismatched.append(f"{target}:{source.name}")
    ok = not missing and not mismatched
    return _section(ok, "bundled skills match package contents" if ok else "bundled skill drift detected", missing=missing, mismatched=mismatched)


def _drift(policy_path: Path, contract_path: Path, state_path: Path) -> dict[str, object]:
    try:
        policy = load_authority(policy_path)
        canonical_policy = json.dumps(authority_public_dict(policy), indent=2, sort_keys=True) + "\n"
        canonical_contract = render_crew_contract(policy)
        current_policy = policy_path.read_text(encoding="utf-8")
        current_contract = contract_path.read_text(encoding="utf-8")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        fingerprint = hashlib.sha256((canonical_policy + "\0" + canonical_contract).encode("utf-8")).hexdigest()
        ok = (
            current_policy == canonical_policy
            and current_contract == canonical_contract
            and state.get("fingerprint") == fingerprint
            and state.get("status", "complete") == "complete"
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        ok = False
    return _section(ok, "policy and crew contract match setup state" if ok else "policy drift detected")


def _remote_slug(remote: str) -> str | None:
    text = remote.strip()
    if not text or text != remote or any(char.isspace() for char in text):
        return None
    scp_match = re.fullmatch(r"git@github\.com:([^/:]+/[^/:]+)", text)
    if scp_match:
        path = scp_match.group(1)
    else:
        try:
            parsed = urlparse(text)
            port = parsed.port
        except ValueError:
            return None
        if parsed.scheme == "https":
            if parsed.username or parsed.password or port is not None:
                return None
        elif parsed.scheme == "ssh":
            if parsed.username != "git" or parsed.password or port is not None:
                return None
        else:
            return None
        if (parsed.hostname or "").casefold() != "github.com":
            return None
        if parsed.query or parsed.fragment:
            return None
        if not parsed.path.startswith("/") or parsed.path.startswith("//") or parsed.path.endswith("/"):
            return None
        path = parsed.path[1:]
        if "%" in path:
            return None
    if path.endswith(".git"):
        path = path[:-4]
    pieces = path.strip("/").split("/")
    if len(pieces) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", item) for item in pieces):
        return None
    return "/".join(pieces)


def _checkouts(repositories: object) -> dict[str, object]:
    findings: list[str] = []
    for repo in repositories:  # type: ignore[union-attr]
        path = repo.worktree
        if not path.is_dir():
            findings.append(f"missing:{repo.slug}")
            continue
        try:
            root = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            fetch_urls = subprocess.run(
                ["git", "-C", str(path), "remote", "get-url", "--all", "origin"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.splitlines()
            push_urls = subprocess.run(
                ["git", "-C", str(path), "remote", "get-url", "--push", "--all", "origin"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            findings.append(f"not-git-worktree:{repo.slug}")
            continue
        if Path(root).resolve() != path.resolve():
            findings.append(f"not-worktree-root:{repo.slug}")
        urls = [item.strip() for item in (*fetch_urls, *push_urls) if item.strip()]
        if not urls or any(
            (_remote_slug(remote) or "").casefold() != repo.slug.casefold()
            for remote in urls
        ):
            findings.append(f"origin-url-mismatch:{repo.slug}")
    ok = not findings
    return _section(
        ok,
        "configured checkouts are Git worktrees with matching origins"
        if ok
        else "configured checkout validation failed",
        findings=findings,
    )


def company_skills_report(policy: Policy) -> dict[str, object]:
    """Report optional executor company-pack status without mutating state."""

    source = policy.skills.source if policy.skills.configured else None
    result = validate_company_pack(source, policy.skills.allowlist)
    return {
        "ok": result.ok,
        "message": result.message,
        "status": result.status,
        "skipped": result.status == STATUS_SKIPPED,
        "errors": list(result.errors),
    }


def signal_report(policy: Policy) -> dict[str, object]:
    """Report policy-selected Signal status without probing or mutating it."""

    if not policy.integrations.signal:
        return _section(
            True,
            "Signal integration declined; Helmet operates without it",
            enabled=False,
            skipped=True,
        )
    return _section(
        False,
        "Signal integration is enabled but no runtime readiness probe is configured",
        enabled=True,
        skipped=False,
    )


def doctor(
    policy_path: Path,
    *,
    home: Path | None = None,
    skills_prefix: Path | None = None,
    github: GitHub | None = None,
    secret_file: Path | None = None,
    principal_paths: Mapping[str, Path] | None = None,
    data_repo: Path | None = None,
    openviking_client_paths: Mapping[str, Path] | None = None,
    require_live: bool = True,
    model_lane_runtime_dir: Path | None = None,
    model_transport: object | None = None,
) -> dict[str, object]:
    """Inspect setup state without writing files, labels, or external services."""

    home = (home or Path.home()).expanduser()
    root = home / ".hermes-helmet"
    secret_file = secret_file or root / "secrets" / "github_worker_pat"
    contract_path = root / "generated" / "crew-contract.md"
    state_path = root / "setup-state.json"
    skills_root = skills_prefix or home
    try:
        _validate_home_boundary(home)
        _validate_helmet_managed_paths(home, extra_paths=(policy_path, secret_file))
        skill_sources = iter_bundled_skills()
        validate_skill_destinations(
            targets=tuple(sorted(DEFAULT_TARGETS)),
            prefix=skills_root,
            skills=skill_sources,
        )
        secret_dir = root / "secrets"
        try:
            secret_children = tuple(secret_dir.iterdir())
        except FileNotFoundError:
            secret_children = ()
        _validate_helmet_managed_paths(home, extra_paths=secret_children)
    except (SetupError, InstallError) as exc:
        return {
            "ok": False,
            "file_modes": _section(False, "managed path safety check failed", findings=[str(exc)]),
        }
    try:
        policy = load_authority(policy_path)
    except Exception as exc:  # authority errors are already secret-safe
        return {"ok": False, "policy": _section(False, str(exc))}

    identity = _section(policy.captain_github_login.casefold() != policy.worker_github_login.casefold(), "Captain and worker identities are distinct")
    secret_dir = root / "secrets"
    try:
        secret_info = secret_dir.lstat()
        safe_to_list = stat.S_ISDIR(secret_info.st_mode) and not stat.S_ISLNK(secret_info.st_mode)
    except FileNotFoundError:
        safe_to_list = False
    secret_paths = tuple(secret_dir.iterdir()) if safe_to_list else ()
    required_paths = (policy_path, contract_path, state_path, secret_file)
    file_modes = _file_modes(root, tuple(dict.fromkeys((*required_paths, *secret_paths))))
    checkouts = _checkouts(policy.repositories)

    if github is None and require_live:
        github = default_github_client()
    if github is None:
        repository_allowlist = _section(True, "live GitHub allowlist check skipped", skipped=True)
        labels = _section(True, "live GitHub label check skipped", skipped=True)
    elif not file_modes["ok"]:
        message = "secret path safety check failed"
        repository_allowlist = _section(False, message)
        labels = _section(False, message)
        identity = _section(False, message)
    else:
        try:
            token = secret_file.read_text(encoding="utf-8").strip()
            gh_report = _verify_github(policy, token, github, create_labels=False)
            repository_allowlist = _section(
                True,
                "worker repository allowlist and permissions verified",
                repositories=gh_report["repositories"],
                worker_access_scope=gh_report.get("worker_access_scope"),
            )
            labels = _section(True, "ready and dispatch labels verified without mutation")
            identity = _section(True, "worker PAT resolves to configured worker and differs from Captain", worker_login=gh_report["worker_login"])
        except (OSError, SetupError) as exc:
            message = str(exc)
            repository_allowlist = _section(False, message)
            labels = _section(False, message)
            identity = _section(False, message)

    provider_name = re.sub(r"[^a-z0-9]+", "_", policy.inference_provider.casefold()).strip("_")
    provider_path = root / "secrets" / f"{provider_name}_api_key"
    credentials = None
    if require_live and provider_path.exists() and file_modes["ok"]:
        credentials = {"hermes_executor": provider_path.read_text(encoding="utf-8").strip()}
    try:
        lanes = doctor_model_lanes(
            policy,
            require_live=require_live,
            transport=model_transport,  # type: ignore[arg-type]
            credentials=credentials,
            runtime_dir=model_lane_runtime_dir,
        ).to_public_dict()
    except ModelLaneError:
        lanes = _section(False, "provider/model configuration or live probe failed")
    ov = doctor_openviking(policy, client_paths=openviking_client_paths, require_live=require_live).to_public_dict()
    fava = doctor_fava_trails(policy, principal_paths=principal_paths, data_repo=data_repo, require_live=require_live).to_public_dict()
    bundled = _skills(skills_prefix or home)
    drift = _drift(policy_path, contract_path, state_path)
    skills_pack = company_skills_report(policy)
    signal = signal_report(policy)
    result: dict[str, object] = {
        "identity": identity,
        "file_modes": file_modes,
        "repository_allowlist": repository_allowlist,
        "labels": labels,
        "checkouts": checkouts,
        "model_lanes": lanes,
        "bundled_skills": bundled,
        "company_skills": skills_pack,
        "signal": signal,
        "openviking": ov,
        "fava_trails": fava,
        "policy_drift": drift,
    }
    result["ok"] = all(bool(section.get("ok")) for section in result.values() if isinstance(section, dict))
    return result
