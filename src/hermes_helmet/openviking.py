#!/usr/bin/env python3
"""Optional OpenViking shared company working-context integration.

OpenViking is operational working context (AGPL-3.0), not governed company
truth. Promotion into FAVA Trails remains an explicit operator action.

This module is intentionally portable:

- templates use placeholders (account, user, service URL, peer ids)
- secrets live only in separate owner-only client configuration files
- Codex/ChatGPT provenance uses actor-peer request metadata
- Hermes provenance uses the supported peer/agent surface (OPENVIKING_AGENT /
  actor_peer_id -> X-OpenViking-Actor-Peer and peer-scoped URIs)
- Codex and Claude Code use official OpenViking memory plugins; Helmet does
  not duplicate those MCP adapters
- Shared markers use OpenViking 0.4.19 home-alias URIs (viking://~/...)
- doctor verifies configuration without printing credentials
- Helmet operates when OpenViking is declined or unavailable

Do not claim actor-peer is serialized into stored OpenViking JSONL records
where the installed server does not provide that field. Attribution is
demonstrated through client configuration, request/header flow, and the
supported peer-or-agent view.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Callable, Mapping, MutableMapping, Sequence
import uuid
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from hermes_helmet.authority import AuthorityError, Policy, load_authority

OPENVIKING_LICENSE = "AGPL-3.0"
DEFAULT_SERVICE_URL = "http://127.0.0.1:1933"
DEFAULT_PEERS = ("codex", "chatgpt", "hermes")
DEFAULT_CLIENTS = DEFAULT_PEERS
DEFAULT_SHARED_USER = "company-context"
# OpenViking 0.4.x type-quota recall searches memory-type subtrees
# (events/entities/preferences/experiences), not free-form folders.
SHARED_MEMORY_TYPE = "events"
SHARED_MEMORY_ROOT = f"viking://~/memories/{SHARED_MEMORY_TYPE}"
ACTOR_PEER_HEADER = "X-OpenViking-Actor-Peer"
ACCOUNT_HEADER = "X-OpenViking-Account"
USER_HEADER = "X-OpenViking-User"
API_KEY_HEADER = "X-API-Key"
ROLE_USER = "user"
FORBIDDEN_ROLES = frozenset({"root", "admin", "administrator", "superuser"})
CHATGPT_OPERATOR_CONNECTIVITY = "loopback_or_private_only"
PLACEHOLDER_KEY_MARKERS = frozenset(
    {"", "UNPROVISIONED", "CHANGE_ME", "__OPENVIKING_USER_API_KEY__"}
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
    }
)
_PEER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ACCOUNT_USER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "root_api_key",
        "user_key",
        "token",
        "password",
        "secret",
        "authorization",
        "openai_tunnel_runtime_key",
        "OPENVIKING_USER_API_KEY",
        "OPENVIKING_ROOT_API_KEY",
        "OPENVIKING_API_KEY",
        "OPENAI_TUNNEL_RUNTIME_KEY",
    }
)


class OpenVikingError(RuntimeError):
    """OpenViking configuration or doctor invariant failed (redacted)."""


@dataclass(frozen=True)
class OpenVikingCompanyTemplate:
    """Non-secret company working-context template."""

    service_url: str = DEFAULT_SERVICE_URL
    account: str = "exampleco"
    user: str = "company-context"
    peers: Mapping[str, str] = field(
        default_factory=lambda: {name: name for name in DEFAULT_PEERS}
    )
    license_id: str = OPENVIKING_LICENSE
    required_role: str = ROLE_USER

    def to_public_dict(self) -> dict[str, object]:
        return {
            "service_url": self.service_url,
            "account": self.account,
            "user": self.user,
            "peers": dict(self.peers),
            "license": self.license_id,
            "required_role": self.required_role,
            "note": (
                "OpenViking is optional operational working context (AGPL-3.0), "
                "not governed company truth. Promote deliberately into FAVA Trails."
            ),
        }


@dataclass(frozen=True)
class ClientConfig:
    """One client adapter's owner-only OpenViking configuration."""

    client: str
    service_url: str
    account: str
    user: str
    peer_id: str
    api_key: str
    path: Path | None = None
    role: str = ROLE_USER
    provenance_surface: str = "actor_peer_request_metadata"
    chatgpt_gates: Mapping[str, object] | None = None

    def public_view(self) -> dict[str, object]:
        return {
            "client": self.client,
            "service_url": redact_service_url(self.service_url, self.api_key),
            "account": self.account,
            "user": self.user,
            "peer_id": self.peer_id,
            "role": self.role,
            "provenance_surface": self.provenance_surface,
            "path": str(self.path) if self.path is not None else None,
            "has_api_key": bool(self.api_key),
        }

    def request_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            ACTOR_PEER_HEADER: self.peer_id,
            API_KEY_HEADER: self.api_key,
            "Authorization": f"Bearer {self.api_key}",
        }
        return headers

    def peer_or_agent_view(self, marker_uri: str | None = None) -> dict[str, object]:
        """Supported peer/agent attribution view (not a stored JSONL field claim)."""

        view: dict[str, object] = {
            "originating_client": self.client,
            "logical_peer_id": self.peer_id,
            "request_header": ACTOR_PEER_HEADER,
            "request_header_value": self.peer_id,
            "account": self.account,
            "user": self.user,
            "provenance_surface": self.provenance_surface,
            "stored_jsonl_actor_peer_field": False,
        }
        if self.client == "hermes":
            view["hermes_agent_env"] = "OPENVIKING_AGENT"
            view["hermes_peer_namespace"] = (
                f"viking://~/peers/{self.peer_id}/memories/"
            )
        if marker_uri:
            view["marker_uri"] = marker_uri
            # Hermes peer-scoped URIs encode the peer in the path; others may not.
            if f"/peers/{self.peer_id}/" in marker_uri:
                view["peer_visible_in_uri"] = True
            else:
                view["peer_visible_in_uri"] = False
        return view


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
    findings: tuple[DoctorFinding, ...]
    clients: tuple[dict[str, object], ...] = ()
    verification_mode: str = "offline"

    def to_public_dict(self) -> dict[str, object]:
        return {
            "integration": "openviking",
            "license": OPENVIKING_LICENSE,
            "enabled": self.enabled,
            "skipped": self.skipped,
            "ok": self.ok,
            "verification_mode": self.verification_mode,
            "findings": [item.to_public_dict() for item in self.findings],
            "clients": list(self.clients),
        }


@dataclass(frozen=True)
class MarkerRecord:
    uri: str
    content: str
    account: str
    user: str
    originating_peer: str
    originating_client: str
    kind: str = "working_context"


Transport = Callable[[str, str, Mapping[str, str], object | None], Mapping[str, object]]


def _hex_digit_class(digit: str) -> str:
    """Regex class for one hex digit with letter-case independence."""

    if digit.isdigit():
        return digit
    lower = digit.lower()
    upper = digit.upper()
    if lower == upper:
        return re.escape(digit)
    return f"[{lower}{upper}]"


def _secret_variant_regex(variant: str) -> str:
    """Build a regex matching *variant* with independent percent-escape hex case.

    Literal surrounding text stays case-sensitive. Each ordinary ``%XX`` escape
    matches any hex-letter case combination (``%2B`` / ``%2b`` / mixed) without
    enumerating an exponential set of whole-string spellings.
    """

    parts: list[str] = []
    index = 0
    length = len(variant)
    while index < length:
        if (
            variant[index] == "%"
            and index + 2 < length
            and all(ch in "0123456789abcdefABCDEF" for ch in variant[index + 1 : index + 3])
        ):
            first = variant[index + 1]
            second = variant[index + 2]
            parts.append("%" + _hex_digit_class(first) + _hex_digit_class(second))
            index += 3
            continue
        parts.append(re.escape(variant[index]))
        index += 1
    return "".join(parts)


def secret_text_variants(secret: str) -> tuple[str, ...]:
    """Return plain and ordinary URL-encoded spellings of a known secret.

    Plaintext matching stays case-sensitive. Callers that redact diagnostics
    should match these spellings through :func:`_secret_variant_regex` so each
    percent-escape hex digit is case-independent (``%2B`` vs ``%2b``).
    """

    if not secret or secret in PLACEHOLDER_KEY_MARKERS or len(secret) < 4:
        return ()
    variants: set[str] = {secret}
    variants.add(urlparse.quote(secret, safe=""))
    variants.add(urlparse.quote(secret, safe="/"))
    variants.add(urlparse.quote_plus(secret))
    decoded = urlparse.unquote(secret)
    if decoded:
        variants.add(decoded)
    decoded_plus = urlparse.unquote_plus(secret)
    if decoded_plus:
        variants.add(decoded_plus)
    # Encoded forms of already-decoded material (covers opaque keys with +/=).
    for base in tuple(variants):
        variants.add(urlparse.quote(base, safe=""))
        variants.add(urlparse.quote_plus(base))
    return tuple(
        sorted((item for item in variants if item and len(item) >= 4), key=len, reverse=True)
    )


def _url_secret_corpus(url: str) -> str:
    """Raw URL plus decoded components for known-credential admission checks."""

    pieces = [url, urlparse.unquote(url), urlparse.unquote_plus(url)]
    parsed = urlparse.urlparse(url)
    for part in (
        parsed.path,
        parsed.params,
        parsed.query,
        parsed.fragment,
        parsed.netloc,
        parsed.username or "",
        parsed.password or "",
    ):
        if not part:
            continue
        pieces.append(part)
        pieces.append(urlparse.unquote(part))
        pieces.append(urlparse.unquote_plus(part))
    for name, value in urlparse.parse_qsl(parsed.query, keep_blank_values=True):
        pieces.extend(
            (
                name,
                value,
                urlparse.unquote(name),
                urlparse.unquote(value),
                urlparse.unquote_plus(name),
                urlparse.unquote_plus(value),
            )
        )
    return "\n".join(pieces)


