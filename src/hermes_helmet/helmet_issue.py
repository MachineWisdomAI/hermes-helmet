#!/usr/bin/env python3
"""Captain-side single-issue orchestration helpers for helmet-issue.

This module owns the resumable state model, non-secret checkpoint, discovery of
existing root tasks / pull requests, merge-gate decisions, and read-only status.
It deliberately does **not**:

- create a second watcher, webhook, repair relay, or queue;
- write the worker checkout;
- invent repair Kanban tasks (the H1 poller owns trusted-review reaction);
- merge by default or blind-retry a failed merge.

The portable skill drives agent review; this package supplies deterministic
authority and GitHub/ledger/Kanban integration points the skill must call.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Protocol, Sequence
from urllib.parse import urlencode

from hermes_helmet.authority import (
    AuthorityError,
    LOCAL_ONLY_COMPLETION_CONTRACT,
    Policy,
    Repository,
    load_authority,
    merge_authority_for,
    unattended_merge_allowed,
    verify_captain_identity,
)
from hermes_helmet.github_issue_poller import (
    ISSUE_URL_RE,
    PULL_REQUEST_URL_RE,
    PollerError,
    _pull_request_url,
    create_task_once,
    load_policy as load_poller_policy,
)


def _env_path(key: str, default: str) -> Path:
    value = os.environ.get(key)
    return Path(value) if value else Path(default)


def _resolve_command(env_key: str, binary: str) -> str:
    """Captain-side binary resolution: env override, then PATH, then bare name."""

    override = os.environ.get(env_key)
    if override:
        return override
    found = shutil.which(binary)
    if found:
        return found
    return binary


# Captain defaults prefer PATH / home so host Codex works without container paths.
# Worker poller keeps its own container defaults separately.
DEFAULT_GH = _resolve_command("HERMES_HELMET_GH", "gh")
DEFAULT_HERMES = _resolve_command("HERMES_HELMET_HERMES", "hermes")
DEFAULT_LEDGER = _env_path(
    "HERMES_HELMET_LEDGER",
    str(Path.home() / ".hermes-helmet" / "ledger.sqlite3"),
)
DEFAULT_CONFIG = _env_path(
    "HERMES_HELMET_CONFIG",
    str(Path.home() / ".hermes-helmet" / "policy.json"),
)
DEFAULT_CHECKPOINT_DIR = _env_path(
    "HERMES_HELMET_CHECKPOINT_DIR",
    str(Path.home() / ".hermes-helmet" / "checkpoints"),
)


def _resolve_worker_runtime() -> tuple[str, ...]:
    """Optional Captain→worker transport command prefix (generic seam).

    ``HERMES_HELMET_WORKER_RUNTIME`` is a shell-like argv string (split on
    whitespace) invoked as ``<runtime> <verb> …``. Verbs used by this package:

    - ``ledger-root ISSUE_URL`` → prints root task id or empty
    - ``ledger-watch ISSUE_URL`` → prints JSON watch object or empty
    - ``dispatch-root ISSUE_URL --json`` → prints ``{"task_id":…,"created":bool}``
    - ``wait ISSUE_URL --timeout-seconds N --json [--cursor TOKEN]`` → blocks
      outside the agent loop and prints one bounded wake result

    MachineWisdom-specific adapters and live dogfood stay in WisdomHelm; this is
    only the portable configuration seam. Empty means local ledger/Path only.
    """

    raw = (os.environ.get("HERMES_HELMET_WORKER_RUNTIME") or "").strip()
    if not raw:
        return ()
    return tuple(part for part in raw.split() if part)


DEFAULT_WORKER_RUNTIME: tuple[str, ...] = _resolve_worker_runtime()
CHECKPOINT_VERSION = 1
STATES = (
    "PREFLIGHT",
    "DISPATCH",
    "WAIT_PR",
    "REVIEW_HEAD",
    "REQUEST_REPAIR",
    "WAIT_REPAIR",
    "READY",
    "MERGE_GATE",
    "MERGE",
    "VERIFY_MERGED",
    "DONE",
    "BLOCKED",
    "FAILED",
)
TERMINAL_STATES = frozenset({"DONE", "BLOCKED", "FAILED"})
ACTIVE_REPAIR_STATUSES = frozenset(
    {"todo", "ready", "running", "blocked", "triage", "in_progress"}
)
TERMINAL_TASK_STATUSES = frozenset({"done", "archived", "complete", "completed"})
# Well-known secret *prefixes* only — never treat free-form prose as safe.
_SECRET_SHAPED_RE = re.compile(
    r"(ghp_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9]+|xai-[A-Za-z0-9]+|"
    r"glpat-[A-Za-z0-9_-]+|AKIA[0-9A-Z]{16})",
    re.IGNORECASE,
)
# KEY=value / KEY: value assignments for credential-shaped names (any value).
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z][A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|"
    r"CREDENTIAL|PRIVATE[_-]?KEY|ACCESS[_-]?KEY|AUTH)[A-Z0-9_]*)\s*[=:]\s*\S+"
)
# GitHub closing keywords (docs: optional colon; #N, owner/repo#N, or full URL).
_CLOSING_KEYWORD_RE = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
_CLOSING_ISSUE_RE = re.compile(
    rf"(?i)\b{_CLOSING_KEYWORD_RE}\s*:?\s*"
    r"(?:"
    r"#(?P<bare>\d+)"
    r"|(?P<slug_q>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<qualified>\d+)"
    r"|https://github\.com/(?P<slug_u>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"/issues/(?P<url_num>\d+)"
    r")(?!\d)"
)
_NEGATIVE_MERGEABLE_STATES = frozenset(
    {"dirty", "blocked", "unstable", "behind", "draft"}
)


class HelmetIssueError(RuntimeError):
    """helmet-issue cannot safely continue."""


class Runner(Protocol):
    def run(self, command: Sequence[str]) -> str: ...


def sanitize_public_text(text: str, *, max_len: int = 240) -> str:
    """Strip secret-shaped tokens/assignments and cap length for public notes.

    Checkpoint persistence must not rely on this alone for free-form command
    output or review prose — prefer :func:`checkpoint_note` codes instead.
    """

    cleaned = _SECRET_ASSIGNMENT_RE.sub(r"\1=[redacted]", text or "")
    cleaned = _SECRET_SHAPED_RE.sub("[redacted]", cleaned)
    cleaned = cleaned.replace("\x00", "").replace("\r", " ").strip()
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    if len(cleaned) > max_len:
        return cleaned[: max_len - 3] + "..."
    return cleaned


def checkpoint_note(code: str, *parts: object, max_len: int = 160) -> str:
    """Build a fixed structured checkpoint/status note (no free-form prose)."""

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


def issue_number_mentioned(text: str, number: int) -> bool:
    """True when #N appears as an exact issue reference (not a prefix of #N0)."""

    pattern = re.compile(rf"(?<!\d)#{number}(?!\d)")
    return pattern.search(text or "") is not None


def issue_url_mentioned(text: str, issue_url: str) -> bool:
    """True when the full issue URL appears as an exact reference (not a prefix).

    ``…/issues/2`` must not match inside ``…/issues/20``.
    """

    url = (issue_url or "").strip().rstrip("/")
    if not url or not ISSUE_URL_RE.fullmatch(url):
        return False
    # Require that a longer digit run does not extend the issue number.
    bare = re.compile(re.escape(url) + r"(?!\d)", re.IGNORECASE)
    return bare.search(text or "") is not None


def closing_references(text: str, *, repository_slug: str | None = None) -> frozenset[int]:
    """Issue numbers targeted by GitHub closing keywords in *text*.

    Bare ``#N`` forms always contribute. Repository-qualified short refs and
    full issue URLs contribute only when they match *repository_slug* (when
    provided) or when *repository_slug* is omitted (all repos).
    """

    found: set[int] = set()
    slug_cf = (repository_slug or "").casefold()
    for match in _CLOSING_ISSUE_RE.finditer(text or ""):
        bare = match.group("bare")
        if bare is not None:
            found.add(int(bare))
            continue
        qualified = match.group("qualified")
        if qualified is not None:
            ref_slug = (match.group("slug_q") or "").casefold()
            if repository_slug is None or ref_slug == slug_cf:
                found.add(int(qualified))
            continue
        url_num = match.group("url_num")
        if url_num is not None:
            ref_slug = (match.group("slug_u") or "").casefold()
            if repository_slug is None or ref_slug == slug_cf:
                found.add(int(url_num))
    return frozenset(found)


def body_closes_issue(text: str, *, repository_slug: str, number: int) -> bool:
    """True when *text* has a closing keyword aimed at this repository issue."""

    return number in closing_references(text, repository_slug=repository_slug)


def pull_associates_with_issue(issue: IssueRef, pull: PullRequestRef) -> bool:
    """True when the PR is an implementation of *this* issue.

    Discovery leads (timeline cross-references, bare ``#N`` mentions, ordinary
    body/Markdown issue links) are not fulfillment evidence. Association
    requires same-repo ownership plus one of:

    - canonical issue branch head ref
    - closing keyword association (``Closes #N``, ``Closes: #N``,
      ``Fixes owner/repo#N``, ``Fixes https://github.com/owner/repo/issues/N``, …)
    """

    if pull.repository_slug.casefold() != issue.repository.slug.casefold():
        return False
    if pull.head_ref == issue.branch:
        return True
    body = pull.body or ""
    return body_closes_issue(
        body, repository_slug=issue.repository.slug, number=issue.number
    )


