"""Portable worker roles. Routing changes assignment, never authority."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

ROLES = ("builder", "reviewer", "documentation", "media")
PROFILE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class WorkerRoles:
    profiles: tuple[tuple[str, str], ...]
    max_in_progress: int
    max_in_progress_per_profile: int

    def profile(self, role: str) -> str:
        return dict(self.profiles)[role]


def parse_roles(raw: Mapping[str, Any]) -> WorkerRoles | None:
    value = raw.get("worker_roles")
    if "worker_roles" not in raw:
        return None
    if not isinstance(value, dict) or set(value) != {
        "profiles", "max_in_progress", "max_in_progress_per_profile"
    }:
        raise ValueError("worker_roles requires profiles and both concurrency limits")
    profiles = value["profiles"]
    if not isinstance(profiles, dict) or set(profiles) != set(ROLES):
        raise ValueError("worker_roles.profiles requires builder, reviewer, documentation and media")
    if any(not isinstance(p, str) or not PROFILE_RE.fullmatch(p) for p in profiles.values()):
        raise ValueError("worker_roles profile names must be safe named profiles")
    if len(set(profiles.values())) != len(ROLES) or "default" in profiles.values():
        raise ValueError("worker_roles profiles must be distinct named profiles")
    if profiles["builder"] != raw.get("assignee"):
        raise ValueError("worker_roles builder must match the default assignee")
    limits = [value[k] for k in ("max_in_progress", "max_in_progress_per_profile")]
    if any(type(n) is not int or n < 1 for n in limits) or limits[1] > limits[0]:
        raise ValueError("worker_roles limits must be positive integers; per-profile must not exceed global")
    return WorkerRoles(tuple((r, profiles[r]) for r in ROLES), *limits)


class AssignedPolicy:
    """Preserve every policy extension while selecting an execution role."""
    def __init__(self, policy: Any, assignee: str):
        self._policy = policy
        self.assignee = assignee

    def __getattr__(self, name: str) -> Any:
        return getattr(self._policy, name)


def for_role(policy: Any, role: str) -> Any:
    roles = getattr(policy, "worker_roles", None)
    if roles is None:
        return policy
    return AssignedPolicy(policy, roles.profile(role))


def for_issue(policy: Any, body: str) -> Any:
    # A standalone, case-sensitive declaration, outside Markdown code fences.
    # Any attempted declaration with a bad value fails closed, never falls back.
    declarations = []
    fence = None
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is None and stripped.startswith("Worker role:"):
            declarations.append(stripped[len("Worker role:"):].strip())
    if not declarations:
        return for_role(policy, "builder")
    if len(declarations) != 1 or declarations[0] not in ("builder", "documentation", "media"):
        raise ValueError("declare exactly one Worker role: builder|documentation|media")
    if getattr(policy, "worker_roles", None) is None:
        raise ValueError("Worker role declared but role routing is not installed")
    return for_role(policy, declarations[0])
