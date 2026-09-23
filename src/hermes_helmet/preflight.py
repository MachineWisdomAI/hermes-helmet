#!/usr/bin/env python3
"""Reusable worker preflight helpers driven by authority policy.

Callers supply the configured Policy and the observed GitHub login (and any
deployment-specific checks). This module never hard-codes adopter identities
and never prints secrets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from hermes_helmet.authority import AuthorityError, Policy, verify_worker_identity


class PreflightError(RuntimeError):
    """A redacted worker-readiness invariant failed."""


class IdentityProbe(Protocol):
    def github_login(self) -> str: ...


@dataclass(frozen=True)
class StaticIdentityProbe:
    login: str

    def github_login(self) -> str:
        return self.login


def check_worker_identity(policy: Policy, probe: IdentityProbe) -> str:
    """Verify the live identity matches the configured worker and not the Captain."""

    try:
        observed = probe.github_login()
    except Exception as exc:  # noqa: BLE001 - probe failures must fail closed
        raise PreflightError("worker identity probe failed") from exc
    try:
        verify_worker_identity(policy, observed)
    except AuthorityError as exc:
        raise PreflightError(str(exc)) from exc
    return policy.github_identity


def assert_worker_ready(
    policy: Policy,
    probe: IdentityProbe,
    *,
    extra_checks: Sequence[str] = (),
) -> tuple[str, ...]:
    """Run the portable identity gate and return the check names that passed."""

    if not policy.github_identity:
        raise PreflightError("policy worker_github_login is missing")
    if policy.version >= 2 and not policy.captain_github_login:
        raise PreflightError("policy captain_github_login is missing")
    check_worker_identity(policy, probe)
    checks = ["github-worker-identity"]
    checks.extend(extra_checks)
    return tuple(checks)
