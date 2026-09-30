#!/usr/bin/env python3
"""Named-request repository enrollment for Hermes Helmet.

When the human Captain explicitly asks the first officer to work on or review
a named repository that is not in the static allowlist, that request is
authorization to enroll that exact repo.  Silence by itself is not consent;
the explicit named-repo delegation is the authority, and an optional
confirmation must not block unattended continuation.

This module provides one deterministic idempotent enrollment seam reused by
the issue, epic, and report-only review workflows:

- ``prepare_repository`` validates the request against the **current** policy
  (Captain identity, allowed owners, and any deliberate enrollment ceilings),
  normalizes a verified Git origin to the exact slug, calls the worker-runtime
  ``prepare-repo`` transport verb, validates the response, and persists a
  sanitized receipt.  Repeats re-run the transport seam so access and clone
  validation are never skipped.
- ``EnrollmentStore`` persists receipts atomically (unique temp file + rename)
  under an advisory store lock.  Recovery removes only torn temp files while
  the lock is held, so it never deletes a live writer's temp file.
- ``resolve_repository`` / ``effective_repositories`` re-validate every stored
  receipt against the current policy before trusting it, so a later owner
  narrowing, Captain change, or explicit enrollment restriction revokes a stale
  receipt instead of re-admitting an excluded repo.

The transport provisions the worker checkout; worker credentials stay out of
the host.  Existing allowlisted repositories remain no-ops.  Read-only status
never enrolls or clones.  Review-only enrollment must not dispatch issues or
confer merge permission.  Enrollment never widens authority to forks or
dependents implicitly.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Iterator, Protocol, Sequence
import uuid

try:  # POSIX advisory locking.
    import fcntl
except ImportError:  # pragma: no cover - platforms without fcntl
    fcntl = None  # type: ignore[assignment]

from hermes_helmet.authority import (
    AuthorityError,
    Policy,
    REPOSITORY_RE,
    Repository,
    _validate_worktree,
    verify_captain_identity,
)
from hermes_helmet.github_issue_poller import ISSUE_URL_RE, PULL_REQUEST_URL_RE


PURPOSE_WORK = "work"
PURPOSE_REVIEW = "review"
ENROLLMENT_PURPOSES = frozenset({PURPOSE_WORK, PURPOSE_REVIEW})
RECEIPT_VERSION = 1
_LOCK_FILENAME = ".lock"

# Canonical credential-free Git origin URL forms (in addition to issue/PR URLs).
_GIT_HTTPS_URL_RE = re.compile(
    r"^https://github\.com/(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$"
)
_GIT_SSH_URL_RE = re.compile(
    r"^ssh://git@github\.com/(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$"
)
_GIT_SCP_RE = re.compile(
    r"^git@github\.com:(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$"
)


class EnrollmentError(RuntimeError):
    """Enrollment validation or transport failure."""


class Runner(Protocol):
    def run(self, command: Sequence[str]) -> str: ...


@dataclass(frozen=True)
class EnrollmentReceipt:
    """Sanitized receipt of one exact named-request enrollment.

    Never contains tokens, credentials, raw transport output, or free-form
    prose.  ``request_ref`` is the sanitized original reference string only;
    the transport itself always receives the normalized exact slug.
    """

    version: int
    slug: str
    purposes: tuple[str, ...]
    worktree: str
    request_ref: str
    actor: str
    authorized_at: str
    created: bool

    def to_public_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "slug": self.slug,
            "purposes": list(self.purposes),
            "worktree": self.worktree,
            "request_ref": self.request_ref,
            "actor": self.actor,
            "authorized_at": self.authorized_at,
            "created": self.created,
        }


@dataclass(frozen=True)
class EnrollmentResult:
    """Outcome of one ``prepare_repository`` call.

    ``status`` is ``allowlisted`` (static no-op), ``reused`` (an existing
    receipt already authorized this purpose under current policy; the transport
    seam still re-validated access/clone), or ``enrolled`` (new authority was
    granted and a receipt persisted).  The public dict always carries the
    transport ABI fields ``slug`` / ``worktree`` / ``purpose`` / ``created``.
    """

    status: str
    purpose: str
    receipt: EnrollmentReceipt

    def to_public_dict(self) -> dict[str, object]:
        payload = self.receipt.to_public_dict()
        payload["status"] = self.status
        payload["purpose"] = self.purpose
        return payload


def _note(code: str, *parts: object, max_len: int = 160) -> str:
    """Structured error code (no free-form prose, no secrets)."""
    tokens = [re.sub(r"[^A-Za-z0-9_.+:/-]+", "", str(code).strip()) or "note"]
    for part in parts:
        if part is None:
            continue
        token = re.sub(r"[^A-Za-z0-9_.+:/-]+", "", str(part).strip())
        if token:
            tokens.append(token[:64])
    text = ":".join(tokens)
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _default_enrollment_dir() -> Path:
    value = os.environ.get("HERMES_HELMET_ENROLLMENT_DIR")
    return Path(value) if value else Path.home() / ".hermes-helmet" / "enrollments"


# ---------------------------------------------------------------------------
# Request-ref parsing
# ---------------------------------------------------------------------------


def parse_repo_ref(ref: str) -> str:
    """Resolve a named-request reference to an exact ``owner/name`` slug.

    Accepts an ``owner/name`` slug, a canonical GitHub issue/PR URL, or a
    credential-free Git origin URL (``https://github.com/owner/repo(.git)``,
    ``ssh://git@github.com/owner/repo(.git)``, ``git@github.com:owner/repo(.git)``).
    Every accepted form normalizes to the exact slug; the transport contract
    only ever receives that slug (or a canonical issue/PR reference), never an
    arbitrary origin syntax.

    Rejects empty refs, control characters, credential-bearing URLs,
    non-canonical URL forms (queries, fragments, non-HTTPS/SSH schemes,
    non-GitHub hosts), and prose that merely mentions a repository.
    """

    text = (ref or "").strip()
    if not text:
        raise EnrollmentError(_note("err", "enroll_ref_empty"))
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in text):
        raise EnrollmentError(_note("err", "enroll_ref_control_chars"))

    # Bare owner/name slug.
    if REPOSITORY_RE.fullmatch(text):
        return text

    # URL forms.
    if "://" in text:
        scheme = text.split("://", 1)[0].strip().casefold()
        if scheme not in {"https", "ssh"}:
            raise EnrollmentError(_note("err", "enroll_ref_scheme_not_allowed"))
        trimmed = text.rstrip("/")
        for regex in (
            ISSUE_URL_RE,
            PULL_REQUEST_URL_RE,
            _GIT_HTTPS_URL_RE,
            _GIT_SSH_URL_RE,
        ):
            match = regex.fullmatch(trimmed)
            if match is not None:
                return match.group("slug")
        # https://user:token@github.com/… and similar fall through here.
        if "@" in trimmed:
            raise EnrollmentError(_note("err", "enroll_ref_credentials"))
        raise EnrollmentError(_note("err", "enroll_ref_not_canonical"))

    # SCP-like SSH origin (git@github.com:owner/repo.git).
    if "@" in text:
        match = _GIT_SCP_RE.fullmatch(text)
        if match is not None:
            return match.group("slug")
        raise EnrollmentError(_note("err", "enroll_ref_credentials"))

    raise EnrollmentError(_note("err", "enroll_ref_invalid"))


# ---------------------------------------------------------------------------
# Enrollment receipt persistence
# ---------------------------------------------------------------------------


def _parse_receipt(raw: object) -> EnrollmentReceipt | None:
    """Parse a receipt dict; return None on any structural problem."""
    if not isinstance(raw, dict):
        return None
    try:
        version = int(raw["version"])
        if version != RECEIPT_VERSION:
            return None
        slug = str(raw["slug"]).strip()
        if not REPOSITORY_RE.fullmatch(slug):
            return None
        purposes_raw = raw["purposes"]
        if not isinstance(purposes_raw, list) or not purposes_raw:
            return None
        purposes = tuple(str(p).strip() for p in purposes_raw)
        if any(p not in ENROLLMENT_PURPOSES for p in purposes):
            return None
        worktree = str(raw["worktree"]).strip()
        if not worktree:
            return None
        request_ref = str(raw["request_ref"]).strip()[:500]
        actor = str(raw["actor"]).strip()
        if not actor:
            return None
        authorized_at = str(raw["authorized_at"]).strip()
        created = bool(raw["created"])
    except (KeyError, TypeError, ValueError):
        return None
    return EnrollmentReceipt(
        version=version,
        slug=slug,
        purposes=purposes,
        worktree=worktree,
        request_ref=request_ref,
        actor=actor,
        authorized_at=authorized_at,
        created=created,
    )


class EnrollmentStore:
    """Atomic idempotent enrollment receipt persistence with recovery.

    Receipts are stored as one JSON file per repository slug under
    ``directory``.  Writes use a unique temp-file-then-rename pattern guarded
    by an advisory store lock, so a repeat or concurrent enrollment converges
    to one receipt and a torn temp file can never be mistaken for a live
    write.  Corrupt or torn files are treated as absent so re-enrollment
    repairs them.
    """

    def __init__(self, directory: Path | str | None = None) -> None:
        self.directory = (
            Path(directory) if directory is not None else _default_enrollment_dir()
        )

    def path_for(self, slug: str) -> Path:
        text = (slug or "").strip()
        if not REPOSITORY_RE.fullmatch(text):
            raise EnrollmentError(_note("err", "enroll_slug_invalid"))
        return self.directory / (text.casefold().replace("/", "+") + ".json")

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the advisory store lock for load/union/collision/write.

        The lock serializes every mutating enrollment on this store.  Recovery
        and writes performed while the lock is held therefore never race a
        live writer.
        """
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.directory / _LOCK_FILENAME
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self._acquire_fd(fd)
            yield
        finally:
            self._release_fd(fd)
            os.close(fd)

    @staticmethod
    def _acquire_fd(fd: int) -> None:
        if fcntl is None:  # pragma: no cover - non-POSIX platforms
            raise EnrollmentError(_note("err", "enroll_lock_unavailable"))
        fcntl.flock(fd, fcntl.LOCK_EX)

    @staticmethod
    def _release_fd(fd: int) -> None:
        if fcntl is None:  # pragma: no cover - non-POSIX platforms
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass

    def load(self, slug: str) -> EnrollmentReceipt | None:
        """Load receipt for *slug*; returns None if absent, torn, or corrupt."""
        try:
            path = self.path_for(slug)
        except EnrollmentError:
            return None
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return _parse_receipt(raw)

    def save(self, receipt: EnrollmentReceipt) -> Path:
        """Persist receipt atomically under the store lock."""
        with self.locked():
            return self._save_locked(receipt)

    def _save_locked(self, receipt: EnrollmentReceipt) -> Path:
        """Persist receipt; caller must already hold :meth:`locked`."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.path_for(receipt.slug)
        unique = f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        tmp = path.with_name(unique)
        try:
            tmp.write_text(
                json.dumps(receipt.to_public_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        return path

    def recover(self) -> int:
        """Remove torn temp files left by interrupted writes; return count.

        Runs under the store lock, so a live writer's temp file is never
        removed.
        """
        with self.locked():
            return self._recover_locked()

    def _recover_locked(self) -> int:
        if not self.directory.is_dir():
            return 0
        removed = 0
        for tmp in self.directory.glob("*.json.*.tmp"):
            try:
                tmp.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    def all(self) -> list[EnrollmentReceipt]:
        """Load all valid receipts (skips corrupt/torn files)."""
        if not self.directory.is_dir():
            return []
        receipts: list[EnrollmentReceipt] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            receipt = _parse_receipt(raw)
            if receipt is not None:
                receipts.append(receipt)
        return receipts


# ---------------------------------------------------------------------------
# Lookup helpers (re-validate against current authority)
# ---------------------------------------------------------------------------


def authorized_purposes(policy: Policy, receipt: EnrollmentReceipt) -> tuple[str, ...]:
    """Purposes from *receipt* still granted by the **current** policy.

    A stored receipt is global and may outlive the authority that created it.
    It grants nothing unless enrollment is still enabled, the slug's owner is
    still allowed, the recorded actor is still the configured Captain, and each
    purpose is still permitted.  An empty result means the receipt is stale and
    must not admit the repository.
    """
    enrollment = policy.enrollment
    if not enrollment.enabled:
        return ()
    owner = receipt.slug.split("/", 1)[0].casefold()
    allowed_owners = {o.casefold() for o in policy.github_owners}
    if not allowed_owners or owner not in allowed_owners:
        return ()
    captain = (policy.captain_github_login or "").strip()
    if not captain or receipt.actor.casefold() != captain.casefold():
        return ()
    return tuple(p for p in receipt.purposes if p in enrollment.allowed_purposes)


def effective_purpose(purposes: Sequence[str]) -> str:
    """Highest purpose granted: work outranks review."""
    return PURPOSE_WORK if PURPOSE_WORK in purposes else PURPOSE_REVIEW


def resolve_repository(
    policy: Policy,
    store: EnrollmentStore | None,
    slug: str,
) -> tuple[Repository | None, str]:
    """Return ``(Repository, enrollment_purpose)`` for allowlisted/enrolled slug.

    Allowlisted repositories always win and carry an empty purpose (full
    authority).  Enrolled repositories carry their effective purpose so callers
    can enforce review-only ceilings.  A stored receipt is only honored if it
    is still authorized by the current policy; otherwise the slug is unknown.
    """
    for repository in policy.repositories:
        if repository.slug.casefold() == slug.casefold():
            return repository, ""
    if store is not None:
        receipt = store.load(slug)
        if receipt is not None:
            purposes = authorized_purposes(policy, receipt)
            if purposes:
                return (
                    Repository(slug=receipt.slug, worktree=Path(receipt.worktree)),
                    effective_purpose(purposes),
                )
    return None, ""


def effective_repositories(
    policy: Policy,
    store: EnrollmentStore | None,
) -> tuple[Repository, ...]:
    """Policy allowlist plus currently-authorized enrolled repositories."""
    repositories = list(policy.repositories)
    if store is None:
        return tuple(repositories)
    known = {repository.slug.casefold() for repository in repositories}
    for receipt in store.all():
        if receipt.slug.casefold() in known:
            continue
        if not authorized_purposes(policy, receipt):
            continue
        repositories.append(
            Repository(slug=receipt.slug, worktree=Path(receipt.worktree))
        )
        known.add(receipt.slug.casefold())
    return tuple(repositories)


# ---------------------------------------------------------------------------
# Main enrollment function
# ---------------------------------------------------------------------------


def _contains_symlink_component(path: Path) -> bool:
    """Check whether any existing component of *path* is a symlink."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def prepare_repository(
    policy: Policy,
    request_ref: str,
    *,
    purpose: str,
    runner: Runner,
    worker_runtime: Sequence[str],
    actor_login: str,
    store: EnrollmentStore | None = None,
) -> EnrollmentResult:
    """Enroll a repository via the worker-runtime transport.

    Deterministic and idempotent: allowlisted repos are no-ops, and every
    non-allowlisted request re-runs the transport seam under the store lock so
    current access and clone validation are never skipped on a repeat.  The
    transport receives the normalized exact slug; the sanitized original
    reference is retained only in the audit receipt.  Enrollment ceilings
    (disabled/narrowed enrollment, owner limits) govern genuinely new
    enrollments only; the static allowlist no-op is unaffected by them.
    Fails closed on malformed/credential-bearing refs, owner mismatch,
    disabled or narrowed enrollment for new repositories, Captain/worker
    identity violations, missing worker runtime, transport failure,
    slug/purpose mismatch, unsafe or symlinked worktrees, and path
    collisions.
    """

    if purpose not in ENROLLMENT_PURPOSES:
        raise EnrollmentError(_note("err", "enroll_purpose_invalid"))
    slug = parse_repo_ref(request_ref)
    sanitized_ref = (request_ref or "").strip()[:500]
    # Transport ABI accepts the exact slug (or canonical issue/PR reference);
    # a verified Git origin is normalized to the slug, never forwarded raw.
    transport_ref = slug

    # Only the configured Captain may authorize operational enrollment.
    try:
        verify_captain_identity(policy, actor_login)
    except AuthorityError as exc:
        raise EnrollmentError(
            _note("err", "enroll_captain_identity_mismatch")
        ) from exc

    # Existing allowlisted repositories remain no-ops: the static allowlist
    # was granted deliberately, so enrollment ceilings (disabled/narrowed
    # enrollment, owner limits) govern new enrollments only and never block
    # the documented no-op for a repository the policy already carries.
    for repository in policy.repositories:
        if repository.slug.casefold() == slug.casefold():
            return EnrollmentResult(
                status="allowlisted",
                purpose=purpose,
                receipt=EnrollmentReceipt(
                    version=RECEIPT_VERSION,
                    slug=repository.slug,
                    purposes=(purpose,),
                    worktree=str(repository.worktree),
                    request_ref=sanitized_ref,
                    actor=actor_login.strip(),
                    authorized_at=_utc_now(),
                    created=False,
                ),
            )

    # Honor deliberate enrollment ceilings for genuinely new repositories;
    # never add owners.
    enrollment = policy.enrollment
    if not enrollment.enabled:
        raise EnrollmentError(_note("err", "enroll_disabled"))
    if purpose not in enrollment.allowed_purposes:
        raise EnrollmentError(_note("err", "enroll_purpose_not_allowed"))
    owner = slug.split("/", 1)[0]
    allowed_owners = {o.casefold() for o in policy.github_owners}
    if not allowed_owners or owner.casefold() not in allowed_owners:
        raise EnrollmentError(_note("err", "enroll_owner_not_allowed"))

    runtime = tuple(str(part) for part in worker_runtime if str(part).strip())
    if not runtime:
        raise EnrollmentError(_note("err", "worker_runtime_unset"))

    store = store or EnrollmentStore()
    with store.locked():
        store._recover_locked()
        existing = store.load(slug)
        existing_purposes = (
            authorized_purposes(policy, existing) if existing is not None else ()
        )
        already_authorized = purpose in existing_purposes

        # Always run the transport seam, including repeats, so current access
        # and clone validation are re-confirmed rather than skipped.
        try:
            output = runner.run(
                [*runtime, "prepare-repo", transport_ref, "--purpose", purpose, "--json"]
            )
        except Exception as exc:  # noqa: BLE001 — transport failure is one typed error
            raise EnrollmentError(_note("err", "enroll_transport_failed")) from exc

        # Parse and validate transport response.
        try:
            payload = json.loads((output or "").strip())
        except json.JSONDecodeError as exc:
            raise EnrollmentError(_note("err", "enroll_transport_invalid_json")) from exc
        if not isinstance(payload, dict):
            raise EnrollmentError(_note("err", "enroll_transport_invalid_json"))

        transport_slug = payload.get("slug")
        if not isinstance(transport_slug, str) or not REPOSITORY_RE.fullmatch(
            transport_slug.strip()
        ):
            raise EnrollmentError(_note("err", "enroll_transport_slug_invalid"))
        if transport_slug.strip().casefold() != slug.casefold():
            # Wrong-origin / wrong-repo defense: never accept a substitute.
            raise EnrollmentError(_note("err", "enroll_transport_slug_mismatch"))

        transport_purpose = payload.get("purpose")
        if transport_purpose != purpose:
            raise EnrollmentError(_note("err", "enroll_transport_purpose_mismatch"))

        transport_worktree = payload.get("worktree")
        if not isinstance(transport_worktree, str) or not transport_worktree.strip():
            raise EnrollmentError(_note("err", "enroll_transport_worktree_missing"))

        created = payload.get("created")
        if not isinstance(created, bool):
            raise EnrollmentError(_note("err", "enroll_transport_created_invalid"))

        # Validate worktree path safety.
        raw_worktree = transport_worktree.strip()
        if _contains_symlink_component(Path(raw_worktree)):
            raise EnrollmentError(_note("err", "enroll_worktree_symlink"))
        try:
            worktree_path = _validate_worktree("enrollment.worktree", raw_worktree)
        except AuthorityError as exc:
            raise EnrollmentError(_note("err", "enroll_worktree_unsafe")) from exc

        # Path collision checks.
        normalized = str(worktree_path)
        for repository in policy.repositories:
            if str(repository.worktree) == normalized:
                raise EnrollmentError(_note("err", "enroll_worktree_collision"))
        for other in store.all():
            if other.slug.casefold() != slug.casefold() and other.worktree == normalized:
                raise EnrollmentError(_note("err", "enroll_worktree_collision"))
        if (
            already_authorized
            and existing is not None
            and existing.worktree != normalized
        ):
            raise EnrollmentError(_note("err", "enroll_worktree_changed"))

        # Build and persist receipt under current authority.
        purposes = tuple(sorted(set(existing_purposes) | {purpose}))
        receipt = EnrollmentReceipt(
            version=RECEIPT_VERSION,
            slug=slug,
            purposes=purposes,
            worktree=normalized,
            request_ref=sanitized_ref,
            actor=actor_login.strip(),
            authorized_at=_utc_now(),
            created=created,
        )
        store._save_locked(receipt)
        status = "reused" if already_authorized else "enrolled"
        return EnrollmentResult(status=status, purpose=purpose, receipt=receipt)