def redact_secrets(text: str, *secrets: str) -> str:
    """Remove known secret values from operator-facing text."""

    redacted = text
    for secret in secrets:
        for variant in secret_text_variants(secret):
            pattern = _secret_variant_regex(variant)
            if not pattern:
                continue
            redacted = re.sub(pattern, "[REDACTED]", redacted)
    # Never leave common secret field dumps intact in free-form text.
    for name in _SECRET_FIELD_NAMES:
        redacted = re.sub(
            rf"({re.escape(name)}\s*[=:]\s*)([^\s,;]+)",
            r"\1[REDACTED]",
            redacted,
            flags=re.IGNORECASE,
        )
    # Strip URL userinfo if any secret-shaped credentials remain embedded.
    redacted = re.sub(r"(://)([^/@\s]+@)", r"\1[REDACTED]@", redacted)
    return redacted


def redact_service_url(service_url: str, *secrets: str) -> str:
    """Return a display-safe service URL (no userinfo, query, or known secrets)."""

    parsed = urlparse.urlparse(service_url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    # Public views never include userinfo, query, or fragment.
    safe = urlparse.urlunparse((parsed.scheme, host, parsed.path, "", "", ""))
    return redact_secrets(safe, *secrets)


def url_contains_secret(url: str, *secrets: str) -> bool:
    """True when any known secret appears in raw or ordinarily decoded URL text."""

    if not url:
        return False
    corpus = _url_secret_corpus(url)
    for secret in secrets:
        for variant in secret_text_variants(secret):
            if variant in url or variant in corpus:
                return True
    return False


def query_looks_secret_bearing(query: str) -> bool:
    """Heuristic rejection for named secret query parameters."""

    if not query:
        return False
    for name, _value in urlparse.parse_qsl(query, keep_blank_values=True):
        folded = name.casefold().replace("-", "_")
        if folded in _NAMED_SECRET_QUERY_TOKENS:
            return True
        if "password" in folded or "secret" in folded or folded.endswith("_token"):
            return True
        if "api" in folded and "key" in folded:
            return True
    return False


def validate_service_url(
    service_url: str,
    *,
    field: str = "service_url",
    known_secrets: Sequence[str] = (),
) -> str:
    """Validate absolute http(s) URL without embedded credentials or known secrets."""

    if not isinstance(service_url, str) or not service_url.strip():
        raise _field_error(field, "must be a non-empty string")
    raw = service_url.strip()
    parsed = urlparse.urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise _field_error(field, "must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise _field_error(field, "must not embed credentials in the URL")
    if query_looks_secret_bearing(parsed.query):
        raise _field_error(field, "must not place secrets on the URL")
    if url_contains_secret(raw, *known_secrets):
        raise _field_error(field, "must not place secrets on the URL")
    return raw.rstrip("/")


def chatgpt_operator_gate_settings() -> dict[str, object]:
    """Explicit ChatGPT operator-gate values written into client configuration."""

    return {
        "operator_gates_required": True,
        "connectivity": CHATGPT_OPERATOR_CONNECTIVITY,
        "intentional_tool_selection": True,
        "transcript_capture": False,
        "auto_scrape": False,
        "official_claude_plugin": "openviking-memory",
        "note": (
            "ChatGPT remains an operator-gated connected-app path with "
            "intentional tools and no transcript capture. Helmet does "
            "not ship a ChatGPT MCP adapter. Claude Code uses the "
            "official OpenViking memory plugin instead of a custom Helmet "
            "client."
        ),
    }


def chatgpt_gates_from_payload(raw: Mapping[str, object]) -> dict[str, object] | None:
    """Extract declared ChatGPT operator gates; missing block is None."""

    block = raw.get("chatgpt")
    if not isinstance(block, Mapping):
        return None
    return {
        "connectivity": block.get("connectivity"),
        "intentional_tool_selection": block.get("intentional_tool_selection"),
        "transcript_capture": block.get("transcript_capture"),
        "auto_scrape": block.get("auto_scrape"),
        "operator_gates_required": block.get("operator_gates_required"),
    }


def chatgpt_operator_gates_finding(config: ClientConfig) -> DoctorFinding:
    """Inspect declared ChatGPT gates; fail closed when missing or unsafe."""

    host = urlparse.urlparse(config.service_url).hostname or ""
    private = is_private_or_loopback_host(host)
    gates = config.chatgpt_gates
    if not isinstance(gates, Mapping):
        return DoctorFinding(
            code="chatgpt-operator-gates",
            ok=False,
            message="ChatGPT operator gates are missing from client configuration",
        )
    if gates.get("operator_gates_required") is not True:
        return DoctorFinding(
            code="chatgpt-operator-gates",
            ok=False,
            message="ChatGPT operator gates require operator_gates_required to be true",
        )
    connectivity = gates.get("connectivity")
    intentional = gates.get("intentional_tool_selection")
    transcript = gates.get("transcript_capture")
    auto_scrape = gates.get("auto_scrape")
    if connectivity != CHATGPT_OPERATOR_CONNECTIVITY or not private:
        return DoctorFinding(
            code="chatgpt-operator-gates",
            ok=False,
            message="ChatGPT operator gates require loopback/private connectivity",
        )
    if intentional is not True:
        return DoctorFinding(
            code="chatgpt-operator-gates",
            ok=False,
            message="ChatGPT operator gates require intentional tools only",
        )
    if transcript is not False or auto_scrape is not False:
        return DoctorFinding(
            code="chatgpt-operator-gates",
            ok=False,
            message="ChatGPT operator gates forbid transcript capture",
        )
    return DoctorFinding(
        code="chatgpt-operator-gates",
        ok=True,
        message=(
            "ChatGPT operator gates verified: loopback/private, "
            "intentional tools, no transcript capture"
        ),
    )


def require_chatgpt_operator_gates(config: ClientConfig) -> None:
    """Fail closed before mutation when ChatGPT operator gates are unsafe."""

    finding = chatgpt_operator_gates_finding(config)
    if not finding.ok:
        raise OpenVikingError(f"openviking shared proof: {finding.message}")


def clients_match_selected_company_template(
    configs: Mapping[str, ClientConfig],
    template: OpenVikingCompanyTemplate,
) -> bool:
    """True when every client file matches the selected template namespace."""

    expected_url = template.service_url.rstrip("/")
    return all(
        cfg.account == template.account
        and cfg.user == template.user
        and cfg.service_url.rstrip("/") == expected_url
        for cfg in configs.values()
    )


def infer_transport_kind(transport: Transport | None) -> str:
    """Classify a transport as fake or http without claiming acceptance."""

    if transport is None:
        return "http"
    service = getattr(transport, "__self__", None)
    if isinstance(service, FakeOpenVikingService):
        return "fake"
    return "http"


def proof_verification_fields(*, transport_kind: str) -> dict[str, object]:
    """Label proof output so synthetic/fake results cannot be read as acceptance."""

    kind = (transport_kind or "unspecified").strip().casefold() or "unspecified"
    if kind == "http":
        return {
            "transport": "http",
            "verification_scope": "live_http_cli_not_official_client_acceptance",
            "acceptance": False,
            "acceptance_note": (
                "Helmet CLI HTTP proof is not official Codex/ChatGPT/Hermes "
                "foreground-app acceptance."
            ),
        }
    return {
        "transport": "fake" if kind == "fake" else kind,
        "verification_scope": "synthetic_non_acceptance",
        "acceptance": False,
        "acceptance_note": (
            "Synthetic/non-acceptance hermetic adapter; not official-client acceptance."
        ),
    }


def selected_company_template(
    policy: Policy,
    *,
    client_paths: Mapping[str, Path] | None = None,
    expected_template: OpenVikingCompanyTemplate | None = None,
) -> OpenVikingCompanyTemplate:
    """Authoritative company template for doctor namespace checks."""

    if expected_template is not None:
        return expected_template
    if client_paths:
        seen: set[Path] = set()
        for path in client_paths.values():
            current = path.expanduser()
            parents = [current.parent, current.parent.parent, current.parent.parent.parent]
            for parent in parents:
                candidate = parent / "company.template.json"
                if candidate in seen:
                    continue
                seen.add(candidate)
                if candidate.is_file():
                    return load_company_template(candidate)
    return company_template_from_policy(policy)


def require_shared_client_provenance(configs: Mapping[str, ClientConfig]) -> None:
    """Enforce shared namespace, one USER key, and distinct peers before mutation."""

    if set(configs) < set(DEFAULT_CLIENTS):
        raise OpenVikingError("openviking marker proof requires codex, chatgpt, hermes")
    accounts = {cfg.account for cfg in configs.values()}
    users = {cfg.user for cfg in configs.values()}
    urls = {cfg.service_url.rstrip("/") for cfg in configs.values()}
    if len(accounts) != 1 or len(users) != 1 or len(urls) != 1:
        raise OpenVikingError(
            "openviking shared proof requires one shared account/user/service URL"
        )
    if len({cfg.api_key for cfg in configs.values()}) != 1:
        raise OpenVikingError(
            "openviking shared proof requires one shared USER key across clients"
        )
    peer_ids = [cfg.peer_id for cfg in configs.values()]
    if any(not peer for peer in peer_ids):
        raise OpenVikingError("openviking shared proof requires non-empty peer ids")
    if len({peer.casefold() for peer in peer_ids}) != len(peer_ids):
        raise OpenVikingError(
            "openviking shared proof requires pairwise-distinct provenance peer ids"
        )


def is_private_or_loopback_host(host: str) -> bool:
    """True when host is loopback or a private IP (not DNS-prefix heuristics)."""

    name = (host or "").strip().casefold().rstrip(".")
    if not name:
        return False
    if name in {"localhost", "localhost.localdomain"}:
        return True
    # Bracketed IPv6 literals from URLs.
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        # DNS names are not certified private from spelling (e.g. 10.attacker.example).
        return False
    return bool(address.is_loopback or address.is_private or address.is_link_local)


def common_user_memory_uri(*, relative_path: str) -> str:
    """Shared company-context memory URI under the ordinary user memory tree.

    OpenViking 0.4.19 rejects uid-less viking://user/memories spellings.
    Shared markers use the home alias viking://~/memories/... so the server
    expands to the authenticated user without inventing a uid in templates.
    Peer-scoped trees stay private to the actor peer.
    """

    rel = relative_path.lstrip("/")
    return f"viking://~/memories/{rel}"


def peer_memory_uri(peer_id: str, *, relative_path: str) -> str:
    rel = relative_path.lstrip("/")
    return f"viking://~/peers/{peer_id}/memories/{rel}"


_UIDLESS_USER_URI = re.compile(
    r"^viking://user/(memories|resources|skills|peers|privacy|sessions)(/|$)"
)


def is_uidless_current_user_uri(uri: str) -> bool:
    """True for the 0.4.19-rejected viking://user/<segment> shorthand."""

    return bool(_UIDLESS_USER_URI.match(uri.strip()))


def _field_error(field: str, message: str) -> OpenVikingError:
    return OpenVikingError(f"openviking {field}: {message}")


def default_company_template(
    *,
    account: str = "exampleco",
    user: str = DEFAULT_SHARED_USER,
    service_url: str = DEFAULT_SERVICE_URL,
    peers: Sequence[str] | Mapping[str, str] | None = None,
) -> OpenVikingCompanyTemplate:
    """Build a templated company namespace without adopter-specific names."""

    if peers is None:
        peer_map = {name: name for name in DEFAULT_PEERS}
    elif isinstance(peers, Mapping):
        peer_map = {str(key): str(value) for key, value in peers.items()}
    else:
        peer_map = {str(name): str(name) for name in peers}
    _validate_company_fields(service_url, account, user, peer_map)
    return OpenVikingCompanyTemplate(
        service_url=service_url.rstrip("/"),
        account=account,
        user=user,
        peers=peer_map,
    )


def company_template_from_policy(
    policy: Policy,
    *,
    account: str | None = None,
    user: str | None = None,
    service_url: str | None = None,
) -> OpenVikingCompanyTemplate:
    """Derive template placeholders from authority policy (non-secret).

    Fixed client types remain ``codex`` / ``chatgpt`` / ``hermes``. Policy
    ``openviking_peers`` supplies the configurable logical peer IDs for those
    clients (by matching known client names first, then assigning remaining
    custom identifiers in order). Adopter account/user/service choices may be
    supplied explicitly so setup/write-client do not force defaults.
    """

    resolved_account = account or policy.company_slug or "exampleco"
    resolved_user = user or DEFAULT_SHARED_USER
    resolved_url = service_url or DEFAULT_SERVICE_URL
    peer_map = _peer_map_from_policy(policy)
    return default_company_template(
        account=resolved_account,
        user=resolved_user,
        service_url=resolved_url,
        peers=peer_map,
    )


def load_company_template(path: Path) -> OpenVikingCompanyTemplate:
    """Load a previously emitted non-secret company template JSON."""

    path = path.expanduser()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _field_error(str(path), "is not valid JSON") from exc
    except OSError as exc:
        raise _field_error(str(path), "could not be read") from exc
    if not isinstance(raw, Mapping):
        raise _field_error(str(path), "must be a JSON object")
    peers_raw = raw.get("peers")
    if not isinstance(peers_raw, Mapping):
        raise _field_error(str(path), "peers must be an object")
    return default_company_template(
        account=str(raw.get("account") or "").strip() or "exampleco",
        user=str(raw.get("user") or "").strip() or DEFAULT_SHARED_USER,
        service_url=str(raw.get("service_url") or raw.get("url") or DEFAULT_SERVICE_URL),
        peers={str(key): str(value) for key, value in peers_raw.items()},
    )


def resolve_company_template(
    policy: Policy,
    *,
    company_template_path: Path | None = None,
    account: str | None = None,
    user: str | None = None,
    service_url: str | None = None,
) -> OpenVikingCompanyTemplate:
    """Resolve adopter company template: explicit file, then CLI overrides, then policy."""

    if company_template_path is not None:
        template = load_company_template(company_template_path)
        peer_map = dict(template.peers)
        return default_company_template(
            account=account or template.account,
            user=user or template.user,
            service_url=service_url or template.service_url,
            peers=peer_map,
        )
    return company_template_from_policy(
        policy,
        account=account,
        user=user,
        service_url=service_url,
    )


def _peer_map_from_policy(policy: Policy) -> dict[str, str]:
    """Map fixed client types to logical peer IDs without renaming clients.

    Reserve exact fixed-client name matches first, then assign remaining custom
    identifiers in policy order. This prevents ``chatgpt, desktop-codex, hermes``
    from assigning ``chatgpt`` to unmatched ``codex`` and then duplicating it.
    """

    defaults = {name: name for name in DEFAULT_PEERS}
    configured = list(policy.openviking_peers)
    if not configured:
        return defaults

    by_id = {peer.peer_id.casefold(): peer.peer_id for peer in configured}
    mapped: dict[str, str] = {}
    claimed: set[str] = set()

    for client in DEFAULT_PEERS:
        if client.casefold() in by_id:
            peer_id = by_id[client.casefold()]
            mapped[client] = peer_id
            claimed.add(peer_id.casefold())

    remaining = [
        peer.peer_id for peer in configured if peer.peer_id.casefold() not in claimed
    ]
    for client in DEFAULT_PEERS:
        if client in mapped:
            continue
        if remaining:
            mapped[client] = remaining.pop(0)
        else:
            mapped[client] = client

    # Any leftover peer ids cannot become new client types; ignore extras.
    return mapped


def _validate_company_fields(
    service_url: str,
    account: str,
    user: str,
    peers: Mapping[str, str],
) -> None:
    validate_service_url(service_url, field="service_url")
    if not _ACCOUNT_USER_RE.fullmatch(account):
        raise _field_error("account", "must be a stable non-empty identifier")
    if not _ACCOUNT_USER_RE.fullmatch(user):
        raise _field_error("user", "must be a stable non-empty identifier")
    if not peers:
        raise _field_error("peers", "must include at least one logical client")
    missing_clients = [name for name in DEFAULT_PEERS if name not in peers]
    if missing_clients:
        raise _field_error(
            "peers",
            "must include fixed client types codex, chatgpt, and hermes",
        )
    seen: set[str] = set()
    for client, peer_id in peers.items():
        if not isinstance(client, str) or not client.strip():
            raise _field_error("peers", "client names must be non-empty strings")
        if client not in DEFAULT_PEERS:
            raise _field_error(
                "peers",
                "client keys must be fixed types codex, chatgpt, hermes",
            )
        if not isinstance(peer_id, str) or not _PEER_ID_RE.fullmatch(peer_id):
            raise _field_error(f"peers.{client}", "peer id is invalid")
        folded = peer_id.casefold()
        if folded in seen:
            raise _field_error("peers", "peer ids must be pairwise distinct")
        seen.add(folded)


def render_company_template(template: OpenVikingCompanyTemplate) -> str:
    return json.dumps(template.to_public_dict(), indent=2, sort_keys=True) + "\n"


def example_client_config_payload(
    template: OpenVikingCompanyTemplate,
    client: str,
    *,
    api_key_placeholder: str = "__OPENVIKING_USER_API_KEY__",
) -> dict[str, object]:
    if client not in template.peers:
        raise _field_error("client", "is not a templated logical client")
    peer_id = template.peers[client]
    payload: dict[str, object] = {
        "url": template.service_url,
        "api_key": api_key_placeholder,
        "account": template.account,
        "user": template.user,
        "actor_peer_id": peer_id,
        "required_role": ROLE_USER,
        "client": client,
        "license": OPENVIKING_LICENSE,
    }
    if client == "hermes":
        payload["hermes"] = {
            "memory_provider": "openviking",
            "OPENVIKING_ENDPOINT": template.service_url,
            "OPENVIKING_AGENT": peer_id,
            "OPENVIKING_API_KEY": api_key_placeholder,
            "note": (
                "Hermes derives peer provenance from OPENVIKING_AGENT / "
                "actor_peer_id and sends X-OpenViking-Actor-Peer. Peer-scoped "
                "memory URIs live under viking://~/peers/<agent>/memories/."
            ),
        }
    if client == "codex":
        payload["official_plugin"] = {
            "name": "openviking-memory",
            "harness": "codex",
            "peer_header": ACTOR_PEER_HEADER,
            "note": (
                "Install the official OpenViking Codex memory plugin against "
                "this ovcli.conf. Helmet does not ship a duplicate Codex MCP "
                "adapter. Shared company markers use Helmet CLI write-marker "
                "or shared-proof, not plugin auto-capture."
            ),
        }
    if client == "chatgpt":
        payload["chatgpt"] = chatgpt_operator_gate_settings()
    return payload


def render_client_config_template(
    template: OpenVikingCompanyTemplate, client: str
) -> str:
    return (
        json.dumps(
            example_client_config_payload(template, client),
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def default_client_config_paths(root: Path) -> dict[str, Path]:
    """Default owner-only paths under a private support root."""

    base = root.expanduser()
    return {
        "codex": base / "clients" / "codex" / "ovcli.conf",
        "chatgpt": base / "clients" / "chatgpt" / "ovcli.conf",
        "hermes": base / "clients" / "hermes" / "ovcli.conf",
    }


def _require_owner_only_file(path: Path) -> None:
    if path.is_symlink():
        raise _field_error(str(path), "must be a regular file, not a symlink")
    if not path.is_file():
        raise _field_error(str(path), "is missing")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise _field_error(str(path), "must be owner-only (mode 0600)")


def read_owner_only_secret_file(path: Path) -> str:
    """Read a secret file only after confirming owner-only mode. Never logs content."""

    path = path.expanduser()
    _require_owner_only_file(path)
    return path.read_text(encoding="utf-8").strip()


def write_owner_only_json(path: Path, payload: Mapping[str, object]) -> None:
    """Atomically write JSON with mode 0600. Never logs payload contents."""

    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    text = json.dumps(dict(payload), indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def write_client_config(
    path: Path,
    *,
    client: str,
    template: OpenVikingCompanyTemplate,
    api_key: str,
) -> ClientConfig:
    """Write one client owner-only config from the shared company template."""

    if client not in template.peers:
        raise _field_error("client", "is not configured in the company template")
    if not isinstance(api_key, str) or api_key.strip() in PLACEHOLDER_KEY_MARKERS:
        raise _field_error("api_key", "must be a non-empty user-scoped credential")
    if any(character.isspace() for character in api_key):
        raise _field_error("api_key", "must not contain whitespace")
    if len(api_key.strip()) < 20:
        raise _field_error("api_key", "is too short to be a user-scoped key")
    peer_id = template.peers[client]
    payload: dict[str, object] = {
        "url": template.service_url,
        "api_key": api_key.strip(),
        "account": template.account,
        "user": template.user,
        "actor_peer_id": peer_id,
        "client": client,
        "required_role": ROLE_USER,
    }
    if client == "hermes":
        payload["OPENVIKING_AGENT"] = peer_id
        payload["OPENVIKING_ENDPOINT"] = template.service_url
        payload["OPENVIKING_API_KEY"] = api_key.strip()
        payload["hermes"] = {
            "memory_provider": "openviking",
            "OPENVIKING_ENDPOINT": template.service_url,
            "OPENVIKING_AGENT": peer_id,
            "OPENVIKING_API_KEY": api_key.strip(),
        }
    if client == "chatgpt":
        payload["chatgpt"] = chatgpt_operator_gate_settings()
    write_owner_only_json(path, payload)
    return ClientConfig(
        client=client,
        service_url=template.service_url,
        account=template.account,
        user=template.user,
        peer_id=peer_id,
        api_key=api_key.strip(),
        path=path.expanduser(),
        role=ROLE_USER,
        provenance_surface=_provenance_surface_for(client),
        chatgpt_gates=chatgpt_gates_from_payload(payload) if client == "chatgpt" else None,
    )


def _provenance_surface_for(client: str) -> str:
    if client == "hermes":
        return "hermes_peer_agent_surface"
    if client in {"codex", "chatgpt"}:
        return "actor_peer_request_metadata"
    return "actor_peer_request_metadata"


def load_client_config(path: Path, *, expected_client: str | None = None) -> ClientConfig:
    """Load and validate one owner-only client configuration."""

    path = path.expanduser()
    _require_owner_only_file(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _field_error(str(path), "is not valid JSON") from exc
    if not isinstance(raw, Mapping):
        raise _field_error(str(path), "must be a JSON object")

    client = str(raw.get("client") or expected_client or "").strip()
    if not client:
        raise _field_error(str(path), "client identity is missing")
    if expected_client and client != expected_client:
        raise _field_error(str(path), "mismatched client identity")
    if client not in DEFAULT_CLIENTS:
        raise _field_error(str(path), "client must be codex, chatgpt, or hermes")

    service_url = str(raw.get("url") or raw.get("service_url") or "").strip()
    account = str(raw.get("account") or "").strip()
    user = str(raw.get("user") or "").strip()
    peer_id = str(
        raw.get("actor_peer_id")
        or raw.get("OPENVIKING_AGENT")
        or raw.get("agent")
        or raw.get("peer_id")
        or ""
    ).strip()
    api_key = str(raw.get("api_key") or raw.get("OPENVIKING_API_KEY") or "").strip()
    role = str(raw.get("role") or raw.get("required_role") or ROLE_USER).strip().casefold()

    if not service_url:
        raise _field_error(f"{client}.url", "must be non-empty")
    if not account:
        raise _field_error(f"{client}.account", "must be non-empty")
    if not user:
        raise _field_error(f"{client}.user", "must be non-empty")
    if not peer_id or not _PEER_ID_RE.fullmatch(peer_id):
        raise _field_error(f"{client}.actor_peer_id", "must be a non-empty peer id")
    if not api_key or api_key in PLACEHOLDER_KEY_MARKERS:
        raise _field_error(f"{client}.api_key", "must be a non-empty user key")
    if any(character.isspace() for character in api_key):
        raise _field_error(f"{client}.api_key", "must not contain whitespace")
    service_url = validate_service_url(
        service_url,
        field=f"{client}.url",
        known_secrets=(api_key,),
    )
    if role in FORBIDDEN_ROLES or role != ROLE_USER:
        raise _field_error(f"{client}.role", "must be USER (never root/admin)")
    # Reject configs that still carry a root key field with a real-looking value.
    root_key = raw.get("root_api_key")
    if isinstance(root_key, str) and root_key.strip() and root_key.strip() not in PLACEHOLDER_KEY_MARKERS:
        raise _field_error(f"{client}.root_api_key", "must not be present in client config")

    # Hermes effective-settings consistency (synthetic config check, not live Hermes).
    if client == "hermes":
        hermes_block = raw.get("hermes")
        hermes_map = hermes_block if isinstance(hermes_block, Mapping) else {}
        agent_values = []
        for key in ("OPENVIKING_AGENT", "actor_peer_id", "agent"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                agent_values.append(value.strip())
        nested_agent = hermes_map.get("OPENVIKING_AGENT") if hermes_map else None
        if isinstance(nested_agent, str) and nested_agent.strip():
            agent_values.append(nested_agent.strip())
        unique_agents = {value.casefold() for value in agent_values}
        if len(unique_agents) > 1:
            raise _field_error(
                f"{client}.OPENVIKING_AGENT",
                "must match actor_peer_id (contradictory Hermes peer settings)",
            )
        endpoint_values = []
        for key in ("OPENVIKING_ENDPOINT", "url", "service_url"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                endpoint_values.append(
                    validate_service_url(
                        value,
                        field=f"{client}.{key}",
                        known_secrets=(api_key,),
                    )
                )
        nested_endpoint = hermes_map.get("OPENVIKING_ENDPOINT") if hermes_map else None
        if isinstance(nested_endpoint, str) and nested_endpoint.strip():
            endpoint_values.append(
                validate_service_url(
                    nested_endpoint,
                    field=f"{client}.hermes.OPENVIKING_ENDPOINT",
                    known_secrets=(api_key,),
                )
            )
        unique_endpoints = {value.rstrip("/").casefold() for value in endpoint_values}
        if len(unique_endpoints) > 1:
            raise _field_error(
                f"{client}.OPENVIKING_ENDPOINT",
                "must match url (contradictory Hermes endpoint aliases)",
            )
        # Credential aliases must agree with the primary api_key without disclosing values.
        credential_values = [api_key]
        for key in ("OPENVIKING_API_KEY", "api_key"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip() and value.strip() not in PLACEHOLDER_KEY_MARKERS:
                credential_values.append(value.strip())
        nested_key = hermes_map.get("OPENVIKING_API_KEY") if hermes_map else None
        if (
            isinstance(nested_key, str)
            and nested_key.strip()
            and nested_key.strip() not in PLACEHOLDER_KEY_MARKERS
        ):
            credential_values.append(nested_key.strip())
        if len({value for value in credential_values}) > 1:
            raise _field_error(
                f"{client}.OPENVIKING_API_KEY",
                "must match api_key (contradictory Hermes credential aliases)",
            )

    return ClientConfig(
        client=client,
        service_url=service_url,
        account=account,
        user=user,
        peer_id=peer_id,
        api_key=api_key,
        path=path,
        role=ROLE_USER,
        provenance_surface=_provenance_surface_for(client),
        chatgpt_gates=chatgpt_gates_from_payload(raw) if client == "chatgpt" else None,
    )


def load_client_configs(
    paths: Mapping[str, Path],
) -> dict[str, ClientConfig]:
    configs: dict[str, ClientConfig] = {}
    for client, path in paths.items():
        configs[client] = load_client_config(path, expected_client=client)
    return configs


def _identity_from_health(payload: Mapping[str, object]) -> tuple[str, str, str]:
    account = str(payload.get("account_id") or payload.get("account") or "").strip()
    user = str(payload.get("user_id") or payload.get("user") or "").strip()
    role = str(payload.get("role") or "").strip().casefold()
    return account, user, role


def http_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload: object | None = None,
    *,
    timeout: float = 10.0,
    known_secrets: Sequence[str] = (),
) -> Mapping[str, object]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = dict(headers)
    if body is not None:
        request_headers.setdefault("Content-Type", "application/json")
    # Never put credentials on the URL query string or userinfo.
    parsed = urlparse.urlparse(url)
    if parsed.username is not None or parsed.password is not None:
        raise OpenVikingError("openviking request must not embed credentials in the URL")
    secrets = tuple(
        secret
        for secret in (
            *known_secrets,
            request_headers.get(API_KEY_HEADER, ""),
            _bearer_token(request_headers.get("Authorization", "")),
        )
        if secret
    )
    if query_looks_secret_bearing(parsed.query) or url_contains_secret(url, *secrets):
        raise OpenVikingError("openviking request must not place secrets on the URL")
    req = urlrequest.Request(url, data=body, headers=request_headers, method=method)
    opener = urlrequest.build_opener(_OriginBoundRedirectHandler())
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read()
    except urlerror.HTTPError as exc:
        raw = exc.read()
        detail = "request failed"
        try:
            parsed_error = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(parsed_error, Mapping):
                detail = str(parsed_error.get("detail") or detail)
            elif isinstance(parsed_error, str):
                detail = parsed_error
        except (json.JSONDecodeError, UnicodeDecodeError):
            detail = "request failed"
        raise OpenVikingError(
            redact_secrets(f"openviking HTTP {exc.code}: {detail}", *secrets)
        ) from None
    except OpenVikingError:
        raise
    except OSError as exc:
        raise OpenVikingError("openviking service unavailable") from exc
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OpenVikingError("openviking returned non-JSON response") from exc
    if not isinstance(data, Mapping):
        raise OpenVikingError("openviking returned a non-object JSON payload")
    return data


def _bearer_token(authorization: str) -> str:
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def _origin_key(url: str) -> tuple[str, str, int | None]:
    parsed = urlparse.urlparse(url)
    scheme = (parsed.scheme or "").casefold()
    host = (parsed.hostname or "").casefold()
    port = parsed.port
    if port is None:
        if scheme == "http":
            port = 80
        elif scheme == "https":
            port = 443
    return scheme, host, port


class _OriginBoundRedirectHandler(urlrequest.HTTPRedirectHandler):
    """Refuse credential-bearing cross-origin redirects (stdlib forwards headers)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        old_origin = _origin_key(req.full_url)
        new_origin = _origin_key(newurl)
        has_credentials = any(
            header.casefold() in {"authorization", "x-api-key", API_KEY_HEADER.casefold()}
            for header in req.headers
        ) or bool(req.has_header("Authorization") or req.has_header(API_KEY_HEADER))
        if has_credentials and old_origin != new_origin:
            raise OpenVikingError(
                "openviking refused cross-origin redirect while credentials were present"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class FakeOpenVikingService:
    """In-memory OpenViking stand-in for hermetic adapter tests and demos.

    This is a synthetic/non-acceptance facility. It does not replace required
    real official-client acceptance. Captures request headers (including
    actor-peer) without claiming that the stored record JSONL contains an
    actor-peer field.
    """

    def __init__(self, *, index_delay_s: float = 0.0) -> None:
        self.records: dict[tuple[str, str, str], MarkerRecord] = {}
        self.keys: dict[str, dict[str, object]] = {}
        self.last_headers: dict[str, Mapping[str, str]] = {}
        self.last_write_headers: dict[str, Mapping[str, str]] = {}
        self._uri_seq = 0
        # When >0, writes are stored immediately but search/recall wait until
        # the delay elapses (models queued indexing without inventing APIs).
        self.index_delay_s = float(index_delay_s)
        self._indexed_at: dict[tuple[str, str, str], float] = {}

    def register_user_key(
        self,
        api_key: str,
        *,
        account: str,
        user: str,
        role: str = ROLE_USER,
    ) -> None:
        self.keys[api_key] = {
            "account_id": account,
            "user_id": user,
            "role": role.casefold(),
        }

    def snapshot(self) -> dict[str, object]:
        return {
            "records": [
                {
                    "uri": record.uri,
                    "content": record.content,
                    "account": record.account,
                    "user": record.user,
                    "originating_peer": record.originating_peer,
                    "originating_client": record.originating_client,
                    "kind": record.kind,
                }
                for record in self.records.values()
            ],
            "keys": dict(self.keys),
            "uri_seq": self._uri_seq,
        }

    def restore(self, snapshot: Mapping[str, object]) -> None:
        self.records.clear()
        self._indexed_at.clear()
        raw_keys = snapshot.get("keys")
        restored_keys: dict[str, dict[str, object]] = {}
        if isinstance(raw_keys, Mapping):
            for key, value in raw_keys.items():
                if isinstance(value, Mapping):
                    restored_keys[str(key)] = {
                        str(inner_key): inner_value
                        for inner_key, inner_value in value.items()
                    }
        self.keys = restored_keys
        raw_seq = snapshot.get("uri_seq")
        self._uri_seq = int(raw_seq) if isinstance(raw_seq, int) else 0
        raw_records = snapshot.get("records")
        if not isinstance(raw_records, list):
            return
        now = time.monotonic()
        for item in raw_records:
            if not isinstance(item, Mapping):
                continue
            record = MarkerRecord(
                uri=str(item["uri"]),
                content=str(item["content"]),
                account=str(item["account"]),
                user=str(item["user"]),
                originating_peer=str(item["originating_peer"]),
                originating_client=str(item["originating_client"]),
                kind=str(item.get("kind") or "working_context"),
            )
            key = (record.account, record.user, record.uri)
            self.records[key] = record
            self._indexed_at[key] = now

    def transport(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: object | None = None,
    ) -> Mapping[str, object]:
        api_key = headers.get(API_KEY_HEADER) or ""
        auth = headers.get("Authorization") or ""
        if not api_key and auth.lower().startswith("bearer "):
            api_key = auth[7:].strip()
        identity = self.keys.get(api_key)
        if identity is None:
            # Echoing the key would be a real server footgun; keep detail generic.
            raise OpenVikingError(
                redact_secrets(f"openviking HTTP 401: unauthorized key={api_key}", api_key)
            )
        account = str(identity["account_id"])
        user = str(identity["user_id"])
        role = str(identity["role"])
        peer = headers.get(ACTOR_PEER_HEADER) or ""
        client = headers.get("X-Hermes-Helmet-Client") or peer or "unknown"
        self.last_headers[client] = dict(headers)
        self.last_write_headers[client] = dict(headers)

        parsed = urlparse.urlparse(url)
        path = parsed.path.rstrip("/") or "/"

        if method == "GET" and path.endswith("/health"):
            return {
                "status": "ok",
                "result": {
                    "account_id": account,
                    "user_id": user,
                    "role": role,
                    "status": "ok",
                },
                # Some deployments also flatten identity at the top level.
                "account_id": account,
                "user_id": user,
                "role": role,
            }

        if method == "POST" and path.endswith("/api/v1/content/write"):
            if not isinstance(payload, Mapping):
                raise OpenVikingError("openviking HTTP 400: invalid payload")
            uri = str(payload.get("uri") or "").strip()
            content = str(payload.get("content") or "")
            mode = str(payload.get("mode") or "replace").strip().casefold()
            if not uri:
                raise OpenVikingError("openviking HTTP 400: uri is required")
            if is_uidless_current_user_uri(uri):
                raise OpenVikingError(
                    "openviking HTTP 400: uid-less viking://user/<segment> "
                    "URIs are rejected; use viking://~/... or viking://user/{user_id}/..."
                )
            if mode not in {"create", "replace", "append"}:
                raise OpenVikingError("openviking HTTP 400: unsupported write mode")
            if not self._actor_may_access(uri, peer=peer):
                raise OpenVikingError(
                    "openviking HTTP 403: actor peer cannot access another peer's context"
                )
            key = (account, user, uri)
            if mode == "create" and key in self.records:
                raise OpenVikingError("openviking HTTP 409: uri already exists")
            existing = self.records.get(key)
            if mode == "append" and existing is not None:
                content = existing.content + content
            record = MarkerRecord(
                uri=uri,
                content=content,
                account=account,
                user=user,
                originating_peer=peer,
                originating_client=str(client),
            )
            self.records[key] = record
            self._indexed_at[key] = time.monotonic() + max(0.0, self.index_delay_s)
            # Intentionally omit actor_peer from the stored record body.
            return {
                "status": "ok",
                "result": {
                    "uri": uri,
                    "written_bytes": len(content.encode("utf-8")),
                    "stored_fields": ["uri", "content"],
                },
            }

        if method == "POST" and path.endswith("/api/v1/search/search"):
            return self._search_response(
                account=account,
                user=user,
                peer=peer,
                payload=payload,
                include_content=False,
            )

        if method == "POST" and path.endswith("/api/v1/search/find"):
            return self._search_response(
                account=account,
                user=user,
                peer=peer,
                payload=payload,
                include_content=False,
            )

        if method == "POST" and path.endswith("/api/v1/search/recall"):
            return self._search_response(
                account=account,
                user=user,
                peer=peer,
                payload=payload,
                include_content=True,
            )

        if method == "GET" and path.endswith("/api/v1/content/read"):
            uri = urlparse.parse_qs(parsed.query).get("uri", [""])[0]
            if not uri:
                raise OpenVikingError("openviking HTTP 400: uri is required")
            if not self._actor_may_access(uri, peer=peer):
                raise OpenVikingError(
                    "openviking HTTP 403: actor peer cannot access another peer's context"
                )
            record = self.records.get((account, user, uri))
            if record is None:
                raise OpenVikingError("openviking HTTP 404: not found")
            # Supported content-read returns the content string in result.
            return {"status": "ok", "result": record.content}

        raise OpenVikingError(f"openviking HTTP 404: unknown path {path}")

    def _actor_may_access(self, uri: str, *, peer: str) -> bool:
        """Mirror OpenViking 0.4.x actor-peer view: other peer trees are hidden."""

        marker = "/peers/"
        if marker not in uri:
            return True
        if not peer:
            # Without an actor peer, peer trees remain accessible for the user.
            return True
        try:
            after = uri.split(marker, 1)[1]
            target_peer = after.split("/", 1)[0]
        except (IndexError, ValueError):
            return False
        return target_peer.casefold() == peer.casefold()

    def _search_response(
        self,
        *,
        account: str,
        user: str,
        peer: str,
        payload: object | None,
        include_content: bool,
    ) -> Mapping[str, object]:
        if not isinstance(payload, Mapping):
            raise OpenVikingError("openviking HTTP 400: invalid payload")
        query = str(payload.get("query") or "")
        target_uri = payload.get("target_uri") or ""
        targets: list[str]
        if isinstance(target_uri, list):
            targets = [str(item) for item in target_uri if str(item).strip()]
        elif isinstance(target_uri, str) and target_uri.strip():
            targets = [target_uri.strip()]
        else:
            targets = []

        hits: list[dict[str, object]] = []
        now = time.monotonic()
        for key, record in self.records.items():
            if key[0] != account or key[1] != user:
                continue
            indexed_at = self._indexed_at.get(key, 0.0)
            if indexed_at > now:
                # Queued indexing: stored but not yet searchable/recallable.
                continue
            if query and query not in record.content:
                continue
            if not self._actor_may_access(record.uri, peer=peer):
                continue
            if targets:
                allowed = False
                for target in targets:
                    normalized = target.rstrip("/")
                    if record.uri == target or record.uri.startswith(normalized + "/"):
                        allowed = True
                        break
                if not allowed:
                    continue
            hit: dict[str, object] = {
                "uri": record.uri,
                "snippet": record.content,
                "abstract": record.content,
                "score": 1.0,
                # Provenance is request/config view only — not a stored JSONL field.
            }
            if include_content:
                hit["content"] = record.content
            hits.append(hit)
        if include_content:
            # OpenViking 0.4.8 recall returns RecallResult shape.
            rendered_parts = [
                str(item.get("content") or item.get("snippet") or "") for item in hits
            ]
            return {
                "status": "ok",
                "result": {
                    "entries": hits,
                    "rendered": "\n".join(part for part in rendered_parts if part),
                    "stats": {"count": len(hits)},
                },
            }
        # OpenViking 0.4.8 search/find returns FindResult shape.
        return {
            "status": "ok",
            "result": {
                "memories": hits,
                "resources": [],
                "skills": [],
                "total": len(hits),
            },
        }


def probe_client_identity(
    config: ClientConfig,
    *,
    transport: Transport | None = None,
    expected_account: str | None = None,
    expected_user: str | None = None,
) -> tuple[str, str, str]:
    """Return (account, user, role) from /health using the client key."""

    transport = transport or (
        lambda method, url, headers, payload=None: http_transport(
            method, url, headers, payload, known_secrets=(config.api_key,)
        )
    )
    headers = config.request_headers()
    headers["X-Hermes-Helmet-Client"] = config.client
    payload = transport(
        "GET",
        f"{config.service_url.rstrip('/')}/health",
        headers,
        None,
    )
    account, user, role = _identity_from_health(payload)
    nested = payload.get("result")
    if (not account or not user or not role) and isinstance(nested, Mapping):
        account, user, role = _identity_from_health(nested)
    if not account or not user:
        raise OpenVikingError(
            redact_secrets(
                f"openviking {config.client}: health did not return account/user",
                config.api_key,
            )
        )
    if role in FORBIDDEN_ROLES or role != ROLE_USER:
        raise OpenVikingError(
            redact_secrets(
                f"openviking {config.client}: credential role must be USER",
                config.api_key,
            )
        )
    wanted_account = expected_account or config.account
    wanted_user = expected_user or config.user
    if account != wanted_account or user != wanted_user:
        raise OpenVikingError(
            redact_secrets(
                f"openviking {config.client}: wrong-account or wrong-user for configured namespace",
                config.api_key,
            )
        )
    return account, user, role


def write_synthetic_marker(
    config: ClientConfig,
    marker_text: str,
    *,
    transport: Transport | None = None,
    shared: bool = True,
    run_id: str | None = None,
    mode: str = "create",
) -> tuple[str, dict[str, object]]:
    """Write a synthetic working-context marker; return URI and peer view.

    Shared company markers write into the common user memory tree with an
    explicit URI (OpenViking 0.4.x ``POST /api/v1/content/write`` requires
    ``uri`` + ``mode``). Peer-scoped URIs remain available for Hermes private
    remember but are not the shared cross-client path.

    Each invocation embeds a unique run token in the URI so retries do not
    collide with prior ``mode=create`` writes. When create still reports an
    existing URI, the helper falls back once to ``replace`` for that same URI.
    """

    transport = transport or (
        lambda method, url, headers, payload=None: http_transport(
            method, url, headers, payload, known_secrets=(config.api_key,)
        )
    )
    headers = config.request_headers()
    headers["X-Hermes-Helmet-Client"] = config.client
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", marker_text)[:48].strip("-") or "marker"
    token = (run_id or uuid.uuid4().hex).strip() or uuid.uuid4().hex
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", token)[:32].strip("-") or uuid.uuid4().hex[:12]
    if shared:
        uri = common_user_memory_uri(
            relative_path=(
                f"{SHARED_MEMORY_TYPE}/helmet-markers/"
                f"{config.client}-{config.peer_id}-{slug}-{token}.md"
            )
        )
    else:
        uri = peer_memory_uri(
            config.peer_id,
            relative_path=f"{SHARED_MEMORY_TYPE}/helmet-markers/{slug}-{token}.md",
        )
    write_mode = (mode or "create").strip().casefold() or "create"
    payload = {"uri": uri, "content": marker_text, "mode": write_mode}
    try:
        response = transport(
            "POST",
            f"{config.service_url.rstrip('/')}/api/v1/content/write",
            headers,
            payload,
        )
    except OpenVikingError as exc:
        # Safe retry path: unique URI should avoid this; if create still collides,
        # replace the same intentional URI once (no delete, no broad storage mgmt).
        detail = str(exc).casefold()
        if write_mode == "create" and ("409" in detail or "already exists" in detail):
            payload = {"uri": uri, "content": marker_text, "mode": "replace"}
            response = transport(
                "POST",
                f"{config.service_url.rstrip('/')}/api/v1/content/write",
                headers,
                payload,
            )
        else:
            raise
    raw_result = response.get("result")
    result: Mapping[str, object] = raw_result if isinstance(raw_result, Mapping) else {}
    returned_uri = str(result.get("uri") or uri)
    if not returned_uri:
        raise OpenVikingError(
            redact_secrets(
                f"openviking {config.client}: write returned no URI",
                config.api_key,
            )
        )
    # Guard: do not treat stored JSONL as carrying actor_peer unless present.
    stored_fields = result.get("stored_fields")
    if isinstance(stored_fields, list) and "actor_peer" in stored_fields:
        # Real servers may one day store it; still attribute via request view.
        pass
    view = config.peer_or_agent_view(returned_uri)
    view["write_uri_namespace"] = "common_user_memory" if shared else "peer_memory"
    view["write_run_id"] = token
    return returned_uri, view


def wait_for_marker_retrieval(
    config: ClientConfig,
    marker_text: str,
    *,
    transport: Transport,
    timeout_s: float = 5.0,
    interval_s: float = 0.05,
) -> bool:
    """Bounded poll until search sees the marker (truthful queued-index wait)."""

    deadline = time.monotonic() + max(0.0, timeout_s)
    interval = max(0.01, interval_s)
    while True:
        found = search_markers(config, marker_text, transport=transport)
        if any(marker_text in json.dumps(item) for item in found):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _normalize_hit_mapping(item: object) -> dict[str, object] | None:
    if not isinstance(item, Mapping):
        return None
    hit = {str(key): value for key, value in item.items()}
    if "content" not in hit:
        for key in ("snippet", "abstract", "overview", "summary", "rendered"):
            value = hit.get(key)
            if isinstance(value, str) and value:
                hit["content"] = value
                break
    return hit


def _decode_search_result(result: object) -> list[dict[str, object]]:
    """Decode OpenViking 0.4.8 FindResult or legacy list envelopes into hit dicts."""

    items: list[object] = []
    if isinstance(result, list):
        items = list(result)
    elif isinstance(result, Mapping):
        collected = False
        for key in ("memories", "resources", "skills", "entries", "results", "items"):
            value = result.get(key)
            if isinstance(value, list):
                items.extend(value)
                collected = True
        if not collected and result.get("uri"):
            items = [result]
    hits: list[dict[str, object]] = []
    for item in items:
        normalized = _normalize_hit_mapping(item)
        if normalized is not None:
            hits.append(normalized)
    return hits


def _decode_recall_result(result: object) -> list[dict[str, object]]:
    """Decode OpenViking 0.4.8 RecallResult or legacy list envelopes into hit dicts."""

    if isinstance(result, list):
        return _decode_search_result(result)
    if not isinstance(result, Mapping):
        return []
    entries = result.get("entries")
    hits = _decode_search_result(entries if entries is not None else [])
    if hits:
        return hits
    # Fall back to other list-bearing keys, then treat rendered text as a soft hit.
    fallback = _decode_search_result(result)
    if fallback:
        return fallback
    rendered = result.get("rendered")
    if isinstance(rendered, str) and rendered.strip():
        return [{"content": rendered, "rendered": rendered}]
    return []


def search_markers(
    config: ClientConfig,
    query: str,
    *,
    transport: Transport | None = None,
    target_uri: str = SHARED_MEMORY_ROOT,
) -> list[dict[str, object]]:
    transport = transport or (
        lambda method, url, headers, payload=None: http_transport(
            method, url, headers, payload, known_secrets=(config.api_key,)
        )
    )
    headers = config.request_headers()
    headers["X-Hermes-Helmet-Client"] = config.client
    response = transport(
        "POST",
        f"{config.service_url.rstrip('/')}/api/v1/search/search",
        headers,
        {"query": query, "target_uri": target_uri, "limit": 20},
    )
    return _decode_search_result(response.get("result"))


def recall_markers(
    config: ClientConfig,
    query: str,
    *,
    transport: Transport | None = None,
) -> list[dict[str, object]]:
    transport = transport or (
        lambda method, url, headers, payload=None: http_transport(
            method, url, headers, payload, known_secrets=(config.api_key,)
        )
    )
    headers = config.request_headers()
    headers["X-Hermes-Helmet-Client"] = config.client
    response = transport(
        "POST",
        f"{config.service_url.rstrip('/')}/api/v1/search/recall",
        headers,
        {
            "query": query,
            "render": True,
            "quotas": {SHARED_MEMORY_TYPE: 10, "entities": 0, "preferences": 0, "experiences": 0},
        },
    )
    hits = _decode_recall_result(response.get("result"))
    # Prefer hits that mention the query when rendered-only fallback is used.
    if query:
        matched = [
            item
            for item in hits
            if query in json.dumps(item, sort_keys=True)
        ]
        if matched:
            return matched
    return hits


def intentional_shared_marker_write(
    config: ClientConfig,
    marker_text: str,
    *,
    transport: Transport | None = None,
    run_id: str | None = None,
) -> tuple[str, dict[str, object], Mapping[str, str]]:
    """Intentional client write into the shared events memory subtree.

    Returns (uri, origin_view, request_headers). Provenance is the request
    header flow and client config view — not a stored JSONL actor_peer field.
    ChatGPT operator gates are required before any identity probe or write
    so unsafe/missing gates cannot mutate. Live identity is probed before
    any write so root/admin and wrong account/user credentials cannot
    mutate the shared namespace. Fake transports still honor the
    registered identity.
    """

    if config.client == "chatgpt":
        require_chatgpt_operator_gates(config)
    transport = transport or (
        lambda method, url, headers, payload=None: http_transport(
            method, url, headers, payload, known_secrets=(config.api_key,)
        )
    )
    probe_client_identity(config, transport=transport)
    headers = config.request_headers()
    headers["X-Hermes-Helmet-Client"] = config.client
    uri, view = write_synthetic_marker(
        config, marker_text, transport=transport, shared=True, run_id=run_id
    )
    # Prefer headers actually observed by a FakeOpenVikingService transport.
    observed: Mapping[str, str] = dict(headers)
    if hasattr(transport, "__self__"):
        service = getattr(transport, "__self__", None)
        if service is not None and hasattr(service, "last_write_headers"):
            captured = service.last_write_headers.get(config.client)
            if isinstance(captured, Mapping):
                observed = dict(captured)
    origin = dict(view)
    origin["write_request_headers"] = {
        "X-OpenViking-Actor-Peer": observed.get(ACTOR_PEER_HEADER, ""),
        "X-Hermes-Helmet-Client": observed.get("X-Hermes-Helmet-Client", config.client),
        "has_authorization": bool(observed.get("Authorization")),
        "has_api_key_header": bool(observed.get(API_KEY_HEADER)),
    }
    origin["supported_origin_evidence"] = (
        "request_headers_and_client_config_view"
    )
    return uri, origin, observed


def read_marker(
    config: ClientConfig,
    uri: str,
    *,
    transport: Transport | None = None,
) -> dict[str, object]:
    transport = transport or (
        lambda method, url, headers, payload=None: http_transport(
            method, url, headers, payload, known_secrets=(config.api_key,)
        )
    )
    headers = config.request_headers()
    headers["X-Hermes-Helmet-Client"] = config.client
    query_string = urlparse.urlencode({"uri": uri})
    response = transport(
        "GET",
        f"{config.service_url.rstrip('/')}/api/v1/content/read?{query_string}",
        headers,
        None,
    )
    result = response.get("result")
    # Supported 0.4.8 content-read places the content string in result.
    if isinstance(result, str):
        return {
            "uri": uri,
            "content": result,
            "stored_fields": ["uri", "content"],
        }
    if not isinstance(result, Mapping):
        raise OpenVikingError(
            redact_secrets(
                f"openviking {config.client}: read returned no content",
                config.api_key,
            )
        )
    body = dict(result)
    if "content" not in body and isinstance(body.get("text"), str):
        body["content"] = body["text"]
    return body


def cross_client_marker_roundtrip(
    configs: Mapping[str, ClientConfig],
    *,
    transport: Transport,
    index_timeout_s: float = 5.0,
    transport_kind: str | None = None,
) -> dict[str, object]:
    """Each client intentionally writes a unique shared marker; others search/recall/read.

    Shared markers use the common user ``events`` memory URI so OpenViking 0.4.x
    type-quota recall and actor-peer view both work. Distinct peers remain in
    request headers and the writer origin evidence. Peer-tree isolation is
    preserved (not broadened).

    Joint provenance and ChatGPT operator gates are enforced before any
    mutation. Live identity is probed and must be USER in the shared namespace
    before the first write. Marker URIs carry a unique run id so retries stay
    safe under create-if-absent semantics, and retrieval uses a bounded wait
    for truthful queued indexing. Fake/hermetic transport is synthetic
    non-acceptance and does not replace official-client acceptance.
    """

    require_shared_client_provenance(configs)
    chatgpt = configs.get("chatgpt")
    if chatgpt is not None:
        require_chatgpt_operator_gates(chatgpt)
    for config in configs.values():
        probe_client_identity(config, transport=transport)
    run_id = uuid.uuid4().hex
    markers: dict[str, dict[str, object]] = {}
    write_headers: dict[str, Mapping[str, str]] = {}
    for client, config in configs.items():
        token = f"helmet-ov-marker-{client}-unique-{run_id[:8]}"
        uri, view, observed = intentional_shared_marker_write(
            config, token, transport=transport, run_id=f"{run_id}-{client}"
        )
        if not wait_for_marker_retrieval(
            config,
            token,
            transport=transport,
            timeout_s=index_timeout_s,
        ):
            raise OpenVikingError(
                f"openviking {client}: marker not searchable within index timeout"
            )
        markers[client] = {"uri": uri, "token": token, "origin_view": view}
        write_headers[client] = observed

    for writer, meta in markers.items():
        writer_view = meta["origin_view"]
        assert isinstance(writer_view, Mapping)
        for reader_name, reader in configs.items():
            if reader_name == writer:
                continue
            token = str(meta["token"])
            uri = str(meta["uri"])
            found_search = search_markers(reader, token, transport=transport)
            found_recall = recall_markers(reader, token, transport=transport)
            if not any(token in json.dumps(item) for item in found_search):
                raise OpenVikingError(
                    f"openviking {reader_name} could not search marker from {writer}"
                )
            if not any(token in json.dumps(item) for item in found_recall):
                raise OpenVikingError(
                    f"openviking {reader_name} could not recall marker from {writer}"
                )
            body = read_marker(reader, uri, transport=transport)
            if token not in str(body.get("content") or ""):
                raise OpenVikingError(
                    f"openviking {reader_name} could not read marker from {writer}"
                )
            # Attribution comes from writer config/header view, not invented JSONL.
            stored_fields = body.get("stored_fields")
            if isinstance(stored_fields, list) and "actor_peer" in stored_fields:
                pass
            if writer_view.get("originating_client") != writer:
                raise OpenVikingError(
                    f"openviking origin view mis-attributed marker from {writer}"
                )
            if writer_view.get("request_header_value") != configs[writer].peer_id:
                raise OpenVikingError(
                    f"openviking origin view peer mismatch for marker from {writer}"
                )
            captured = write_headers.get(writer)
            if captured is not None:
                if captured.get(ACTOR_PEER_HEADER) != configs[writer].peer_id:
                    raise OpenVikingError(
                        f"openviking write header peer mismatch for {writer}"
                    )
    kind = transport_kind or infer_transport_kind(transport)
    result: dict[str, object] = {
        "markers": markers,
        "ok": True,
        "shared_namespace": SHARED_MEMORY_ROOT,
        "shared_memory_type": SHARED_MEMORY_TYPE,
        "peer_tree_isolation": True,
        "stored_jsonl_actor_peer_field": False,
        "intentional_path": "content_write_common_events_memory",
        "origin_evidence": "request_headers_and_client_config_view",
        "proof_run_id": run_id,
        "write_headers": {
            client: {
                ACTOR_PEER_HEADER: headers.get(ACTOR_PEER_HEADER, ""),
                "client": headers.get("X-Hermes-Helmet-Client", client),
            }
            for client, headers in write_headers.items()
        },
        "limitation": (
            "Native Hermes intentional remember still targets peer-scoped URIs; "
            "Helmet shared markers use intentional content/write into common "
            f"{SHARED_MEMORY_TYPE} memory with distinct actor-peer request headers. "
            "Codex and Claude Code use the official openviking-memory plugins; "
            "Hermes uses its native provider plus Helmet write-marker/shared-proof. "
            "Helmet does not ship a duplicate MCP adapter. Fake transport is "
            "synthetic/non-acceptance and does not replace official-client acceptance."
        ),
    }
    result.update(proof_verification_fields(transport_kind=kind))
    return result


def doctor_openviking(
    policy: Policy,
    *,
    client_paths: Mapping[str, Path] | None = None,
    transport: Transport | None = None,
    require_live: bool = False,
    expected_template: OpenVikingCompanyTemplate | None = None,
) -> DoctorReport:
    """Verify optional OpenViking shared-namespace configuration.

    When ``policy.integrations.openviking`` is false, returns a skipped OK
    report so Helmet remains operable without the integration.
    """

    if not policy.integrations.openviking:
        return DoctorReport(
            enabled=False,
            skipped=True,
            ok=True,
            findings=(
                DoctorFinding(
                    code="openviking-declined",
                    ok=True,
                    message="OpenViking integration declined; Helmet operates without it",
                ),
            ),
        )

    findings: list[DoctorFinding] = []
    template = selected_company_template(
        policy,
        client_paths=client_paths,
        expected_template=expected_template,
    )
    findings.append(
        DoctorFinding(
            code="template",
            ok=True,
            message=(
                f"company template account/user/url/peers ready "
                f"(license {OPENVIKING_LICENSE})"
            ),
        )
    )

    expected_peers = {
        peer.peer_id.casefold() for peer in policy.openviking_peers
    } or {name.casefold() for name in DEFAULT_PEERS}
    for required in DEFAULT_CLIENTS:
        if required.casefold() not in expected_peers and policy.openviking_peers:
            # Policy may customize names; still require three distinct configured clients.
            pass

    if client_paths is None:
        return DoctorReport(
            enabled=True,
            skipped=False,
            ok=False,
            findings=(
                DoctorFinding(
                    code="client-paths",
                    ok=False,
                    message="owner-only client configuration paths were not provided",
                ),
            ),
        )

    missing = [name for name in DEFAULT_CLIENTS if name not in client_paths]
    if missing:
        return DoctorReport(
            enabled=True,
            skipped=False,
            ok=False,
            findings=(
                DoctorFinding(
                    code="client-paths",
                    ok=False,
                    message="codex, chatgpt, and hermes client paths are required",
                ),
            ),
        )

    try:
        configs = load_client_configs(client_paths)
    except OpenVikingError as exc:
        return DoctorReport(
            enabled=True,
            skipped=False,
            ok=False,
            findings=(
                DoctorFinding(
                    code="client-config",
                    ok=False,
                    message=redact_secrets(str(exc)),
                ),
            ),
        )

    # Same account/user/url/key across clients, then against the selected
    # company template. Key equality is configuration-only and never printed.
    # Mixed old/new keys fail closed offline. Explicit custom templates
    # (company.template.json or expected_template) remain accepted.
    accounts = {cfg.account for cfg in configs.values()}
    users = {cfg.user for cfg in configs.values()}
    urls = {cfg.service_url.rstrip("/") for cfg in configs.values()}
    if len(accounts) != 1 or len(users) != 1 or len(urls) != 1:
        findings.append(
            DoctorFinding(
                code="shared-namespace",
                ok=False,
                message="clients must share one account, user, and service URL",
            )
        )
    else:
        findings.append(
            DoctorFinding(
                code="shared-namespace",
                ok=True,
                message="all clients address the same configured account/user/URL",
            )
        )
    mixed_keys = len({cfg.api_key for cfg in configs.values()}) != 1
    if mixed_keys:
        findings.append(
            DoctorFinding(
                code="shared-user-key",
                ok=False,
                message="clients must share one USER key; mixed old/new keys fail closed",
            )
        )
    else:
        findings.append(
            DoctorFinding(
                code="shared-user-key",
                ok=True,
                message="all clients share one USER key",
            )
        )
    namespace_ok = clients_match_selected_company_template(configs, template)
    findings.append(
        DoctorFinding(
            code="expected-namespace",
            ok=namespace_ok,
            message=(
                "clients match the selected company template account/user/service URL"
                if namespace_ok
                else (
                    "client account/user/service URL must match the selected "
                    "company template"
                )
            ),
        )
    )

    # Peer ids must match the fixed client→peer template mapping when present.
    peer_ids = [cfg.peer_id for cfg in configs.values()]
    if any(not peer for peer in peer_ids):
        findings.append(
            DoctorFinding(
                code="peer-ids",
                ok=False,
                message="every client needs a non-empty provenance peer id",
            )
        )
    elif len({peer.casefold() for peer in peer_ids}) != len(peer_ids):
        findings.append(
            DoctorFinding(
                code="peer-ids",
                ok=False,
                message="provenance peer ids must be pairwise distinct",
            )
        )
    else:
        mismatched = [
            name
            for name, cfg in configs.items()
            if name in template.peers and cfg.peer_id != template.peers[name]
        ]
        if mismatched and policy.openviking_peers:
            findings.append(
                DoctorFinding(
                    code="peer-ids",
                    ok=False,
                    message="client peer ids must match the configured template mapping",
                )
            )
        else:
            findings.append(
                DoctorFinding(
                    code="peer-ids",
                    ok=True,
                    message="peer ids are non-empty, pairwise distinct, and client-typed",
                )
            )

    hermes = configs.get("hermes")
    if hermes is not None:
        other_peers = {
            cfg.peer_id.casefold()
            for name, cfg in configs.items()
            if name != "hermes"
        }
        if hermes.peer_id.casefold() in other_peers:
            findings.append(
                DoctorFinding(
                    code="hermes-effective-settings",
                    ok=False,
                    message="Hermes peer id must not duplicate another client's provenance",
                )
            )
        else:
            findings.append(
                DoctorFinding(
                    code="hermes-effective-settings",
                    ok=True,
                    message="Hermes peer/endpoint settings are internally consistent",
                )
            )

    # ChatGPT private connectivity gate (loopback/private IP only; not DNS spelling).
    chatgpt = configs.get("chatgpt")
    if chatgpt is not None:
        host = urlparse.urlparse(chatgpt.service_url).hostname or ""
        private = is_private_or_loopback_host(host)
        if not private:
            findings.append(
                DoctorFinding(
                    code="chatgpt-connectivity",
                    ok=False,
                    message="ChatGPT OpenViking URL must be loopback or a private IP",
                )
            )
        else:
            findings.append(
                DoctorFinding(
                    code="chatgpt-connectivity",
                    ok=True,
                    message="ChatGPT path uses loopback/private IP connectivity",
                )
            )
        findings.append(chatgpt_operator_gates_finding(chatgpt))

    verification_mode = "offline"
    if transport is not None or require_live:
        verification_mode = "live"
        active_transport = transport
        if active_transport is None:
            active_transport = (
                lambda method, url, headers, payload=None: http_transport(
                    method, url, headers, payload
                )
            )
        live_ok = True
        known_secrets = tuple(cfg.api_key for cfg in configs.values())
        for name, cfg in configs.items():
            try:
                probe_client_identity(
                    cfg,
                    transport=active_transport,
                    expected_account=template.account,
                    expected_user=template.user,
                )
            except OpenVikingError as exc:
                live_ok = False
                findings.append(
                    DoctorFinding(
                        code=f"live-{name}",
                        ok=False,
                        message=redact_secrets(str(exc), *known_secrets),
                    )
                )
        if live_ok:
            findings.append(
                DoctorFinding(
                    code="live-identity",
                    ok=True,
                    message="all client keys authenticate as USER in the shared namespace",
                )
            )
    else:
        findings.append(
            DoctorFinding(
                code="identity-verification",
                ok=True,
                message=(
                    "offline configuration check only; credentials were not "
                    "authenticated against the service (pass --live to verify)"
                ),
            )
        )

    # Redact any residual secrets from public client views and findings.
    known_secrets = tuple(cfg.api_key for cfg in configs.values())
    safe_findings = tuple(
        DoctorFinding(
            code=item.code,
            ok=item.ok,
            message=redact_secrets(item.message, *known_secrets),
        )
        for item in findings
    )
    safe_clients: list[dict[str, object]] = []
    for cfg in configs.values():
        view = cfg.public_view()
        safe_clients.append(
            json.loads(redact_secrets(json.dumps(view, sort_keys=True), *known_secrets))
        )
    ok = all(item.ok for item in safe_findings)
    return DoctorReport(
        enabled=True,
        skipped=False,
        ok=ok,
        findings=safe_findings,
        clients=tuple(safe_clients),
        verification_mode=verification_mode,
    )


def doctor(
    policy_path: Path,
    *,
    client_paths: Mapping[str, Path] | None = None,
    transport: Transport | None = None,
    require_live: bool = False,
    expected_template: OpenVikingCompanyTemplate | None = None,
) -> dict[str, object]:
    """Top-level doctor entry used by the CLI."""

    try:
        policy = load_authority(policy_path)
    except AuthorityError as exc:
        return {
            "ok": False,
            "policy": {"ok": False, "message": str(exc)},
            "openviking": None,
        }
    except OSError as exc:
        return {
            "ok": False,
            "policy": {"ok": False, "message": "policy could not be read"},
            "openviking": None,
            "error": type(exc).__name__,
        }

    ov_report = doctor_openviking(
        policy,
        client_paths=client_paths,
        transport=transport,
        require_live=require_live,
        expected_template=expected_template,
    )
    return {
        "ok": ov_report.ok,
        "policy": {
            "ok": True,
            "company": policy.company_slug,
            "integrations_openviking": policy.integrations.openviking,
        },
        "openviking": ov_report.to_public_dict(),
    }


def chatgpt_operator_checklist() -> tuple[str, ...]:
    """Explicit operator gates for the ChatGPT path (never automated)."""

    return (
        "Create the Secure MCP tunnel in the operator Platform organization.",
        "Create a limited tunnel runtime key (never an admin key).",
        "Store the runtime key only in an owner-only private file (mode 0600).",
        "Enable ChatGPT developer mode and a custom app with Connection: Tunnel.",
        "Select/approve the tunnel and intentional write tools only.",
        "Keep tunnel health/UI on loopback or private connectivity.",
        "Do not scrape, capture, or auto-ingest ChatGPT transcripts.",
        "Validate provenance via config + header chain, not a nonexistent JSONL field.",
    )


def setup_messages(template: OpenVikingCompanyTemplate) -> list[str]:
    """Human-oriented setup guidance without secrets."""

    return [
        f"OpenViking license: {OPENVIKING_LICENSE} (optional dependency).",
        "OpenViking is operational working context, not governed company truth.",
        "Promotion into FAVA Trails is an explicit separate action.",
        f"Template account={template.account} user={template.user} url={template.service_url}",
        f"Logical peers: {', '.join(f'{k}={v}' for k, v in sorted(template.peers.items()))}",
        "The company-context USER is the shared-all company working-context boundary.",
        "The Captain/operator is the sole credential owner; do not add a helper identity.",
        "Provision one least-privilege USER key (never root/admin) for the shared user.",
        "Write separate owner-only configs for codex, chatgpt, and hermes.",
        "Rotate by rewriting every owner-only client config from one new USER key, then doctor --live, then retire the old key; mixed keys fail closed.",
        "Codex uses the official OpenViking memory plugin against ovcli.conf; do not install a Helmet MCP duplicate.",
        "Claude Code uses the official OpenViking memory plugin; Helmet does not ship a Claude MCP adapter.",
        "ChatGPT stays operator-gated (intentional tools, loopback/private, no transcript capture).",
        "Codex/ChatGPT map peer via actor_peer_id and X-OpenViking-Actor-Peer.",
        "Hermes maps peer via OPENVIKING_AGENT / actor_peer_id and peer-scoped home-alias URIs.",
        (
            f"Shared intentional markers use content/write under {SHARED_MEMORY_ROOT}/… "
            "with distinct actor-peer headers (not native Hermes peer-remember)."
        ),
        "Use openviking shared-proof to exercise write/search/recall/read across clients.",
        "Helmet starts and operates when OpenViking is declined or unavailable.",
        *chatgpt_operator_checklist(),
    ]
