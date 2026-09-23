#!/usr/bin/env python3
"""Optional FAVA Trails governed company-brain integration.

FAVA Trails is the governed company brain and important-decisions ledger
(Apache-2.0). OpenViking remains optional operational working context.
Promotion from OpenViking into FAVA is an explicit operator/Captain decision,
never automatic copying.

This module is portable Helmet setup + diagnostics + a protocol-faithful
lifecycle example. It does **not** reimplement FAVA's governance engine.
Canonical installation and agent contracts live upstream:

- installation README (pin FAVA_PIN)
- AGENTS_SETUP_INSTRUCTIONS.md
- docs/governed-recall.md

Secrets stay in owner-only files and process environment variables. Generated
templates, doctor output, and model-visible prose never echo credentials.
Helmet starts and operates when FAVA Trails is declined or unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, MutableMapping, Sequence
from urllib import parse as urlparse
import uuid

from hermes_helmet.authority import AuthorityError, Policy, load_authority

# Inspected FAVA baseline for Helmet H7 (0.7.0 / MCP 2.2).
FAVA_PIN = "10f689f7455c0c5c5898f2a2e6bc8cf4fe84a6d7"
FAVA_VERSION_HINT = "0.7.0"
FAVA_LICENSE = "Apache-2.0"
CANONICAL_REPO = "https://github.com/MachineWisdomAI/fava-trails"
CANONICAL_INSTALL = f"{CANONICAL_REPO}/blob/{FAVA_PIN}/README.md"
CANONICAL_AGENT_SETUP = f"{CANONICAL_REPO}/blob/{FAVA_PIN}/AGENTS_SETUP_INSTRUCTIONS.md"
CANONICAL_GOVERNED_RECALL = f"{CANONICAL_REPO}/blob/{FAVA_PIN}/docs/governed-recall.md"

DEFAULT_PRINCIPALS = ("captain", "executor")
DEFAULT_OPERATOR_ROLE = "operator"
ROLE_ORDINARY = "ordinary"
ROLE_OPERATOR = "operator"
FORBIDDEN_ROLES = frozenset({"root", "admin", "administrator", "superuser", "operator"})
ORDINARY_ALLOWED = frozenset({ROLE_ORDINARY})
PLACEHOLDER_MARKERS = frozenset(
    {
        "",
        "UNPROVISIONED",
        "CHANGE_ME",
        "__FAVA_OPENROUTER_API_KEY__",
        "__FAVA_TRUST_GATE_API_KEY__",
        "sk-or-v1-...",
        "sk-or-v1-REDACTED",
    }
)
_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}(/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}){0,7}$")
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "openrouter_api_key",
        "trust_gate_api_key",
        "token",
        "password",
        "secret",
        "authorization",
        "OPENROUTER_API_KEY",
        "FAVA_TRAILS_API_KEY",
        "TRUST_GATE_API_KEY",
    }
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(sk-[a-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9\-]{10,}|bearer\s+[A-Za-z0-9\._\-]{8,})"
)
_NAMED_SECRET_QUERY_TOKENS = frozenset(
    {
        "api_key",
        "apikey",
        "token",
        "access_token",
        "password",
        "secret",
        "authorization",
        "auth_token",
        "private_key",
        "client_secret",
    }
)
_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
    }
)
# Public FAVA pin links are the only MachineWisdomAI/fava-trails surface exceptions.
CANONICAL_PUBLIC_MARKERS = (
    f"MachineWisdomAI/fava-trails/blob/{FAVA_PIN}",
    f"MachineWisdomAI/fava-trails/tree/{FAVA_PIN}",
    f"github.com/MachineWisdomAI/fava-trails/blob/{FAVA_PIN}",
    f"github.com/MachineWisdomAI/fava-trails/tree/{FAVA_PIN}",
    CANONICAL_REPO,
)


class FavaTrailsError(RuntimeError):
    """FAVA Trails configuration or doctor invariant failed (redacted)."""


@dataclass(frozen=True)
class FavaCompanyTemplate:
    """Non-secret company brain template."""

    company_slug: str = "exampleco"
    company_scope: str = "exampleco/engineering"
    data_repo_placeholder: str = "/path/to/company-fava-trails-data"
    remote_url_placeholder: str = "https://github.com/YOUR-ORG/fava-trails-data.git"
    principals: Mapping[str, str] = field(
        default_factory=lambda: {
            "captain": "example-captain-fava",
            "executor": "example-agent-fava",
        }
    )
    trust_gate: str = "llm-oneshot"
    openrouter_api_key_env: str = "OPENROUTER_API_KEY"
    license_id: str = FAVA_LICENSE
    fava_pin: str = FAVA_PIN
    fava_version_hint: str = FAVA_VERSION_HINT

    def to_public_dict(self) -> dict[str, object]:
        return {
            "company_slug": self.company_slug,
            "company_scope": self.company_scope,
            "data_repo_placeholder": self.data_repo_placeholder,
            "remote_url_placeholder": self.remote_url_placeholder,
            "principals": dict(self.principals),
            "trust_gate": self.trust_gate,
            "openrouter_api_key_env": self.openrouter_api_key_env,
            "license": self.license_id,
            "fava_pin": self.fava_pin,
            "fava_version_hint": self.fava_version_hint,
            "canonical": {
                "install": CANONICAL_INSTALL,
                "agent_setup": CANONICAL_AGENT_SETUP,
                "governed_recall": CANONICAL_GOVERNED_RECALL,
            },
            "note": (
                "FAVA Trails is the governed company brain. OpenViking is optional "
                "working context. Promotion is explicit, never automatic."
            ),
        }


@dataclass(frozen=True)
class PrincipalConfig:
    """One ordinary-principal launch configuration (owner-only file)."""

    principal: str
    agent_id: str
    role: str
    company_scope: str
    data_repo: str
    path: Path | None = None
    openrouter_api_key_env: str = "OPENROUTER_API_KEY"
    # Optional pointer to owner-only secret file; value never loaded into templates.
    trust_gate_key_file: str | None = None
    operator: bool = False

    def public_view(self) -> dict[str, object]:
        return {
            "principal": self.principal,
            "agent_id": self.agent_id,
            "role": self.role,
            "company_scope": self.company_scope,
            "data_repo": self.data_repo,
            "path": str(self.path) if self.path is not None else None,
            "openrouter_api_key_env": self.openrouter_api_key_env,
            "trust_gate_key_file_configured": bool(self.trust_gate_key_file),
            "operator": self.operator,
            "launch_env_public": {
                "FAVA_TRAILS_DATA_REPO": self.data_repo,
                "FAVA_TRAILS_AGENT_ID": self.agent_id,
                "FAVA_TRAILS_SCOPE": self.company_scope,
                "FAVA_TRAILS_OPERATOR": "1" if self.operator else "0",
                "openrouter_api_key_env": self.openrouter_api_key_env,
            },
        }

    def launch_env(self, *, include_operator: bool = False) -> dict[str, str]:
        """Non-secret launch environment for a dedicated FAVA MCP process."""

        if self.operator and not include_operator:
            raise FavaTrailsError("operator principal launch requires explicit include_operator")
        if not self.operator and self.role not in ORDINARY_ALLOWED:
            raise FavaTrailsError("ordinary principal must use role=ordinary")
        env = {
            "FAVA_TRAILS_DATA_REPO": self.data_repo,
            "FAVA_TRAILS_AGENT_ID": self.agent_id,
            "FAVA_TRAILS_SCOPE": self.company_scope,
        }
        if self.operator:
            env["FAVA_TRAILS_OPERATOR"] = "1"
        # Never inject secret values; only advertise the env var *name*.
        env["FAVA_TRAILS_OPENROUTER_API_KEY_ENV"] = self.openrouter_api_key_env
        return env


@dataclass(frozen=True)
class DoctorFinding:
    code: str
    ok: bool
    message: str

    def to_public_dict(self) -> dict[str, object]:
        return {"code": self.code, "ok": self.ok, "message": self.message}


@dataclass(frozen=True)
class DoctorReport:
    enabled: bool
    skipped: bool
    ok: bool
    findings: tuple[DoctorFinding, ...] = ()
    principals: tuple[dict[str, object], ...] = ()
    verification_mode: str = "offline"

    def to_public_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "skipped": self.skipped,
            "ok": self.ok,
            "verification_mode": self.verification_mode,
            "findings": [item.to_public_dict() for item in self.findings],
            "principals": list(self.principals),
            "canonical": {
                "install": CANONICAL_INSTALL,
                "agent_setup": CANONICAL_AGENT_SETUP,
                "governed_recall": CANONICAL_GOVERNED_RECALL,
            },
        }


def _field_error(field: str, message: str) -> FavaTrailsError:
    return FavaTrailsError(f"{field}: {message}")


def redact_secrets(text: str, *secrets_in: str) -> str:
    """Remove known secret values and common credential shapes from text."""

    out = text
    for secret in secrets_in:
        if not secret or secret in PLACEHOLDER_MARKERS or len(secret) < 6:
            continue
        out = out.replace(secret, "[REDACTED]")
    out = _SECRET_VALUE_RE.sub("[REDACTED]", out)
    return out


def _query_looks_secret_bearing(query: str) -> bool:
    for name, value in urlparse.parse_qsl(query or "", keep_blank_values=True):
        token = urlparse.unquote(name).strip().casefold().replace("-", "_")
        if token in _NAMED_SECRET_QUERY_TOKENS:
            return True
        if value and _SECRET_VALUE_RE.search(value):
            return True
    return False


def reject_secret_material(value: str, *, field: str) -> str:
    """Fail closed when a template/config field embeds secret-shaped material."""

    text = value if isinstance(value, str) else str(value)
    if _SECRET_VALUE_RE.search(text):
        raise _field_error(field, "must not embed secret values")
    lowered = text.casefold()
    for marker in ("password=", "secret=", "api_key=", "authorization:"):
        if marker in lowered:
            raise _field_error(field, "must not embed secret values")
    return text


def validate_remote_url(url: str, *, field: str = "remote_url_placeholder") -> str:
    """Accept ordinary credential-free remotes; reject userinfo and secret shapes."""

    if not isinstance(url, str) or not url.strip():
        raise _field_error(field, "must be a non-empty remote URL or path placeholder")
    raw = url.strip()
    reject_secret_material(raw, field=field)

    # scp-like git@host:path form (no password channel) is allowed.
    if re.match(r"^git@[A-Za-z0-9._-]+:", raw) and "://" not in raw:
        if raw.count("@") != 1:
            raise _field_error(field, "must not embed credentials in the remote")
        return raw

    parsed = urlparse.urlparse(raw)
    if parsed.scheme in {"http", "https", "ssh", "git", "file"}:
        if parsed.username is not None or parsed.password is not None:
            raise _field_error(field, "must not embed credentials in the remote")
        # Non-standard userinfo that urlparse still exposes via netloc.
        host = parsed.netloc or ""
        if "@" in host:
            raise _field_error(field, "must not embed credentials in the remote")
        if _query_looks_secret_bearing(parsed.query):
            raise _field_error(field, "must not place secrets on the remote URL")
        if parsed.fragment and _SECRET_VALUE_RE.search(parsed.fragment):
            raise _field_error(field, "must not place secrets on the remote URL")
        return raw

    # Bare path placeholders (no scheme) — still forbid userinfo-like embeds.
    if "://" in raw:
        raise _field_error(field, "unsupported remote URL scheme")
    if re.search(r"//[^/\s]*:[^/\s]*@", raw) or re.search(r"https?:[^/]*@", raw):
        raise _field_error(field, "must not embed credentials in the remote")
    return raw


def validate_env_var_name(value: str, *, field: str = "openrouter_api_key_env") -> str:
    """Admit credential-free environment variable *names* only (never secret values)."""

    if not isinstance(value, str) or not value.strip():
        raise _field_error(field, "must be a non-empty environment variable name")
    raw = value.strip()
    reject_secret_material(raw, field=field)
    if not _ENV_NAME_RE.match(raw):
        raise _field_error(field, "must be a credential-free environment variable name")
    lowered = raw.casefold()
    if lowered.startswith("sk-") or lowered.startswith("ghp_") or lowered.startswith("bearer"):
        raise _field_error(field, "must not embed secret values")
    return raw


def validate_data_repo_path(value: str, *, field: str = "data_repo") -> str:
    """Validate a local data-repo path placeholder (never a credential URL)."""

    if not isinstance(value, str) or not value.strip():
        raise _field_error(field, "must be a non-empty path")
    raw = value.strip()
    reject_secret_material(raw, field=field)
    # Credential-bearing remotes must fail with the shared credential rule first.
    parsed = urlparse.urlparse(raw)
    looks_remote = bool(parsed.scheme) or "://" in raw or bool(re.match(r"^git@[A-Za-z0-9._-]+:", raw))
    if looks_remote:
        validate_remote_url(raw, field=field)
        raise _field_error(field, "must be a filesystem path, not a URL")
    if re.search(r"//[^/\s]*:[^/\s]*@", raw):
        raise _field_error(field, "must not embed credentials")
    return raw


def try_import_fava() -> dict[str, Any] | None:
    """Import accepted FAVA Trails APIs when installed; else None."""

    try:
        from fava_trails.config import ConfigStore  # type: ignore
        from fava_trails.governance import Principal, Visibility  # type: ignore
        from fava_trails.models import GlobalConfig, SourceType, TrailConfig  # type: ignore
        from fava_trails.readiness import ReadinessFailure, probe_data_repository  # type: ignore
        from fava_trails.trail import TrailManager  # type: ignore
        from fava_trails.trust_gate import TrustResult  # type: ignore
        from fava_trails.vcs.jj_backend import JjBackend  # type: ignore
    except ImportError:
        return None
    try:
        from fava_trails import server as fava_server  # type: ignore
    except ImportError:
        fava_server = None
    return {
        "ConfigStore": ConfigStore,
        "Principal": Principal,
        "Visibility": Visibility,
        "GlobalConfig": GlobalConfig,
        "SourceType": SourceType,
        "TrailConfig": TrailConfig,
        "ReadinessFailure": ReadinessFailure,
        "probe_data_repository": probe_data_repository,
        "TrailManager": TrailManager,
        "TrustResult": TrustResult,
        "JjBackend": JjBackend,
        "server": fava_server,
    }


def validate_scope(scope: str, *, field: str = "company_scope") -> str:
    value = (scope or "").strip()
    reject_secret_material(value, field=field)
    if not value or not _SCOPE_RE.match(value):
        raise _field_error(field, "must be a slash-separated company scope path")
    if ".." in value or value.startswith("/") or "\\" in value:
        raise _field_error(field, "must not contain unsafe path elements")
    return value


def validate_agent_id(agent_id: str, *, field: str = "agent_id") -> str:
    value = (agent_id or "").strip()
    reject_secret_material(value, field=field)
    if not value or not _AGENT_ID_RE.match(value):
        raise _field_error(field, "must be a stable ordinary role id")
    lowered = value.casefold()
    if lowered in FORBIDDEN_ROLES or lowered.startswith("root") or lowered.startswith("admin"):
        raise _field_error(field, "must not claim root/admin/operator powers")
    return value


def validate_company_template(template: FavaCompanyTemplate) -> FavaCompanyTemplate:
    """Validate every generated field before it can reach disk or stdout."""

    reject_secret_material(template.company_slug, field="company_slug")
    validate_scope(template.company_scope)
    validate_data_repo_path(template.data_repo_placeholder, field="data_repo_placeholder")
    validate_remote_url(template.remote_url_placeholder)
    for principal in template.principals:
        reject_secret_material(str(principal), field="principals key")
    if set(template.principals) != set(DEFAULT_PRINCIPALS):
        raise _field_error("principals", "must contain exactly captain and executor")
    for principal, agent_id in template.principals.items():
        validate_agent_id(agent_id, field=f"principals.{principal}")
    reject_secret_material(template.trust_gate, field="trust_gate")
    validate_env_var_name(template.openrouter_api_key_env, field="openrouter_api_key_env")
    reject_secret_material(template.license_id, field="license")
    reject_secret_material(template.fava_pin, field="fava_pin")
    reject_secret_material(template.fava_version_hint, field="fava_version_hint")
    return template


def default_company_template(
    *,
    company_slug: str = "exampleco",
    company_scope: str | None = None,
) -> FavaCompanyTemplate:
    slug = (company_slug or "exampleco").strip().casefold() or "exampleco"
    scope = company_scope or f"{slug}/engineering"
    return validate_company_template(FavaCompanyTemplate(
        company_slug=slug,
        company_scope=validate_scope(scope),
        principals={
            "captain": f"{slug}-captain-fava",
            "executor": f"{slug}-agent-fava",
        },
    ))


def company_template_from_policy(policy: Policy) -> FavaCompanyTemplate:
    slug = policy.company_slug.strip().casefold() or "exampleco"
    # Keep agent ids generic from company slug — never copy private GitHub logins
    # into FAVA principal defaults (those stay in private overlays).
    return default_company_template(company_slug=slug)


def load_company_template(path: Path) -> FavaCompanyTemplate:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FavaTrailsError("company template could not be read") from exc
    if not isinstance(raw, dict):
        raise FavaTrailsError("company template must be a JSON object")
    principals_raw = raw.get("principals") or {}
    if not isinstance(principals_raw, dict):
        raise FavaTrailsError("company template principals must be an object")
    principals = {
        str(key): validate_agent_id(str(value), field=f"principals.{key}")
        for key, value in principals_raw.items()
    }
    for required in DEFAULT_PRINCIPALS:
        if required not in principals:
            raise FavaTrailsError(f"company template missing principal {required}")
    if len({v.casefold() for v in principals.values()}) != len(principals):
        raise FavaTrailsError("principal agent ids must be pairwise distinct")
    return validate_company_template(FavaCompanyTemplate(
        company_slug=str(raw.get("company_slug") or "exampleco").strip().casefold(),
        company_scope=validate_scope(str(raw.get("company_scope") or "")),
        data_repo_placeholder=validate_data_repo_path(
            str(raw.get("data_repo_placeholder") or "/path/to/company-fava-trails-data"),
            field="data_repo_placeholder",
        ),
        remote_url_placeholder=validate_remote_url(
            str(
                raw.get("remote_url_placeholder")
                or "https://github.com/YOUR-ORG/fava-trails-data.git"
            ),
            field="remote_url_placeholder",
        ),
        principals=principals,
        trust_gate=str(raw.get("trust_gate") or "llm-oneshot"),
        openrouter_api_key_env=validate_env_var_name(
            str(raw.get("openrouter_api_key_env") or "OPENROUTER_API_KEY"),
            field="openrouter_api_key_env",
        ),
        license_id=str(raw.get("license") or FAVA_LICENSE),
        fava_pin=str(raw.get("fava_pin") or FAVA_PIN),
        fava_version_hint=str(raw.get("fava_version_hint") or FAVA_VERSION_HINT),
    ))


def resolve_company_template(
    policy: Policy,
    *,
    company_template_path: Path | None = None,
    company_scope: str | None = None,
    company_slug: str | None = None,
    remote_url: str | None = None,
    data_repo_placeholder: str | None = None,
) -> FavaCompanyTemplate:
    if company_template_path is not None:
        template = load_company_template(company_template_path)
    else:
        template = company_template_from_policy(policy)
        if company_slug:
            template = default_company_template(
                company_slug=company_slug,
                company_scope=company_scope or None,
            )
    if company_scope:
        template = FavaCompanyTemplate(
            company_slug=template.company_slug,
            company_scope=validate_scope(company_scope),
            data_repo_placeholder=template.data_repo_placeholder,
            remote_url_placeholder=template.remote_url_placeholder,
            principals=dict(template.principals),
            trust_gate=template.trust_gate,
            openrouter_api_key_env=template.openrouter_api_key_env,
            license_id=template.license_id,
            fava_pin=template.fava_pin,
            fava_version_hint=template.fava_version_hint,
        )
    if remote_url is not None:
        template = FavaCompanyTemplate(
            company_slug=template.company_slug,
            company_scope=template.company_scope,
            data_repo_placeholder=template.data_repo_placeholder,
            remote_url_placeholder=validate_remote_url(remote_url, field="remote_url"),
            principals=dict(template.principals),
            trust_gate=template.trust_gate,
            openrouter_api_key_env=template.openrouter_api_key_env,
            license_id=template.license_id,
            fava_pin=template.fava_pin,
            fava_version_hint=template.fava_version_hint,
        )
    if data_repo_placeholder is not None:
        template = FavaCompanyTemplate(
            company_slug=template.company_slug,
            company_scope=template.company_scope,
            data_repo_placeholder=validate_data_repo_path(
                data_repo_placeholder, field="data_repo_placeholder"
            ),
            remote_url_placeholder=template.remote_url_placeholder,
            principals=dict(template.principals),
            trust_gate=template.trust_gate,
            openrouter_api_key_env=template.openrouter_api_key_env,
            license_id=template.license_id,
            fava_pin=template.fava_pin,
            fava_version_hint=template.fava_version_hint,
        )
    return validate_company_template(template)


def render_company_template(template: FavaCompanyTemplate) -> str:
    validate_company_template(template)
    return json.dumps(template.to_public_dict(), indent=2, sort_keys=True) + "\n"


def example_principal_payload(
    template: FavaCompanyTemplate,
    principal: str,
    *,
    data_repo: str | None = None,
) -> dict[str, object]:
    if principal not in template.principals:
        raise FavaTrailsError(f"unknown principal {principal!r}")
    if principal == DEFAULT_OPERATOR_ROLE:
        raise FavaTrailsError("operator launch config is separate and never an ordinary principal")
    agent_id = validate_agent_id(template.principals[principal])
    repo = validate_data_repo_path(
        data_repo if data_repo is not None else template.data_repo_placeholder,
        field="data_repo",
    )
    env_name = validate_env_var_name(template.openrouter_api_key_env, field="openrouter_api_key_env")
    return {
        "principal": principal,
        "agent_id": agent_id,
        "role": ROLE_ORDINARY,
        "operator": False,
        "company_scope": template.company_scope,
        "data_repo": repo,
        "openrouter_api_key_env": env_name,
        "trust_gate_key_file": None,
        "note": (
            "Ordinary principal only. Scope is a hint, not an authorization boundary. "
            "Dedicated FAVA_TRAILS_AGENT_ID process identity governs authoring. "
            "Never set FAVA_TRAILS_OPERATOR=1 on captain/executor endpoints."
        ),
        "canonical_agent_setup": CANONICAL_AGENT_SETUP,
    }


def render_principal_template(
    template: FavaCompanyTemplate,
    principal: str,
    *,
    data_repo: str | None = None,
) -> str:
    return json.dumps(
        example_principal_payload(template, principal, data_repo=data_repo),
        indent=2,
        sort_keys=True,
    ) + "\n"


def render_data_repo_config_yaml(template: FavaCompanyTemplate) -> str:
    """Non-secret data-repo config.yaml body accepted by FAVA 0.7.0 GlobalConfig."""

    remote = validate_remote_url(template.remote_url_placeholder, field="remote_url")
    scope = validate_scope(template.company_scope)
    env_name = validate_env_var_name(template.openrouter_api_key_env, field="openrouter_api_key_env")
    return (
        f"# Generated by hermes-helmet fava setup — no secrets.\n"
        f"# Canonical install: {CANONICAL_INSTALL}\n"
        f"trails_dir: trails\n"
        f'remote_url: "{remote}"\n'
        f"push_strategy: manual\n"
        f"trust_gate: {template.trust_gate}\n"
        f"trust_gate_model: google/gemini-2.5-flash\n"
        f"openrouter_api_key_env: {env_name}\n"
        f"trails:\n"
        f"  {scope}:\n"
        f"    name: {scope}\n"
        f"    trust_gate_policy: {template.trust_gate}\n"
    )


def render_trust_gate_prompt(template: FavaCompanyTemplate) -> str:
    return (
        f"# Trust Gate prompt — {template.company_scope}\n\n"
        "Approve only durable, attributable company decisions and validated\n"
        "observations. Reject speculation, secrets, personal context, and\n"
        "automatic copies from working-context systems.\n\n"
        "Require:\n"
        "- clear decision or observation statement\n"
        "- company scope alignment\n"
        "- no credentials or personal data\n"
        "- explicit promotion intent when sourced from OpenViking\n"
    )


def render_mcp_registration_example(
    template: FavaCompanyTemplate,
    principal: str,
    *,
    data_repo: str | None = None,
) -> str:
    payload = example_principal_payload(template, principal, data_repo=data_repo)
    env_name = validate_env_var_name(str(payload["openrouter_api_key_env"]), field="openrouter_api_key_env")
    env = {
        "FAVA_TRAILS_DATA_REPO": payload["data_repo"],
        "FAVA_TRAILS_AGENT_ID": payload["agent_id"],
        "FAVA_TRAILS_SCOPE": payload["company_scope"],
        env_name: f"${{{env_name}}}",
    }
    doc = {
        "mcpServers": {
            f"fava-trails-{principal}": {
                "command": "fava-trails-server",
                "env": env,
            }
        },
        "notes": [
            "One dedicated process per ordinary principal.",
            "Do not set FAVA_TRAILS_OPERATOR on captain/executor.",
            "Substitute env values from owner-only secret stores; never commit them.",
            f"Canonical setup: {CANONICAL_AGENT_SETUP}",
        ],
    }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def default_principal_paths(root: Path) -> dict[str, Path]:
    return {
        name: root / "principals" / name / "principal.json"
        for name in DEFAULT_PRINCIPALS
    }


def _require_owner_only_file(path: Path) -> None:
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise FavaTrailsError("principal config is not readable") from exc
    if not stat.S_ISREG(mode):
        raise FavaTrailsError("principal config must be a regular file")
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise FavaTrailsError("principal config must be owner-only (mode 0600)")


def write_owner_only_json(path: Path, payload: Mapping[str, object]) -> None:
    """Atomically write JSON with mode 0600 using a unique owned temp file.

    Predictable ``*.tmp`` names and symlink-following truncates are refused so
    unrelated configuration targets cannot be clobbered. Pre-existing parent
    directory modes are preserved; only newly created parents are made private.
    """

    path = path.expanduser()
    parent = path.parent
    parent_preexisted = parent.exists()
    if parent_preexisted and parent.is_symlink():
        raise FavaTrailsError("principal config parent directory must not be a symlink")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_preexisted:
        # Newly created leaf directory only — never rewrite modes on shared parents.
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
    # Refuse unsafe destination aliases without following/altering their targets.
    if path.is_symlink() or path.exists() and path.is_symlink():
        raise FavaTrailsError("principal config path must not be a symlink")
    if path.exists():
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise FavaTrailsError("principal config path is not usable") from exc
        if stat.S_ISLNK(mode):
            raise FavaTrailsError("principal config path must not be a symlink")
        if not stat.S_ISREG(mode):
            raise FavaTrailsError("principal config path must be a regular file")
    text = json.dumps(dict(payload), indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(parent),
    )
    temporary = Path(temporary_name)
    try:
        # mkstemp returns a real file; refuse if something replaced it with an alias.
        if temporary.is_symlink():
            raise FavaTrailsError("refusing to write through a temporary symlink")
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        # Final destination must still not be a symlink at replace time.
        if path.is_symlink():
            raise FavaTrailsError("principal config path must not be a symlink")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_principal_config(
    path: Path,
    *,
    principal: str,
    template: FavaCompanyTemplate,
    data_repo: str,
    trust_gate_key_file: str | None = None,
) -> PrincipalConfig:
    if principal not in DEFAULT_PRINCIPALS:
        raise FavaTrailsError("only captain and executor ordinary principals are supported")
    payload = example_principal_payload(template, principal, data_repo=data_repo)
    payload["data_repo"] = str(Path(data_repo).expanduser())
    if trust_gate_key_file:
        payload["trust_gate_key_file"] = str(Path(trust_gate_key_file).expanduser())
    # Reject accidental secret material in the payload before write.
    blob = json.dumps(payload)
    if _SECRET_VALUE_RE.search(blob):
        raise FavaTrailsError("principal config must not embed secret values")
    for key in payload:
        if str(key) in _SECRET_FIELD_NAMES:
            raise FavaTrailsError(f"principal config must not include secret field {key}")
    write_owner_only_json(path, payload)
    return load_principal_config(path, expected_principal=principal)


def load_principal_config(
    path: Path,
    *,
    expected_principal: str | None = None,
) -> PrincipalConfig:
    _require_owner_only_file(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FavaTrailsError("principal config could not be read") from exc
    if not isinstance(raw, dict):
        raise FavaTrailsError("principal config must be a JSON object")
    for key in raw:
        if str(key) in _SECRET_FIELD_NAMES:
            raise FavaTrailsError(f"principal config must not include secret field {key}")
    blob = json.dumps(raw)
    if _SECRET_VALUE_RE.search(blob):
        raise FavaTrailsError("principal config must not embed secret values")
    principal = str(raw.get("principal") or "").strip()
    if expected_principal and principal != expected_principal:
        raise FavaTrailsError("mismatched principal for config path")
    if principal not in DEFAULT_PRINCIPALS:
        raise FavaTrailsError("principal must be captain or executor")
    role = str(raw.get("role") or ROLE_ORDINARY).strip().casefold()
    if role not in ORDINARY_ALLOWED:
        raise FavaTrailsError("ordinary principal role must be USER-equivalent ordinary")
    if bool(raw.get("operator")):
        raise FavaTrailsError("executor/captain principal config must not enable operator mode")
    agent_id = validate_agent_id(str(raw.get("agent_id") or ""))
    company_scope = validate_scope(str(raw.get("company_scope") or ""))
    data_repo = str(raw.get("data_repo") or "").strip()
    if not data_repo or data_repo == "/path/to/company-fava-trails-data":
        raise FavaTrailsError("data_repo must be a real adopter path, not the placeholder")
    key_file = raw.get("trust_gate_key_file")
    return PrincipalConfig(
        principal=principal,
        agent_id=agent_id,
        role=ROLE_ORDINARY,
        company_scope=company_scope,
        data_repo=data_repo,
        path=path,
        openrouter_api_key_env=str(raw.get("openrouter_api_key_env") or "OPENROUTER_API_KEY"),
        trust_gate_key_file=str(key_file) if key_file else None,
        operator=False,
    )


def load_principal_configs(paths: Mapping[str, Path]) -> dict[str, PrincipalConfig]:
    configs = {
        name: load_principal_config(path, expected_principal=name)
        for name, path in paths.items()
    }
    require_company_scoped_principals(configs)
    return configs


def require_company_scoped_principals(configs: Mapping[str, PrincipalConfig]) -> None:
    missing = [name for name in DEFAULT_PRINCIPALS if name not in configs]
    if missing:
        raise FavaTrailsError("captain and executor principal configs are required")
    scopes = {cfg.company_scope for cfg in configs.values()}
    repos = {str(Path(cfg.data_repo).expanduser()) for cfg in configs.values()}
    if len(scopes) != 1:
        raise FavaTrailsError("principals must share one company scope")
    if len(repos) != 1:
        raise FavaTrailsError("principals must share one company-owned data repository path")
    agent_ids = [cfg.agent_id for cfg in configs.values()]
    if len({item.casefold() for item in agent_ids}) != len(agent_ids):
        raise FavaTrailsError("principal agent ids must be pairwise distinct")
    for cfg in configs.values():
        if cfg.operator or cfg.role not in ORDINARY_ALLOWED:
            raise FavaTrailsError("ordinary principal boundary violated")


def prepare_data_repo_scaffold(
    target: Path,
    template: FavaCompanyTemplate,
    *,
    allow_existing: bool = False,
) -> dict[str, object]:
    """Create a non-destructive data-repo scaffold (config + prompt only).

    Does not run `fava-trails bootstrap` over an existing repo and does not
    initialize JJ/Git. Adopters follow canonical FAVA install for VCS bootstrap.
    """

    target = target.expanduser()
    if target.exists():
        if not target.is_dir():
            raise FavaTrailsError("data repo path exists and is not a directory")
        if not allow_existing and any(target.iterdir()):
            raise FavaTrailsError(
                "refusing to scaffold over a non-empty path; "
                "use connect mode or an empty directory"
            )
    else:
        target.mkdir(parents=True, exist_ok=True, mode=0o700)

    config_path = target / "config.yaml"
    gitignore_path = target / ".gitignore"
    prompt_path = target / "trails" / "trust-gate-prompt.md"
    scope_prompt = target / "trails" / Path(template.company_scope) / "trust-gate-prompt.md"

    if config_path.exists() and not allow_existing:
        raise FavaTrailsError("config.yaml already present; refusing destructive bootstrap")

    if not config_path.exists():
        config_path.write_text(render_data_repo_config_yaml(template), encoding="utf-8")
    if not gitignore_path.exists():
        gitignore_path.write_text(
            ".jj/\n__pycache__/\n*.pyc\n.venv/\n.env\n",
            encoding="utf-8",
        )
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    if not prompt_path.exists():
        prompt_path.write_text(render_trust_gate_prompt(template), encoding="utf-8")
    scope_prompt.parent.mkdir(parents=True, exist_ok=True)
    if not scope_prompt.exists():
        scope_prompt.write_text(render_trust_gate_prompt(template), encoding="utf-8")

    return {
        "data_repo": str(target),
        "config": str(config_path),
        "trust_gate_prompt": str(prompt_path),
        "company_scope_prompt": str(scope_prompt),
        "next_steps": [
            f"Follow canonical bootstrap/clone: {CANONICAL_INSTALL}",
            "Do not bootstrap over an existing remote data repository — use fava-trails clone.",
            "Register dedicated MCP processes per ordinary principal.",
            "Keep OPENROUTER_API_KEY in process env / owner-only secret store only.",
        ],
    }


def inspect_data_repo(path: Path, *, company_scope: str | None = None) -> dict[str, object]:
    """Offline structural inspection of a company data repository (no secrets)."""

    root = path.expanduser()
    report: dict[str, object] = {
        "path": str(root),
        "exists": root.is_dir(),
        "has_config": False,
        "has_trails_dir": False,
        "has_trust_gate_prompt": False,
        "has_jj": False,
        "has_git": False,
        "config_schema_ok": False,
        "ok": False,
        "message": "",
    }
    if not root.is_dir():
        report["message"] = "data repository path is missing"
        return report
    config = root / "config.yaml"
    trails = root / "trails"
    report["has_config"] = config.is_file()
    report["has_trails_dir"] = trails.is_dir()
    report["has_trust_gate_prompt"] = (trails / "trust-gate-prompt.md").is_file()
    report["has_jj"] = (root / ".jj").exists()
    report["has_git"] = (root / ".git").exists()
    if not report["has_config"]:
        report["message"] = "config.yaml missing"
        return report
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        report["message"] = "config.yaml unreadable"
        return report
    if _SECRET_VALUE_RE.search(text):
        report["message"] = "config.yaml must not embed secret values"
        return report
    if "trails_dir" not in text:
        report["message"] = "config.yaml missing trails_dir"
        return report

    # Prefer accepted FAVA GlobalConfig validation when the package is available.
    fava = try_import_fava()
    if fava is not None:
        try:
            import yaml  # type: ignore
        except ImportError:
            yaml = None  # type: ignore
        if yaml is not None:
            try:
                parsed = yaml.safe_load(text)
                if not isinstance(parsed, dict):
                    raise ValueError("config must be a mapping")
                global_cfg = fava["GlobalConfig"].model_validate(parsed)
                report["config_schema_ok"] = True
                if company_scope:
                    trail_cfg = global_cfg.trails.get(company_scope)
                    if trail_cfg is None:
                        report["message"] = (
                            f"config.yaml missing trails entry for scope {company_scope}"
                        )
                        return report
                    if getattr(trail_cfg, "name", None) != company_scope:
                        report["message"] = (
                            f"trails.{company_scope}.name must equal the scope path"
                        )
                        return report
            except Exception as exc:  # noqa: BLE001 - surface schema failures only
                report["message"] = redact_secrets(
                    f"config.yaml failed FAVA schema validation: {exc}"
                )
                return report
    else:
        # Structural fallback when FAVA is not installed: require per-trail name.
        if company_scope and f"name: {company_scope}" not in text and f'name: "{company_scope}"' not in text:
            report["message"] = (
                f"config.yaml trails.{company_scope} must include name: {company_scope}"
            )
            return report
        if "name:" not in text and "trails:" in text:
            report["message"] = "config.yaml trail entries must include required name field"
            return report
        report["config_schema_ok"] = True

    report["ok"] = True
    report["message"] = "data repository scaffold looks structurally ready"
    return report


def _bound_env_for_data_repo(
    data_repo: Path,
    *,
    agent_id: str | None = None,
    operator: bool = False,
    base: Mapping[str, str] | None = None,
    credential_env_names: Sequence[str] = (),
) -> dict[str, str]:
    """Build a child env bound to the selected data root (not ambient FAVA root)."""

    source = os.environ if base is None else base
    # An MCP subprocess receives only ordinary runtime variables. This prevents
    # unrelated host credentials from crossing the optional integration boundary.
    env = {key: source[key] for key in _CHILD_ENV_ALLOWLIST if key in source}
    for name in credential_env_names:
        safe_name = validate_env_var_name(name, field="credential_env_name")
        if safe_name in source:
            env[safe_name] = source[safe_name]
    env["FAVA_TRAILS_DATA_REPO"] = str(data_repo.expanduser().resolve())
    if agent_id:
        env["FAVA_TRAILS_AGENT_ID"] = agent_id
    if operator:
        env["FAVA_TRAILS_OPERATOR"] = "1"
    else:
        env.pop("FAVA_TRAILS_OPERATOR", None)
    return env


def probe_fava_cli(
    *,
    data_repo: str | None = None,
    agent_id: str | None = None,
    credential_env_name: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, object]:
    """Optional live probe via installed `fava-trails doctor` bound to selected root."""

    exe = shutil.which("fava-trails")
    if not exe:
        return {
            "ok": False,
            "available": False,
            "message": "fava-trails CLI not found on PATH (optional live probe skipped)",
        }
    if not data_repo:
        return {
            "ok": False,
            "available": True,
            "message": "live doctor requires an explicit selected data repository path",
        }
    repo = Path(data_repo).expanduser()
    env = _bound_env_for_data_repo(
        repo,
        agent_id=agent_id,
        credential_env_names=(credential_env_name,) if credential_env_name else (),
    )
    known_values = {
        value
        for key, value in env.items()
        if key in _SECRET_FIELD_NAMES or key.endswith("_API_KEY") or key.endswith("_TOKEN")
        if value
    }
    if credential_env_name and env.get(credential_env_name):
        known_values.add(env[credential_env_name])
    known = tuple(known_values)
    cmd = [exe, "doctor"]
    run = runner or subprocess.run
    try:
        completed = run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=str(repo),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "available": True,
            "message": redact_secrets(f"fava-trails doctor failed: {type(exc).__name__}", *known),
        }
    stdout = redact_secrets(completed.stdout or "", *known)
    stderr = redact_secrets(completed.stderr or "", *known)
    return {
        "ok": completed.returncode == 0,
        "available": True,
        "exit_code": completed.returncode,
        "message": (
            "fava-trails doctor completed for selected data root"
            if completed.returncode == 0
            else "fava-trails doctor reported failure for selected data root"
        ),
        "stdout_redacted": stdout[-2000:],
        "stderr_redacted": stderr[-2000:],
        "bound_data_repo": str(repo),
        "bound_agent_id": agent_id,
    }


def resolve_fava_server_command() -> list[str] | None:
    """Locate the accepted FAVA MCP server entrypoint for live transport contact."""

    found = shutil.which("fava-trails-server")
    if found:
        return [found]
    sibling = Path(sys.executable).resolve().parent / "fava-trails-server"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return [str(sibling)]
    if try_import_fava() is not None:
        # Same interpreter that can import the accepted package.
        return [sys.executable, "-c", "from fava_trails.server import run; run()"]
    return None


def _structured_tool_result(result: object) -> dict[str, Any] | None:
    if not isinstance(result, dict) or result.get("isError", False):
        return None
    structured = result.get("structuredContent")
    return structured if isinstance(structured, dict) else None


def _listed_scope_paths(payload: object) -> set[str]:
    """Extract exact canonical scope paths from FAVA list/readiness payloads."""

    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return set()
    scopes = payload.get("scopes")
    if not isinstance(scopes, list):
        return set()
    paths: set[str] = set()
    for item in scopes:
        if isinstance(item, str):
            paths.add(item)
        elif isinstance(item, dict) and isinstance(item.get("path"), str):
            paths.add(item["path"])
    return paths


def contact_principal_mcp_connection(
    config: PrincipalConfig,
    *,
    data_repo: Path,
    company_scope: str,
    timeout: float = 45.0,
) -> dict[str, object]:
    """Initialize and call the declared FAVA MCP stdio transport for one principal.

    This is a real connection attempt against ``fava-trails-server`` bound to the
    selected data root and process identity. Library-only helpers are not enough
    for live doctor success. Fails closed when the server cannot be started,
    initialized, or authenticated for the intended scope.
    """

    command = resolve_fava_server_command()
    if command is None:
        return {
            "ok": False,
            "connection_attempted": False,
            "transport": "stdio",
            "message": "fava-trails-server is not available for live MCP contact",
        }

    repo = data_repo.expanduser().resolve()
    declared = config.agent_id
    env = _bound_env_for_data_repo(repo, agent_id=declared)
    env["FAVA_TRAILS_SCOPE"] = company_scope
    env["FAVA_TRAILS_DIR"] = str(repo / "trails")
    # Keep logs/config outside the data repo so JJ dirty-path guards stay clean.
    scratch = Path(tempfile.mkdtemp(prefix=f"helmet-fava-mcp-{config.principal}-"))
    env["FAVA_TRAILS_LOG_DIR"] = str(scratch / "logs")
    env["XDG_CONFIG_HOME"] = str(scratch / "xdg-config")

    import asyncio

    async def _run() -> dict[str, object]:
        stderr_path = scratch / "logs" / f"mcp-doctor-{config.principal}.log"
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        checks: list[dict[str, object]] = []

        def _check(code: str, ok: bool, message: str) -> None:
            checks.append({"code": code, "ok": ok, "message": redact_secrets(message)})

        process: asyncio.subprocess.Process | None = None
        try:
            with stderr_path.open("wb") as stderr_fh:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(repo),
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=stderr_fh,
                )
        except (OSError, ValueError) as exc:
            return {
                "ok": False,
                "connection_attempted": True,
                "transport": "stdio",
                "message": redact_secrets(f"failed to start MCP server: {type(exc).__name__}: {exc}"),
                "checks": checks,
            }

        next_id = 0

        async def rpc(method: str, params: dict[str, Any] | None = None, *, notification: bool = False) -> Any:
            nonlocal next_id
            if process.stdin is None or process.stdout is None:
                raise RuntimeError("MCP server stdio pipes are unavailable")
            next_id += 1
            message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                message["params"] = params
            if not notification:
                message["id"] = next_id
            process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            await process.stdin.drain()
            if notification:
                return None
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"MCP RPC timed out waiting for {method}")
                line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
                if not line:
                    err_tail = ""
                    try:
                        err_tail = stderr_path.read_text(encoding="utf-8")[-1000:]
                    except OSError:
                        pass
                    raise RuntimeError(f"MCP server closed stdout during {method}: {err_tail}")
                response = json.loads(line.decode("utf-8"))
                if response.get("id") != next_id:
                    continue
                if "error" in response:
                    raise RuntimeError(f"MCP error on {method}: {response['error']}")
                return response.get("result")

        try:
            initialized = await rpc(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "hermes-helmet-doctor", "version": "1"},
                },
            )
            if not isinstance(initialized, dict) or "serverInfo" not in initialized and "capabilities" not in initialized:
                _check("mcp-initialize", False, "initialize returned unexpected payload")
            else:
                caps = initialized.get("capabilities") if isinstance(initialized, dict) else None
                has_tools = isinstance(caps, dict) and "tools" in caps
                _check(
                    "mcp-initialize",
                    bool(has_tools),
                    "MCP initialize succeeded with tools capability"
                    if has_tools
                    else "MCP initialize missing tools capability",
                )
            await rpc("notifications/initialized", notification=True)

            listed = await rpc("tools/list")
            tools = listed.get("tools") if isinstance(listed, dict) else None
            names = {
                str(tool.get("name"))
                for tool in (tools or [])
                if isinstance(tool, dict) and tool.get("name")
            }
            required = {"save_thought", "propose_truth", "recall", "list_scopes"}
            missing = sorted(required - names)
            _check(
                "mcp-tools-list",
                not missing,
                "required MCP tools present over stdio" if not missing else f"missing tools over stdio: {missing}",
            )

            # Read-only calls establish reachability, exact scope, and the
            # process-owned identity without adding diagnostic ledger records.
            scopes_result = await rpc("tools/call", {"name": "list_scopes", "arguments": {}})
            scope_payload = _structured_tool_result(scopes_result)
            scope_ok = company_scope in _listed_scope_paths(scope_payload)
            _check(
                "mcp-list-scopes",
                scope_ok,
                f"list_scopes confirmed exact scope {company_scope}"
                if scope_ok
                else f"list_scopes did not confirm exact scope {company_scope}",
            )

            recalled = await rpc(
                "tools/call",
                {
                    "name": "recall",
                    "arguments": {
                        "trail_name": company_scope,
                        "mode": "authoring",
                        "agent_id": declared,
                        "limit": 1,
                    },
                },
            )
            recall_payload = _structured_tool_result(recalled)
            identity_ok = bool(recall_payload and recall_payload.get("status") == "ok")
            _check(
                "mcp-bound-identity",
                identity_ok,
                f"read-only recall accepted exact server identity {declared}"
                if identity_ok
                else "read-only recall did not accept the declared server identity",
            )

            ok = all(bool(item.get("ok")) for item in checks)
            return {
                "ok": ok,
                "connection_attempted": True,
                "transport": "stdio",
                "command": command[0],
                "verified_agent_id": declared if identity_ok else None,
                "declared_agent_id": declared,
                "message": "MCP stdio connection verified" if ok else "MCP stdio connection verification failed",
                "checks": checks,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "connection_attempted": True,
                "transport": "stdio",
                "message": redact_secrets(f"MCP connection failed: {type(exc).__name__}: {exc}"),
                "checks": checks,
            }
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except (TimeoutError, ProcessLookupError):
                    process.kill()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except (TimeoutError, ProcessLookupError):
                        pass

    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "connection_attempted": True,
            "transport": "stdio",
            "message": redact_secrets(f"MCP connection runner failed: {type(exc).__name__}: {exc}"),
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def verify_principal_live(
    config: PrincipalConfig,
    *,
    data_repo: Path,
    company_scope: str,
) -> dict[str, object]:
    """Verify one principal via readiness, identity, and live MCP transport contact."""

    fava = try_import_fava()
    if fava is None:
        return {
            "ok": False,
            "principal": config.principal,
            "declared_agent_id": config.agent_id,
            "message": "accepted FAVA Trails package is not importable for live verification",
        }

    repo = data_repo.expanduser().resolve()
    declared = config.agent_id
    # Bind process-owned identity to the selected root only.
    previous = {
        key: os.environ.get(key)
        for key in (
            "FAVA_TRAILS_DATA_REPO",
            "FAVA_TRAILS_AGENT_ID",
            "FAVA_TRAILS_OPERATOR",
            "FAVA_TRAILS_DIR",
            "FAVA_TRAILS_SCOPE",
        )
    }
    result: dict[str, object] = {
        "ok": False,
        "principal": config.principal,
        "declared_agent_id": declared,
        "verified_agent_id": None,
        "scope": company_scope,
        "checks": [],
        "connection": None,
    }
    checks: list[dict[str, object]] = []

    def _check(code: str, ok: bool, message: str) -> None:
        checks.append({"code": code, "ok": ok, "message": redact_secrets(message)})

    try:
        for key in previous:
            os.environ.pop(key, None)
        os.environ["FAVA_TRAILS_DATA_REPO"] = str(repo)
        os.environ["FAVA_TRAILS_AGENT_ID"] = declared
        fava["ConfigStore"].reset()

        # Schema + readiness against the selected root (not ambient).
        try:
            ready = fava["probe_data_repository"](repo)
            _check(
                "readiness",
                ready.get("status") == "ok",
                f"readiness status={ready.get('status')} scopes={ready.get('scopes')}",
            )
        except fava["ReadinessFailure"] as exc:
            _check("readiness", False, f"{exc.reason}: {exc.message}")
            result["checks"] = checks
            result["message"] = "selected data repository failed FAVA readiness"
            return result
        except Exception as exc:  # noqa: BLE001
            _check("readiness", False, f"{type(exc).__name__}")
            result["checks"] = checks
            result["message"] = "selected data repository readiness probe failed"
            return result

        # Canonical FAVA creates per-scope configuration under trails/. The root
        # config's optional trails map is not authoritative for these scopes.
        try:
            import yaml  # type: ignore

            scope_config = repo / "trails" / Path(*company_scope.split("/")) / ".fava-trails.yaml"
            parsed = yaml.safe_load(scope_config.read_text(encoding="utf-8"))
            scope_ok = isinstance(parsed, dict) and parsed.get("name") == company_scope
            _check(
                "configured-scope",
                bool(scope_ok),
                (
                    f"scope {company_scope} has canonical per-scope configuration"
                    if scope_ok
                    else f"scope {company_scope} missing or name mismatch in selected data root"
                ),
            )
            if not scope_ok:
                result["checks"] = checks
                result["message"] = "declared company scope is not configured in selected data root"
                return result
        except Exception as exc:  # noqa: BLE001
            _check("configured-scope", False, f"{type(exc).__name__}: {exc}")
            result["checks"] = checks
            result["message"] = "could not validate canonical per-scope configuration"
            return result

        # Process-owned identity (cannot be established by tool args).
        from fava_trails.governance import runtime_principal  # type: ignore

        runtime = runtime_principal()
        result["verified_agent_id"] = runtime.agent_id
        identity_ok = runtime.agent_id == declared and not runtime.operator
        _check(
            "process-identity",
            identity_ok,
            (
                f"verified agent_id={runtime.agent_id} operator={runtime.operator}"
                if identity_ok
                else "declared identity does not match process-owned FAVA_TRAILS_AGENT_ID"
            ),
        )
        if not identity_ok:
            result["checks"] = checks
            result["message"] = "principal identity verification failed"
            return result

        # Supplemental in-process auth surface checks (not a substitute for transport).
        server = fava.get("server")
        if server is not None:
            import asyncio

            async def _local_auth_checks() -> None:
                principal = fava["Principal"](agent_id=declared, operator=False)
                try:
                    await server._authorize_tool(
                        "save_thought",
                        {"trail_name": company_scope, "content": "x", "agent_id": "other-agent"},
                        principal,
                    )
                    _check("local-mcp-spoof-agent", False, "spoofed agent_id was accepted")
                except PermissionError:
                    _check("local-mcp-spoof-agent", True, "spoofed agent_id rejected locally")
                except Exception as exc:  # noqa: BLE001
                    _check("local-mcp-spoof-agent", False, f"{type(exc).__name__}")
                try:
                    await server._authorize_tool("rollback", {"op_id": "must-not-run"}, principal)
                    _check("local-mcp-operator-boundary", False, "operator tool allowed for ordinary principal")
                except PermissionError:
                    _check("local-mcp-operator-boundary", True, "operator tools denied for ordinary principal")
                except Exception as exc:  # noqa: BLE001
                    _check("local-mcp-operator-boundary", False, f"{type(exc).__name__}")

            try:
                asyncio.run(_local_auth_checks())
            except Exception as exc:  # noqa: BLE001
                _check("local-mcp-auth", False, f"{type(exc).__name__}: {exc}")

        # Required: contact the declared MCP stdio connection.
        connection = contact_principal_mcp_connection(
            config,
            data_repo=repo,
            company_scope=company_scope,
        )
        result["connection"] = {
            key: connection.get(key)
            for key in (
                "ok",
                "connection_attempted",
                "transport",
                "message",
                "verified_agent_id",
                "command",
            )
            if key in connection
        }
        conn_ok = bool(connection.get("ok")) and bool(connection.get("connection_attempted"))
        _check(
            "mcp-connection",
            conn_ok,
            str(connection.get("message") or "MCP connection check"),
        )
        conn_checks = connection.get("checks")
        if isinstance(conn_checks, list):
            for item in conn_checks:
                if isinstance(item, dict):
                    _check(
                        f"mcp-{item.get('code')}",
                        bool(item.get("ok")),
                        str(item.get("message") or ""),
                    )
        if conn_ok and connection.get("verified_agent_id"):
            result["verified_agent_id"] = connection.get("verified_agent_id")

        result["checks"] = checks
        result["ok"] = all(bool(item.get("ok")) for item in checks)
        result["message"] = (
            "principal live verification passed"
            if result["ok"]
            else "principal live verification failed"
        )
        return result
    finally:
        fava["ConfigStore"].reset()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        result["checks"] = checks or result.get("checks") or []


def probe_live_principals(
    configs: Mapping[str, PrincipalConfig],
    *,
    data_repo: Path,
    company_scope: str,
) -> dict[str, object]:
    """Live verification for all declared principals against the selected root."""

    if try_import_fava() is None:
        return {
            "ok": False,
            "available": False,
            "message": (
                "accepted FAVA Trails package is required for live principal verification "
                f"(install pin {FAVA_PIN[:12]}…)"
            ),
            "principals": [],
        }
    reports = [
        verify_principal_live(cfg, data_repo=data_repo, company_scope=company_scope)
        for cfg in configs.values()
    ]
    ok = all(bool(item.get("ok")) for item in reports)
    return {
        "ok": ok,
        "available": True,
        "message": "live principal verification passed" if ok else "live principal verification failed",
        "principals": reports,
        "bound_data_repo": str(data_repo.expanduser()),
        "company_scope": company_scope,
    }


# ── Protocol-faithful in-process lifecycle engine (hermetic example) ─────────


@dataclass
class Thought:
    thought_id: str
    content: str
    agent_id: str
    scope: str
    source_type: str = "decision"
    status: str = "draft"
    namespace: str = "drafts"
    parent_id: str | None = None
    supersedes_id: str | None = None
    superseded_by: str | None = None
    approval: dict[str, object] | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def public_view(self) -> dict[str, object]:
        return {
            "thought_id": self.thought_id,
            "content": self.content,
            "agent_id": self.agent_id,
            "scope": self.scope,
            "source_type": self.source_type,
            "status": self.status,
            "namespace": self.namespace,
            "parent_id": self.parent_id,
            "supersedes_id": self.supersedes_id,
            "superseded_by": self.superseded_by,
            "approval": dict(self.approval) if self.approval else None,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "frozen": self.status in {"approved", "rejected", "tombstoned"}
            or bool(self.superseded_by),
        }


class FakeFavaEngine:
    """In-memory FAVA lifecycle model for hermetic Helmet verification.

    Mirrors governed-recall visibility and ordinary-principal boundaries from
    FAVA 0.6.x without requiring JJ, MCP, or network. Not a substitute for the
    real engine in production.
    """

    FROZEN = frozenset({"approved", "rejected", "tombstoned"})

    def __init__(self) -> None:
        self._thoughts: dict[str, Thought] = {}
        self._principals: dict[str, dict[str, object]] = {}

    def register_principal(
        self,
        agent_id: str,
        *,
        role: str = ROLE_ORDINARY,
        operator: bool = False,
        scopes: Sequence[str] | None = None,
    ) -> None:
        agent_id = validate_agent_id(agent_id)
        if operator and role != ROLE_OPERATOR:
            raise FavaTrailsError("operator flag requires operator role")
        if not operator and role not in ORDINARY_ALLOWED:
            raise FavaTrailsError("non-operator principals must be ordinary")
        self._principals[agent_id] = {
            "role": role,
            "operator": operator,
            "scopes": tuple(scopes or ()),
        }

    def _require_principal(self, agent_id: str) -> dict[str, object]:
        if agent_id not in self._principals:
            raise FavaTrailsError("writes require a server-configured ordinary principal")
        return self._principals[agent_id]

    def save_draft(
        self,
        *,
        agent_id: str,
        scope: str,
        content: str,
        source_type: str = "decision",
        metadata: Mapping[str, object] | None = None,
    ) -> Thought:
        principal = self._require_principal(agent_id)
        if principal["operator"]:
            raise FavaTrailsError("operator endpoint must not author ordinary drafts in this demo")
        scope = validate_scope(scope)
        thought = Thought(
            thought_id=str(uuid.uuid4()).replace("-", "")[:26].upper(),
            content=content,
            agent_id=agent_id,
            scope=scope,
            source_type=source_type,
            status="draft",
            namespace="drafts",
            metadata=dict(metadata or {}),
        )
        self._thoughts[thought.thought_id] = thought
        return thought

    def propose(
        self,
        thought_id: str,
        *,
        agent_id: str,
    ) -> Thought:
        self._require_principal(agent_id)
        thought = self._get(thought_id)
        if thought.agent_id != agent_id:
            raise FavaTrailsError("authoring is limited to the configured principal's own drafts")
        if thought.status not in {"draft", "proposed"}:
            raise FavaTrailsError("only draft thoughts can be proposed")
        thought.status = "proposed"
        return thought

    def trust_gate(
        self,
        thought_id: str,
        *,
        verdict: str,
        reviewer: str = "trust-gate:demo",
        approval_kind: str = "llm_advisory",
        reason: str = "",
    ) -> Thought:
        thought = self._get(thought_id)
        if thought.status not in {"proposed", "draft"}:
            raise FavaTrailsError("trust gate applies to proposed drafts only")
        verdict_n = verdict.strip().casefold()
        if verdict_n == "approve":
            permanent = {
                "decision": "decisions",
                "observation": "observations",
                "user_input": "preferences",
            }.get(thought.source_type, "observations")
            thought.status = "approved"
            thought.namespace = permanent
            thought.approval = {
                "kind": approval_kind,
                "reviewer": reviewer,
                "reason": reason or "approved",
            }
            if thought.supersedes_id:
                original = self._thoughts.get(thought.supersedes_id)
                if original is not None and original.superseded_by is None:
                    original.superseded_by = thought.thought_id
            return thought
        if verdict_n == "reject":
            thought.status = "rejected"
            thought.namespace = "drafts"
            thought.approval = {
                "kind": approval_kind,
                "reviewer": reviewer,
                "reason": reason or "rejected",
            }
            # Fail-closed: original approved truth stays current.
            return thought
        thought.status = "error"
        thought.approval = {
            "kind": approval_kind,
            "reviewer": reviewer,
            "reason": reason or "trust gate error",
        }
        return thought

    def supersede(
        self,
        original_id: str,
        *,
        agent_id: str,
        new_content: str,
        reason: str = "",
    ) -> Thought:
        self._require_principal(agent_id)
        original = self._get(original_id)
        if original.status != "approved" or original.superseded_by:
            raise FavaTrailsError("only current approved thoughts can be superseded")
        successor = Thought(
            thought_id=str(uuid.uuid4()).replace("-", "")[:26].upper(),
            content=new_content,
            agent_id=agent_id,
            scope=original.scope,
            source_type=original.source_type,
            status="draft",
            namespace="drafts",
            parent_id=original.thought_id,
            supersedes_id=original.thought_id,
            metadata={
                "supersede_reason": reason,
                "lineage_original": original.thought_id,
            },
        )
        self._thoughts[successor.thought_id] = successor
        # Original remains current until successor is approved.
        return successor

    def update_content(self, thought_id: str, new_content: str) -> Thought:
        thought = self._get(thought_id)
        if thought.status in self.FROZEN or thought.superseded_by:
            raise FavaTrailsError("approved/rejected content is frozen")
        thought.content = new_content
        return thought

    def recall(
        self,
        *,
        scope: str,
        mode: str = "governed",
        agent_id: str | None = None,
        query: str | None = None,
        include_superseded: bool = False,
    ) -> list[Thought]:
        scope = validate_scope(scope)
        mode_n = mode.strip().casefold()
        if include_superseded and mode_n != "history":
            raise FavaTrailsError("include_superseded requires history mode")
        if mode_n == "history":
            if not agent_id or not self._principals.get(agent_id, {}).get("operator"):
                raise FavaTrailsError("history mode requires an operator-controlled endpoint")
        hits: list[Thought] = []
        for thought in self._thoughts.values():
            if thought.scope != scope:
                continue
            if query and query.casefold() not in thought.content.casefold():
                continue
            if mode_n == "governed":
                if thought.status == "approved" and not thought.superseded_by:
                    hits.append(thought)
            elif mode_n == "authoring":
                if not agent_id:
                    raise FavaTrailsError("authoring mode requires configured agent identity")
                if thought.agent_id == agent_id and thought.status in {"draft", "proposed"}:
                    hits.append(thought)
            elif mode_n == "history":
                if include_superseded or not thought.superseded_by:
                    hits.append(thought)
            else:
                raise FavaTrailsError("unknown recall mode")
        return hits

    def _get(self, thought_id: str) -> Thought:
        thought = self._thoughts.get(thought_id)
        if thought is None:
            raise FavaTrailsError("thought not found")
        return thought


def _serialize_fava_thought(record: Any) -> dict[str, object]:
    fm = record.frontmatter
    return {
        "thought_id": record.thought_id,
        "content": record.content,
        "agent_id": fm.agent_id,
        "status": fm.validation_status.value if hasattr(fm.validation_status, "value") else str(fm.validation_status),
        "source_type": fm.source_type.value if hasattr(fm.source_type, "value") else str(fm.source_type),
        "parent_id": fm.parent_id,
        "supersedes_id": fm.supersedes_id,
        "superseded_by": fm.superseded_by,
        "approval": (fm.metadata.extra or {}).get("approval") if getattr(fm, "metadata", None) else None,
        "trust_gate": (fm.metadata.extra or {}).get("trust_gate") if getattr(fm, "metadata", None) else None,
        "metadata_extra": dict(fm.metadata.extra or {}) if getattr(fm, "metadata", None) else {},
    }


def run_governed_lifecycle_example(
    *,
    company_scope: str = "exampleco/engineering",
    captain_id: str = "exampleco-captain-fava",
    executor_id: str = "exampleco-agent-fava",
    engine: FakeFavaEngine | None = None,
    data_repo: Path | None = None,
    keep_data_repo: bool = False,
) -> dict[str, object]:
    """Demonstrate draft → propose → approve/reject → supersede → recall on accepted FAVA.

    When ``engine`` is provided, runs the isolated FakeFava path (unit tests only).
    The default path uses the accepted FAVA TrailManager + JJ backend with injected
    TrustResult verdicts (no network / no private credentials).
    """

    company_scope = validate_scope(company_scope)
    captain_id = validate_agent_id(captain_id)
    executor_id = validate_agent_id(executor_id)

    if engine is not None:
        return _run_fake_lifecycle_example(
            engine,
            company_scope=company_scope,
            captain_id=captain_id,
            executor_id=executor_id,
        )

    fava = try_import_fava()
    if fava is None:
        return {
            "ok": False,
            "company_scope": company_scope,
            "principals": {"captain": captain_id, "executor": executor_id},
            "steps": [],
            "engine": "unavailable",
            "note": (
                "Accepted FAVA Trails package is required for the governed lifecycle "
                f"example (pin {FAVA_PIN}). Install fava-trails and ensure jj is on PATH."
            ),
            "canonical": {
                "install": CANONICAL_INSTALL,
                "agent_setup": CANONICAL_AGENT_SETUP,
                "governed_recall": CANONICAL_GOVERNED_RECALL,
            },
        }

    return _run_accepted_fava_lifecycle_example(
        fava,
        company_scope=company_scope,
        captain_id=captain_id,
        executor_id=executor_id,
        data_repo=data_repo,
        keep_data_repo=keep_data_repo,
    )


def _run_fake_lifecycle_example(
    engine: FakeFavaEngine,
    *,
    company_scope: str,
    captain_id: str,
    executor_id: str,
) -> dict[str, object]:
    """Isolated deterministic FakeFava path for unit tests only."""

    engine.register_principal(captain_id, role=ROLE_ORDINARY)
    engine.register_principal(executor_id, role=ROLE_ORDINARY)
    engine.register_principal("example-operator", role=ROLE_OPERATOR, operator=True)

    steps: list[dict[str, object]] = []
    draft = engine.save_draft(
        agent_id=captain_id,
        scope=company_scope,
        content="Decision: adopt helmet doctor for FAVA readiness checks.",
        source_type="decision",
        metadata={"phase": "draft"},
    )
    steps.append({"step": "draft", "thought": draft.public_view()})
    early = engine.recall(scope=company_scope, mode="governed", agent_id=executor_id)
    steps.append({"step": "draft-isolation", "governed_count": len(early), "ok": len(early) == 0})
    proposed = engine.propose(draft.thought_id, agent_id=captain_id)
    steps.append({"step": "propose", "thought": proposed.public_view()})
    rejected_draft = engine.save_draft(
        agent_id=captain_id,
        scope=company_scope,
        content="Speculative claim without evidence.",
        source_type="inference",
    )
    engine.propose(rejected_draft.thought_id, agent_id=captain_id)
    rejected = engine.trust_gate(
        rejected_draft.thought_id, verdict="reject", reason="insufficient evidence"
    )
    still_hidden = engine.recall(scope=company_scope, mode="governed")
    steps.append(
        {
            "step": "reject-fail-closed",
            "thought": rejected.public_view(),
            "governed_count": len(still_hidden),
            "ok": rejected.status == "rejected" and len(still_hidden) == 0,
        }
    )
    approved = engine.trust_gate(
        proposed.thought_id,
        verdict="approve",
        approval_kind="llm_advisory",
        reason="durable company decision",
    )
    steps.append({"step": "approve", "thought": approved.public_view()})
    frozen_ok = False
    try:
        engine.update_content(approved.thought_id, "tamper")
    except FavaTrailsError:
        frozen_ok = True
    steps.append(
        {
            "step": "frozen-approved",
            "ok": frozen_ok and approved.status == "approved" and bool(approved.approval),
            "attributable_agent": approved.agent_id,
            "approval": approved.approval,
        }
    )
    shared = engine.recall(scope=company_scope, mode="governed", agent_id=executor_id)
    steps.append(
        {
            "step": "approved-shared-recall",
            "count": len(shared),
            "ids": [item.thought_id for item in shared],
            "ok": len(shared) == 1 and shared[0].thought_id == approved.thought_id,
        }
    )
    successor = engine.supersede(
        approved.thought_id,
        agent_id=captain_id,
        new_content="Decision: adopt helmet doctor and fava lifecycle-demo checks.",
        reason="clarify demo coverage",
    )
    engine.propose(successor.thought_id, agent_id=captain_id)
    mid = engine.recall(scope=company_scope, mode="governed")
    mid_ok = len(mid) == 1 and mid[0].thought_id == approved.thought_id
    promoted_successor = engine.trust_gate(successor.thought_id, verdict="approve")
    after = engine.recall(scope=company_scope, mode="governed")
    steps.append(
        {
            "step": "supersede",
            "successor": promoted_successor.public_view(),
            "original_after": engine._get(approved.thought_id).public_view(),
            "mid_governed_still_original": mid_ok,
            "final_governed_ids": [item.thought_id for item in after],
            "ok": (
                mid_ok
                and len(after) == 1
                and after[0].thought_id == promoted_successor.thought_id
                and engine._get(approved.thought_id).superseded_by
                == promoted_successor.thought_id
            ),
        }
    )
    promotion = engine.save_draft(
        agent_id=captain_id,
        scope=company_scope,
        content=(
            "Explicit promotion from OpenViking working context marker "
            "viking://user/memories/events/demo into FAVA governed decision."
        ),
        source_type="decision",
        metadata={
            "promotion": {
                "from": "openviking",
                "to": "fava_trails",
                "automatic": False,
                "source_uri": "viking://user/memories/events/demo-marker",
                "decision": "promote",
            }
        },
    )
    engine.propose(promotion.thought_id, agent_id=captain_id)
    promoted = engine.trust_gate(promotion.thought_id, verdict="approve")
    steps.append(
        {
            "step": "explicit-openviking-promotion",
            "thought": promoted.public_view(),
            "ok": (
                promoted.status == "approved"
                and isinstance(promoted.metadata.get("promotion"), dict)
                and promoted.metadata["promotion"].get("automatic") is False  # type: ignore[union-attr]
            ),
        }
    )
    ok = all(bool(step.get("ok", True)) for step in steps if "ok" in step)
    return {
        "ok": ok,
        "company_scope": company_scope,
        "principals": {"captain": captain_id, "executor": executor_id},
        "steps": steps,
        "engine": "fake-fava-lifecycle",
        "note": (
            "Isolated FakeFava unit path only. Default lifecycle-demo uses accepted FAVA."
        ),
        "canonical": {
            "install": CANONICAL_INSTALL,
            "agent_setup": CANONICAL_AGENT_SETUP,
            "governed_recall": CANONICAL_GOVERNED_RECALL,
        },
    }


def _run_accepted_fava_lifecycle_example(
    fava: Mapping[str, Any],
    *,
    company_scope: str,
    captain_id: str,
    executor_id: str,
    data_repo: Path | None,
    keep_data_repo: bool,
) -> dict[str, object]:
    """Protocol example on accepted FAVA TrailManager + JJ (TrustResult injected)."""

    import asyncio

    jj_bin = shutil.which("jj") or str(Path.home() / ".local" / "bin" / "jj")
    if not Path(jj_bin).exists():
        return {
            "ok": False,
            "company_scope": company_scope,
            "principals": {"captain": captain_id, "executor": executor_id},
            "steps": [],
            "engine": "fava-missing-jj",
            "note": "jj binary is required for the accepted FAVA lifecycle example",
            "canonical": {
                "install": CANONICAL_INSTALL,
                "agent_setup": CANONICAL_AGENT_SETUP,
                "governed_recall": CANONICAL_GOVERNED_RECALL,
            },
        }

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if data_repo is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="helmet-fava-lifecycle-")
        home = Path(temp_dir.name) / "data"
    else:
        home = Path(data_repo).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    # Canonicalize once so env and JjBackend share the same root (macOS /var vs /private/var).
    home = home.resolve()
    (home / "trails").mkdir(parents=True, exist_ok=True)

    template = default_company_template(company_scope=company_scope)
    # Align principal ids with the demo identities.
    template = FavaCompanyTemplate(
        company_slug=template.company_slug,
        company_scope=company_scope,
        data_repo_placeholder=template.data_repo_placeholder,
        remote_url_placeholder=template.remote_url_placeholder,
        principals={"captain": captain_id, "executor": executor_id},
        trust_gate=template.trust_gate,
        openrouter_api_key_env=template.openrouter_api_key_env,
        license_id=template.license_id,
        fava_pin=template.fava_pin,
        fava_version_hint=template.fava_version_hint,
    )
    config_path = home / "config.yaml"
    if not config_path.exists():
        config_path.write_text(render_data_repo_config_yaml(template), encoding="utf-8")
    prompt_path = home / "trails" / "trust-gate-prompt.md"
    if not prompt_path.exists():
        prompt_path.write_text(render_trust_gate_prompt(template), encoding="utf-8")

    previous = {
        key: os.environ.get(key)
        for key in (
            "FAVA_TRAILS_DATA_REPO",
            "FAVA_TRAILS_AGENT_ID",
            "FAVA_TRAILS_OPERATOR",
            "FAVA_TRAILS_DIR",
            "FAVA_TRAILS_SCOPE",
        )
    }
    steps: list[dict[str, object]] = []

    try:
        for key in previous:
            os.environ.pop(key, None)
        os.environ["FAVA_TRAILS_DATA_REPO"] = str(home)
        fava["ConfigStore"].reset()

        if not (home / ".jj").exists():
            subprocess.run(
                [jj_bin, "git", "init", "--colocate"],
                cwd=str(home),
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [jj_bin, "config", "set", "--repo", "user.name", "Helmet FAVA Demo"],
                cwd=str(home),
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [jj_bin, "config", "set", "--repo", "user.email", "fava-demo@example.invalid"],
                cwd=str(home),
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [jj_bin, "commit", "-m", "helmet fava lifecycle baseline"],
                cwd=str(home),
                check=True,
                capture_output=True,
                text=True,
            )

        TrailManager = fava["TrailManager"]
        JjBackend = fava["JjBackend"]
        TrustResult = fava["TrustResult"]
        SourceType = fava["SourceType"]
        Visibility = fava["Visibility"]
        Principal = fava["Principal"]

        trail_path = home / "trails" / company_scope
        backend = JjBackend(repo_root=home, trail_path=trail_path)
        manager = TrailManager(company_scope, vcs=backend)

        async def _run() -> None:
            await manager.init()

            draft = await manager.save_thought(
                content="Decision: adopt helmet doctor for FAVA readiness checks.",
                agent_id=captain_id,
                source_type=SourceType.DECISION,
                metadata={"extra": {"phase": "draft"}},
            )
            steps.append({"step": "draft", "thought": _serialize_fava_thought(draft)})

            early = await manager.recall(
                visibility=Visibility(mode="governed", principal=Principal(agent_id=executor_id))
            )
            steps.append(
                {
                    "step": "draft-isolation",
                    "governed_count": len(early),
                    "ok": len(early) == 0,
                }
            )
            steps.append(
                {
                    "step": "propose",
                    "thought": _serialize_fava_thought(draft),
                    "ok": draft.frontmatter.validation_status.value == "draft",
                }
            )

            rejected_draft = await manager.save_thought(
                content="Speculative claim without evidence.",
                agent_id=captain_id,
                source_type=SourceType.INFERENCE,
            )
            rejected = await manager.propose_truth(
                rejected_draft.thought_id,
                TrustResult(
                    verdict="reject",
                    reasoning="insufficient evidence",
                    reviewer="trust-gate:demo",
                    approval_kind="llm_advisory",
                ),
            )
            still_hidden = await manager.recall(visibility=Visibility())
            steps.append(
                {
                    "step": "reject-fail-closed",
                    "thought": _serialize_fava_thought(rejected),
                    "governed_count": len(still_hidden),
                    "ok": (
                        rejected.frontmatter.validation_status.value == "rejected"
                        and len(still_hidden) == 0
                    ),
                }
            )

            approved = await manager.propose_truth(
                draft.thought_id,
                TrustResult(
                    verdict="approve",
                    reasoning="durable company decision",
                    reviewer="trust-gate:demo",
                    approval_kind="llm_advisory",
                ),
            )
            steps.append({"step": "approve", "thought": _serialize_fava_thought(approved)})

            frozen_ok = False
            try:
                await manager.update_thought(approved.thought_id, "tamper")
            except ValueError:
                frozen_ok = True
            approval = (approved.frontmatter.metadata.extra or {}).get("approval")
            steps.append(
                {
                    "step": "frozen-approved",
                    "ok": (
                        frozen_ok
                        and approved.frontmatter.validation_status.value == "approved"
                        and bool(approval)
                    ),
                    "attributable_agent": approved.frontmatter.agent_id,
                    "approval": approval,
                }
            )

            shared = await manager.recall(
                visibility=Visibility(mode="governed", principal=Principal(agent_id=executor_id))
            )
            steps.append(
                {
                    "step": "approved-shared-recall",
                    "count": len(shared),
                    "ids": [item.thought_id for item in shared],
                    "ok": len(shared) == 1 and shared[0].thought_id == approved.thought_id,
                }
            )

            successor = await manager.supersede(
                approved.thought_id,
                "Decision: adopt helmet doctor and fava lifecycle-demo checks.",
                reason="clarify demo coverage",
                agent_id=captain_id,
            )
            mid = await manager.recall(visibility=Visibility())
            mid_ok = len(mid) == 1 and mid[0].thought_id == approved.thought_id
            promoted_successor = await manager.propose_truth(
                successor.thought_id,
                TrustResult(
                    verdict="approve",
                    reasoning="clarified decision",
                    reviewer="trust-gate:demo",
                    approval_kind="llm_advisory",
                ),
            )
            after = await manager.recall(visibility=Visibility())
            # Reload original for lineage backlink.
            orig_after = None
            history = await manager.recall(
                visibility=Visibility(
                    mode="history",
                    principal=Principal(agent_id="example-operator", operator=True),
                    include_superseded=True,
                )
            )
            for item in history:
                if item.thought_id == approved.thought_id:
                    orig_after = item
                    break
            steps.append(
                {
                    "step": "supersede",
                    "successor": _serialize_fava_thought(promoted_successor),
                    "original_after": _serialize_fava_thought(orig_after) if orig_after else None,
                    "mid_governed_still_original": mid_ok,
                    "final_governed_ids": [item.thought_id for item in after],
                    "ok": (
                        mid_ok
                        and len(after) == 1
                        and after[0].thought_id == promoted_successor.thought_id
                        and orig_after is not None
                        and orig_after.frontmatter.superseded_by
                        == promoted_successor.thought_id
                    ),
                }
            )

            promotion = await manager.save_thought(
                content=(
                    "Explicit promotion from OpenViking working context marker "
                    "viking://user/memories/events/demo into FAVA governed decision."
                ),
                agent_id=captain_id,
                source_type=SourceType.DECISION,
                metadata={
                    "extra": {
                        "promotion": {
                            "from": "openviking",
                            "to": "fava_trails",
                            "automatic": False,
                            "source_uri": "viking://user/memories/events/demo-marker",
                            "decision": "promote",
                        }
                    }
                },
            )
            promoted = await manager.propose_truth(
                promotion.thought_id,
                TrustResult(
                    verdict="approve",
                    reasoning="explicit promotion decision",
                    reviewer="trust-gate:demo",
                    approval_kind="llm_advisory",
                ),
            )
            promo_extra = promoted.frontmatter.metadata.extra or {}
            promo_meta = promo_extra.get("promotion")
            steps.append(
                {
                    "step": "explicit-openviking-promotion",
                    "thought": _serialize_fava_thought(promoted),
                    "ok": (
                        promoted.frontmatter.validation_status.value == "approved"
                        and isinstance(promo_meta, dict)
                        and promo_meta.get("automatic") is False
                    ),
                }
            )

        asyncio.run(_run())
        ok = all(bool(step.get("ok", True)) for step in steps if "ok" in step)
        return {
            "ok": ok,
            "company_scope": company_scope,
            "principals": {"captain": captain_id, "executor": executor_id},
            "steps": steps,
            "engine": "accepted-fava-trail-manager",
            "fava_pin": FAVA_PIN,
            "data_repo": str(home),
            "note": (
                "Accepted FAVA TrailManager lifecycle with injected TrustResult "
                "(no network). Live Captain/executor MCP discovery in foreground apps "
                "and private Trust Gate LLM calls remain Captain acceptance."
            ),
            "canonical": {
                "install": CANONICAL_INSTALL,
                "agent_setup": CANONICAL_AGENT_SETUP,
                "governed_recall": CANONICAL_GOVERNED_RECALL,
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "company_scope": company_scope,
            "principals": {"captain": captain_id, "executor": executor_id},
            "steps": steps,
            "engine": "accepted-fava-error",
            "message": redact_secrets(f"{type(exc).__name__}: {exc}"),
            "canonical": {
                "install": CANONICAL_INSTALL,
                "agent_setup": CANONICAL_AGENT_SETUP,
                "governed_recall": CANONICAL_GOVERNED_RECALL,
            },
        }
    finally:
        fava["ConfigStore"].reset()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if temp_dir is not None and not keep_data_repo:
            temp_dir.cleanup()


def doctor_fava_trails(
    policy: Policy,
    *,
    principal_paths: Mapping[str, Path] | None = None,
    data_repo: Path | None = None,
    require_live: bool = False,
    live_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> DoctorReport:
    """Verify optional FAVA Trails configuration without echoing secrets."""

    if not policy.integrations.fava_trails:
        return DoctorReport(
            enabled=False,
            skipped=True,
            ok=True,
            findings=(
                DoctorFinding(
                    code="fava-declined",
                    ok=True,
                    message="FAVA Trails integration declined; Helmet operates without it",
                ),
            ),
        )

    findings: list[DoctorFinding] = []
    template = company_template_from_policy(policy)
    findings.append(
        DoctorFinding(
            code="template",
            ok=True,
            message=(
                f"company brain template ready for scope {template.company_scope} "
                f"(FAVA pin {template.fava_pin[:12]}…, license {FAVA_LICENSE})"
            ),
        )
    )
    findings.append(
        DoctorFinding(
            code="canonical-contracts",
            ok=True,
            message="canonical install/agent/governed-recall contracts linked (not copied)",
        )
    )

    if principal_paths is None:
        return DoctorReport(
            enabled=True,
            skipped=False,
            ok=False,
            findings=tuple(findings)
            + (
                DoctorFinding(
                    code="principal-paths",
                    ok=False,
                    message="owner-only captain/executor principal paths were not provided",
                ),
            ),
        )

    missing = [name for name in DEFAULT_PRINCIPALS if name not in principal_paths]
    if missing:
        return DoctorReport(
            enabled=True,
            skipped=False,
            ok=False,
            findings=tuple(findings)
            + (
                DoctorFinding(
                    code="principal-paths",
                    ok=False,
                    message="captain and executor principal paths are required",
                ),
            ),
        )

    try:
        configs = load_principal_configs(principal_paths)
    except FavaTrailsError as exc:
        return DoctorReport(
            enabled=True,
            skipped=False,
            ok=False,
            findings=tuple(findings)
            + (
                DoctorFinding(
                    code="principal-config",
                    ok=False,
                    message=redact_secrets(str(exc)),
                ),
            ),
        )

    findings.append(
        DoctorFinding(
            code="ordinary-principals",
            ok=True,
            message="captain and executor are distinct ordinary principals (not operator)",
        )
    )
    findings.append(
        DoctorFinding(
            code="company-scope",
            ok=True,
            message="principals share one company scope and one data repository path",
        )
    )
    findings.append(
        DoctorFinding(
            code="authorization-boundary",
            ok=True,
            message=(
                "scope is a hint only; authoring bound to dedicated FAVA_TRAILS_AGENT_ID "
                "process identity"
            ),
        )
    )

    repo_path = data_repo or Path(next(iter(configs.values())).data_repo)
    shared_scope = next(iter(configs.values())).company_scope
    inspection = inspect_data_repo(repo_path, company_scope=shared_scope)
    findings.append(
        DoctorFinding(
            code="data-repo",
            ok=bool(inspection.get("ok")),
            message=str(inspection.get("message") or "data repository check"),
        )
    )

    verification_mode = "offline"
    if require_live:
        verification_mode = "live"
        live = probe_live_principals(
            configs,
            data_repo=repo_path,
            company_scope=shared_scope,
        )
        if not live.get("available"):
            findings.append(
                DoctorFinding(
                    code="live-principals",
                    ok=False,
                    message=str(live.get("message") or "live principal verification unavailable"),
                )
            )
        else:
            findings.append(
                DoctorFinding(
                    code="live-principals",
                    ok=bool(live.get("ok")),
                    message=redact_secrets(str(live.get("message") or "")),
                )
            )
            for item in live.get("principals") or []:
                if not isinstance(item, dict):
                    continue
                detail = str(item.get("message") or "")
                connection = item.get("connection")
                if isinstance(connection, dict) and connection.get("message"):
                    detail = f"{detail}; connection={connection.get('message')}"
                checks = item.get("checks")
                if isinstance(checks, list):
                    failed = [
                        str(check.get("code"))
                        for check in checks
                        if isinstance(check, dict) and not check.get("ok")
                    ]
                    if failed:
                        detail = f"{detail}; failed_checks={','.join(failed)}"
                findings.append(
                    DoctorFinding(
                        code=f"live-principal-{item.get('principal')}",
                        ok=bool(item.get("ok")),
                        message=redact_secrets(
                            detail
                            or (
                                f"declared={item.get('declared_agent_id')} "
                                f"verified={item.get('verified_agent_id')}"
                            )
                        ),
                    )
                )
        # Optional supplemental CLI doctor still bound to selected root (never ambient-only).
        if live_runner is not None or shutil.which("fava-trails"):
            probe = probe_fava_cli(
                data_repo=str(repo_path),
                agent_id=next(iter(configs.values())).agent_id,
                credential_env_name=next(iter(configs.values())).openrouter_api_key_env,
                runner=live_runner,
            )
            if probe.get("available"):
                findings.append(
                    DoctorFinding(
                        code="live-cli-bound",
                        ok=bool(probe.get("ok")),
                        message=redact_secrets(str(probe.get("message") or "")),
                    )
                )
    else:
        findings.append(
            DoctorFinding(
                code="identity-verification",
                ok=True,
                message=(
                    "offline configuration check only; live principal/MCP verification not run "
                    "(pass --live when accepted FAVA and the selected data repo are available)"
                ),
            )
        )

    safe_principals = [cfg.public_view() for cfg in configs.values()]
    ok = all(item.ok for item in findings)
    return DoctorReport(
        enabled=True,
        skipped=False,
        ok=ok,
        findings=tuple(findings),
        principals=tuple(safe_principals),
        verification_mode=verification_mode,
    )


def doctor(
    policy_path: Path,
    *,
    principal_paths: Mapping[str, Path] | None = None,
    data_repo: Path | None = None,
    require_live: bool = False,
    live_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    include_openviking: bool = True,
    openviking_client_paths: Mapping[str, Path] | None = None,
    openviking_require_live: bool = False,
    model_lane_runtime_dir: Path | None = None,
) -> dict[str, object]:
    """Top-level doctor entry used by the CLI."""

    try:
        policy = load_authority(policy_path)
    except AuthorityError as exc:
        return {
            "ok": False,
            "policy": {"ok": False, "message": str(exc)},
            "fava_trails": None,
            "openviking": None,
            "model_lanes": None,
        }
    except OSError:
        return {
            "ok": False,
            "policy": {"ok": False, "message": "policy could not be read"},
            "fava_trails": None,
            "openviking": None,
            "model_lanes": None,
        }

    fava_report = doctor_fava_trails(
        policy,
        principal_paths=principal_paths,
        data_repo=data_repo,
        require_live=require_live,
        live_runner=live_runner,
    )

    from hermes_helmet.model_lanes import doctor_model_lanes, lanes_from_policy

    # Legacy ``doctor --live`` authenticates FAVA/OpenViking integrations.
    # Provider-native Hermes executors (for example OAuth-backed providers)
    # have no OpenAI-compatible base URL here; their live verification belongs
    # to the setup-state doctor, which has provider-specific runtime context.
    model_lane_live = require_live
    configured_lanes = lanes_from_policy(policy)
    executor = configured_lanes.hermes_executor
    if (
        require_live
        and not configured_lanes.optional_lanes_selected
        and executor.base_url is None
        and executor.provider != "openai"
    ):
        model_lane_live = False

    lanes_report = doctor_model_lanes(
        policy,
        require_live=model_lane_live,
        runtime_dir=model_lane_runtime_dir,
    )

    openviking_section: dict[str, object] | None = None
    ov_ok = True
    if include_openviking:
        try:
            import importlib

            ov_mod = importlib.import_module("hermes_helmet.openviking")
            ov_doctor = getattr(ov_mod, "doctor", None)
            if callable(ov_doctor):
                ov_section = ov_doctor(
                    policy_path,
                    client_paths=openviking_client_paths,
                    require_live=openviking_require_live,
                )
                if isinstance(ov_section, dict):
                    raw_ov = ov_section.get("openviking")
                    openviking_section = raw_ov if isinstance(raw_ov, dict) else None
                    ov_ok = bool(ov_section.get("ok", True))
                else:
                    openviking_section = None
                    ov_ok = True
            else:
                raise ImportError("openviking.doctor missing")
        except ImportError:
            # OpenViking module may land on a parallel branch; FAVA doctor stays independent.
            openviking_section = {
                "enabled": bool(policy.integrations.openviking),
                "skipped": not bool(policy.integrations.openviking),
                "ok": not bool(policy.integrations.openviking),
                "message": (
                    "OpenViking module not installed in this tree; "
                    "FAVA doctor continues independently"
                ),
            }
            ov_ok = not bool(policy.integrations.openviking)

    return {
        "ok": fava_report.ok and ov_ok and lanes_report.ok,
        "policy": {
            "ok": True,
            "company": policy.company_slug,
            "integrations_fava_trails": policy.integrations.fava_trails,
            "integrations_openviking": policy.integrations.openviking,
        },
        "fava_trails": fava_report.to_public_dict(),
        "openviking": openviking_section,
        "model_lanes": lanes_report.to_public_dict(),
    }


def setup_messages(template: FavaCompanyTemplate) -> list[str]:
    return [
        f"FAVA Trails license: {FAVA_LICENSE} (optional governed company brain).",
        f"Inspected baseline pin: {FAVA_PIN} ({FAVA_VERSION_HINT}, MCP 2.2).",
        f"Canonical install: {CANONICAL_INSTALL}",
        f"Canonical agent setup: {CANONICAL_AGENT_SETUP}",
        f"Canonical governed recall: {CANONICAL_GOVERNED_RECALL}",
        "OpenViking is optional working context; FAVA is governed truth.",
        "Promotion from OpenViking into FAVA is explicit — never automatic copy.",
        f"Company scope: {template.company_scope}",
        f"Ordinary principals: {', '.join(f'{k}={v}' for k, v in sorted(template.principals.items()))}",
        "Use dedicated MCP processes per principal (FAVA_TRAILS_AGENT_ID).",
        "Never set FAVA_TRAILS_OPERATOR=1 on captain/executor endpoints.",
        "A scope hint is not an authorization boundary.",
        "Bootstrap empty data repos only; clone existing company data instead of overwriting.",
        "Store OPENROUTER_API_KEY in env/owner-only secret files — never in templates or policy.",
        "Run hermes-helmet fava lifecycle-demo for the hermetic governed example.",
        "Helmet starts and operates when FAVA Trails is declined or unavailable.",
    ]


def complementary_systems_note() -> dict[str, str]:
    return {
        "openviking": (
            "Operational working context across clients (search/recall). Not governed truth."
        ),
        "fava_trails": (
            "Governed company brain: draft isolation, Trust Gate, frozen approved content, "
            "supersession, provenance, and approved shared recall."
        ),
        "promotion": (
            "OpenViking → FAVA promotion is an explicit decision that leaves a governed trail."
        ),
    }