def parse_worker_runtime(value: str | Sequence[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part for part in value.split() if part)
    return tuple(str(part) for part in value if str(part).strip())


_HTTP_STATUS_PAREN_RE = re.compile(r"\(HTTP\s+(\d{3})\)", re.IGNORECASE)
_HTTP_STATUS_JSON_RE = re.compile(r'"status"\s*:\s*"?(\d{3})"?', re.IGNORECASE)


def _extract_http_status_code(*blobs: str | None) -> str | None:
    """Return a sanitized HTTP status token from gh output, or None.

    Only the numeric status is retained — never message bodies or headers.
    """

    for blob in blobs:
        text = blob or ""
        match = _HTTP_STATUS_PAREN_RE.search(text) or _HTTP_STATUS_JSON_RE.search(text)
        if match is not None:
            return match.group(1)
    return None


class SubprocessRunner:
    def __init__(self, *, gh: str = DEFAULT_GH, hermes: str = DEFAULT_HERMES) -> None:
        self.gh = gh
        self.hermes = hermes

    def run(self, command: Sequence[str]) -> str:
        import subprocess

        completed = subprocess.run(
            list(command), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if completed.returncode:
            # Never carry raw stdout/stderr into exceptions → blockers/checkpoints.
            # Preserve a typed HTTP status so callers can distinguish 404 (unsupported)
            # from 401/403 (denied) without reading secret-bearing bodies.
            name = Path(command[0]).name if command else "command"
            http = _extract_http_status_code(completed.stderr, completed.stdout)
            if http is not None:
                raise HelmetIssueError(
                    checkpoint_note(
                        "err",
                        "command_failed",
                        name,
                        f"exit{completed.returncode}",
                        f"http{http}",
                    )
                )
            raise HelmetIssueError(
                checkpoint_note("err", "command_failed", name, f"exit{completed.returncode}")
            )
        return completed.stdout


@dataclass(frozen=True)
class IssueRef:
    repository: Repository
    number: int
    title: str
    body: str
    state: str
    labels: tuple[str, ...]
    html_url: str

    @property
    def url(self) -> str:
        return self.html_url

    @property
    def branch(self) -> str:
        name = self.repository.slug.rsplit("/", 1)[1].lower()
        return f"automation/{name}-{self.number}"


@dataclass(frozen=True)
class PullRequestRef:
    repository_slug: str
    number: int
    url: str
    head_sha: str
    head_ref: str
    base_ref: str
    author_login: str
    state: str
    merged: bool
    mergeable_state: str
    draft: bool
    issue_urls: tuple[str, ...] = ()
    # PR body retained for association checks (closing keywords only).
    body: str = ""


@dataclass
class Checkpoint:
    """Non-secret resumable state keyed by issue URL."""

    version: int
    issue_url: str
    state: str
    root_task_id: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    reviewed_head: str | None = None
    clean_head: str | None = None
    pending_repair: bool = False
    repair_rounds: int = 0
    merge_mode: str = "explicit_captain_approval"
    merge_attempted_head: str | None = None
    last_blocker: str | None = None
    identical_blocker_count: int = 0
    started_at: str = ""
    updated_at: str = ""
    notes: list[str] = field(default_factory=list)
    host_continuation: str = "unknown"
    one_pass_only: bool = False
    # Validated epic root URL when this child is driven under helmet-epic.
    # Used to re-read live parent merge authority on resume without epic_body.
    parent_epic_url: str | None = None

    def to_public_dict(self) -> dict[str, object]:
        payload = asdict(self)
        # Never allow accidental secret-shaped keys.
        return payload


@dataclass(frozen=True)
class StatusReport:
    issue_url: str
    state: str
    root_task_id: str | None
    pr_url: str | None
    reviewed_head: str | None
    clean_head: str | None
    repair_state: str
    merge_gate: str
    blocker: str | None
    terminal: bool
    details: dict[str, object] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "issue_url": self.issue_url,
            "state": self.state,
            "root_task_id": self.root_task_id,
            "pr_url": self.pr_url,
            "reviewed_head": self.reviewed_head,
            "clean_head": self.clean_head,
            "repair_state": self.repair_state,
            "merge_gate": self.merge_gate,
            "blocker": self.blocker,
            "terminal": self.terminal,
            "details": self.details,
        }


@dataclass(frozen=True)
class WaitReport:
    """A secret-free result from one deployment-side blocking wait."""

    issue_url: str
    outcome: str
    reason: str
    cursor: str

    def to_public_dict(self) -> dict[str, str]:
        return asdict(self)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def checkpoint_path(checkpoint_dir: Path, issue_url: str) -> Path:
    digest = hashlib.sha256(issue_url.encode("utf-8")).hexdigest()[:24]
    match = ISSUE_URL_RE.fullmatch(issue_url)
    if match is None:
        stem = f"issue-{digest}"
    else:
        owner, name = match.group("slug").split("/", 1)
        stem = f"{owner}__{name}__{match.group('number')}__{digest[:8]}"
    return checkpoint_dir / f"{stem}.json"


def load_checkpoint(
    path: Path,
    *,
    expected_issue_url: str | None = None,
) -> Checkpoint | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HelmetIssueError(f"checkpoint is unreadable: {path}") from exc
    if not isinstance(raw, dict):
        raise HelmetIssueError("checkpoint must be a JSON object")
    try:
        notes = raw.get("notes") or []
        if not isinstance(notes, list):
            raise TypeError("notes")
        version = int(raw["version"])
        if version != CHECKPOINT_VERSION:
            raise HelmetIssueError(f"unsupported checkpoint version: {version}")
        issue_url = str(raw["issue_url"]).strip()
        if not ISSUE_URL_RE.fullmatch(issue_url):
            raise HelmetIssueError("checkpoint issue_url is malformed")
        if expected_issue_url is not None and issue_url != expected_issue_url.strip():
            raise HelmetIssueError("checkpoint issue_url does not match requested issue")
        state = str(raw["state"])
        if state not in STATES and state != "UNKNOWN":
            raise HelmetIssueError(f"checkpoint state is unknown: {state}")
        repair_rounds = int(raw.get("repair_rounds", 0))
        identical_blocker_count = int(raw.get("identical_blocker_count", 0))
        if repair_rounds < 0 or identical_blocker_count < 0:
            raise HelmetIssueError("checkpoint counters must be non-negative")
        pr_number = raw.get("pr_number")
        if pr_number is not None:
            pr_number = int(pr_number)
            if pr_number < 1:
                raise HelmetIssueError("checkpoint pr_number must be positive")
        return Checkpoint(
            version=version,
            issue_url=issue_url,
            state=state,
            root_task_id=raw.get("root_task_id") if raw.get("root_task_id") is None else str(raw.get("root_task_id")),
            pr_url=raw.get("pr_url") if raw.get("pr_url") is None else str(raw.get("pr_url")),
            pr_number=pr_number,
            reviewed_head=raw.get("reviewed_head") if raw.get("reviewed_head") is None else str(raw.get("reviewed_head")),
            clean_head=raw.get("clean_head") if raw.get("clean_head") is None else str(raw.get("clean_head")),
            pending_repair=bool(raw.get("pending_repair", False)),
            repair_rounds=repair_rounds,
            merge_mode=str(raw.get("merge_mode", "explicit_captain_approval")),
            merge_attempted_head=(
                None
                if raw.get("merge_attempted_head") is None
                else str(raw.get("merge_attempted_head"))
            ),
            last_blocker=(
                None if raw.get("last_blocker") is None else sanitize_public_text(str(raw.get("last_blocker")))
            ),
            identical_blocker_count=identical_blocker_count,
            started_at=str(raw.get("started_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            notes=[sanitize_public_text(str(item), max_len=400) for item in notes],
            host_continuation=str(raw.get("host_continuation") or "unknown"),
            one_pass_only=bool(raw.get("one_pass_only", False)),
            parent_epic_url=(
                None
                if raw.get("parent_epic_url") is None
                else str(raw.get("parent_epic_url")).strip() or None
            ),
        )
    except HelmetIssueError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise HelmetIssueError("checkpoint is missing required fields") from exc


def save_checkpoint(path: Path, checkpoint: Checkpoint) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if checkpoint.version != CHECKPOINT_VERSION:
        raise HelmetIssueError(f"refusing to persist unsupported checkpoint version: {checkpoint.version}")
    if checkpoint.state not in STATES and checkpoint.state != "UNKNOWN":
        raise HelmetIssueError(f"refusing to persist unknown checkpoint state: {checkpoint.state}")
    if checkpoint.repair_rounds < 0 or checkpoint.identical_blocker_count < 0:
        raise HelmetIssueError("refusing to persist negative checkpoint counters")
    checkpoint.last_blocker = (
        None if checkpoint.last_blocker is None else sanitize_public_text(str(checkpoint.last_blocker))
    )
    checkpoint.notes = [sanitize_public_text(str(item), max_len=400) for item in checkpoint.notes]
    prior_on_disk: Checkpoint | None = None
    if path.is_file():
        try:
            prior_on_disk = load_checkpoint(path, expected_issue_url=checkpoint.issue_url)
        except HelmetIssueError:
            prior_on_disk = None
    preserved = _preserved_legacy_updated_at(prior_on_disk, checkpoint)
    checkpoint.updated_at = preserved if preserved is not None else _utc_now()
    if not checkpoint.started_at:
        checkpoint.started_at = checkpoint.updated_at
    text = json.dumps(checkpoint.to_public_dict(), indent=2, sort_keys=True) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def parse_issue_url(issue_url: str) -> tuple[str, int]:
    match = ISSUE_URL_RE.fullmatch((issue_url or "").strip())
    if match is None:
        raise HelmetIssueError("issue URL must be https://github.com/owner/repo/issues/N")
    return match.group("slug"), int(match.group("number"))


def _github_json(runner: Runner, gh: str, endpoint: str, *extra: str) -> object:
    command = [gh, "api", *extra, endpoint]
    try:
        output = runner.run(command)
    except HelmetIssueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HelmetIssueError(f"GitHub API failed for {endpoint}") from exc
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise HelmetIssueError(f"GitHub API returned invalid JSON for {endpoint}") from exc


def _normalize_issue_url_strict(text: str) -> str:
    slug, number = parse_issue_url(text)
    return f"https://github.com/{slug}/issues/{number}"


def _child_parent_urls(
    *,
    child_body: str,
    child_repository_slug: str,
) -> tuple[str, ...]:
    """Return normalized Parent URLs for membership checks (lazy epic import)."""

    # Local import avoids import cycle: helmet_epic imports helmet_issue.
    from hermes_helmet.helmet_epic import parse_parent_links

    parents = parse_parent_links(
        child_body,
        repository_slug=child_repository_slug,
        reject_malformed=True,
    )
    return tuple(_normalize_issue_url_strict(url) for url in parents)


def resolve_parent_epic_body(
    policy: Policy,
    *,
    epic_body: str | None,
    parent_epic_url: str | None,
    runner: Runner | None,
    gh: str = DEFAULT_GH,
    child_issue: IssueRef | None = None,
    child_url: str | None = None,
) -> tuple[str | None, str | None]:
    """Return (epic_body, parent_epic_url) with live parent body when needed.

    Live **native** parent association (``GET …/issues/{n}/parent``) is
    authoritative when present. A matching saved/body ``Parent`` link must not
    bypass a different current native parent. When no native parent exists,
    body ``Parent`` is the documented fallback (empty-native plans). Parent-side
    sub-issue membership still confirms native-only children that lack a body
    link. A removed, moved, or contradictory association drops inheritance
    rather than retaining stale merge authority. Caller-provided parent text
    alone is never trusted.
    """

    parent_url = None
    if parent_epic_url:
        text = str(parent_epic_url).strip()
        if text and ISSUE_URL_RE.fullmatch(text.rstrip("/")):
            parent_url = _normalize_issue_url_strict(text)
        else:
            raise HelmetIssueError(checkpoint_note("err", "parent_epic_url_malformed"))

    # Resolve child identity for membership revalidation when inheritance applies.
    child_ref = child_issue
    if child_ref is None and child_url and runner is not None and (
        parent_url is not None or epic_body is not None
    ):
        try:
            child_ref = load_issue(policy, child_url, runner, gh=gh)
        except HelmetIssueError:
            # Cannot confirm membership — fail closed on inherited authority.
            return None, None

    def _native_parent() -> str | None:
        if runner is None or child_ref is None:
            return None
        from hermes_helmet.helmet_epic import (
            HelmetEpicError as _EpicErr,
            native_parent_of,
        )

        try:
            return native_parent_of(child_ref.url, runner, gh=gh)
        except _EpicErr as exc:
            raise HelmetIssueError(
                checkpoint_note("err", "native_parent_membership_unreadable")
            ) from exc

    def _native_member_of(candidate_parent: str) -> bool | None:
        if runner is None or child_ref is None:
            return None
        # Local import avoids import cycle: helmet_epic imports helmet_issue.
        from hermes_helmet.helmet_epic import (
            HelmetEpicError as _EpicErr,
            child_is_native_sub_issue_of,
        )

        try:
            return child_is_native_sub_issue_of(
                candidate_parent, child_ref.url, runner, gh=gh
            )
        except _EpicErr as exc:
            raise HelmetIssueError(
                checkpoint_note("err", "native_parent_membership_unreadable")
            ) from exc

    if child_ref is not None and (parent_url is not None or epic_body is not None):
        # 1) Live native parent wins over saved/body links (including a matching
        # stale body Parent that would otherwise retain obsolete merge authority).
        live_native = _native_parent()
        if live_native is not None:
            # Keep caller epic_body only when it was already bound to this native
            # parent; otherwise re-read live parent body (never trust unbound text).
            if parent_url != live_native:
                epic_body = None
            parent_url = live_native
            # Native authority selected: body multi-Parent ambiguity does not
            # override the live association (and does not grant the old parent).
        else:
            try:
                parents = _child_parent_urls(
                    child_body=child_ref.body,
                    child_repository_slug=child_ref.repository.slug,
                )
            except Exception as exc:  # noqa: BLE001 — epic parse errors fail closed
                raise HelmetIssueError(
                    checkpoint_note("err", "parent_membership_unreadable")
                ) from exc
            if parent_url is None:
                # Explicit epic_body without parent URL: require a sole live Parent
                # that we can attribute the body to; otherwise drop inheritance.
                if len(set(parents)) == 1:
                    parent_url = parents[0]
                elif len(set(parents)) > 1:
                    raise HelmetIssueError(
                        checkpoint_note(
                            "err", "child_ambiguous_parents", child_ref.url
                        )
                    )
                else:
                    return None, None
            elif parent_url not in parents:
                # No native parent + body Parent missing/moved — accept only with
                # parent-side native membership of the claimed parent.
                native_ok = _native_member_of(parent_url)
                if native_ok is True:
                    pass
                elif len(set(parents)) == 1:
                    # Rebind to the sole live body parent (may lack opt-in).
                    parent_url = parents[0]
                    epic_body = None
                else:
                    # Removed, unavailable native, or contradictory: drop.
                    return None, None
            elif len(set(parents)) > 1:
                # Body fallback selected: multi-Parent without native is ambiguous.
                raise HelmetIssueError(
                    checkpoint_note("err", "child_ambiguous_parents", child_ref.url)
                )
            # Matching sole body Parent with no native parent: body fallback OK
            # (empty-native plans). Do not consult parent-side empty membership
            # as a veto — absence of native parent is what enables body fallback.

    if epic_body is not None:
        return epic_body, parent_url
    if parent_url is None or runner is None:
        return None, parent_url
    parent_issue = load_issue(policy, parent_url, runner, gh=gh)
    return parent_issue.body, parent_url


def observed_github_login(runner: Runner, gh: str = DEFAULT_GH) -> str:
    payload = _github_json(runner, gh, "user")
    if not isinstance(payload, dict):
        raise HelmetIssueError("GitHub user probe returned invalid payload")
    login = payload.get("login")
    if not isinstance(login, str) or not login.strip():
        raise HelmetIssueError("GitHub user probe returned no login")
    return login.strip()


def load_issue(policy: Policy, issue_url: str, runner: Runner, *, gh: str = DEFAULT_GH) -> IssueRef:
    slug, number = parse_issue_url(issue_url)
    repository = next((item for item in policy.repositories if item.slug.casefold() == slug.casefold()), None)
    if repository is None:
        raise HelmetIssueError("issue repository is outside the configured allowlist")
    raw = _github_json(runner, gh, f"repos/{repository.slug}/issues/{number}")
    if not isinstance(raw, dict):
        raise HelmetIssueError("GitHub issue payload is invalid")
    if "pull_request" in raw:
        raise HelmetIssueError("URL refers to a pull request, not an issue")
    title = raw.get("title")
    body = raw.get("body") if raw.get("body") is not None else ""
    state = raw.get("state")
    html_url = raw.get("html_url")
    labels_raw = raw.get("labels")
    if (
        not isinstance(title, str)
        or not title.strip()
        or not isinstance(state, str)
        or not isinstance(html_url, str)
        or not ISSUE_URL_RE.fullmatch(html_url.strip())
    ):
        raise HelmetIssueError("GitHub issue payload is malformed")
    if body is not None and not isinstance(body, str):
        raise HelmetIssueError("GitHub issue body is malformed")
    labels: list[str] = []
    if isinstance(labels_raw, list):
        for item in labels_raw:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                labels.append(item["name"])
    return IssueRef(
        repository=repository,
        number=number,
        title=title.strip(),
        body=body or "",
        state=state,
        labels=tuple(labels),
        html_url=html_url.strip(),
    )


def ledger_root_task(ledger: Path, issue_url: str) -> str | None:
    if not ledger.is_file():
        return None
    try:
        connection = sqlite3.connect(ledger, timeout=30)
    except sqlite3.Error as exc:
        raise HelmetIssueError(checkpoint_note("err", "ledger_open_failed")) from exc
    try:
        row = connection.execute(
            "SELECT task_id FROM issue_tasks WHERE issue_url = ?",
            (issue_url,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise HelmetIssueError(checkpoint_note("err", "ledger_root_read_failed")) from exc
    finally:
        connection.close()
    if row is None:
        return None
    return str(row[0])


def ledger_watch(ledger: Path, issue_url: str) -> dict[str, object] | None:
    if not ledger.is_file():
        return None
    try:
        connection = sqlite3.connect(ledger, timeout=30)
    except sqlite3.Error as exc:
        raise HelmetIssueError(checkpoint_note("err", "ledger_open_failed")) from exc
    try:
        row = connection.execute(
            """
            SELECT pull_url, discovery_complete, last_repair_task_id,
                   last_event_at, last_event_kind, last_event_id
            FROM pull_request_watches
            WHERE issue_url = ?
            """,
            (issue_url,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise HelmetIssueError(checkpoint_note("err", "ledger_watch_read_failed")) from exc
    finally:
        connection.close()
    if row is None:
        return None
    last_event_id = row[5]
    if isinstance(last_event_id, bool) or last_event_id is None:
        parsed_event_id: int | None = None
    else:
        try:
            parsed_event_id = int(last_event_id)
        except (TypeError, ValueError):
            parsed_event_id = None
        if parsed_event_id is not None and parsed_event_id < 1:
            parsed_event_id = None
    return {
        "pull_url": str(row[0]) if row[0] is not None else None,
        "discovery_complete": bool(row[1]),
        "last_repair_task_id": str(row[2]) if row[2] is not None else None,
        "last_event_at": str(row[3]) if row[3] is not None else None,
        "last_event_kind": str(row[4]) if row[4] is not None else None,
        "last_event_id": parsed_event_id,
    }


def _worker_runtime_run(
    runner: Runner,
    worker_runtime: Sequence[str],
    *args: str,
) -> str:
    if not worker_runtime:
        raise HelmetIssueError(checkpoint_note("err", "worker_runtime_unset"))
    return runner.run([*worker_runtime, *args])


def resolve_root_task_id(
    ledger: Path,
    issue_url: str,
    runner: Runner,
    *,
    worker_runtime: Sequence[str] = (),
) -> str | None:
    """Authoritative root task: local ledger first, else optional worker runtime."""

    local = ledger_root_task(ledger, issue_url)
    if local:
        return local
    runtime = parse_worker_runtime(worker_runtime)
    if not runtime:
        return None
    output = _worker_runtime_run(runner, runtime, "ledger-root", issue_url).strip()
    if not output:
        return None
    task_id = output.splitlines()[0].strip()
    return task_id or None


def resolve_ledger_watch(
    ledger: Path,
    issue_url: str,
    runner: Runner,
    *,
    worker_runtime: Sequence[str] = (),
) -> dict[str, object] | None:
    local = ledger_watch(ledger, issue_url)
    if local is not None:
        return local
    runtime = parse_worker_runtime(worker_runtime)
    if not runtime:
        return None
    output = _worker_runtime_run(runner, runtime, "ledger-watch", issue_url).strip()
    if not output:
        return None
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise HelmetIssueError(checkpoint_note("err", "worker_runtime_watch_invalid")) from exc
    if not isinstance(payload, dict):
        raise HelmetIssueError(checkpoint_note("err", "worker_runtime_watch_invalid"))
    return payload


def dispatch_root_via_worker_runtime(
    issue: IssueRef,
    runner: Runner,
    *,
    worker_runtime: Sequence[str],
) -> tuple[str, bool]:
    """Create/adopt root on the worker side through the configured runtime seam."""

    runtime = parse_worker_runtime(worker_runtime)
    if not runtime:
        raise HelmetIssueError(checkpoint_note("err", "worker_runtime_unset"))
    output = _worker_runtime_run(
        runner, runtime, "dispatch-root", issue.url, "--json"
    ).strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise HelmetIssueError(checkpoint_note("err", "worker_runtime_dispatch_invalid")) from exc
    if not isinstance(payload, dict):
        raise HelmetIssueError(checkpoint_note("err", "worker_runtime_dispatch_invalid"))
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise HelmetIssueError(checkpoint_note("err", "worker_runtime_dispatch_missing_task"))
    created = bool(payload.get("created", False))
    return task_id.strip(), created


_WAIT_CURSOR_RE = re.compile(r"v1\.[0-9a-f]{64}")
_WAIT_OUTCOMES = frozenset({"changed", "timeout"})
_WAIT_REASONS = frozenset(
    {"task_terminal", "task_attention", "watch_changed", "repair_changed", "timeout"}
)


def wait_for_issue_change(
    issue_url: str,
    runner: Runner,
    *,
    worker_runtime: str | Sequence[str] | None = None,
    timeout_seconds: int = 1800,
    cursor: str = "",
) -> WaitReport:
    """Block once in the worker runtime until meaningful state changes.

    This is an observation seam only. The deployment adapter owns the wait and
    must not mutate Kanban, the H1 ledger, GitHub, or the Captain checkpoint.
    """

    if ISSUE_URL_RE.fullmatch(issue_url or "") is None:
        raise HelmetIssueError(checkpoint_note("err", "issue_url_invalid"))
    if timeout_seconds < 1 or timeout_seconds > 86400:
        raise HelmetIssueError(checkpoint_note("err", "wait_timeout_invalid"))
    if cursor and _WAIT_CURSOR_RE.fullmatch(cursor) is None:
        raise HelmetIssueError(checkpoint_note("err", "wait_cursor_invalid"))
    runtime = parse_worker_runtime(worker_runtime) or DEFAULT_WORKER_RUNTIME
    if not runtime:
        raise HelmetIssueError(checkpoint_note("err", "worker_runtime_unset"))
    command = [
        *runtime,
        "wait",
        issue_url,
        "--timeout-seconds",
        str(timeout_seconds),
        "--json",
    ]
    if cursor:
        command.extend(["--cursor", cursor])
    output = runner.run(command).strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise HelmetIssueError(checkpoint_note("err", "worker_runtime_wait_invalid")) from exc
    if not isinstance(payload, dict):
        raise HelmetIssueError(checkpoint_note("err", "worker_runtime_wait_invalid"))
    outcome = payload.get("outcome")
    reason = payload.get("reason")
    next_cursor = payload.get("cursor")
    if outcome not in _WAIT_OUTCOMES or reason not in _WAIT_REASONS:
        raise HelmetIssueError(checkpoint_note("err", "worker_runtime_wait_invalid"))
    if outcome == "timeout" and reason != "timeout":
        raise HelmetIssueError(checkpoint_note("err", "worker_runtime_wait_invalid"))
    if outcome == "changed" and reason == "timeout":
        raise HelmetIssueError(checkpoint_note("err", "worker_runtime_wait_invalid"))
    if not isinstance(next_cursor, str) or _WAIT_CURSOR_RE.fullmatch(next_cursor) is None:
        raise HelmetIssueError(checkpoint_note("err", "worker_runtime_wait_cursor_invalid"))
    return WaitReport(
        issue_url=issue_url,
        outcome=outcome,
        reason=reason,
        cursor=next_cursor,
    )


def _kanban_show(policy: Policy, task_id: str, runner: Runner, *, hermes: str = DEFAULT_HERMES) -> dict[str, object]:
    output = runner.run(
        [hermes, "kanban", "--board", policy.board, "show", task_id, "--json"]
    )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise HelmetIssueError(checkpoint_note("err", "kanban_show_invalid", task_id)) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("task"), dict):
        raise HelmetIssueError(checkpoint_note("err", "kanban_show_invalid", task_id))
    return payload


def _pr_url_from_kanban(payload: dict[str, object], *, task_id: str) -> str | None:
    try:
        return _pull_request_url(payload, task_id=task_id)
    except PollerError as exc:
        raise HelmetIssueError(
            checkpoint_note("err", "kanban_pr_metadata_disagree", task_id)
        ) from exc


def discover_linked_pull_requests(
    issue: IssueRef,
    runner: Runner,
    *,
    gh: str = DEFAULT_GH,
) -> list[PullRequestRef]:
    """Discover PRs for an issue from GitHub (timeline + open PR scan)."""

    found: dict[int, PullRequestRef] = {}

    # 1) Issue timeline cross-references.
    timeline = _github_json(
        runner,
        gh,
        f"repos/{issue.repository.slug}/issues/{issue.number}/timeline?per_page=100",
        "-H",
        "Accept: application/vnd.github+json",
        "--paginate",
    )
    # paginate may return a list or concatenated pages already decoded as list
    records: list[object]
    if isinstance(timeline, list):
        records = timeline
    else:
        records = []
    pr_numbers: set[int] = set()
    for raw in records:
        if not isinstance(raw, dict):
            continue
        for key in ("source", "data"):
            node = raw.get(key)
            if isinstance(node, dict):
                issue_node = node.get("issue") if key == "source" else node
                if isinstance(issue_node, dict) and "pull_request" in issue_node:
                    number = issue_node.get("number")
                    if isinstance(number, int):
                        pr_numbers.add(number)
        body = raw.get("body")
        if isinstance(body, str):
            for match in PULL_REQUEST_URL_RE.finditer(body):
                if match.group("slug").casefold() == issue.repository.slug.casefold():
                    pr_numbers.add(int(match.group("number")))

    # 2) Open PRs that mention the issue or use the canonical branch.
    query = urlencode({"state": "open", "per_page": "100"})
    open_pulls = _github_json(runner, gh, f"repos/{issue.repository.slug}/pulls?{query}")
    if not isinstance(open_pulls, list):
        open_pulls = []
    for raw in open_pulls:
        if not isinstance(raw, dict):
            continue
        number = raw.get("number")
        body = raw.get("body") or ""
        head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
        head_ref = head.get("ref") if isinstance(head, dict) else None
        mentions = isinstance(body, str) and (
            issue_number_mentioned(body, issue.number)
            or issue_url_mentioned(body, issue.html_url)
            or body_closes_issue(
                body, repository_slug=issue.repository.slug, number=issue.number
            )
        )
        branch_match = isinstance(head_ref, str) and head_ref == issue.branch
        if mentions or branch_match:
            if isinstance(number, int):
                pr_numbers.add(number)

    for number in sorted(pr_numbers):
        ref = load_pull_request(issue.repository.slug, number, runner, gh=gh)
        found[number] = ref
    return [found[key] for key in sorted(found)]


def load_pull_request(
    repository_slug: str,
    number: int,
    runner: Runner,
    *,
    gh: str = DEFAULT_GH,
) -> PullRequestRef:
    raw = _github_json(runner, gh, f"repos/{repository_slug}/pulls/{number}")
    if not isinstance(raw, dict):
        raise HelmetIssueError("GitHub pull request payload is invalid")
    html_url = raw.get("html_url")
    head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
    base = raw.get("base") if isinstance(raw.get("base"), dict) else {}
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    head_sha = head.get("sha") if isinstance(head, dict) else None
    head_ref = head.get("ref") if isinstance(head, dict) else None
    base_ref = base.get("ref") if isinstance(base, dict) else None
    login = user.get("login") if isinstance(user, dict) else None
    state = raw.get("state")
    merged = bool(raw.get("merged"))
    mergeable_state = raw.get("mergeable_state")
    draft = bool(raw.get("draft"))
    body_raw = raw.get("body")
    body = body_raw if isinstance(body_raw, str) else ""
    if (
        not isinstance(html_url, str)
        or not PULL_REQUEST_URL_RE.fullmatch(html_url.strip())
        or not isinstance(head_sha, str)
        or not head_sha.strip()
        or not isinstance(head_ref, str)
        or not isinstance(base_ref, str)
        or not isinstance(login, str)
        or not isinstance(state, str)
    ):
        raise HelmetIssueError("GitHub pull request payload is malformed")
    issue_urls = tuple(
        match.group(0)
        for match in ISSUE_URL_RE.finditer(body)
        if match.group("slug").casefold() == repository_slug.casefold()
    )
    return PullRequestRef(
        repository_slug=repository_slug,
        number=number,
        url=html_url.strip(),
        head_sha=head_sha.strip(),
        head_ref=head_ref,
        base_ref=base_ref,
        author_login=login.strip(),
        state=state,
        merged=merged,
        mergeable_state=str(mergeable_state or ""),
        draft=draft,
        issue_urls=issue_urls,
        body=body,
    )


def select_canonical_pull_request(
    policy: Policy,
    issue: IssueRef,
    candidates: Sequence[PullRequestRef],
    *,
    ledger_pull_url: str | None = None,
) -> PullRequestRef | None:
    """Pick the one canonical worker PR or raise on ambiguity/replacement.

    Candidates that lack repository/issue association are discovery leads only
    and never become the canonical implementation unless the ledger/Kanban
    watch already adopts that exact PR URL.
    """

    associated = [
        item for item in candidates if pull_associates_with_issue(issue, item)
    ]
    # Ledger/Kanban adoption is authoritative ownership even when the live PR
    # body/branch no longer carries a closing keyword (worker delivery lag).
    if ledger_pull_url:
        by_url_all = {item.url: item for item in candidates}
        if ledger_pull_url not in by_url_all:
            # Still allow association-only candidates below if ledger is stale.
            pass
        else:
            adopted = by_url_all[ledger_pull_url]
            if adopted not in associated:
                associated = list(associated) + [adopted]

    if not associated:
        if ledger_pull_url:
            raise HelmetIssueError(
                "ledger records a pull request but live discovery found none"
            )
        return None

    by_url = {item.url: item for item in associated}

    def _live_worker_choice() -> PullRequestRef | None:
        open_worker = [
            item
            for item in associated
            if item.state == "open"
            and not item.merged
            and item.author_login.casefold() == policy.github_identity.casefold()
        ]
        if len(open_worker) > 1:
            branch_hits = [item for item in open_worker if item.head_ref == issue.branch]
            if len(branch_hits) == 1:
                return branch_hits[0]
            raise HelmetIssueError(
                "ambiguous open worker pull requests; refuse to choose without Captain input"
            )
        if len(open_worker) == 1:
            return open_worker[0]
        merged_worker = [
            item
            for item in associated
            if item.merged
            and item.author_login.casefold() == policy.github_identity.casefold()
        ]
        if len(merged_worker) == 1:
            return merged_worker[0]
        if len(associated) == 1:
            only = associated[0]
            if only.author_login.casefold() != policy.github_identity.casefold():
                raise HelmetIssueError(
                    "discovered pull request author is not the configured worker"
                )
            return only
        return None

    live_choice = _live_worker_choice()

    if ledger_pull_url:
        if ledger_pull_url not in by_url:
            raise HelmetIssueError(
                "ledger pull request URL is not present among live candidates"
            )
        chosen = by_url[ledger_pull_url]
        _validate_worker_pr(policy, issue, chosen)
        if live_choice is not None and live_choice.url != chosen.url:
            # Associated live choice that disagrees with ledger is still a stop.
            # Unassociated discovery leads never participate in live_choice.
            raise HelmetIssueError(
                "ledger and live GitHub disagree on canonical worker pull request"
            )
        return chosen

    if live_choice is not None:
        return live_choice
    raise HelmetIssueError(
        "could not determine a canonical worker pull request; graph/ledger disagreement"
    )


def _validate_worker_pr(policy: Policy, issue: IssueRef, pull: PullRequestRef) -> None:
    if pull.repository_slug.casefold() != issue.repository.slug.casefold():
        raise HelmetIssueError("pull request repository does not match the issue")
    if pull.author_login.casefold() != policy.github_identity.casefold():
        raise HelmetIssueError("pull request author is not the configured worker")


def preflight(
    policy: Policy,
    issue_url: str,
    runner: Runner,
    *,
    gh: str = DEFAULT_GH,
    epic_body: str | None = None,
) -> tuple[IssueRef, str, str]:
    """Validate Captain authority, allowlist, issue state, labels, and budgets."""

    if policy.version < 2:
        raise HelmetIssueError("helmet-issue requires authority policy version 2")
    if not policy.captain_github_login or not policy.github_identity:
        raise HelmetIssueError("policy is missing captain_github_login or worker_github_login")
    if policy.captain_github_login.casefold() == policy.github_identity.casefold():
        raise HelmetIssueError("captain and worker identities must differ")
    if policy.budgets.max_issue_runtime_minutes < 1 or policy.budgets.max_repair_rounds < 1:
        raise HelmetIssueError("policy budgets are invalid")

    observed = observed_github_login(runner, gh=gh)
    try:
        verify_captain_identity(policy, observed)
    except AuthorityError as exc:
        raise HelmetIssueError(str(exc)) from exc

    issue = load_issue(policy, issue_url, runner, gh=gh)
    if issue.state != "open":
        raise HelmetIssueError("issue is not open")
    # Labels: either ready or dispatch is acceptable for orchestration; dispatch
    # is applied in DISPATCH when missing.
    known = {label.casefold() for label in issue.labels}
    if (
        policy.ready_label.casefold() not in known
        and policy.dispatch_label.casefold() not in known
    ):
        raise HelmetIssueError(
            "issue lacks ready_label and dispatch_label; refuse silent dispatch"
        )
    merge_mode = merge_authority_for(
        policy, issue_body=issue.body, epic_body=epic_body
    )
    return issue, observed, merge_mode


def ensure_dispatch_label(
    issue: IssueRef,
    policy: Policy,
    runner: Runner,
    *,
    gh: str = DEFAULT_GH,
) -> bool:
    """Ensure the H1 dispatch label is present. Returns True if added."""

    known = {label.casefold() for label in issue.labels}
    if policy.dispatch_label.casefold() in known:
        return False
    runner.run(
        [
            gh,
            "api",
            "--method",
            "POST",
            f"repos/{issue.repository.slug}/issues/{issue.number}/labels",
            "-f",
            f"labels[]={policy.dispatch_label}",
        ]
    )
    return True


def adopt_or_dispatch_root_task(
    policy: Policy,
    issue: IssueRef,
    ledger: Path,
    runner: Runner,
    *,
    hermes: str = DEFAULT_HERMES,
    gh: str = DEFAULT_GH,
    allow_create: bool = True,
    worker_runtime: Sequence[str] = (),
) -> tuple[str | None, bool]:
    """Return (task_id, created). Never duplicates an existing ledger/Kanban root.

    When allow_create is False (discovery / --no-dispatch), only adopt existing
    roots — never mutate labels, ledger, or Kanban.

    When the local ledger has no root, an optional ``worker_runtime`` seam is
    consulted for authoritative worker-side lookup/create. Local Path remains
    the default when the ledger file exists.
    """

    existing = resolve_root_task_id(
        ledger, issue.url, runner, worker_runtime=worker_runtime
    )
    if existing:
        return existing, False
    if not allow_create:
        return None, False

    runtime = parse_worker_runtime(worker_runtime)
    # Prefer worker-runtime dispatch when local ledger is absent so Captain host
    # never invents a second ledger of truth or skips worker checkout validation.
    if runtime and not ledger.is_file():
        return dispatch_root_via_worker_runtime(
            issue, runner, worker_runtime=runtime
        )

    # Use the same poller create path (idempotent by issue URL).
    from hermes_helmet.github_issue_poller import Issue as PollerIssue

    poller_issue = PollerIssue(
        repository=issue.repository,
        number=issue.number,
        title=issue.title,
        body=issue.body,
    )
    # Temporarily point poller helpers at the same binaries if customized.
    import hermes_helmet.github_issue_poller as poller_mod

    old_gh, old_hermes = poller_mod.GH, poller_mod.HERMES
    poller_mod.GH, poller_mod.HERMES = gh, hermes
    try:
        task_id, created = create_task_once(poller_issue, policy, ledger, runner)
    except PollerError as exc:
        raise HelmetIssueError(checkpoint_note("err", "dispatch_root_failed")) from exc
    finally:
        poller_mod.GH, poller_mod.HERMES = old_gh, old_hermes
    return task_id, created


def discover_progress(
    policy: Policy,
    issue: IssueRef,
    ledger: Path,
    runner: Runner,
    *,
    gh: str = DEFAULT_GH,
    hermes: str = DEFAULT_HERMES,
    worker_runtime: Sequence[str] = (),
) -> tuple[str | None, PullRequestRef | None, str | None]:
    """Discover root task id, canonical PR, and repair-task id without mutation."""

    root_task_id = resolve_root_task_id(
        ledger, issue.url, runner, worker_runtime=worker_runtime
    )
    watch = resolve_ledger_watch(
        ledger, issue.url, runner, worker_runtime=worker_runtime
    ) or {}
    ledger_pull_value = watch.get("pull_url")
    ledger_pull = ledger_pull_value if isinstance(ledger_pull_value, str) else None
    repair_value = watch.get("last_repair_task_id")
    repair_id = repair_value if isinstance(repair_value, str) else None

    kanban_pull = None
    if root_task_id:
        # Fail closed on Kanban errors — never treat read failure as absence.
        payload = _kanban_show(policy, root_task_id, runner, hermes=hermes)
        kanban_pull = _pr_url_from_kanban(payload, task_id=root_task_id)

    candidates = discover_linked_pull_requests(issue, runner, gh=gh)
    # Also adopt PR URL from ledger/kanban even if not yet linked in GitHub body.
    extra_urls = [url for url in (ledger_pull, kanban_pull) if isinstance(url, str)]
    for url in extra_urls:
        match = PULL_REQUEST_URL_RE.fullmatch(url)
        if match is None:
            continue
        if match.group("slug").casefold() != issue.repository.slug.casefold():
            raise HelmetIssueError(checkpoint_note("err", "pr_outside_repository"))
        number = int(match.group("number"))
        if not any(item.number == number for item in candidates):
            candidates.append(
                load_pull_request(issue.repository.slug, number, runner, gh=gh)
            )

    if ledger_pull and kanban_pull and ledger_pull != kanban_pull:
        raise HelmetIssueError(checkpoint_note("err", "ledger_kanban_pr_disagree"))

    pull = select_canonical_pull_request(
        policy,
        issue,
        candidates,
        ledger_pull_url=ledger_pull or kanban_pull,
    )
    return root_task_id, pull, repair_id


def resolve_repair_status(
    policy: Policy,
    repair_task_id: str | None,
    runner: Runner,
    *,
    hermes: str = DEFAULT_HERMES,
) -> str | None:
    """Return live Kanban status for a repair id, or None when absent/terminal-unknown."""

    if not repair_task_id:
        return None
    payload = _kanban_show(policy, repair_task_id, runner, hermes=hermes)
    task = payload["task"]
    assert isinstance(task, dict)
    status = task.get("status")
    if not isinstance(status, str) or not status.strip():
        raise HelmetIssueError(checkpoint_note("err", "repair_status_invalid", repair_task_id))
    return status.strip().casefold()


def head_changed(checkpoint: Checkpoint, head_sha: str) -> bool:
    """True when live head differs from the last clean *or* last reviewed head."""

    if checkpoint.clean_head and checkpoint.clean_head.casefold() == head_sha.casefold():
        return False
    if checkpoint.reviewed_head and checkpoint.reviewed_head.casefold() == head_sha.casefold():
        return False
    if not checkpoint.clean_head and not checkpoint.reviewed_head:
        return True
    return True


def _normalize_review_event_id(value: object) -> int | None:
    """Return a positive GitHub review/event id, or None when absent/invalid."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed >= 1 else None
    return None


def _parse_utc_timestamp(value: object) -> datetime | None:
    """Parse a checkpoint/GitHub ISO timestamp into an aware UTC datetime."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _formal_review_after_checkpoint(
    checkpoint: Checkpoint,
    review_submitted_at: object | None,
) -> bool:
    """True when available chronology proves the review is newer than the checkpoint.

    A checkpoint cannot already contain a formal review submitted after its own
    ``updated_at``. Ordinary resume/adoption preserves that cutoff while the head
    still carries only a legacy head-only CR marker (see
    :func:`_preserved_legacy_updated_at`). Missing or unparseable timestamps fail
    open for the legacy migration path (ambiguous chronology stays conservative
    for already-counted replays, not for inventing exemptions of demonstrably
    later reviews).
    """

    review_ts = _parse_utc_timestamp(review_submitted_at)
    checkpoint_ts = _parse_utc_timestamp(checkpoint.updated_at)
    if review_ts is None or checkpoint_ts is None:
        return False
    return review_ts > checkpoint_ts


def _changes_requested_note(head_sha: str, review_event_id: int | None = None) -> str:
    if review_event_id is None:
        return checkpoint_note("review", "changes_requested", head_sha[:12])
    return checkpoint_note("review", "changes_requested", head_sha[:12], review_event_id)


def _has_legacy_changes_requested_note(checkpoint: Checkpoint, head_sha: str) -> bool:
    """True when a pre-event-id head-only changes_requested note exists for head."""

    marker = _changes_requested_note(head_sha)
    return any(note == marker for note in checkpoint.notes)


def _has_event_scoped_changes_requested_note(checkpoint: Checkpoint, head_sha: str) -> bool:
    """True when any event-id-scoped changes_requested note exists for head."""

    prefix = _changes_requested_note(head_sha) + ":"
    return any(note.startswith(prefix) for note in checkpoint.notes)


def _legacy_cr_chronology_active(checkpoint: Checkpoint, head_sha: str) -> bool:
    """True when head still relies on head-only CR notes (no event-scoped migration)."""

    return _has_legacy_changes_requested_note(
        checkpoint, head_sha
    ) and not _has_event_scoped_changes_requested_note(checkpoint, head_sha)


def _preserved_legacy_updated_at(
    prior: Checkpoint | None,
    checkpoint: Checkpoint,
) -> str | None:
    """Return prior on-disk ``updated_at`` when legacy CR chronology must survive save.

    Ordinary resume/adoption saves append op notes and must not erase the cutoff
    used by :func:`_formal_review_after_checkpoint`. Once event-scoped CR history
    exists for the head (or the head no longer needs the legacy marker), return
    None so the caller bumps ``updated_at`` normally.
    """

    if prior is None:
        return None
    prior_ts = str(prior.updated_at or "").strip()
    if not prior_ts:
        return None
    head = checkpoint.reviewed_head
    if not isinstance(head, str) or not head.strip():
        return None
    prior_head = prior.reviewed_head
    if not isinstance(prior_head, str) or prior_head.casefold() != head.casefold():
        return None
    if not (
        _legacy_cr_chronology_active(prior, head)
        and _legacy_cr_chronology_active(checkpoint, head)
    ):
        return None
    return prior_ts


def _changes_requested_already_counted(
    checkpoint: Checkpoint,
    head_sha: str,
    review_event_id: int | None = None,
    *,
    prior_cycle_event_id: int | None = None,
    review_submitted_at: str | None = None,
) -> bool:
    """True when this verified formal review cycle already burned a repair round.

    Distinct formal review events on the same head each consume a round. Replaying
    the same verified event is idempotent even if intermediate adoption cleared
    the transient ``pending_repair`` flag. Without an event id, fall back to the
    legacy head-scoped marker (and pending boolean) for unit/direct callers.

    Upgrade path: version-1 checkpoints written before event-scoped notes only
    carry a head-only marker for an already-counted cycle. When authoritative
    delivery evidence (watch / repair body) names that same formal review event
    *and* this head has not yet been migrated to event-scoped history, replaying
    it must not burn another round — unless available checkpoint/review
    chronology proves the formal review was submitted after the checkpoint was
    saved (a genuinely later cycle the legacy marker cannot cover). Once any
    event-scoped CR note exists for the head, the persistent head-only marker no
    longer exempts later events — only the exact event-scoped marker counts. A
    later distinct event id is never treated as covered by the legacy marker alone.
    """

    prior_reviewed = checkpoint.reviewed_head
    if not isinstance(prior_reviewed, str):
        return False
    if prior_reviewed.casefold() != head_sha.casefold():
        return False
    if review_event_id is not None:
        marker = _changes_requested_note(head_sha, review_event_id)
        if any(note == marker for note in checkpoint.notes):
            return True
        prior_cycle = _normalize_review_event_id(prior_cycle_event_id)
        # Legacy migration is one-shot: after the checkpoint carries any
        # event-scoped CR note for this head, do not re-use the head-only
        # marker for whatever event the current watch/body now names. Also
        # refuse the exemption when chronology shows the formal review landed
        # after the legacy checkpoint was persisted.
        if (
            prior_cycle is not None
            and prior_cycle == review_event_id
            and _has_legacy_changes_requested_note(checkpoint, head_sha)
            and not _has_event_scoped_changes_requested_note(checkpoint, head_sha)
            and not _formal_review_after_checkpoint(checkpoint, review_submitted_at)
        ):
            return True
        return False
    if checkpoint.pending_repair:
        return True
    marker = _changes_requested_note(head_sha)
    return any(note == marker for note in checkpoint.notes)


def record_review_result(
    checkpoint: Checkpoint,
    *,
    head_sha: str,
    outcome: str,
    summary: str = "",
    review_event_id: int | None = None,
    prior_cycle_event_id: int | None = None,
    review_submitted_at: str | None = None,
) -> Checkpoint:
    """Update checkpoint after a Captain review of an exact head.

    outcome: clean | changes_requested | blocked

    Free-form ``summary`` is intentionally discarded from persistence — review
    prose stays on GitHub. Only structured outcome codes (and optional formal
    review event ids) are recorded.

    ``prior_cycle_event_id`` is optional delivery evidence (ledger watch and/or
    repair-body review id) used only to migrate legacy head-only
    ``changes_requested`` markers onto the already-counted formal event.
    ``review_submitted_at`` is optional formal-review chronology used with
    checkpoint ``updated_at`` so a review submitted after the legacy checkpoint
    was saved is not treated as already counted. Resume/adoption preserves that
    ``updated_at`` cutoff while the head remains on an unmigrated head-only
    marker so adopt-then-record matches direct record.
    """

    del summary  # never persist review prose in the checkpoint
    event_id = _normalize_review_event_id(review_event_id)
    prior_cycle = _normalize_review_event_id(prior_cycle_event_id)
    submitted_at = (
        review_submitted_at.strip()
        if isinstance(review_submitted_at, str) and review_submitted_at.strip()
        else None
    )
    checkpoint.last_blocker = None
    if outcome == "clean":
        checkpoint.reviewed_head = head_sha
        checkpoint.clean_head = head_sha
        checkpoint.pending_repair = False
        checkpoint.state = "READY"
        if event_id is None:
            checkpoint.notes.append(checkpoint_note("review", "clean", head_sha[:12]))
        else:
            checkpoint.notes.append(
                checkpoint_note("review", "clean", head_sha[:12], event_id)
            )
    elif outcome == "changes_requested":
        # Idempotent for the same verified formal review event. A later distinct
        # changes_requested event on the same head consumes a new repair round.
        already_counted = _changes_requested_already_counted(
            checkpoint,
            head_sha,
            event_id,
            prior_cycle_event_id=prior_cycle,
            review_submitted_at=submitted_at,
        )
        checkpoint.reviewed_head = head_sha
        checkpoint.clean_head = None
        checkpoint.pending_repair = True
        if not already_counted:
            checkpoint.repair_rounds += 1
        checkpoint.state = "REQUEST_REPAIR"
        # Always append the event-scoped marker when known so a legacy head-only
        # checkpoint migrates forward without a live checkpoint rewrite.
        checkpoint.notes.append(_changes_requested_note(head_sha, event_id))
    elif outcome == "blocked":
        # Explicit blocked review is a hard merge stop: clear clean authority so
        # merge_gate / resume cannot treat a prior APPROVED clean_head as current.
        checkpoint.reviewed_head = head_sha
        checkpoint.clean_head = None
        checkpoint.pending_repair = False
        checkpoint.state = "BLOCKED"
        checkpoint.last_blocker = checkpoint_note("review", "blocked", head_sha[:12])
        checkpoint.notes.append(checkpoint_note("review", "blocked", head_sha[:12]))
    else:
        raise HelmetIssueError(checkpoint_note("err", "unknown_review_outcome"))
    return checkpoint


def should_create_repair_from_activity(
    *,
    activity_kind: str,
    actor_login: str,
    actor_is_worker: bool,
    trusted: bool,
    has_actionable_findings: bool,
    is_approval_only: bool,
    is_ci_failure_only: bool,
    is_repeat_poll: bool,
) -> bool:
    """Policy helper: helmet-issue never creates repair work itself.

    Returns False for every non-actionable class listed in acceptance criteria.
    The H1 poller is the only repair creator; this helper exists so the skill and
    tests share one fail-closed definition of "do not create repair".
    """

    del activity_kind  # reserved for future diagnostics
    if actor_is_worker:
        return False
    if not trusted:
        return False
    if is_approval_only:
        return False
    if is_ci_failure_only:
        return False
    if is_repeat_poll:
        return False
    if not has_actionable_findings:
        return False
    if not actor_login:
        return False
    # Still False: helmet-issue must not create the repair card.
    return False


def evaluate_merge_gate(
    policy: Policy,
    issue: IssueRef,
    pull: PullRequestRef,
    checkpoint: Checkpoint,
    *,
    required_checks_green: bool | None = None,
    mergeable: bool | None = None,
    runner: Runner | None = None,
    gh: str = DEFAULT_GH,
    epic_body: str | None = None,
) -> tuple[str, str | None]:
    """Return (decision, blocker).

    decision: stop_for_approval | merge_allowed | not_ready | already_merged

    When runner is provided, live GitHub mergeable_state and the effective
    Captain review on the current head are authoritative. Caller-supplied
    True flags never override live dirty/blocked/unknown state. GitHub
    ``clean`` alone is not sufficient without a matching clean Captain review
    of the live head. An explicit checkpoint BLOCKED review outcome always
    stops the gate until a newly accepted clean review recovers authority.
    Policy ``local-only`` worker completion does not treat GitHub ``clean`` as
    independently verified required checks, and never grants unattended merge
    from checkless ``clean``; explicit Captain approval remains available.
    """

    if checkpoint.state == "BLOCKED":
        return (
            "not_ready",
            checkpoint.last_blocker
            or checkpoint_note("err", "review_blocked", (checkpoint.reviewed_head or "")[:12]),
        )

    live = pull
    if runner is not None:
        live = load_pull_request(pull.repository_slug, pull.number, runner, gh=gh)
        if live.head_sha.casefold() != pull.head_sha.casefold():
            return "not_ready", checkpoint_note("err", "merge_gate_head_changed")
        state = (live.mergeable_state or "").casefold()
        if state in _NEGATIVE_MERGEABLE_STATES or state in {"", "unknown"}:
            # Authoritative negative/unknown always wins over caller booleans.
            mergeable = False
            if state in {"blocked", "unstable", ""}:
                required_checks_green = False
            elif required_checks_green is None:
                required_checks_green = False
            if state == "dirty":
                mergeable = False
        elif state == "clean":
            # Live clean may fill unknowns; never upgrade past a caller False.
            if mergeable is None:
                mergeable = True
            if (
                required_checks_green is None
                and policy.worker_completion_contract != LOCAL_ONLY_COMPLETION_CONTRACT
            ):
                required_checks_green = True
        else:
            mergeable = False
            if required_checks_green is None:
                required_checks_green = False
        pull = live
        # Re-check effective Captain review at the authority boundary.
        effective = effective_captain_review_state(
            policy=policy, pull=pull, head_sha=pull.head_sha, runner=runner, gh=gh
        )
        if checkpoint.clean_head and checkpoint.clean_head.casefold() == pull.head_sha.casefold():
            if effective != "APPROVED":
                return "not_ready", checkpoint_note(
                    "err", "merge_gate_review_not_approved", effective or "none"
                )
    if mergeable is None:
        return "not_ready", checkpoint_note("err", "merge_gate_requires_authority")
    if required_checks_green is None:
        if policy.worker_completion_contract != LOCAL_ONLY_COMPLETION_CONTRACT:
            return "not_ready", checkpoint_note("err", "merge_gate_requires_authority")

    if pull.merged or pull.state == "closed" and pull.merged:
        return "already_merged", None
    if pull.draft:
        return "not_ready", checkpoint_note("err", "pr_is_draft")
    if checkpoint.clean_head is None or checkpoint.clean_head != pull.head_sha:
        return "not_ready", checkpoint_note("err", "head_lacks_clean_captain_review")
    if required_checks_green is False:
        return "not_ready", checkpoint_note("err", "required_checks_not_green")
    if not mergeable:
        return "not_ready", checkpoint_note("err", "pr_not_mergeable")

    resolved_body, parent_url = resolve_parent_epic_body(
        policy,
        epic_body=epic_body,
        parent_epic_url=checkpoint.parent_epic_url,
        runner=runner,
        gh=gh,
        child_issue=issue,
    )
    if parent_url:
        checkpoint.parent_epic_url = parent_url
    elif checkpoint.parent_epic_url:
        # Live Parent no longer matches saved association — clear stale link.
        checkpoint.parent_epic_url = None
    mode = merge_authority_for(
        policy, issue_body=issue.body, epic_body=resolved_body
    )
    checkpoint.merge_mode = mode
    if mode == "unattended_when_clean" or unattended_merge_allowed(
        policy, issue_body=issue.body, epic_body=resolved_body
    ):
        if required_checks_green is True:
            return "merge_allowed", None
        return "stop_for_approval", checkpoint_note(
            "err", "explicit_captain_approval_required"
        )
    return "stop_for_approval", checkpoint_note("err", "explicit_captain_approval_required")


def merge_pull_request(
    pull: PullRequestRef,
    runner: Runner,
    *,
    gh: str = DEFAULT_GH,
    method: str = "squash",
) -> PullRequestRef:
    """Captain-side merge with mandatory single re-query. Never blind-retries."""

    merge_error: HelmetIssueError | None = None
    try:
        runner.run(
            [
                gh,
                "api",
                "--method",
                "PUT",
                f"repos/{pull.repository_slug}/pulls/{pull.number}/merge",
                "-f",
                f"merge_method={method}",
                "-f",
                f"sha={pull.head_sha}",
            ]
        )
    except HelmetIssueError as exc:
        merge_error = exc

    verified = verify_merged(pull, runner, gh=gh)
    if verified.merged:
        return verified
    if merge_error is not None:
        raise HelmetIssueError(
            checkpoint_note("err", "merge_failed_not_merged")
        ) from merge_error
    raise HelmetIssueError(checkpoint_note("err", "merge_api_success_but_not_merged"))


def verify_merged(
    pull: PullRequestRef,
    runner: Runner,
    *,
    gh: str = DEFAULT_GH,
) -> PullRequestRef:
    """Re-query authoritative GitHub state after a merge attempt."""

    return load_pull_request(pull.repository_slug, pull.number, runner, gh=gh)


def set_blocker(checkpoint: Checkpoint, reason: str, *, fatal: bool = False) -> Checkpoint:
    """Record a blocker using structured codes only (no free-form secrets/prose)."""

    text = (reason or "").strip()
    prefix = text.split(":", 1)[0] if text else ""
    if prefix in {"err", "review", "note", "op"}:
        # Re-parse through checkpoint_note so only code tokens survive.
        parts = text.split(":")
        reason = checkpoint_note(*parts)
    else:
        # Opaque code for unexpected free-form strings — never persist raw text.
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        reason = checkpoint_note("err", "blocked", digest)
    if checkpoint.last_blocker == reason:
        checkpoint.identical_blocker_count += 1
    else:
        checkpoint.last_blocker = reason
        checkpoint.identical_blocker_count = 1
    if checkpoint.identical_blocker_count >= 3:
        checkpoint.state = "BLOCKED"
        checkpoint.notes.append(checkpoint_note("op", "repeated_blocker", reason))
    elif fatal:
        checkpoint.state = "FAILED"
        checkpoint.notes.append(reason)
    else:
        checkpoint.state = "BLOCKED"
        checkpoint.notes.append(reason)
    return checkpoint


def budget_exhausted(checkpoint: Checkpoint, policy: Policy) -> str | None:
    if checkpoint.repair_rounds > policy.budgets.max_repair_rounds:
        return checkpoint_note("err", "budget", "max_repair_rounds")
    if checkpoint.started_at:
        try:
            started = datetime.fromisoformat(checkpoint.started_at)
        except ValueError:
            started = None
        if started is not None:
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed = datetime.now(timezone.utc) - started
            if elapsed.total_seconds() > policy.budgets.max_issue_runtime_minutes * 60:
                return checkpoint_note("err", "budget", "max_issue_runtime_minutes")
    return None


def build_status(
    policy: Policy,
    issue_url: str,
    checkpoint: Checkpoint | None,
    *,
    root_task_id: str | None = None,
    pull: PullRequestRef | None = None,
    repair_task_id: str | None = None,
    repair_status: str | None = None,
    live_error: str | None = None,
) -> StatusReport:
    state = checkpoint.state if checkpoint else "UNKNOWN"
    reviewed = checkpoint.reviewed_head if checkpoint else None
    clean = checkpoint.clean_head if checkpoint else None
    blocker = checkpoint.last_blocker if checkpoint else None
    merge_gate = checkpoint.merge_mode if checkpoint else policy.merge_default_mode
    if live_error:
        # Surface authority uncertainty without mutating persisted checkpoint.
        uncertainty = checkpoint_note("err", "live_uncertain")
        if live_error.startswith("err:"):
            uncertainty = sanitize_public_text(live_error, max_len=160)
        blocker = uncertainty if blocker is None else f"{blocker};{uncertainty}"
    if pull and pull.merged:
        repair_state = "n/a-merged"
        if state not in TERMINAL_STATES and live_error is None:
            state = "DONE"
    elif repair_task_id and repair_status and repair_status in ACTIVE_REPAIR_STATUSES:
        repair_state = f"outstanding:{repair_task_id}:{repair_status}"
    elif checkpoint and checkpoint.pending_repair:
        repair_state = "awaiting-h1-poller"
    elif clean and pull and clean == pull.head_sha:
        repair_state = "none"
    else:
        repair_state = "none"

    # Read-only status must surface a new unreviewed head without claiming READY.
    head_needs_review = False
    if (
        pull
        and not pull.merged
        and live_error is None
        and (
            clean is None
            or clean.casefold() != pull.head_sha.casefold()
        )
    ):
        head_needs_review = True
        if state == "READY":
            state = "REVIEW_HEAD"
            needs = checkpoint_note("err", "current_head_needs_review", pull.head_sha[:12])
            blocker = needs if blocker is None else f"{blocker};{needs}"
            merge_gate = f"{(checkpoint.merge_mode if checkpoint else policy.merge_default_mode)}:not_ready"

    if checkpoint and checkpoint.state == "MERGE_GATE" and not head_needs_review:
        merge_gate = f"{checkpoint.merge_mode}:awaiting"
    elif checkpoint and state == "READY" and not head_needs_review:
        merge_gate = f"{checkpoint.merge_mode}:ready"
    elif checkpoint and state == "DONE":
        merge_gate = f"{checkpoint.merge_mode}:done"

    return StatusReport(
        issue_url=issue_url,
        state=state,
        root_task_id=root_task_id or (checkpoint.root_task_id if checkpoint else None),
        pr_url=(pull.url if pull else None) or (checkpoint.pr_url if checkpoint else None),
        reviewed_head=reviewed,
        clean_head=clean,
        repair_state=repair_state,
        merge_gate=merge_gate,
        blocker=blocker,
        terminal=state in TERMINAL_STATES and live_error is None and not head_needs_review,
        details={
            "pr_number": pull.number if pull else (checkpoint.pr_number if checkpoint else None),
            "pr_head": pull.head_sha if pull else None,
            "repair_task_id": repair_task_id,
            "repair_rounds": checkpoint.repair_rounds if checkpoint else 0,
            "host_continuation": checkpoint.host_continuation if checkpoint else "unknown",
            "one_pass_only": checkpoint.one_pass_only if checkpoint else False,
            "continuation": (
                "one_pass_only:continuation_unavailable"
                if checkpoint
                and (
                    checkpoint.one_pass_only
                    or checkpoint.host_continuation in {"none", "unknown"}
                )
                else "managed"
            ),
            "live_error": live_error,
            "head_needs_review": head_needs_review,
        },
    )


def status_issue(
    policy: Policy,
    issue_url: str,
    *,
    ledger: Path = DEFAULT_LEDGER,
    checkpoint_dir: Path = DEFAULT_CHECKPOINT_DIR,
    runner: Runner | None = None,
    gh: str = DEFAULT_GH,
    hermes: str = DEFAULT_HERMES,
    worker_runtime: Sequence[str] = (),
) -> StatusReport:
    """Read-only status for an issue. Never mutates GitHub, ledger, or checkpoint."""

    runner = runner or SubprocessRunner(gh=gh, hermes=hermes)
    runtime = parse_worker_runtime(worker_runtime) or DEFAULT_WORKER_RUNTIME
    path = checkpoint_path(checkpoint_dir, issue_url)
    checkpoint = load_checkpoint(path, expected_issue_url=issue_url)
    # Best-effort live discovery; failures become report details, not writes.
    root_task_id = None
    pull = None
    repair_id = None
    repair_status = None
    live_error = None
    try:
        root_task_id = resolve_root_task_id(
            ledger, issue_url, runner, worker_runtime=runtime
        )
        issue = load_issue(policy, issue_url, runner, gh=gh)
        root_task_id, pull, repair_id = discover_progress(
            policy,
            issue,
            ledger,
            runner,
            gh=gh,
            hermes=hermes,
            worker_runtime=runtime,
        )
        if repair_id:
            repair_status = resolve_repair_status(
                policy, repair_id, runner, hermes=hermes
            )
    except HelmetIssueError as exc:
        live_error = str(exc) if str(exc).startswith("err:") else checkpoint_note(
            "err", "live_uncertain"
        )
        if checkpoint is None:
            checkpoint = Checkpoint(
                version=CHECKPOINT_VERSION,
                issue_url=issue_url if ISSUE_URL_RE.fullmatch(issue_url or "") else (
                    # Keep status callable on bad URLs without claiming a durable file.
                    issue_url or "https://github.com/invalid/invalid/issues/0"
                ),
                state="UNKNOWN",
                last_blocker=live_error,
            )
            if not ISSUE_URL_RE.fullmatch(issue_url or ""):
                checkpoint.issue_url = "https://github.com/invalid/invalid/issues/0"
    return build_status(
        policy,
        issue_url,
        checkpoint,
        root_task_id=root_task_id,
        pull=pull,
        repair_task_id=repair_id,
        repair_status=repair_status,
        live_error=live_error,
    )


def run_preflight_and_adopt(
    policy: Policy,
    issue_url: str,
    *,
    ledger: Path = DEFAULT_LEDGER,
    checkpoint_dir: Path = DEFAULT_CHECKPOINT_DIR,
    runner: Runner | None = None,
    gh: str = DEFAULT_GH,
    hermes: str = DEFAULT_HERMES,
    host_continuation: str = "unknown",
    one_pass_only: bool = False,
    apply_dispatch: bool = True,
    worker_runtime: Sequence[str] = (),
    epic_body: str | None = None,
    parent_epic_url: str | None = None,
) -> tuple[Checkpoint, StatusReport]:
    """One safe orchestration pass through discovery (no review body generation)."""

    runner = runner or SubprocessRunner(gh=gh, hermes=hermes)
    runtime = parse_worker_runtime(worker_runtime) or DEFAULT_WORKER_RUNTIME
    path = checkpoint_path(checkpoint_dir, issue_url)
    checkpoint = load_checkpoint(path, expected_issue_url=issue_url)
    if checkpoint is None:
        checkpoint = Checkpoint(version=CHECKPOINT_VERSION, issue_url=issue_url, state="PREFLIGHT")
    checkpoint.host_continuation = host_continuation
    checkpoint.one_pass_only = one_pass_only
    continuation_limited = one_pass_only or host_continuation in {"none", "unknown"}
    continuation_note = checkpoint_note("op", "one_pass_only", "continuation_unavailable")
    if continuation_limited and continuation_note not in checkpoint.notes:
        checkpoint.notes.append(continuation_note)

    try:
        # Load issue first so closed+merged completion can resolve before refusing
        # new dispatch on a closed issue.
        observed = observed_github_login(runner, gh=gh)
        try:
            verify_captain_identity(policy, observed)
        except AuthorityError as exc:
            raise HelmetIssueError(checkpoint_note("err", "captain_identity_mismatch")) from exc
        if policy.version < 2:
            raise HelmetIssueError(checkpoint_note("err", "policy_version_unsupported"))
        if not policy.captain_github_login or not policy.github_identity:
            raise HelmetIssueError(checkpoint_note("err", "policy_identities_missing"))
        if policy.captain_github_login.casefold() == policy.github_identity.casefold():
            raise HelmetIssueError(checkpoint_note("err", "captain_worker_must_differ"))
        if policy.budgets.max_issue_runtime_minutes < 1 or policy.budgets.max_repair_rounds < 1:
            raise HelmetIssueError(checkpoint_note("err", "policy_budgets_invalid"))

        issue = load_issue(policy, issue_url, runner, gh=gh)
        # Carry / refresh validated parent association for merge inheritance.
        effective_parent = parent_epic_url or checkpoint.parent_epic_url
        resolved_epic_body, resolved_parent = resolve_parent_epic_body(
            policy,
            epic_body=epic_body,
            parent_epic_url=effective_parent,
            runner=runner,
            gh=gh,
            child_issue=issue,
        )
        if resolved_parent:
            checkpoint.parent_epic_url = resolved_parent
        else:
            checkpoint.parent_epic_url = None
        merge_mode = merge_authority_for(
            policy, issue_body=issue.body, epic_body=resolved_epic_body
        )
        checkpoint.merge_mode = merge_mode
        if not checkpoint.started_at:
            checkpoint.started_at = _utc_now()

        # Budgets before any dispatch mutation.
        exhausted = budget_exhausted(checkpoint, policy)
        if exhausted:
            set_blocker(checkpoint, exhausted, fatal=True)
            save_checkpoint(path, checkpoint)
            report = build_status(policy, issue_url, checkpoint)
            return checkpoint, report

        # Authoritative discovery BEFORE any label/root mutation.
        root_task_id, pull, repair_id = discover_progress(
            policy,
            issue,
            ledger,
            runner,
            gh=gh,
            hermes=hermes,
            worker_runtime=runtime,
        )

        if issue.state != "open":
            if pull is not None and pull.merged:
                checkpoint.state = "DONE"
                checkpoint.root_task_id = root_task_id or checkpoint.root_task_id
                checkpoint.pr_url = pull.url
                checkpoint.pr_number = pull.number
                checkpoint.clean_head = pull.head_sha
                checkpoint.reviewed_head = pull.head_sha
                checkpoint.pending_repair = False
                checkpoint.notes.append(checkpoint_note("op", "closed_issue_merged_pr"))
                save_checkpoint(path, checkpoint)
                report = build_status(
                    policy,
                    issue_url,
                    checkpoint,
                    root_task_id=checkpoint.root_task_id,
                    pull=pull,
                    repair_task_id=repair_id,
                )
                return checkpoint, report
            raise HelmetIssueError(checkpoint_note("err", "issue_not_open"))

        known = {label.casefold() for label in issue.labels}
        if (
            policy.ready_label.casefold() not in known
            and policy.dispatch_label.casefold() not in known
        ):
            raise HelmetIssueError(checkpoint_note("err", "issue_missing_ready_or_dispatch_label"))

        prior_state = checkpoint.state
        checkpoint.state = "DISPATCH"
        if root_task_id:
            checkpoint.root_task_id = root_task_id
            checkpoint.notes.append(checkpoint_note("op", "adopted_root", root_task_id))

        if apply_dispatch and not root_task_id:
            if pull is not None:
                # Canonical worker PR already exists without a recoverable root.
                # Creating a second root would duplicate work; stop without intake
                # mutation (no dispatch label, no Kanban create).
                raise HelmetIssueError(
                    checkpoint_note("err", "pr_without_recoverable_root", pull.url)
                )
            # Only mutate after discovery confirmed no existing root *and* no PR.
            ensure_dispatch_label(issue, policy, runner, gh=gh)
            created_id, created = adopt_or_dispatch_root_task(
                policy,
                issue,
                ledger,
                runner,
                hermes=hermes,
                gh=gh,
                allow_create=True,
                worker_runtime=runtime,
            )
            checkpoint.root_task_id = created_id
            if created:
                checkpoint.notes.append(checkpoint_note("op", "dispatched_root", created_id))
            else:
                checkpoint.notes.append(checkpoint_note("op", "adopted_root", created_id))
            # Re-discover after create so PR/ledger stay coherent.
            root_task_id, pull, repair_id = discover_progress(
                policy,
                issue,
                ledger,
                runner,
                gh=gh,
                hermes=hermes,
                worker_runtime=runtime,
            )
            checkpoint.root_task_id = root_task_id or checkpoint.root_task_id
        elif not apply_dispatch and not root_task_id:
            checkpoint.notes.append(checkpoint_note("op", "no_dispatch_no_root"))

        repair_status = None
        if repair_id:
            repair_status = resolve_repair_status(
                policy, repair_id, runner, hermes=hermes
            )

        if pull is None:
            checkpoint.state = "WAIT_PR"
            checkpoint.pr_url = None
            checkpoint.pr_number = None
        else:
            checkpoint.pr_url = pull.url
            checkpoint.pr_number = pull.number
            if pull.merged:
                checkpoint.state = "DONE"
                checkpoint.clean_head = pull.head_sha
                checkpoint.reviewed_head = pull.head_sha
                checkpoint.pending_repair = False
            elif head_changed(checkpoint, pull.head_sha):
                # New head vs last clean *or* last reviewed → re-review even when
                # pending_repair cleared clean_head. Also the recovery path out of
                # an explicit BLOCKED decision on a prior head.
                if checkpoint.clean_head:
                    checkpoint.notes.append(
                        checkpoint_note("op", "head_changed_invalidated_clean", checkpoint.clean_head[:12])
                    )
                elif checkpoint.reviewed_head:
                    checkpoint.notes.append(
                        checkpoint_note(
                            "op",
                            "head_changed_since_review",
                            checkpoint.reviewed_head[:12],
                            pull.head_sha[:12],
                        )
                    )
                checkpoint.clean_head = None
                checkpoint.pending_repair = False
                checkpoint.state = "REVIEW_HEAD"
            elif (
                prior_state == "BLOCKED"
                and checkpoint.reviewed_head
                and checkpoint.reviewed_head.casefold() == pull.head_sha.casefold()
            ):
                # Explicit blocked review sticks on the unchanged head until a
                # newly accepted clean review (apply_review_outcome) recovers.
                # prior_state is required because DISPATCH is set above.
                checkpoint.clean_head = None
                checkpoint.pending_repair = False
                checkpoint.state = "BLOCKED"
            elif repair_id and repair_status in ACTIVE_REPAIR_STATUSES:
                checkpoint.pending_repair = True
                checkpoint.state = "WAIT_REPAIR"
            elif checkpoint.pending_repair and (
                repair_id is None
                or (repair_status is not None and repair_status not in TERMINAL_TASK_STATUSES)
            ):
                if repair_status in TERMINAL_TASK_STATUSES:
                    checkpoint.pending_repair = False
                    checkpoint.state = "REVIEW_HEAD"
                else:
                    checkpoint.state = "WAIT_REPAIR"
            elif repair_id and repair_status in TERMINAL_TASK_STATUSES:
                # Correlate terminal repair with the current review cycle before
                # clearing pending review. A previous Done card still named in the
                # watch is not delivery for a fresh changes-requested on the same
                # live head — head_changed() already advances when repair lands a
                # new SHA. A Done card bound to the currently recorded formal
                # review event returns for Captain re-assessment even when the
                # worker delivered without changing the head.
                watch = resolve_ledger_watch(
                    ledger, issue_url, runner, worker_runtime=runtime
                )
                current_cycle_delivery = _terminal_repair_matches_current_cycle(
                    checkpoint=checkpoint,
                    pull=pull,
                    repair_id=repair_id,
                    watch=watch,
                    policy=policy,
                    runner=runner,
                    gh=gh,
                    hermes=hermes,
                )
                if (
                    checkpoint.pending_repair
                    and not current_cycle_delivery
                    and (
                        checkpoint.clean_head is None
                        or checkpoint.clean_head.casefold() != pull.head_sha.casefold()
                    )
                ):
                    checkpoint.state = "WAIT_REPAIR"
                else:
                    checkpoint.pending_repair = False
                    if checkpoint.clean_head == pull.head_sha:
                        checkpoint.state = "READY"
                    else:
                        checkpoint.state = "REVIEW_HEAD"
            elif checkpoint.clean_head == pull.head_sha:
                checkpoint.state = "READY"
                checkpoint.pending_repair = False
            else:
                checkpoint.state = "REVIEW_HEAD"

        save_checkpoint(path, checkpoint)
        report = build_status(
            policy,
            issue_url,
            checkpoint,
            root_task_id=checkpoint.root_task_id,
            pull=pull,
            repair_task_id=repair_id,
            repair_status=repair_status,
        )
        return checkpoint, report
    except HelmetIssueError as exc:
        set_blocker(checkpoint, str(exc), fatal=True)
        save_checkpoint(path, checkpoint)
        report = build_status(policy, issue_url, checkpoint)
        return checkpoint, report


def _github_reviews_for_pull(
    pull: PullRequestRef,
    runner: Runner,
    *,
    gh: str = DEFAULT_GH,
) -> list[dict[str, object]]:
    raw = _github_json(
        runner,
        gh,
        f"repos/{pull.repository_slug}/pulls/{pull.number}/reviews?per_page=100",
    )
    if not isinstance(raw, list):
        raise HelmetIssueError(checkpoint_note("err", "reviews_payload_invalid"))
    reviews: list[dict[str, object]] = []
    for item in raw:
        if isinstance(item, dict):
            reviews.append(item)
    return reviews


def effective_captain_review(
    *,
    policy: Policy,
    pull: PullRequestRef,
    head_sha: str,
    runner: Runner,
    gh: str,
) -> dict[str, object] | None:
    """Latest formal Captain review on the given head (not any historical match)."""

    reviews = _github_reviews_for_pull(pull, runner, gh=gh)
    captain = policy.captain_github_login.casefold()
    latest: dict[str, object] | None = None
    latest_key: tuple[str, int] | None = None
    for index, review in enumerate(reviews):
        user = review.get("user") if isinstance(review.get("user"), dict) else {}
        login = user.get("login") if isinstance(user, dict) else None
        state = review.get("state")
        commit_id = review.get("commit_id")
        if not isinstance(login, str) or not isinstance(state, str):
            continue
        if login.casefold() != captain:
            continue
        if not isinstance(commit_id, str) or commit_id.casefold() != head_sha.casefold():
            continue
        submitted = review.get("submitted_at")
        # Prefer submitted_at ordering; fall back to list order (GitHub returns ascending).
        key = (str(submitted) if isinstance(submitted, str) else "", index)
        if latest_key is None or key >= latest_key:
            latest_key = key
            latest = review
    return latest


def effective_captain_review_state(
    *,
    policy: Policy,
    pull: PullRequestRef,
    head_sha: str,
    runner: Runner,
    gh: str,
) -> str | None:
    """Latest formal Captain review state on the given head (not any historical match)."""

    latest = effective_captain_review(
        policy=policy, pull=pull, head_sha=head_sha, runner=runner, gh=gh
    )
    if latest is None:
        return None
    state = latest.get("state")
    return str(state).upper() if isinstance(state, str) else None


def _repair_task_review_event_id(
    policy: Policy,
    repair_task_id: str,
    runner: Runner,
    *,
    hermes: str = DEFAULT_HERMES,
) -> int | None:
    """Extract the poller-authored review event id from a repair task body, if any."""

    payload = _kanban_show(policy, repair_task_id, runner, hermes=hermes)
    task = payload.get("task") if isinstance(payload, dict) else None
    if not isinstance(task, dict):
        return None
    body = task.get("body")
    if not isinstance(body, str) or not body.strip():
        return None
    match = re.search(r"pullrequestreview-(\d+)", body, flags=re.IGNORECASE)
    if match is None:
        return None
    return _normalize_review_event_id(match.group(1))


def _delivery_review_event_id(
    *,
    watch: dict[str, object] | None,
    policy: Policy,
    runner: Runner,
    hermes: str = DEFAULT_HERMES,
) -> int | None:
    """Poller delivery event id from watch and/or repair body when they agree.

    Used to correlate a legacy head-only changes_requested marker with the
    formal review cycle that already consumed a repair round. When watch and
    body both supply ids and disagree, return None (fail closed for migration).
    """

    if not watch:
        return None
    watch_event_id = _normalize_review_event_id(watch.get("last_event_id"))
    body_event_id: int | None = None
    repair_id = watch.get("last_repair_task_id")
    if isinstance(repair_id, str) and repair_id.strip():
        try:
            body_event_id = _repair_task_review_event_id(
                policy, repair_id.strip(), runner, hermes=hermes
            )
        except HelmetIssueError:
            body_event_id = None
    if watch_event_id is not None and body_event_id is not None:
        if watch_event_id != body_event_id:
            return None
        return watch_event_id
    if watch_event_id is not None:
        return watch_event_id
    return body_event_id


def _latest_changes_requested_event_id(
    checkpoint: Checkpoint, head_sha: str
) -> int | None:
    """Most recent event-scoped changes_requested note for this head, if any."""

    prefix = checkpoint_note("review", "changes_requested", head_sha[:12])
    scoped = prefix + ":"
    for note in reversed(checkpoint.notes):
        if note.startswith(scoped):
            suffix = note[len(scoped) :]
            token = suffix.split(":", 1)[0]
            return _normalize_review_event_id(token)
        if note == prefix:
            # Legacy head-only marker — no event id in the note stream.
            return None
    return None


def _terminal_repair_matches_current_cycle(
    *,
    checkpoint: Checkpoint,
    pull: PullRequestRef,
    repair_id: str,
    watch: dict[str, object] | None,
    policy: Policy,
    runner: Runner,
    gh: str,
    hermes: str,
) -> bool:
    """True when a terminal repair is delivery for the currently recorded review cycle.

    A previous Done card still named in the watch is not delivery for a fresh
    same-head changes_requested. Require poller event binding (watch and/or
    repair body) that matches the *current* verified formal review cycle on this
    head — not an older CR event still present in history.
    """

    if not checkpoint.pending_repair:
        return True
    watch = watch or {}
    named_repair = watch.get("last_repair_task_id")
    if not isinstance(named_repair, str) or named_repair != repair_id:
        return False

    delivery_event_id = _delivery_review_event_id(
        watch=watch, policy=policy, runner=runner, hermes=hermes
    )
    if delivery_event_id is None:
        return False

    head = pull.head_sha
    current_event_id = _latest_changes_requested_event_id(checkpoint, head)
    latest = effective_captain_review(
        policy=policy, pull=pull, head_sha=head, runner=runner, gh=gh
    )
    if latest is not None:
        state = latest.get("state")
        commit_id = latest.get("commit_id")
        latest_id = _normalize_review_event_id(latest.get("id"))
        if (
            isinstance(state, str)
            and state.upper() == "CHANGES_REQUESTED"
            and isinstance(commit_id, str)
            and commit_id.casefold() == head.casefold()
            and latest_id is not None
        ):
            # Live formal review is authoritative for the current cycle.
            current_event_id = latest_id

    if current_event_id is None:
        return False
    return delivery_event_id == current_event_id


def _verify_formal_review(
    *,
    policy: Policy,
    pull: PullRequestRef,
    head_sha: str,
    outcome: str,
    runner: Runner,
    gh: str,
) -> tuple[int | None, str | None]:
    """Bind review recording to the effective formal GitHub review on current head.

    Returns ``(review_event_id, submitted_at)`` when GitHub supplies them.
    """

    if outcome == "blocked":
        # Local safety stop may not have a formal review event.
        return None, None
    expected_state = {
        "clean": "APPROVED",
        "changes_requested": "CHANGES_REQUESTED",
    }.get(outcome)
    if expected_state is None:
        raise HelmetIssueError(checkpoint_note("err", "unknown_review_outcome"))

    latest = effective_captain_review(
        policy=policy, pull=pull, head_sha=head_sha, runner=runner, gh=gh
    )
    effective = None
    if latest is not None:
        state = latest.get("state")
        effective = str(state).upper() if isinstance(state, str) else None
    if effective != expected_state:
        raise HelmetIssueError(
            checkpoint_note(
                "err",
                "formal_review_mismatch",
                effective or "none",
                expected_state,
            )
        )
    event_id = _normalize_review_event_id(latest.get("id") if latest else None)
    submitted_raw = latest.get("submitted_at") if latest else None
    submitted_at = submitted_raw.strip() if isinstance(submitted_raw, str) and submitted_raw.strip() else None
    return event_id, submitted_at


def apply_review_outcome(
    issue_url: str,
    *,
    head_sha: str,
    outcome: str,
    summary: str = "",
    checkpoint_dir: Path = DEFAULT_CHECKPOINT_DIR,
    policy: Policy | None = None,
    ledger: Path = DEFAULT_LEDGER,
    runner: Runner | None = None,
    gh: str = DEFAULT_GH,
    hermes: str = DEFAULT_HERMES,
    config: Path | None = None,
    worker_runtime: Sequence[str] = (),
) -> Checkpoint:
    """Record a Captain review only after binding to live head + formal review."""

    if not head_sha or not str(head_sha).strip():
        raise HelmetIssueError(checkpoint_note("err", "head_sha_required"))
    head_sha = str(head_sha).strip()
    # summary intentionally unused for persistence (GitHub holds prose).
    del summary

    path = checkpoint_path(checkpoint_dir, issue_url)
    checkpoint = load_checkpoint(path, expected_issue_url=issue_url)
    if checkpoint is None:
        raise HelmetIssueError(checkpoint_note("err", "no_checkpoint"))

    if policy is None:
        if config is None:
            raise HelmetIssueError(checkpoint_note("err", "policy_or_config_required"))
        policy = load_policy_for_issue(config)

    runner = runner or SubprocessRunner(gh=gh, hermes=hermes)
    runtime = parse_worker_runtime(worker_runtime) or DEFAULT_WORKER_RUNTIME
    observed = observed_github_login(runner, gh=gh)
    try:
        verify_captain_identity(policy, observed)
    except AuthorityError as exc:
        raise HelmetIssueError(checkpoint_note("err", "captain_identity_mismatch")) from exc

    issue = load_issue(policy, issue_url, runner, gh=gh)
    _root, pull, _repair = discover_progress(
        policy, issue, ledger, runner, gh=gh, hermes=hermes, worker_runtime=runtime
    )
    if pull is None:
        raise HelmetIssueError(checkpoint_note("err", "no_live_pr_for_review"))
    if pull.head_sha.casefold() != head_sha.casefold():
        raise HelmetIssueError(checkpoint_note("err", "review_head_mismatch"))
    if checkpoint.pr_url and checkpoint.pr_url != pull.url:
        raise HelmetIssueError(checkpoint_note("err", "checkpoint_pr_mismatch"))

    review_event_id, review_submitted_at = _verify_formal_review(
        policy=policy,
        pull=pull,
        head_sha=head_sha,
        outcome=outcome,
        runner=runner,
        gh=gh,
    )

    prior_cycle_event_id: int | None = None
    if outcome == "changes_requested":
        watch = resolve_ledger_watch(
            ledger, issue_url, runner, worker_runtime=runtime
        )
        prior_cycle_event_id = _delivery_review_event_id(
            watch=watch, policy=policy, runner=runner, hermes=hermes
        )

    if outcome == "changes_requested":
        # Skill posts the formal GitHub review; we only flip state so H1 can react.
        record_review_result(
            checkpoint,
            head_sha=head_sha,
            outcome=outcome,
            summary="",
            review_event_id=review_event_id,
            prior_cycle_event_id=prior_cycle_event_id,
            review_submitted_at=review_submitted_at,
        )
        checkpoint.state = "WAIT_REPAIR"
    else:
        record_review_result(
            checkpoint,
            head_sha=head_sha,
            outcome=outcome,
            summary="",
            review_event_id=review_event_id,
            review_submitted_at=review_submitted_at,
        )
    save_checkpoint(path, checkpoint)
    return checkpoint


def load_policy_for_issue(path: Path) -> Policy:
    try:
        return load_authority(path)
    except AuthorityError:
        # Fall back to poller loader for clearer errors on v1 docs.
        try:
            policy = load_poller_policy(path)
        except PollerError as exc:
            raise HelmetIssueError(checkpoint_note("err", "policy_load_failed")) from exc
        if policy.version < 2:
            raise HelmetIssueError(checkpoint_note("err", "policy_version_unsupported"))
        return policy
