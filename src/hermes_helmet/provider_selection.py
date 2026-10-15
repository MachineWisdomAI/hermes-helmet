"""Non-secret operational provider selection, projected onto native Hermes config.

Transport implementations own authentication, persistence and application. Native
Hermes owns retry, fallback and primary recovery; this module is not a router.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Mapping, Sequence


class ProviderSelectionError(ValueError):
    pass


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ProviderSelectionError(f"{field} must be a non-empty identifier")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/+-]*", value):
        raise ProviderSelectionError(f"{field} contains unsupported characters")
    return value


def validate(selection: object) -> dict:
    if not isinstance(selection, dict) or set(selection) != {"version", "routes", "selected"}:
        raise ProviderSelectionError("selection requires version, routes and selected only")
    if type(selection["version"]) is not int or selection["version"] != 1:
        raise ProviderSelectionError("unsupported selection version")
    routes = selection["routes"]
    if not isinstance(routes, list) or not 1 <= len(routes) <= 16:
        raise ProviderSelectionError("configure between one and sixteen ordered routes")
    names: set[str] = set()
    identities: set[tuple[str, str]] = set()
    for route in routes:
        if not isinstance(route, dict) or set(route) != {"name", "provider", "model"}:
            raise ProviderSelectionError("each route requires name, provider and model only; no credentials")
        name = _identifier(route["name"], "route name")
        provider = _identifier(route["provider"], "provider")
        model = _identifier(route["model"], "model")
        if name == "auto" or name in names or (provider, model) in identities:
            raise ProviderSelectionError("route names and provider/model pairs must be unique; auto is reserved")
        names.add(name)
        identities.add((provider, model))
    if not isinstance(selection["selected"], str) or (selection["selected"] != "auto" and selection["selected"] not in names):
        raise ProviderSelectionError("selected route is not configured")
    return copy.deepcopy(selection)


def configure(specs: Sequence[str]) -> dict:
    routes = []
    for spec in specs:
        try:
            name, pair = spec.split("=", 1)
            provider, model = pair.split(":", 1)
        except ValueError as exc:
            raise ProviderSelectionError("use --route NAME=PROVIDER:MODEL in priority order") from exc
        routes.append({"name": name, "provider": provider, "model": model})
    return validate({"version": 1, "routes": routes, "selected": "auto"})


def choose(selection: dict, name: str) -> dict:
    return validate({**selection, "selected": name})


def effective(selection: dict) -> tuple[dict, list[dict]]:
    selection = validate(selection)
    routes = selection["routes"]
    if selection["selected"] == "auto":
        primary, backups = routes[0], routes[1:]
    else:
        primary = next(route for route in routes if route["name"] == selection["selected"])
        backups = []  # An explicit manual pin does not borrow the automatic chain.
    return primary, [{"provider": route["provider"], "model": route["model"]} for route in backups]


def project_policy(policy: Mapping, selection: dict) -> dict:
    primary, _ = effective(selection)
    result = copy.deepcopy(dict(policy))
    result.update(inference_provider=primary["provider"], inference_model=primary["model"])
    executor = (result.get("model_lanes") or {}).get("hermes_executor")
    if executor is not None:
        # Endpoint-bound executor lanes need an explicit deployment migration.
        if executor.get("base_url"):
            raise ProviderSelectionError("provider switching cannot replace an endpoint-bound executor lane")
        executor.update(provider=primary["provider"], model=primary["model"])
    return result


def project_config(config: dict, selection: dict, *, effort: str | None = None) -> dict:
    primary, backups = effective(selection)
    result = copy.deepcopy(config)
    if isinstance(result.get("model"), str):
        result["model"] = {}
    model = result.setdefault("model", {})
    if not isinstance(model, dict):
        raise ProviderSelectionError("profile model config must be a mapping or model name")
    # Neither carry an old endpoint/key across providers nor discard credentials.
    if any(model.get(key) for key in ("base_url", "api_key", "api_key_env", "api_mode", "transport")):
        raise ProviderSelectionError("profile has endpoint or credential overrides; migrate those before switching providers")
    model.update(provider=primary["provider"], default=primary["model"])
    result["fallback_providers"] = backups
    result.pop("fallback_model", None)  # Hermes appends legacy entries otherwise.
    if effort is not None:
        overrides = result.setdefault("agent", {}).setdefault("reasoning_overrides", {})
        for route in validate(selection)["routes"]:
            overrides[route["model"]] = effort
    sonnet = result.get("agent", {}).get("helmet_sonnet_55_effort")
    if sonnet is not None:
        from .effort_policy import migrate_config
        result = migrate_config(result, sonnet)
    return result


def read_selection(path: Path) -> dict | None:
    if path.is_symlink():
        raise ProviderSelectionError("selection file must not be a symlink")
    if not path.exists():
        return None
    try:
        return validate(json.loads(path.read_text()))
    except (OSError, ValueError) as exc:
        raise ProviderSelectionError("cannot read valid provider selection") from exc


def write_selection(path: Path, selection: dict) -> None:
    selection = validate(selection)
    if path.is_symlink():
        raise ProviderSelectionError("selection file must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".providers-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(selection, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def configure_profile(path: Path, selection: dict) -> None:
    """Update one existing worker-owned profile; never copy auth or sessions."""
    from hermes_helmet.worker_role_runtime import _read_config, _yaml_dump

    config = project_config(_read_config(path), selection)
    fd, name = tempfile.mkstemp(prefix=".provider-config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(_yaml_dump(config))
        os.replace(name, path)
        if _read_config(path) != config:
            raise ProviderSelectionError("profile selection readback failed")
    finally:
        Path(name).unlink(missing_ok=True)


def transport_request(command: Sequence[str], request: dict) -> dict:
    if not command:
        raise ProviderSelectionError("configure HERMES_HELMET_WORKER_RUNTIME or --worker-runtime")
    try:
        result = subprocess.run(
            [*command, "providers", json.dumps(request)],
            capture_output=True, text=True, timeout=900,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProviderSelectionError("provider transport failed; inspect the deployment before retrying") from exc
    if result.returncode:
        # A transport may include diagnostics, never raw provider credentials.
        raise ProviderSelectionError("provider transport rejected the operation; run providers status and check Captain identity, paused dispatch and drained tasks")
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise ProviderSelectionError("provider transport did not return JSON") from exc
    if not isinstance(payload, dict):
        raise ProviderSelectionError("provider transport must return an object")
    return payload
