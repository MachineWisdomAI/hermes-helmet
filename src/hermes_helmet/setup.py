#!/usr/bin/env python3
"""Deterministic, resumable Hermes Helmet adopter setup."""

from __future__ import annotations

from dataclasses import dataclass
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Callable, Mapping, Protocol
from urllib import error as urlerror
from urllib import parse, request

from hermes_helmet.authority import (
    AuthorityError,
    authority_public_dict,
    policy_from_mapping,
    render_crew_contract,
    verify_worker_identity,
)
from hermes_helmet.install_skills import (
    InstallError,
    install_skills,
    last_install_report,
    preflight_skills,
    validate_managed_path,
)
from hermes_helmet.model_lanes import (
    HttpOpenAITransport,
    ModelLaneError,
    lanes_from_policy,
    setup_model_lanes,
)


class SetupError(RuntimeError):
    """Setup failed without exposing credentials."""


class GitHub(Protocol):
    def current_user(self, token: str) -> str: ...
    def repository_access(self, token: str, slug: str) -> dict[str, object]: ...
    def list_accessible_repository_slugs(self, token: str) -> tuple[str, ...]: ...
    def list_visible_repositories(self, token: str) -> tuple[dict[str, object], ...]: ...
    def list_labels(self, token: str, slug: str) -> tuple[str, ...]: ...
    def create_label(self, token: str, slug: str, name: str) -> None: ...


SECRET_VALUE_RE = re.compile(
    r"(?:github_pat_[A-Za-z0-9_]{20,}|gh[opusr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{16,}|xai-[A-Za-z0-9_-]{16,})"
)
SECRET_KEY_PARTS = frozenset({"token", "secret", "password", "credential", "apikey"})
REQUIRED_PERMISSIONS = {
    "metadata": "read",
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
}
ANSWER_KEYS = frozenset(
    {
        "company",
        "captain_github_login",
        "worker_github_login",
        "schedule",
        "board",
        "assignee",
        "inference_provider",
        "inference_model",
        "worker_max_turns",
        "ready_label",
        "dispatch_label",
        "github_owners",
        "repositories",
        "cron_deliver",
        "trusted_review_bots",
        "model_lanes",
        "openviking",
        "fava_trails",
        "matt_pocock_skills",
        "gstack",
        "company_skill_pack",
        "worker_access_scope",
    }
)
ANSWER_NESTED_KEYS = {
    "company": frozenset({"display_name", "slug"}),
    "repositories": frozenset({"slug", "worktree"}),
    "openviking": frozenset({"selected", "confirmed"}),
    "fava_trails": frozenset({"selected", "confirmed"}),
    "matt_pocock_skills": frozenset({"accepted"}),
    "gstack": frozenset({"accepted"}),
    "company_skill_pack": frozenset({"declined"}),
}
MATT_POCOCK_PIN = "c55ee46073ed923f86ce59a5eb3b6d895095d1b7"
GSTACK_PIN = "a6b3a57512ca6d5c6aa5b68f74f736195021f96e"
_INVALID_REQUEST_MISSING_FIELDS = re.compile(
    r"\AInvalid request\.\s+"
    r"(?P<quoted>\"[^\"]+\"(?:\s*,\s*\"[^\"]+\")*)\s+"
    r"(?P<verb>wasn't|weren't)\s+supplied\.\Z"
)
_QUOTED_FIELD_NAME = re.compile(r'"([^"]+)"')


def _structured_write_errors(payload: object) -> set[tuple[str, str]] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("errors"), list):
        return None
    return {
        (str(item.get("field") or ""), str(item.get("code") or ""))
        for item in payload["errors"]
        if isinstance(item, dict)
    }


def _invalid_request_missing_fields(payload: object) -> frozenset[str] | None:
    if not isinstance(payload, dict) or payload.get("errors") is not None:
        return None
    message = payload.get("message")
    if not isinstance(message, str):
        return None
    match = _INVALID_REQUEST_MISSING_FIELDS.fullmatch(message.strip())
    if match is None:
        return None
    fields = tuple(_QUOTED_FIELD_NAME.findall(match.group("quoted")))
    if not fields or len(fields) != len(set(fields)):
        return None
    verb = match.group("verb")
    if len(fields) == 1 and verb != "wasn't":
        return None
    if len(fields) > 1 and verb != "weren't":
        return None
    return frozenset(fields)


