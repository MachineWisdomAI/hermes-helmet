#!/usr/bin/env python3
"""Provider-neutral model-lane contracts, probes, and owner-only runtime config.

Hermes executor selection is independent of optional FAVA generation,
OpenViking semantic generation, and embedding lanes. Hosted providers and
local OpenAI-compatible endpoints share the same contracts. Credentials never
enter authority policy. Setup reports success only after each selected lane
passes the smallest meaningful probe for its own contract.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import http.client
import json
import math
import os
from pathlib import Path
import stat
import tempfile
from typing import Callable, Mapping, Protocol, Sequence
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from hermes_helmet.authority import AuthorityError


LANE_IDS = (
    "hermes_executor",
    "fava_generation",
    "openviking_semantic_generation",
    "embeddings",
)
OPTIONAL_LANE_IDS = (
    "fava_generation",
    "openviking_semantic_generation",
    "embeddings",
)
CHAT_LANE_IDS = frozenset(
    {"hermes_executor", "fava_generation", "openviking_semantic_generation"}
)
KIND_CHAT = "chat_completions"
KIND_EMBED = "embeddings"
ALLOWED_KINDS = frozenset({KIND_CHAT, KIND_EMBED})
ALLOWED_BINDS = frozenset({"loopback", "lan", "container-host"})
LANE_KEYS = frozenset(
    {
        "kind",
        "provider",
        "model",
        "base_url",
        "timeout_seconds",
        "bind",
        "structured_output",
        "dimensions",
        "quantization",
    }
)
DEFAULT_TIMEOUT_SECONDS = 60
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ModelLaneError(RuntimeError):
    """Model-lane configuration or probe failed without echoing secrets."""


@dataclass(frozen=True)
class ModelLane:
    lane_id: str
    kind: str
    model: str
    provider: str = ""
    base_url: str | None = None
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    bind: str | None = None
    structured_output: bool = False
    dimensions: int | None = None
    quantization: str | None = None

    def public_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "lane": self.lane_id,
            "kind": self.kind,
            "provider": self.provider,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.base_url:
            payload["base_url"] = self.base_url
        if self.bind:
            payload["bind"] = self.bind
        if self.kind == KIND_CHAT:
            payload["structured_output"] = self.structured_output
        if self.kind == KIND_EMBED:
            payload["dimensions"] = self.dimensions
            if self.quantization:
                payload["quantization"] = self.quantization
        return payload

    def fingerprint(self) -> str:
        return "|".join(
            [
                self.lane_id,
                self.kind,
                self.model,
                str(self.dimensions or ""),
                self.quantization or "",
            ]
        )


@dataclass(frozen=True)
class ModelLanesConfig:
    hermes_executor: ModelLane
    fava_generation: ModelLane | None = None
    openviking_semantic_generation: ModelLane | None = None
    embeddings: ModelLane | None = None

    @property
    def optional_lanes_selected(self) -> bool:
        return any(
            lane is not None
            for lane in (
                self.fava_generation,
                self.openviking_semantic_generation,
                self.embeddings,
            )
        )

    def selected_lanes(self) -> tuple[ModelLane, ...]:
        lanes = [self.hermes_executor]
        for lane in (
            self.fava_generation,
            self.openviking_semantic_generation,
            self.embeddings,
        ):
            if lane is not None:
                lanes.append(lane)
        return tuple(lanes)

    def public_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "hermes_executor": self.hermes_executor.public_dict(),
        }
        if self.fava_generation is not None:
            payload["fava_generation"] = self.fava_generation.public_dict()
        if self.openviking_semantic_generation is not None:
            payload["openviking_semantic_generation"] = (
                self.openviking_semantic_generation.public_dict()
            )
        if self.embeddings is not None:
            payload["embeddings"] = self.embeddings.public_dict()
        return payload


@dataclass(frozen=True)
class ProbeResult:
    lane_id: str
    contract: str
    ok: bool
    message: str
    observed_dimensions: int | None = None
    discarded_index: bool = False


@dataclass(frozen=True)
class SetupReport:
    ok: bool
    setup_success: bool
    skipped_optional: bool
    probes: tuple[ProbeResult, ...] = ()
    hermes_executor: ModelLane | None = None
    message: str = ""

    def to_public_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "setup_success": self.setup_success,
            "skipped_optional": self.skipped_optional,
            "message": self.message,
            "hermes_executor": (
                self.hermes_executor.public_dict() if self.hermes_executor else None
            ),
            "probes": [
                {
                    "lane": item.lane_id,
                    "contract": item.contract,
                    "ok": item.ok,
                    "message": item.message,
                    "observed_dimensions": item.observed_dimensions,
                    "discarded_index": item.discarded_index,
                }
                for item in self.probes
            ],
        }


@dataclass(frozen=True)
class DoctorReport:
    ok: bool
    skipped_optional: bool
    hermes_executor: ModelLane
    findings: tuple[str, ...] = ()

    def to_public_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "skipped_optional": self.skipped_optional,
            "hermes_executor": self.hermes_executor.public_dict(),
            "findings": list(self.findings),
        }


@dataclass(frozen=True)
class EmbeddingRollbackPlan:
    previous_model: str
    previous_dimensions: int
    previous_quantization: str | None
    candidate_model: str
    candidate_dimensions: int
    preserves_production_data: bool = True


def _lane_error(field: str, message: str) -> AuthorityError:
    return AuthorityError(f"policy {field}: {message}")


def _require_mapping(raw: object, field: str) -> Mapping[str, object]:
    if not isinstance(raw, dict):
        raise _lane_error(field, "must be an object")
    return raw


def _optional_bool(raw: Mapping[str, object], field: str, path: str) -> bool:
    if field not in raw or raw.get(field) is None:
        return False
    value = raw.get(field)
    if not isinstance(value, bool):
        raise _lane_error(path, "must be a boolean")
    return value


def _optional_positive_int(
    raw: Mapping[str, object], field: str, path: str, default: int | None = None
) -> int | None:
    if field not in raw or raw.get(field) is None:
        return default
    value = raw.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _lane_error(path, "must be a positive integer")
    return value


def _optional_string(raw: Mapping[str, object], field: str, path: str) -> str | None:
    if field not in raw or raw.get(field) is None:
        return None
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _lane_error(path, "must be a non-empty string")
    return value.strip()


def _validate_base_url(url: str, path: str, bind: str | None) -> str:
    parsed = urlparse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise _lane_error(path, "must be an http(s) OpenAI-compatible base URL")
    if parsed.username or parsed.password:
        raise _lane_error(path, "must not embed credentials")
    if parsed.query or parsed.fragment:
        raise _lane_error(path, "must not include query or fragment")
    host = (parsed.hostname or "").casefold()
    if bind == "loopback" and host not in LOOPBACK_HOSTS:
        raise _lane_error(path, "loopback bind requires 127.0.0.1 or localhost")
    if bind == "container-host" and host not in {"host.docker.internal"} | LOOPBACK_HOSTS:
        # host.docker.internal is the documented container-to-host name; loopback
        # remains valid when the probe runs on the host itself.
        raise _lane_error(path, "container-host bind requires host.docker.internal or loopback")
    return url.rstrip("/")


def lane_from_mapping(lane_id: str, raw: Mapping[str, object]) -> ModelLane:
    """Validate one secret-free lane contract."""

    if lane_id not in LANE_IDS:
        raise _lane_error("model_lanes", "is not a recognized policy field")
    prefix = f"model_lanes.{lane_id}"
    for key in raw.keys():
        key_text = str(key)
        if key_text not in LANE_KEYS:
            # Secret-shaped keys are rejected by authority before this parser.
            raise _lane_error(f"{prefix}.{key_text}", "is not a recognized policy field")
    kind = _optional_string(raw, "kind", f"{prefix}.kind")
    expected_kind = KIND_EMBED if lane_id == "embeddings" else KIND_CHAT
    if kind is None:
        kind = expected_kind
    if kind not in ALLOWED_KINDS:
        raise _lane_error(f"{prefix}.kind", "must be chat_completions or embeddings")
    if kind != expected_kind:
        raise _lane_error(f"{prefix}.kind", "does not match the lane contract")
    model = _optional_string(raw, "model", f"{prefix}.model")
    if model is None:
        raise _lane_error(f"{prefix}.model", "must be a non-empty string")
    provider = _optional_string(raw, "provider", f"{prefix}.provider") or ""
    timeout = _optional_positive_int(
        raw, "timeout_seconds", f"{prefix}.timeout_seconds", DEFAULT_TIMEOUT_SECONDS
    )
    bind = _optional_string(raw, "bind", f"{prefix}.bind")
    if bind is not None and bind not in ALLOWED_BINDS:
        raise _lane_error(f"{prefix}.bind", "must be loopback, lan, or container-host")
    structured = _optional_bool(raw, "structured_output", f"{prefix}.structured_output")
    dimensions = None
    if "dimensions" in raw and raw.get("dimensions") is not None:
        if kind != KIND_EMBED:
            raise _lane_error(f"{prefix}.dimensions", "is only valid on the embeddings lane")
        dimensions = _optional_positive_int(raw, "dimensions", f"{prefix}.dimensions")
    if kind == KIND_EMBED:
        if dimensions is None:
            raise _lane_error(f"{prefix}.dimensions", "must be a positive integer")
        if structured:
            raise _lane_error(
                f"{prefix}.structured_output", "is only valid on chat_completions lanes"
            )
    quantization = _optional_string(raw, "quantization", f"{prefix}.quantization")
    base_url_raw = _optional_string(raw, "base_url", f"{prefix}.base_url")
    require_url = lane_id != "hermes_executor"
    if require_url and base_url_raw is None:
        raise _lane_error(f"{prefix}.base_url", "must be a non-empty string")
    base_url = (
        _validate_base_url(base_url_raw, f"{prefix}.base_url", bind)
        if base_url_raw
        else None
    )
    return ModelLane(
        lane_id=lane_id,
        kind=kind,
        model=model,
        provider=provider,
        base_url=base_url,
        timeout_seconds=int(timeout or DEFAULT_TIMEOUT_SECONDS),
        bind=bind,
        structured_output=structured if kind == KIND_CHAT else False,
        dimensions=dimensions,
        quantization=quantization,
    )


def default_hermes_executor(provider: str, model: str) -> ModelLane:
    return ModelLane(
        lane_id="hermes_executor",
        kind=KIND_CHAT,
        provider=provider,
        model=model,
    )


def parse_model_lanes_for_policy(
    raw: object,
    *,
    inference_provider: str,
    inference_model: str,
) -> ModelLanesConfig:
    executor = default_hermes_executor(inference_provider, inference_model)
    if raw is None:
        return ModelLanesConfig(hermes_executor=executor)
    mapping = _require_mapping(raw, "model_lanes")
    from hermes_helmet.authority import _reject_unknown_and_secret_keys

    _reject_unknown_and_secret_keys(mapping, frozenset(LANE_IDS), prefix="model_lanes")
    for lane_id, value in mapping.items():
        if isinstance(value, dict):
            _reject_unknown_and_secret_keys(
                value,
                LANE_KEYS,
                prefix=f"model_lanes.{lane_id}",
            )
    fava = ov = embeddings = None
    if "hermes_executor" in mapping:
        override_raw = _require_mapping(mapping.get("hermes_executor"), "model_lanes.hermes_executor")
        override = lane_from_mapping("hermes_executor", override_raw)
        if override.provider and override.provider != inference_provider:
            raise _lane_error(
                "model_lanes.hermes_executor",
                "must match inference_provider",
            )
        if override.model != inference_model:
            raise _lane_error(
                "model_lanes.hermes_executor",
                "must match inference_model",
            )
        executor = replace(override, provider=inference_provider, model=inference_model)
    if "fava_generation" in mapping:
        fava = lane_from_mapping(
            "fava_generation",
            _require_mapping(mapping.get("fava_generation"), "model_lanes.fava_generation"),
        )
    if "openviking_semantic_generation" in mapping:
        ov = lane_from_mapping(
            "openviking_semantic_generation",
            _require_mapping(
                mapping.get("openviking_semantic_generation"),
                "model_lanes.openviking_semantic_generation",
            ),
        )
    if "embeddings" in mapping:
        embeddings = lane_from_mapping(
            "embeddings",
            _require_mapping(mapping.get("embeddings"), "model_lanes.embeddings"),
        )
    return ModelLanesConfig(
        hermes_executor=executor,
        fava_generation=fava,
        openviking_semantic_generation=ov,
        embeddings=embeddings,
    )


def lanes_from_policy(policy: object) -> ModelLanesConfig:
    existing = getattr(policy, "model_lanes", None)
    if isinstance(existing, ModelLanesConfig):
        return existing
    provider = str(getattr(policy, "inference_provider", "") or "")
    model = str(getattr(policy, "inference_model", "") or "")
    return ModelLanesConfig(hermes_executor=default_hermes_executor(provider, model))


class OpenAICompatibleTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: Mapping[str, object] | str | bytes | None = None,
        timeout: float | None = None,
    ) -> dict[str, object]: ...


class FakeOpenAITransport:
    """Hermetic OpenAI-compatible probe transport."""

    def __init__(
        self,
        *,
        chat_content: str = "{}",
        embedding: Sequence[float] | None = None,
        embeddings_by_text: Mapping[str, Sequence[float]] | None = None,
        chat_payload: Mapping[str, object] | None = None,
        embeddings_payload: Mapping[str, object] | None = None,
    ) -> None:
        self.chat_content = chat_content
        self.embedding = list(embedding or [0.1, 0.2, 0.3, 0.4])
        self.embeddings_by_text = {
            key: list(value) for key, value in dict(embeddings_by_text or {}).items()
        }
        self.chat_payload = dict(chat_payload) if chat_payload is not None else None
        self.embeddings_payload = (
            dict(embeddings_payload) if embeddings_payload is not None else None
        )
        self.chat_called = False
        self.embeddings_called = False
        self.last_url: str | None = None
        self.last_headers: dict[str, str] = {}
        self.before_request: Callable[..., None] | None = None

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: Mapping[str, object] | str | bytes | None = None,
        timeout: float | None = None,
    ) -> dict[str, object]:
        self.last_url = url
        self.last_headers = dict(headers or {})
        if self.before_request is not None:
            self.before_request(method, url, headers=headers, body=body, timeout=timeout)
        payload: Mapping[str, object]
        if isinstance(body, Mapping):
            payload = body
        elif isinstance(body, (bytes, bytearray)):
            payload = json.loads(body.decode("utf-8") or "{}")
        elif isinstance(body, str) and body:
            payload = json.loads(body)
        else:
            payload = {}
        if "/chat/completions" in url:
            self.chat_called = True
            if self.chat_payload is not None:
                return dict(self.chat_payload)
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": self.chat_content}}
                ]
            }
        if url.rstrip("/").endswith("/embeddings") or "/embeddings" in url:
            self.embeddings_called = True
            if self.embeddings_payload is not None:
                return dict(self.embeddings_payload)
            raw_input = payload.get("input")
            texts: list[str]
            if isinstance(raw_input, str):
                texts = [raw_input]
            elif isinstance(raw_input, list):
                texts = [str(item) for item in raw_input]
            else:
                texts = [""]
            data = []
            for text in texts:
                vector = self.embeddings_by_text.get(text, self.embedding)
                data.append({"embedding": list(vector)})
            return {"data": data}
        raise ModelLaneError("unsupported OpenAI-compatible probe path")


def _http_origin(url: str) -> tuple[str, str, int | None]:
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


class _CredentialRedirectHandler(urlrequest.HTTPRedirectHandler):
    """Refuse credential-bearing cross-origin redirects (stdlib forwards headers)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        has_credentials = any(
            str(header).casefold() == "authorization" for header in req.headers
        ) or bool(req.has_header("Authorization"))
        if has_credentials and _http_origin(req.full_url) != _http_origin(str(newurl)):
            raise ModelLaneError(
                "lane probe refused cross-origin redirect while credentials were present"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpOpenAITransport:
    """Live OpenAI-compatible HTTP transport. Credentials stay in headers."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: Mapping[str, object] | str | bytes | None = None,
        timeout: float | None = None,
    ) -> dict[str, object]:
        if isinstance(body, Mapping):
            encoded = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            encoded = body.encode("utf-8")
        elif isinstance(body, (bytes, bytearray)):
            encoded = bytes(body)
        else:
            encoded = b"{}"
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(
                {key: value for key, value in headers.items() if key.lower() != "authorization" or value}
            )
        req = urlrequest.Request(url, data=encoded, headers=request_headers, method=method)
        opener = urlrequest.build_opener(_CredentialRedirectHandler())
        try:
            with opener.open(req, timeout=timeout or DEFAULT_TIMEOUT_SECONDS) as response:
                raw_bytes = response.read()
        except ModelLaneError:
            raise
        except (
            TimeoutError,
            urlerror.URLError,
            OSError,
            http.client.HTTPException,
        ) as exc:
            raise ModelLaneError("lane probe failed") from exc
        try:
            raw = raw_bytes.decode("utf-8")
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise ModelLaneError("lane probe returned non-JSON") from exc
        if not isinstance(parsed, dict):
            raise ModelLaneError("lane probe returned a non-object")
        return parsed


def _join_url(base_url: str | None, suffix: str, *, provider: str) -> str:
    base = (base_url or "").rstrip("/")
    if not base:
        if provider == "openai":
            base = "https://api.openai.com/v1"
        else:
            raise ModelLaneError("lane is missing a base_url")
    return f"{base}/{suffix.lstrip('/')}"


def _authorization_headers(credential: str | None) -> dict[str, str]:
    if not credential:
        return {}
    return {"Authorization": f"Bearer {credential}"}


def _usable_assistant_content(payload: Mapping[str, object]) -> str | None:
    if payload.get("error"):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if role is not None and role != "assistant":
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return content


def _finite_vector(raw: object) -> list[float]:
    if not isinstance(raw, list):
        raise ModelLaneError("embedding payload is malformed")
    vector: list[float] = []
    for item in raw:
        try:
            value = float(item)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ModelLaneError("embedding payload is malformed") from exc
        if not math.isfinite(value):
            raise ModelLaneError("embedding payload is malformed")
        vector.append(value)
    return vector


def probe_chat_completions(
    lane: ModelLane,
    *,
    transport: OpenAICompatibleTransport,
    credential: str | None = None,
) -> ProbeResult:
    if lane.kind != KIND_CHAT:
        return ProbeResult(lane.lane_id, KIND_CHAT, False, "lane is not a chat_completions contract")
    url = _join_url(lane.base_url, "chat/completions", provider=lane.provider)
    body: dict[str, object] = {
        "model": lane.model,
        "messages": [{"role": "user", "content": "helmet-lane-probe"}],
        "max_tokens": 8,
        "temperature": 0,
    }
    if lane.structured_output:
        body["response_format"] = {"type": "json_object"}
    try:
        payload = transport.request(
            "POST",
            url,
            headers=_authorization_headers(credential),
            body=body,
            timeout=float(lane.timeout_seconds),
        )
    except ModelLaneError as exc:
        return ProbeResult(lane.lane_id, KIND_CHAT, False, str(exc) or "chat probe failed")
    content = _usable_assistant_content(payload)
    if content is None:
        return ProbeResult(
            lane.lane_id,
            KIND_CHAT,
            False,
            "chat probe did not return a usable assistant choice",
        )
    if lane.structured_output:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return ProbeResult(
                lane.lane_id,
                KIND_CHAT,
                False,
                "structured-output validation failed",
            )
        if not isinstance(parsed, (dict, list)):
            return ProbeResult(
                lane.lane_id,
                KIND_CHAT,
                False,
                "structured-output validation failed",
            )
    return ProbeResult(lane.lane_id, KIND_CHAT, True, "chat_completions probe passed")


def probe_embeddings(
    lane: ModelLane,
    *,
    transport: OpenAICompatibleTransport,
    credential: str | None = None,
) -> ProbeResult:
    if lane.kind != KIND_EMBED or lane.dimensions is None:
        return ProbeResult(lane.lane_id, KIND_EMBED, False, "lane is not an embeddings contract")
    url = _join_url(lane.base_url, "embeddings", provider=lane.provider)
    body = {"model": lane.model, "input": "helmet-dimension-probe"}
    try:
        payload = transport.request(
            "POST",
            url,
            headers=_authorization_headers(credential),
            body=body,
            timeout=float(lane.timeout_seconds),
        )
        data = payload.get("data")
        vector: list[float] = []
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                vector = _finite_vector(first.get("embedding"))
    except ModelLaneError as exc:
        return ProbeResult(lane.lane_id, KIND_EMBED, False, str(exc) or "embeddings probe failed")
    observed = len(vector)
    if observed != lane.dimensions:
        return ProbeResult(
            lane.lane_id,
            KIND_EMBED,
            False,
            f"embedding dimension mismatch: declared {lane.dimensions} observed {observed}",
            observed_dimensions=observed,
        )
    return ProbeResult(
        lane.lane_id,
        KIND_EMBED,
        True,
        "embeddings probe passed",
        observed_dimensions=observed,
    )


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    n1 = math.sqrt(sum(a * a for a in left))
    n2 = math.sqrt(sum(b * b for b in right))
    if n1 == 0 or n2 == 0:
        return 0.0
    return dot / (n1 * n2)


def probe_disposable_retrieval(
    lane: ModelLane,
    *,
    transport: OpenAICompatibleTransport,
    credential: str | None = None,
) -> ProbeResult:
    if lane.kind != KIND_EMBED or lane.dimensions is None:
        return ProbeResult(
            lane.lane_id,
            "disposable_retrieval",
            False,
            "lane is not an embeddings contract",
        )
    url = _join_url(lane.base_url, "embeddings", provider=lane.provider)
    documents = ("alpha marker", "unrelated noise")
    query = "alpha query"
    try:
        payload = transport.request(
            "POST",
            url,
            headers=_authorization_headers(credential),
            body={"model": lane.model, "input": list(documents) + [query]},
            timeout=float(lane.timeout_seconds),
        )
        data = payload.get("data")
        vectors: list[list[float]] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    vectors.append(_finite_vector(item.get("embedding")))
    except ModelLaneError as exc:
        return ProbeResult(
            lane.lane_id,
            "disposable_retrieval",
            False,
            str(exc) or "retrieval probe failed",
        )
    if len(vectors) != 3:
        return ProbeResult(
            lane.lane_id,
            "disposable_retrieval",
            False,
            "disposable retrieval probe did not return three embeddings",
        )
    for vector in vectors:
        if len(vector) != lane.dimensions:
            return ProbeResult(
                lane.lane_id,
                "disposable_retrieval",
                False,
                "embedding dimension mismatch",
                observed_dimensions=len(vector),
            )
    doc_a, doc_b, query_vec = vectors
    index = {"alpha marker": doc_a, "unrelated noise": doc_b}
    ranked = sorted(
        index.items(),
        key=lambda item: _cosine(query_vec, item[1]),
        reverse=True,
    )
    winner = ranked[0][0] if ranked else ""
    index.clear()
    if winner != "alpha marker":
        return ProbeResult(
            lane.lane_id,
            "disposable_retrieval",
            False,
            "disposable retrieval ranking failed",
            discarded_index=True,
        )
    return ProbeResult(
        lane.lane_id,
        "disposable_retrieval",
        True,
        "disposable-index retrieval probe passed; production data untouched",
        observed_dimensions=lane.dimensions,
        discarded_index=True,
    )


def _probe_lane(
    lane: ModelLane,
    *,
    transport: OpenAICompatibleTransport,
    credential: str | None = None,
) -> list[ProbeResult]:
    if lane.kind == KIND_CHAT:
        return [probe_chat_completions(lane, transport=transport, credential=credential)]
    results = [probe_embeddings(lane, transport=transport, credential=credential)]
    results.append(
        probe_disposable_retrieval(lane, transport=transport, credential=credential)
    )
    return results


def _embedding_fingerprint(model: object, dimensions: object, quantization: object) -> tuple[str, int, str | None]:
    dim = 0
    if isinstance(dimensions, int) and not isinstance(dimensions, bool):
        dim = dimensions
    elif isinstance(dimensions, str) and dimensions.strip().lstrip("-").isdigit():
        dim = int(dimensions)
    return (
        str(model or ""),
        dim,
        str(quantization) if quantization else None,
    )


def _preview_embedding_index(
    lanes: ModelLanesConfig,
    *,
    embedding_index_state: Path | None,
    reindex_confirmed: bool,
) -> None:
    if lanes.embeddings is None or embedding_index_state is None:
        return
    state_path = embedding_index_state.expanduser()
    if not state_path.is_file():
        return
    state = _load_index_state(state_path)
    current = state.get("current")
    if not isinstance(current, dict):
        raise ModelLaneError("embedding index state is invalid")
    changed = _embedding_fingerprint(
        current.get("model"),
        current.get("dimensions"),
        current.get("quantization"),
    ) != _embedding_fingerprint(
        lanes.embeddings.model,
        lanes.embeddings.dimensions,
        lanes.embeddings.quantization,
    )
    if changed and not reindex_confirmed:
        raise ModelLaneError(
            "changing embedding model, dimension, or quantization requires re-index"
        )


def _commit_embedding_index(
    lanes: ModelLanesConfig,
    *,
    embedding_index_state: Path | None,
    reindex_confirmed: bool,
) -> None:
    if lanes.embeddings is None or embedding_index_state is None:
        return
    state_path = embedding_index_state.expanduser()
    if state_path.is_file():
        apply_embedding_lane_change(
            state_path,
            lanes.embeddings,
            reindex_confirmed=reindex_confirmed,
        )
        return
    write_embedding_index_state(state_path, lanes.embeddings)


def setup_model_lanes(
    policy: object,
    *,
    transport: OpenAICompatibleTransport | None = None,
    probe: bool = False,
    credentials: Mapping[str, str] | None = None,
    embedding_index_state: Path | None = None,
    reindex_confirmed: bool = False,
) -> SetupReport:
    lanes = lanes_from_policy(policy)
    skipped = not lanes.optional_lanes_selected
    _preview_embedding_index(
        lanes,
        embedding_index_state=embedding_index_state,
        reindex_confirmed=reindex_confirmed,
    )
    if not probe:
        if skipped:
            return SetupReport(
                ok=True,
                setup_success=False,
                skipped_optional=True,
                hermes_executor=lanes.hermes_executor,
                message=(
                    "optional local-model lanes declined; minimum runtime remains "
                    "operational; Hermes executor is not yet validated"
                ),
            )
        return SetupReport(
            ok=False,
            setup_success=False,
            skipped_optional=False,
            hermes_executor=lanes.hermes_executor,
            message="selected lanes were not probed",
        )
    if transport is None:
        return SetupReport(
            ok=False,
            setup_success=False,
            skipped_optional=skipped,
            hermes_executor=lanes.hermes_executor,
            message="probe transport is required before setup reports success",
        )
    probes: list[ProbeResult] = []
    creds = dict(credentials or {})
    for lane in lanes.selected_lanes():
        probes.extend(
            _probe_lane(lane, transport=transport, credential=creds.get(lane.lane_id))
        )
    ok = all(item.ok for item in probes)
    if ok:
        _commit_embedding_index(
            lanes,
            embedding_index_state=embedding_index_state,
            reindex_confirmed=reindex_confirmed,
        )
    return SetupReport(
        ok=ok,
        setup_success=ok,
        skipped_optional=skipped,
        probes=tuple(probes),
        hermes_executor=lanes.hermes_executor,
        message="all selected lanes probed" if ok else "one or more lane probes failed",
    )


def doctor_model_lanes(
    policy: object,
    *,
    require_live: bool = False,
    transport: OpenAICompatibleTransport | None = None,
    credentials: Mapping[str, str] | None = None,
    runtime_dir: Path | None = None,
) -> DoctorReport:
    lanes = lanes_from_policy(policy)
    skipped = not lanes.optional_lanes_selected
    findings = [
        f"Hermes executor {lanes.hermes_executor.provider}/{lanes.hermes_executor.model} is independent of optional FAVA and OpenViking lanes",
    ]
    if skipped:
        findings.append("optional local-model lanes declined; minimum runtime operational")
    else:
        findings.append("optional lanes configured as separate contracts")
        if lanes.embeddings is not None:
            findings.append(
                f"embedding dimensions fixed at {lanes.embeddings.dimensions} before use"
            )
    if require_live:
        creds = dict(credentials or {})
        if not creds:
            creds = resolve_runtime_credentials(lanes, runtime_dir)
        report = setup_model_lanes(
            policy,
            transport=transport or HttpOpenAITransport(),
            probe=True,
            credentials=creds,
        )
        return DoctorReport(
            ok=report.ok,
            skipped_optional=skipped,
            hermes_executor=lanes.hermes_executor,
            findings=tuple(findings + [report.message]),
        )
    return DoctorReport(
        ok=True,
        skipped_optional=skipped,
        hermes_executor=lanes.hermes_executor,
        findings=tuple(findings),
    )


def claim_shared_artifact(
    lanes: Sequence[ModelLane],
    evidence: Mapping[str, object] | None,
) -> None:
    if len(lanes) < 2:
        return
    if evidence:
        return
    raise ModelLaneError(
        "refusing to treat one artifact as suitable for multiple model lanes without evidence"
    )


def assert_independent_lane_probes(
    lanes: Sequence[ModelLane],
    probes: Sequence[ProbeResult],
) -> None:
    by_lane = {item.lane_id: item for item in probes if item.ok}
    missing = [lane.lane_id for lane in lanes if lane.lane_id not in by_lane]
    if missing:
        raise ModelLaneError(
            "each selected lane requires its own successful probe; missing: "
            + ",".join(missing)
        )
    contracts = {by_lane[lane.lane_id].contract for lane in lanes}
    if len(lanes) > 1 and len(contracts) < 2 and any(lane.kind == KIND_EMBED for lane in lanes):
        raise ModelLaneError(
            "generation and embedding models must be probed as separate contracts"
        )


def _atomic_owner_only_write(path: Path, payload: Mapping[str, object]) -> None:
    path = path.expanduser()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = json.dumps(dict(payload), indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(parent),
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
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


@dataclass(frozen=True)
class LaneRuntimeConfig:
    lane: ModelLane
    credential_env: str | None = None

    def public_dict(self) -> dict[str, object]:
        payload = self.lane.public_dict()
        if self.credential_env:
            payload["credential_env"] = self.credential_env
        return payload


def write_lane_runtime_config(
    path: Path,
    lane: ModelLane,
    *,
    credential_env: str | None = None,
) -> LaneRuntimeConfig:
    payload = lane.public_dict()
    if credential_env:
        payload["credential_env"] = credential_env
    _atomic_owner_only_write(path, payload)
    return LaneRuntimeConfig(lane=lane, credential_env=credential_env)


def _require_owner_only_file(path: Path) -> None:
    if path.is_symlink():
        raise ModelLaneError("lane runtime config must be a regular file, not a symlink")
    if not path.is_file():
        raise ModelLaneError("lane runtime config is missing")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ModelLaneError("lane runtime config must be owner-only (mode 0600)")


def resolve_runtime_credentials(
    lanes: ModelLanesConfig,
    runtime_dir: Path | None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    if runtime_dir is None:
        return {}
    environ = os.environ if env is None else env
    credentials: dict[str, str] = {}
    for lane in lanes.selected_lanes():
        path = runtime_dir.expanduser() / f"{lane.lane_id}.json"
        if not path.exists() and not path.is_symlink():
            continue
        _require_owner_only_file(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelLaneError("lane runtime config is invalid") from exc
        if not isinstance(raw, dict):
            raise ModelLaneError("lane runtime config is invalid")
        env_name = raw.get("credential_env")
        if env_name is None:
            continue
        if not isinstance(env_name, str) or not env_name.strip():
            raise ModelLaneError("lane runtime config is invalid")
        value = environ.get(env_name.strip())
        if not value:
            raise ModelLaneError(f"{lane.lane_id} credential is missing")
        credentials[lane.lane_id] = value
    return credentials


def write_embedding_index_state(path: Path, lane: ModelLane) -> None:
    payload = {
        "current": {
            "model": lane.model,
            "dimensions": lane.dimensions,
            "quantization": lane.quantization,
        },
        "previous": None,
    }
    _atomic_owner_only_write(path, payload)


def _state_dimension(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ModelLaneError("embedding index state is invalid")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ModelLaneError("embedding index state is invalid")
        as_int = int(value)
        if value != as_int:
            raise ModelLaneError("embedding index state is invalid")
        return as_int
    if isinstance(value, str):
        try:
            return int(value)
        except (ValueError, OverflowError) as exc:
            raise ModelLaneError("embedding index state is invalid") from exc
    raise ModelLaneError("embedding index state is invalid")


def _validated_embedding_fingerprint(
    raw: object, *, missing: str
) -> tuple[str, int, str | None]:
    if not isinstance(raw, dict):
        raise ModelLaneError(missing)
    model = raw.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ModelLaneError("embedding index state is invalid")
    dimensions = _state_dimension(raw.get("dimensions"))
    if dimensions <= 0:
        raise ModelLaneError("embedding index state is invalid")
    quantization = raw.get("quantization")
    if quantization is None or quantization == "":
        return model, dimensions, None
    if not isinstance(quantization, str):
        raise ModelLaneError("embedding index state is invalid")
    return model, dimensions, quantization


def _load_index_state(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ModelLaneError("embedding index state is invalid") from exc
    if not isinstance(raw, dict):
        raise ModelLaneError("embedding index state is invalid")
    return raw


def apply_embedding_lane_change(
    state_path: Path,
    candidate: ModelLane,
    *,
    reindex_confirmed: bool,
) -> ModelLane:
    if candidate.kind != KIND_EMBED or candidate.dimensions is None:
        raise ModelLaneError("candidate is not an embeddings lane")
    state = _load_index_state(state_path)
    previous_model, previous_dimensions, previous_quant = (
        _validated_embedding_fingerprint(
            state.get("current"), missing="embedding index state is invalid"
        )
    )
    changed = (
        previous_model != candidate.model
        or previous_dimensions != candidate.dimensions
        or previous_quant != (candidate.quantization or None)
    )
    if not changed:
        return candidate
    if not reindex_confirmed:
        raise ModelLaneError(
            "changing embedding model, dimension, or quantization requires re-index"
        )
    payload = {
        "current": {
            "model": candidate.model,
            "dimensions": candidate.dimensions,
            "quantization": candidate.quantization,
        },
        "previous": {
            "model": previous_model,
            "dimensions": previous_dimensions,
            "quantization": previous_quant,
        },
    }
    _atomic_owner_only_write(state_path, payload)
    return candidate


def embedding_rollback_plan(state_path: Path, candidate: ModelLane) -> EmbeddingRollbackPlan:
    state = _load_index_state(state_path)
    current = state.get("current")
    if not isinstance(current, dict):
        raise ModelLaneError("embedding index state is invalid")
    return EmbeddingRollbackPlan(
        previous_model=str(current.get("model") or ""),
        previous_dimensions=_state_dimension(current.get("dimensions")),
        previous_quantization=(
            str(current.get("quantization")) if current.get("quantization") else None
        ),
        candidate_model=candidate.model,
        candidate_dimensions=int(candidate.dimensions or 0),
        preserves_production_data=True,
    )


def rollback_embedding_lane(
    state_path: Path,
    *,
    index_restore_confirmed: bool = False,
) -> ModelLane:
    if not index_restore_confirmed:
        raise ModelLaneError(
            "embedding rollback requires a compatible preserved-index restore or re-index"
        )
    state = _load_index_state(state_path)
    previous = state.get("previous")
    current = state.get("current")
    if not isinstance(previous, dict) or not isinstance(current, dict):
        raise ModelLaneError("embedding rollback path is missing")
    model, dimensions, quantization = _validated_embedding_fingerprint(
        previous, missing="embedding rollback path is missing"
    )
    current_model, current_dimensions, current_quantization = (
        _validated_embedding_fingerprint(
            current, missing="embedding index state is invalid"
        )
    )
    payload = {
        "current": {
            "model": model,
            "dimensions": dimensions,
            "quantization": quantization,
        },
        "previous": {
            "model": current_model,
            "dimensions": current_dimensions,
            "quantization": current_quantization,
        },
    }
    _atomic_owner_only_write(state_path, payload)
    return ModelLane(
        lane_id="embeddings",
        kind=KIND_EMBED,
        model=model,
        dimensions=dimensions,
        quantization=quantization,
    )


def secret_free_contract_examples() -> dict[str, dict[str, str]]:
    return {
        "fava_generation": {
            "path": "http://127.0.0.1:11434/v1/chat/completions",
            "note": "FAVA generation uses /v1/chat/completions as its own contract.",
        },
        "openviking_semantic_generation": {
            "path": "http://127.0.0.1:11434/v1/chat/completions",
            "note": "OpenViking semantic generation is a separate chat contract from embeddings.",
        },
        "embeddings": {
            "path": "http://127.0.0.1:11434/v1/embeddings",
            "note": "Embedding dimensions are fixed and verified before indexing.",
        },
    }


def setup_messages() -> list[str]:
    return [
        "Hermes executor provider/model is independent of optional FAVA and OpenViking lanes.",
        "Hosted and local OpenAI-compatible endpoints share documented /v1 contracts.",
        "Keep provider credentials in owner-only runtime files or process env — never in policy.",
        "Probe each selected lane before setup reports success.",
        "Generation and embeddings are separate contracts; do not reuse one artifact without evidence.",
        "Changing embedding model, dimension, or quantization requires re-index plus a rollback path.",
        "Disposable-index retrieval checks must not touch production data.",
        "Declining every optional local-model path leaves the minimum runtime operational.",
        "On Mac, Ollama, Unsloth Studio, and OrbStack are equally valid host-native options.",
    ]
