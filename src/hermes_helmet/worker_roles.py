"""Portable worker roles. Routing changes assignment, never authority."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

ROLES = ("builder", "reviewer", "writing")
LEGACY_ROLES = ("builder", "reviewer", "documentation", "media")
REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
PROFILE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class WorkerRoles:
    profiles: tuple[tuple[str, str], ...]
    max_in_progress: int
    max_in_progress_per_profile: int
    reasoning_effort: tuple[tuple[str, str], ...] = ()
    sonnet_55_effort: tuple[tuple[str, str], ...] = ()

    def profile(self, role: str) -> str:
        profiles = dict(self.profiles)
        if "writing" in profiles and role in ("documentation", "media"):
            role = "writing"
        return profiles[role]


def parse_roles(raw: Mapping[str, Any]) -> WorkerRoles | None:
    value = raw.get("worker_roles")
    if "worker_roles" not in raw:
        return None
    required = {"profiles", "max_in_progress", "max_in_progress_per_profile"}
    if (not isinstance(value, dict) or not required <= set(value)
            or set(value) - required - {"reasoning_effort", "sonnet_55_effort"}):
        raise ValueError("worker_roles requires profiles and both concurrency limits")
    profiles = value["profiles"]
    if not isinstance(profiles, dict) or set(profiles) not in (set(ROLES), set(LEGACY_ROLES)):
        raise ValueError("worker_roles.profiles requires builder, reviewer and writing (or legacy documentation and media)")
    roles = ROLES if "writing" in profiles else LEGACY_ROLES
    if any(not isinstance(p, str) or not PROFILE_RE.fullmatch(p) for p in profiles.values()):
        raise ValueError("worker_roles profile names must be safe named profiles")
    if len(set(profiles.values())) != len(roles) or "default" in profiles.values():
        raise ValueError("worker_roles profiles must be distinct named profiles")
    if profiles["builder"] != raw.get("assignee"):
        raise ValueError("worker_roles builder must match the default assignee")
    limits = [value[k] for k in ("max_in_progress", "max_in_progress_per_profile")]
    if any(type(n) is not int or n < 1 for n in limits) or limits[1] > limits[0]:
        raise ValueError("worker_roles limits must be positive integers; per-profile must not exceed global")
    efforts = value.get("reasoning_effort", {})
    if "reasoning_effort" in value:
        if not isinstance(efforts, dict) or set(efforts) != set(roles):
            raise ValueError("worker_roles.reasoning_effort requires one level for every configured role")
        if any(not isinstance(e, str) or e not in REASONING_EFFORTS for e in efforts.values()):
            raise ValueError("worker_roles.reasoning_effort levels must be low|medium|high|xhigh|max")
    sonnet = value.get("sonnet_55_effort", {})
    if "sonnet_55_effort" in value:
        if not isinstance(sonnet, dict) or set(sonnet) != set(roles):
            raise ValueError("worker_roles.sonnet_55_effort requires every configured role")
        if any(e not in ("medium", "high") for e in sonnet.values()):
            raise ValueError("Sonnet 5.5 standing effort must be medium or high")
    return WorkerRoles(tuple((r, profiles[r]) for r in roles), *limits,
                       tuple((r, efforts[r]) for r in roles if r in efforts),
                       tuple((r, sonnet[r]) for r in roles if r in sonnet))


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
    if len(declarations) != 1 or declarations[0] not in ("builder", "writing", "documentation", "media"):
        raise ValueError("declare exactly one Worker role: builder|writing|documentation|media")
    if getattr(policy, "worker_roles", None) is None:
        raise ValueError("Worker role declared but role routing is not installed")
    try:
        return for_role(policy, declarations[0])
    except KeyError as exc:
        raise ValueError("declared Worker role is not configured") from exc