@dataclass(frozen=True)
class SetupReport:
    ok: bool
    fingerprint: str
    policy_path: str
    contract_path: str
    github: dict[str, object]
    model_probe: dict[str, object]
    provider_gates: tuple[str, ...]
    skills: dict[str, object]
    external_openviking: bool
    external_fava: bool
    recommendations: tuple[dict[str, object], ...]
    company_skill_pack_plan: dict[str, object]

    def to_public_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "fingerprint": self.fingerprint,
            "policy_path": self.policy_path,
            "contract_path": self.contract_path,
            "github": self.github,
            "model_probe": self.model_probe,
            "provider_gates": list(self.provider_gates),
            "skills": self.skills,
            "external_openviking": self.external_openviking,
            "external_fava": self.external_fava,
            "recommendations": list(self.recommendations),
            "company_skill_pack_plan": self.company_skill_pack_plan,
        }


class GitHubClient:
    """Small GitHub API client used only with the worker PAT."""

    api = "https://api.github.com"

    def _request(self, token: str, method: str, path: str, body: object = None) -> object:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = request.Request(
            self.api + path,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with request.urlopen(req, timeout=20) as response:
                raw = response.read()
        except (OSError, urlerror.URLError, urlerror.HTTPError) as exc:
            raise SetupError("GitHub worker verification failed") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SetupError("GitHub worker verification returned invalid JSON") from exc

    def current_user(self, token: str) -> str:
        payload = self._request(token, "GET", "/user")
        return str(payload.get("login") or "") if isinstance(payload, dict) else ""

    def repository_access(self, token: str, slug: str) -> dict[str, object]:
        quoted = parse.quote(slug, safe="/")
        payload = self._request(token, "GET", f"/repos/{quoted}")
        if not isinstance(payload, dict):
            raise SetupError("GitHub repository verification failed")
        # A deliberately invalid write request reaches GitHub's authorization
        # gate but cannot create content. GitHub returns 422 only after the
        # token has the endpoint's required write permission; read-only tokens
        # fail with 403/404. This proves capability without repository changes.
        checks = {
            "metadata": "read",
            "contents": "write" if self._write_probe(token, "POST", f"/repos/{quoted}/git/refs", {}, expected_errors=(("ref", "missing_field"), ("sha", "missing_field")), exact_errors=True) else "read",
            "pull_requests": "write" if self._write_probe(token, "POST", f"/repos/{quoted}/pulls", {}, expected_errors=(("head", "missing_field"), ("base", "missing_field"))) else "read",
            # Create-issue uniquely requires Issues(write). Create-label may be
            # authorized by Pull requests(write), so it cannot prove this scope.
            "issues": "write" if self._write_probe(token, "POST", f"/repos/{quoted}/issues", {}, expected_errors=(("title", "missing_field"),)) else "read",
        }
        return {"permissions": checks}

    def _write_probe(
        self,
        token: str,
        method: str,
        path: str,
        body: object,
        *,
        expected_errors: tuple[tuple[str, str], ...],
        exact_errors: bool = False,
    ) -> bool:
        data = json.dumps(body).encode("utf-8")
        req = request.Request(
            self.api + path,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with request.urlopen(req, timeout=20):
                # The payload is intentionally invalid. Success would violate
                # the non-mutating contract, so fail closed.
                raise SetupError("GitHub capability probe unexpectedly succeeded")
        except urlerror.HTTPError as exc:
            if exc.code == 422:
                try:
                    payload = json.loads(exc.read().decode("utf-8"))
                except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                    raise SetupError("GitHub capability probe returned unexpected validation") from exc
                expected = set(expected_errors)
                observed = _structured_write_errors(payload)
                if observed is not None:
                    valid = observed == expected if exact_errors else expected.issubset(observed)
                    if valid:
                        return True
                    raise SetupError("GitHub capability probe returned unexpected validation") from exc
                named = _invalid_request_missing_fields(payload)
                expected_fields = {field for field, _code in expected_errors}
                if (
                    named is not None
                    and named == expected_fields
                    and all(code == "missing_field" for _field, code in expected_errors)
                ):
                    return True
                raise SetupError("GitHub capability probe returned unexpected validation") from exc
            if exc.code in {401, 403, 404}:
                return False
            raise SetupError("GitHub worker capability verification failed") from exc
        except (OSError, urlerror.URLError) as exc:
            raise SetupError("GitHub worker capability verification failed") from exc

    def list_visible_repositories(self, token: str) -> tuple[dict[str, object], ...]:
        rows: list[dict[str, object]] = []
        for page in range(1, 101):
            payload = self._request(
                token,
                "GET",
                f"/user/repos?per_page=100&page={page}&affiliation=owner,collaborator,organization_member",
            )
            if not isinstance(payload, list):
                raise SetupError("GitHub repository allowlist verification failed")
            for item in payload:
                if not isinstance(item, dict) or not item.get("full_name"):
                    continue
                rows.append(
                    {
                        "slug": str(item.get("full_name")),
                        "private": False if item.get("private") is False else True,
                    }
                )
            if len(payload) < 100:
                return tuple(rows)
        raise SetupError("GitHub repository allowlist verification exceeded pagination limit")

    def list_accessible_repository_slugs(self, token: str) -> tuple[str, ...]:
        return tuple(str(item["slug"]) for item in self.list_visible_repositories(token))

    def list_labels(self, token: str, slug: str) -> tuple[str, ...]:
        labels: list[str] = []
        quoted = parse.quote(slug, safe="/")
        for page in range(1, 101):
            payload = self._request(
                token,
                "GET",
                f"/repos/{quoted}/labels?per_page=100&page={page}",
            )
            if not isinstance(payload, list):
                raise SetupError("GitHub label verification failed")
            labels.extend(
                str(item.get("name"))
                for item in payload
                if isinstance(item, dict) and item.get("name")
            )
            if len(payload) < 100:
                return tuple(labels)
        raise SetupError("GitHub label verification exceeded pagination limit")

    def create_label(self, token: str, slug: str, name: str) -> None:
        self._request(
            token,
            "POST",
            f"/repos/{parse.quote(slug, safe='/')}/labels",
            {"name": name, "color": "0E8A16" if name == "ready-for-agent" else "B60205"},
        )


def default_github_client() -> GitHubClient:
    return GitHubClient()


def default_transport() -> HttpOpenAITransport:
    return HttpOpenAITransport()


def prompt_secret(prompt: str = "GitHub worker PAT: ") -> str:
    return getpass.getpass(prompt)


def default_questionnaire() -> dict[str, object]:
    return {
        "openviking": {"selected": True, "confirmed": False},
        "fava_trails": {"selected": True, "confirmed": False},
        "matt_pocock_skills": {"accepted": False},
        "gstack": {"accepted": False},
        "company_skill_pack": {"declined": False},
    }


def _secret_key(key: str) -> bool:
    parts = {part for part in re.split(r"[-_]", key.casefold()) if part}
    return bool(parts & SECRET_KEY_PARTS) or ("api" in parts and "key" in parts)


def _reject_secrets(value: object, path: str = "answers") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _secret_key(str(key)):
                raise SetupError(f"{path}: secret fields are not permitted")
            _reject_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secrets(child, f"{path}[{index}]")
    elif isinstance(value, str) and SECRET_VALUE_RE.search(value):
        raise SetupError(f"{path}: secret values are not permitted")


def load_answers_mapping(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise SetupError("answers must be a JSON object")
    _reject_secrets(raw)
    unknown = sorted(str(key) for key in raw if str(key) not in ANSWER_KEYS)
    if unknown:
        raise SetupError(f"answers.{unknown[0]} is not a recognized field")
    for field, allowed in ANSWER_NESTED_KEYS.items():
        if field not in raw:
            continue
        values = raw[field]
        entries = values if isinstance(values, list) else [values]
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                continue
            nested_unknown = sorted(str(key) for key in entry if str(key) not in allowed)
            if nested_unknown:
                prefix = f"answers.{field}[{index}]" if isinstance(values, list) else f"answers.{field}"
                raise SetupError(f"{prefix}.{nested_unknown[0]} is not a recognized field")
    return dict(raw)


def load_answers(path: Path) -> dict[str, object]:
    try:
        return load_answers_mapping(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError("answers could not be read as JSON") from exc


def _choice(answers: Mapping[str, object], name: str) -> tuple[bool, bool]:
    raw = answers.get(name, default_questionnaire()[name])
    if not isinstance(raw, dict):
        raise SetupError(f"{name} must be an object")
    selected = raw.get("selected")
    confirmed = raw.get("confirmed")
    if not isinstance(selected, bool) or not isinstance(confirmed, bool):
        raise SetupError(f"{name} selected/confirmed must be booleans")
    return selected, confirmed


def answers_to_policy_mapping(answers: Mapping[str, object]) -> dict[str, object]:
    answers = load_answers_mapping(dict(answers))
    openviking_selected, openviking_confirmed = _choice(answers, "openviking")
    fava_selected, fava_confirmed = _choice(answers, "fava_trails")
    policy: dict[str, object] = {
        key: answers[key]
        for key in (
            "company", "captain_github_login", "worker_github_login", "schedule",
            "board", "assignee", "inference_provider", "inference_model",
            "worker_max_turns", "ready_label", "dispatch_label", "github_owners",
            "repositories", "worker_access_scope",
        )
        if key in answers
    }
    policy.update(
        {
            "version": 2,
            "cron_deliver": str(answers.get("cron_deliver") or "local"),
            "trusted_review_bots": list(answers.get("trusted_review_bots") or []),
            "integrations": {
                "openviking": openviking_selected and openviking_confirmed,
                "fava_trails": fava_selected and fava_confirmed,
                "signal": False,
            },
            "openviking_peers": [{"id": item} for item in ("codex", "chatgpt", "hermes")],
        }
    )
    if "model_lanes" in answers:
        policy["model_lanes"] = answers["model_lanes"]
    return policy


def _atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        os.chmod(path, mode)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _fingerprint(policy_text: str, contract: str) -> str:
    return hashlib.sha256((policy_text + "\0" + contract).encode("utf-8")).hexdigest()


def _provider_gates(provider: str) -> tuple[str, ...]:
    if provider.casefold() == "openai":
        return (
            "Create an API key with only the project access needed for the selected model.",
            "Confirm model access and billing before dispatch.",
        )
    return (
        "Configure an explicit OpenAI-compatible hermes_executor base_url and restricted credential before dispatch.",
    )


def _validate_provider_configuration(policy: object) -> None:
    lanes = lanes_from_policy(policy)
    executor = lanes.hermes_executor
    if executor.provider.casefold() == "openai":
        return
    if executor.base_url:
        return
    raise SetupError(
        "inference provider is unsupported by the default probe; use openai or "
        "configure an OpenAI-compatible model_lanes.hermes_executor.base_url"
    )


def _validate_owned_path(path: Path, *, directory: bool, mode: int, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise SetupError(f"{label} is missing") from None
    if stat.S_ISLNK(info.st_mode):
        raise SetupError(f"{label} must not be a symlink")
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_kind(info.st_mode):
        raise SetupError(f"{label} has the wrong file type")
    if info.st_uid != os.getuid():
        raise SetupError(f"{label} must be owned by the current user")
    if stat.S_IMODE(info.st_mode) != mode:
        raise SetupError(f"{label} must have mode {mode:04o}")


def _validate_home_boundary(home: Path) -> None:
    try:
        info = home.lstat()
    except FileNotFoundError:
        raise SetupError("setup home is missing") from None
    if stat.S_ISLNK(info.st_mode):
        raise SetupError("setup home must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise SetupError("setup home must be a directory")
    if info.st_uid != os.getuid():
        raise SetupError("setup home must be owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise SetupError("setup home must not be group- or other-writable")


def _validate_existing_secret_tree(root: Path) -> None:
    secrets = root / "secrets"
    if root.exists() or root.is_symlink():
        _validate_owned_path(root, directory=True, mode=0o700, label="Hermes Helmet state directory")
    if secrets.exists() or secrets.is_symlink():
        _validate_owned_path(secrets, directory=True, mode=0o700, label="Hermes Helmet secrets directory")


def _validate_helmet_managed_paths(home: Path, *, extra_paths: tuple[Path, ...] = ()) -> None:
    root = home / ".hermes-helmet"
    paths = (
        (root, True),
        (root / "generated", True),
        (root / "generated" / "crew-contract.md", False),
        (root / "secrets", True),
        (root / "policy.json", False),
        (root / "setup-state.json", False),
        *((path, False) for path in extra_paths),
    )
    try:
        for path, directory in paths:
            validate_managed_path(home, path, final_directory=directory)
    except InstallError as exc:
        raise SetupError(str(exc)) from None


def _prepare_secret_tree(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / "secrets").mkdir(parents=False, exist_ok=True, mode=0o700)
    _validate_existing_secret_tree(root)


def _recommendations(answers: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    specs = (
        ("mattpocock-skills", "matt_pocock_skills", "https://github.com/mattpocock/skills", MATT_POCOCK_PIN),
        ("gstack", "gstack", "https://github.com/garrytan/gstack", GSTACK_PIN),
    )
    items = []
    for name, field, source, pin in specs:
        choice = answers.get(field) or {"accepted": False}
        accepted = bool(choice.get("accepted")) if isinstance(choice, dict) else False
        items.append({"name": name, "offered": True, "accepted": accepted, "source_url": source, "pin": pin, "installed": False, "runtime_dependency": False})
    return tuple(items)


def _visible_repositories(github: GitHub, token: str) -> tuple[dict[str, object], ...]:
    lister = getattr(github, "list_visible_repositories", None)
    rows: tuple[object, ...]
    if callable(lister):
        raw = lister(token)
        if not isinstance(raw, (list, tuple)):
            raise SetupError("GitHub repository allowlist verification failed")
        rows = tuple(raw)
    else:
        rows = tuple(
            {"slug": slug, "private": True}
            for slug in github.list_accessible_repository_slugs(token)
        )
    visible: list[dict[str, object]] = []
    for item in rows:
        if isinstance(item, Mapping) and item.get("slug"):
            visible.append(
                {
                    "slug": str(item.get("slug")),
                    "private": False if item.get("private") is False else True,
                }
            )
        elif isinstance(item, str) and item:
            visible.append({"slug": item, "private": True})
    return tuple(visible)


def _verify_github(policy: object, token: str, github: GitHub, *, create_labels: bool) -> dict[str, object]:
    if not token:
        raise SetupError("GitHub worker PAT is missing")
    try:
        login = github.current_user(token)
        verify_worker_identity(policy, login)  # type: ignore[arg-type]
    except AuthorityError as exc:
        raise SetupError(str(exc).replace("worker identity:", "GitHub worker identity:")) from exc
    selected = {repo.slug.casefold(): repo.slug for repo in policy.repositories}  # type: ignore[attr-defined]
    visible = _visible_repositories(github, token)
    accessible = {str(item["slug"]).casefold() for item in visible}
    extras = [item for item in visible if str(item["slug"]).casefold() not in selected]
    extra_public = sorted(str(item["slug"]) for item in extras if item.get("private") is False)
    extra_private = sorted(str(item["slug"]) for item in extras if item.get("private") is not False)
    missing = sorted(set(selected) - accessible)
    scope = str(getattr(policy, "worker_access_scope", "selected") or "selected")
    if scope not in {"selected", "broader"}:
        raise SetupError("GitHub worker credential scope is invalid")
    if missing or (extra_private and scope != "broader"):
        raise SetupError("GitHub worker repository access does not match the configured allowlist")
    created: list[str] = []
    for repo in policy.repositories:  # type: ignore[attr-defined]
        access = github.repository_access(token, repo.slug)
        permissions = access.get("permissions") if isinstance(access, dict) else None
        for permission, level in REQUIRED_PERMISSIONS.items():
            if not isinstance(permissions, dict) or permissions.get(permission) != level:
                raise SetupError(f"GitHub repository permission {permission}:{level} is required")
        labels = set(github.list_labels(token, repo.slug))
        for label in (policy.ready_label, policy.dispatch_label):  # type: ignore[attr-defined]
            if label not in labels:
                if not create_labels:
                    raise SetupError(f"GitHub label is missing: {label}")
                github.create_label(token, repo.slug, label)
                created.append(f"{repo.slug}:{label}")
    return {
        "ok": True,
        "worker_login": login,
        "repositories": sorted(selected.values()),
        "permissions": [f"{name}:{level}" for name, level in REQUIRED_PERMISSIONS.items()],
        "created_labels": created,
        "dispatch_applied": False,
        "worker_access_scope": scope,
        "extra_public_repositories": extra_public,
        "extra_private_repositories": extra_private,
    }


def run_setup(
    *,
    answers_path: Path,
    home: Path | None = None,
    pat_provider: Callable[[], str] | None = None,
    github: GitHub | None = None,
    transport: object | None = None,
    probe: bool = True,
) -> SetupReport:
    answers = load_answers(answers_path)
    try:
        policy = policy_from_mapping(answers_to_policy_mapping(answers))
    except AuthorityError as exc:
        raise SetupError(str(exc)) from exc
    except ModelLaneError as exc:
        raise SetupError("provider/model configuration is invalid") from exc
    _validate_provider_configuration(policy)
    home = (home or Path.home()).expanduser()
    _validate_home_boundary(home)
    root = home / ".hermes-helmet"
    provider_name = re.sub(r"[^a-z0-9]+", "_", policy.inference_provider.casefold()).strip("_")
    if not provider_name:
        raise SetupError("inference provider cannot name a safe credential file")
    secret_path = root / "secrets" / "github_worker_pat"
    provider_secret_path = root / "secrets" / f"{provider_name}_api_key"
    _validate_helmet_managed_paths(home, extra_paths=(secret_path, provider_secret_path))
    try:
        skill_preflight = preflight_skills(
            targets=("codex", "claude", "hermes"),
            prefix=home,
        )
    except InstallError:
        raise SetupError("bundled skill preflight failed; setup made no changes") from None
    if skill_preflight.conflicts:
        raise SetupError("bundled skill conflict detected; setup made no changes")
    _validate_existing_secret_tree(root)
    new_token = False
    if secret_path.exists() or secret_path.is_symlink():
        _validate_owned_path(secret_path, directory=False, mode=0o600, label="GitHub worker PAT file")
        token = secret_path.read_text(encoding="utf-8").strip()
    else:
        token = (pat_provider or prompt_secret)().strip()
        new_token = True
    selected_transport = transport or default_transport()
    provider_secret = ""
    if provider_secret_path.exists() or provider_secret_path.is_symlink():
        _validate_owned_path(provider_secret_path, directory=False, mode=0o600, label="provider credential file")
        provider_secret = provider_secret_path.read_text(encoding="utf-8").strip()
    elif transport is None and probe:
        provider_secret = prompt_secret(f"{policy.inference_provider} provider credential: ").strip()
    try:
        model_report = setup_model_lanes(
            policy,
            transport=selected_transport,
            probe=probe,
            credentials={"hermes_executor": provider_secret} if provider_secret else None,
        )
    except ModelLaneError as exc:
        raise SetupError("provider/model configuration or probe failed") from exc
    if not model_report.ok:
        raise SetupError("provider/model probe failed")
    github_report = _verify_github(policy, token, github or default_github_client(), create_labels=True)
    _prepare_secret_tree(root)
    if new_token:
        _atomic_write(secret_path, token, 0o600)
    if provider_secret and not provider_secret_path.exists():
        _atomic_write(provider_secret_path, provider_secret, 0o600)
    policy_text = json.dumps(authority_public_dict(policy), indent=2, sort_keys=True) + "\n"
    contract = render_crew_contract(policy)
    policy_path = root / "policy.json"
    contract_path = root / "generated" / "crew-contract.md"
    _atomic_write(policy_path, policy_text, 0o600)
    _atomic_write(contract_path, contract, 0o600)
    fingerprint = _fingerprint(policy_text, contract)
    state_path = root / "setup-state.json"
    incomplete_state = {
        "version": 1,
        "status": "incomplete",
        "stage": "skills",
        "fingerprint": fingerprint,
        "recovery": "rerun helmet setup with the same answers",
    }
    _atomic_write(state_path, json.dumps(incomplete_state, indent=2, sort_keys=True) + "\n", 0o600)
    try:
        install_skills(targets=("codex", "claude", "hermes"), prefix=home)
    except InstallError:
        raise SetupError(
            "bundled skill installation failed and new skill destinations were rolled back; "
            "setup is incomplete and can be resumed by rerunning setup"
        ) from None
    skill_report = last_install_report()
    if skill_report.conflicts or skill_report.skipped_hosts:
        raise SetupError(
            "bundled skill destinations changed after preflight; setup is incomplete; "
            "resolve the conflict and rerun setup"
        )
    complete_state = {"version": 1, "status": "complete", "fingerprint": fingerprint}
    _atomic_write(state_path, json.dumps(complete_state, indent=2, sort_keys=True) + "\n", 0o600)
    company_choice = answers.get("company_skill_pack") or {"declined": False}
    declined = bool(company_choice.get("declined")) if isinstance(company_choice, dict) else False
    plan = {"status": "declined" if declined else "planned", "recommended_skill": "repo-bootstrap", "location": "a separate company-owned skill-pack repository", "never_copy_into_hermes_helmet": True}
    report = SetupReport(
        ok=not skill_report.conflicts and not skill_report.skipped_hosts,
        fingerprint=fingerprint,
        policy_path=str(policy_path),
        contract_path=str(contract_path),
        github=github_report,
        model_probe=model_report.to_public_dict(),
        provider_gates=_provider_gates(policy.inference_provider),
        skills={"installed": list(skill_report.installed), "conflicts": list(skill_report.conflicts), "skipped_hosts": list(skill_report.skipped_hosts)},
        external_openviking=policy.integrations.openviking,
        external_fava=policy.integrations.fava_trails,
        recommendations=_recommendations(answers),
        company_skill_pack_plan=plan,
    )
    return report
