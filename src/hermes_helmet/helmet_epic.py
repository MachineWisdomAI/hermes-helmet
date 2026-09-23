#!/usr/bin/env python3
"""Captain-side epic orchestration helpers for helmet-epic.

Operates one GitHub epic root plus its child-issue dependency graph. Native
GitHub sub-issue and dependency relationships are preferred; explicit body
``Parent`` and ``Blocked by`` issue links are the documented fallback. Never
infers edges from issue numbers or prose ordering.

The epic root is an orchestration record only: it is never dispatch-labeled and
never sent to a worker. Children are driven by invoking helmet-issue helpers
with bounded parallelism (policy ``max_epic_parallelism``, default 2).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Sequence
from urllib.parse import quote

from hermes_helmet.authority import (
    AuthorityError,
    Policy,
    merge_authority_for,
    verify_captain_identity,
)
from hermes_helmet.github_issue_poller import ISSUE_URL_RE
from hermes_helmet.helmet_issue import (
    DEFAULT_CHECKPOINT_DIR,
    DEFAULT_CONFIG,
    DEFAULT_GH,
    DEFAULT_HERMES,
    DEFAULT_LEDGER,
    DEFAULT_WORKER_RUNTIME,
    HelmetIssueError,
    IssueRef,
    Runner,
    SubprocessRunner,
    checkpoint_note,
    load_issue,
    load_policy_for_issue,
    observed_github_login,
    parse_issue_url,
    parse_worker_runtime,
    run_preflight_and_adopt,
    sanitize_public_text,
    status_issue,
    _github_json,
)

# Re-export defaults for CLI convenience.
__all__ = [
    "DEFAULT_CHECKPOINT_DIR",
    "DEFAULT_CONFIG",
    "DEFAULT_GH",
    "DEFAULT_HERMES",
    "DEFAULT_LEDGER",
    "HelmetEpicError",
    "EpicCheckpoint",
    "EpicStatusReport",
    "EpicGraph",
    "EpicNode",
    "run_epic_pass",
    "status_epic",
    "load_policy_for_epic",
]

EPIC_CHECKPOINT_VERSION = 1
EPIC_STATES = (
    "PREFLIGHT",
    "GRAPH_READY",
    "GRAPH_CHANGED",
    "DISPATCHING",
    "WAITING",
    "DONE",
    "BLOCKED",
    "FAILED",
)
EPIC_TERMINAL = frozenset({"DONE", "BLOCKED", "FAILED"})

# Child rollup buckets reported by status.
CHILD_BUCKETS = (
    "completed",
    "active",
    "ready",
    "blocked",
    "failed",
    "awaiting_human",
)

# Explicit body sections (fallback when native sub-issue APIs are empty).
_PARENT_HEADING_RE = re.compile(
    r"(?im)^[ \t]{0,3}#{1,6}[ \t]+parent(?:s)?[ \t]*$"
)
_BLOCKED_BY_HEADING_RE = re.compile(
    r"(?im)^[ \t]{0,3}#{1,6}[ \t]+blocked[ \t]+by[ \t]*$"
)
_NEXT_HEADING_RE = re.compile(r"(?im)^[ \t]{0,3}#{1,6}[ \t]+")
# Inline full URLs must consume the whole issue path — `/issues/2oops` is not #2.
_ISSUE_URL_INLINE_RE = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+"
    r"(?![A-Za-z0-9_.-]|/)"
)
# Whole-line Parent / Blocked-by without a heading (single-link forms).
_PARENT_LINE_RE = re.compile(
    r"(?im)^[ \t]{0,3}(?:[-*][ \t]+)?parent\s*:\s*(\S+)\s*$"
)
_BLOCKED_BY_LINE_RE = re.compile(
    r"(?im)^[ \t]{0,3}(?:[-*][ \t]+)?blocked[ \t]+by\s*:\s*(\S+)\s*$"
)
# Markdown link target capture (full URL or same-repo hash / slug#N forms).
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
# Same-repository short refs are resolved in the *declaring issue's* repository.
_HASH_REF_RE = re.compile(r"^#(\d+)$")
_SLUG_HASH_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)$")
# Operative empty-blocker lines inside a Blocked-by section.
_EMPTY_BLOCKER_LINE_RE = re.compile(
    r"(?i)^(?:none|n/?a|no blockers?|nothing|-|—|–)(?:\s*[.—].*)?$"
)
# Human-only must be an operative instruction line/title, not arbitrary prose.
_HUMAN_ONLY_TITLE_RE = re.compile(
    r"(?i)(?:^|\s)human-only(?:\s|$)|(?:^|\s)never\s+dispatch(?:\s|$)|"
    r"\(\s*human[\s\-]+only\s*\)|\[\s*human[\s\-]+only\s*\]"
)
_HUMAN_ONLY_PAREN_RE = re.compile(
    r"(?i)(?:\(\s*human[\s\-]+only\s*\)|\[\s*human[\s\-]+only\s*\])"
)
# Operative section heading, e.g. "## Human-only authority gate".
_HUMAN_ONLY_HEADING_RE = re.compile(
    r"(?im)^[ \t]{0,3}#{1,6}[ \t]+(?:.*\b)?human[\s\-]+only\b"
)
_HUMAN_ONLY_LINE_RE = re.compile(
    r"(?i)^(?:[-*]\s+|\d+\.\s+)?(?:\*\*|__)?("
    r"human-only\b.*|"
    r"human\s+only\b.*|"
    r"never\s+dispatch\b.*|"
    r"do\s+not\s+dispatch\b.*|"
    r"this\s+ticket\s+is\s+human-only\b.*|"
    r"this\s+ticket\s+is\s+human\s+only\b.*"
    r")(?:\*\*|__)?"
)


class HelmetEpicError(RuntimeError):
    """helmet-epic cannot safely continue."""


@dataclass(frozen=True)
class EpicNode:
    """One issue in the epic graph (root or child)."""

    url: str
    number: int
    repository_slug: str
    title: str
    body: str
    state: str  # open | closed
    state_reason: str | None  # completed | not_planned | reopened | None
    labels: tuple[str, ...]
    is_root: bool = False
    parent_urls: tuple[str, ...] = ()
    blocked_by_urls: tuple[str, ...] = ()
    source: str = "body"  # native_sub_issue | native_dependency | body

    @property
    def closed_complete(self) -> bool:
        if self.state != "closed":
            return False
        # GitHub: completed = done; not_planned = incomplete close.
        if self.state_reason is None:
            # Legacy closes without reason count as complete for blockers.
            return True
        return self.state_reason.casefold() == "completed"

    @property
    def closed_incomplete(self) -> bool:
        if self.state != "closed":
            return False
        reason = (self.state_reason or "").casefold()
        return reason in {"not_planned", "duplicate"}


@dataclass(frozen=True)
class EpicGraph:
    """Validated epic dependency graph."""

    root_url: str
    root: EpicNode
    children: tuple[EpicNode, ...]
    # directed edges: child_url -> frozenset of blocker urls (must complete first)
    blocked_by: dict[str, frozenset[str]]
    fingerprint: str
    source: str  # native | body | mixed
    # External (non-child) blockers keyed by URL; states participate in readiness.
    external_blockers: dict[str, EpicNode] = field(default_factory=dict)

    def child_map(self) -> dict[str, EpicNode]:
        return {node.url: node for node in self.children}

    def blocker_node(self, url: str) -> EpicNode | None:
        return self.child_map().get(url) or self.external_blockers.get(url)

    def all_urls(self) -> frozenset[str]:
        return frozenset(
            {
                self.root_url,
                *(c.url for c in self.children),
                *self.external_blockers,
            }
        )


@dataclass
class EpicCheckpoint:
    """Non-secret resumable epic orchestration state."""

    version: int
    epic_url: str
    state: str
    graph_fingerprint: str | None = None
    accepted_fingerprint: str | None = None
    max_parallelism: int = 2
    continue_independent_branches: bool = True
    active_children: list[str] = field(default_factory=list)
    completed_children: list[str] = field(default_factory=list)
    failed_children: list[str] = field(default_factory=list)
    blocked_children: list[str] = field(default_factory=list)
    awaiting_human_children: list[str] = field(default_factory=list)
    last_blocker: str | None = None
    started_at: str = ""
    updated_at: str = ""
    notes: list[str] = field(default_factory=list)
    host_continuation: str = "unknown"
    one_pass_only: bool = False
    epic_merge_mode: str = "explicit_captain_approval"
    child_invocations: int = 0

    def to_public_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class EpicStatusReport:
    epic_url: str
    state: str
    fingerprint: str | None
    max_parallelism: int
    continue_independent_branches: bool
    completed: tuple[str, ...]
    active: tuple[str, ...]
    ready: tuple[str, ...]
    blocked: tuple[str, ...]
    failed: tuple[str, ...]
    awaiting_human: tuple[str, ...]
    root_open: bool
    blocker: str | None
    terminal: bool
    details: dict[str, object] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "epic_url": self.epic_url,
            "state": self.state,
            "fingerprint": self.fingerprint,
            "max_parallelism": self.max_parallelism,
            "continue_independent_branches": self.continue_independent_branches,
            "completed": list(self.completed),
            "active": list(self.active),
            "ready": list(self.ready),
            "blocked": list(self.blocked),
            "failed": list(self.failed),
            "awaiting_human": list(self.awaiting_human),
            "root_open": self.root_open,
            "blocker": self.blocker,
            "terminal": self.terminal,
            "details": self.details,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_policy_for_epic(path: Path) -> Policy:
    return load_policy_for_issue(path)


def epic_checkpoint_path(checkpoint_dir: Path, epic_url: str) -> Path:
    digest = hashlib.sha256(epic_url.encode("utf-8")).hexdigest()[:24]
    match = ISSUE_URL_RE.fullmatch(epic_url)
    if match is None:
        stem = f"epic-{digest}"
    else:
        owner, name = match.group("slug").split("/", 1)
        stem = f"epic__{owner}__{name}__{match.group('number')}__{digest[:8]}"
    return checkpoint_dir / f"{stem}.json"


def load_epic_checkpoint(
    path: Path,
    *,
    expected_epic_url: str | None = None,
) -> EpicCheckpoint | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HelmetEpicError(f"epic checkpoint is unreadable: {path}") from exc
    if not isinstance(raw, dict):
        raise HelmetEpicError("epic checkpoint must be a JSON object")
    try:
        version = int(raw["version"])
        if version != EPIC_CHECKPOINT_VERSION:
            raise HelmetEpicError(f"unsupported epic checkpoint version: {version}")
        epic_url = str(raw["epic_url"]).strip()
        if not ISSUE_URL_RE.fullmatch(epic_url):
            raise HelmetEpicError("epic checkpoint epic_url is malformed")
        if expected_epic_url is not None and epic_url != expected_epic_url.strip():
            raise HelmetEpicError("epic checkpoint epic_url does not match request")
        state = str(raw["state"])
        if state not in EPIC_STATES and state != "UNKNOWN":
            raise HelmetEpicError(f"epic checkpoint state is unknown: {state}")

        def _str_list(key: str) -> list[str]:
            value = raw.get(key) or []
            if not isinstance(value, list):
                raise TypeError(key)
            return [str(item) for item in value if str(item).strip()]

        notes = raw.get("notes") or []
        if not isinstance(notes, list):
            raise TypeError("notes")
        return EpicCheckpoint(
            version=version,
            epic_url=epic_url,
            state=state,
            graph_fingerprint=(
                None
                if raw.get("graph_fingerprint") is None
                else str(raw.get("graph_fingerprint"))
            ),
            accepted_fingerprint=(
                None
                if raw.get("accepted_fingerprint") is None
                else str(raw.get("accepted_fingerprint"))
            ),
            max_parallelism=max(1, int(raw.get("max_parallelism") or 2)),
            continue_independent_branches=bool(
                raw.get("continue_independent_branches", True)
            ),
            active_children=_str_list("active_children"),
            completed_children=_str_list("completed_children"),
            failed_children=_str_list("failed_children"),
            blocked_children=_str_list("blocked_children"),
            awaiting_human_children=_str_list("awaiting_human_children"),
            last_blocker=(
                None
                if raw.get("last_blocker") is None
                else sanitize_public_text(str(raw.get("last_blocker")))
            ),
            started_at=str(raw.get("started_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            notes=[sanitize_public_text(str(item), max_len=400) for item in notes],
            host_continuation=str(raw.get("host_continuation") or "unknown"),
            one_pass_only=bool(raw.get("one_pass_only", False)),
            epic_merge_mode=str(
                raw.get("epic_merge_mode") or "explicit_captain_approval"
            ),
            child_invocations=max(0, int(raw.get("child_invocations") or 0)),
        )
    except HelmetEpicError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise HelmetEpicError("epic checkpoint is missing required fields") from exc


def save_epic_checkpoint(path: Path, checkpoint: EpicCheckpoint) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if checkpoint.version != EPIC_CHECKPOINT_VERSION:
        raise HelmetEpicError(
            f"refusing to persist unsupported epic checkpoint version: {checkpoint.version}"
        )
    if checkpoint.state not in EPIC_STATES and checkpoint.state != "UNKNOWN":
        raise HelmetEpicError(
            f"refusing to persist unknown epic checkpoint state: {checkpoint.state}"
        )
    checkpoint.last_blocker = (
        None
        if checkpoint.last_blocker is None
        else sanitize_public_text(str(checkpoint.last_blocker))
    )
    checkpoint.notes = [
        sanitize_public_text(str(item), max_len=400) for item in checkpoint.notes
    ]
    checkpoint.updated_at = _utc_now()
    if not checkpoint.started_at:
        checkpoint.started_at = checkpoint.updated_at
    text = json.dumps(checkpoint.to_public_dict(), indent=2, sort_keys=True) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def normalize_issue_url(url: str) -> str:
    text = (url or "").strip().rstrip("/")
    match = ISSUE_URL_RE.fullmatch(text)
    if match is None:
        raise HelmetEpicError(checkpoint_note("err", "issue_url_malformed"))
    slug = match.group("slug")
    number = match.group("number")
    return f"https://github.com/{slug}/issues/{number}"


def _section_body(text: str, heading_re: re.Pattern[str]) -> str | None:
    match = heading_re.search(text or "")
    if match is None:
        return None
    rest = text[match.end() :]
    next_heading = _NEXT_HEADING_RE.search(rest)
    if next_heading is None:
        return rest
    return rest[: next_heading.start()]


def resolve_issue_ref(token: str, *, repository_slug: str) -> str | None:
    """Resolve a full URL or same-repo short ref in the declaring issue's repo.

    Accepted forms: full issue URL, ``#N``, ``owner/name#N``. Returns None when
    the token is empty/not a ref (caller decides whether that is malformed).
    GitHub issue URL *attempts* that are not exact issue URLs raise rather than
    silently accepting a numeric prefix (``…/issues/2oops`` is not issue #2).
    """

    raw = (token or "").strip().rstrip(").,;\"'")
    if not raw:
        return None
    if ISSUE_URL_RE.fullmatch(raw):
        return normalize_issue_url(raw)
    # URL-shaped tokens that fail fullmatch are malformed refs, not "not a ref".
    if re.match(r"(?i)^https?://(?:www\.)?github\.com/", raw):
        raise HelmetEpicError(checkpoint_note("err", "issue_url_malformed", raw[:80]))
    if re.match(r"(?i)^(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/", raw):
        raise HelmetEpicError(checkpoint_note("err", "issue_url_malformed", raw[:80]))
    hash_match = _HASH_REF_RE.fullmatch(raw)
    if hash_match is not None:
        if not repository_slug or "/" not in repository_slug:
            raise HelmetEpicError(checkpoint_note("err", "issue_ref_repo_missing", raw))
        return normalize_issue_url(
            f"https://github.com/{repository_slug}/issues/{hash_match.group(1)}"
        )
    slug_match = _SLUG_HASH_RE.fullmatch(raw)
    if slug_match is not None:
        return normalize_issue_url(
            f"https://github.com/{slug_match.group(1)}/issues/{slug_match.group(2)}"
        )
    return None


def _line_looks_like_ref_attempt(line: str) -> bool:
    text = (line or "").strip()
    if not text:
        return False
    if _EMPTY_BLOCKER_LINE_RE.fullmatch(text):
        return False
    if text.startswith(("#", "http://", "https://")):
        return True
    if _MD_LINK_RE.search(text):
        return True
    if _SLUG_HASH_RE.fullmatch(text) or _HASH_REF_RE.fullmatch(text):
        return True
    if re.search(r"(?i)\bissues?/\d+\b", text):
        return True
    if re.search(r"(?i)[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+", text):
        return True
    return False


def _urls_from_block(
    block: str,
    *,
    repository_slug: str,
    section: str,
    reject_malformed: bool,
) -> list[str]:
    """Extract issue refs from an operative Parent / Blocked-by body section."""

    found: list[str] = []
    seen: set[str] = set()

    def _add(url: str) -> None:
        if url not in seen:
            seen.add(url)
            found.append(url)

    def _reject(token: str) -> None:
        raise HelmetEpicError(
            checkpoint_note("err", "malformed_issue_link", section, token[:80])
        )

    for raw_line in (block or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        is_bullet = bool(re.match(r"^(?:[-*]|\d+\.)\s+", line))
        bullet = re.sub(r"^(?:[-*]|\d+\.)\s+", "", line).strip()
        if not bullet:
            continue
        if _EMPTY_BLOCKER_LINE_RE.fullmatch(bullet):
            continue
        # Free prose under a section (non-list, non-ref-shaped) is ignored so
        # operative instructions after Parent links do not become false edges.
        if not is_bullet and not _line_looks_like_ref_attempt(bullet):
            continue

        working = bullet
        line_resolved = False

        # Markdown link targets first (mutate a working copy for residual checks).
        md_spans: list[tuple[int, int]] = []
        for match in _MD_LINK_RE.finditer(bullet):
            target = match.group(1).strip().rstrip(").,;")
            try:
                url = resolve_issue_ref(target, repository_slug=repository_slug)
            except HelmetEpicError:
                if reject_malformed:
                    raise
                url = None
            if url is not None:
                _add(url)
                line_resolved = True
                md_spans.append((match.start(), match.end()))
            elif reject_malformed:
                _reject(target)
        if md_spans:
            parts: list[str] = []
            cursor = 0
            for start, end in md_spans:
                parts.append(working[cursor:start])
                parts.append(" ")
                cursor = end
            parts.append(working[cursor:])
            working = "".join(parts)

        # Bare full URLs (boundary-safe; numeric prefixes of malformed paths
        # are not accepted — those fall through to residual rejection).
        url_spans: list[tuple[int, int]] = []
        for match in _ISSUE_URL_INLINE_RE.finditer(working):
            _add(normalize_issue_url(match.group(0)))
            line_resolved = True
            url_spans.append((match.start(), match.end()))
        if url_spans:
            parts = []
            cursor = 0
            for start, end in url_spans:
                parts.append(working[cursor:start])
                parts.append(" ")
                cursor = end
            parts.append(working[cursor:])
            working = "".join(parts)

        residual = working.strip()
        if residual:
            # Any github.com …/issues/<token> residue that is not a clean URL
            # must fail closed (including /issues/2oops after a rejected prefix).
            for bad in re.finditer(
                r"(?i)https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\S+",
                residual,
            ):
                token = bad.group(0).rstrip(").,;\"'")
                if not ISSUE_URL_RE.fullmatch(token):
                    if reject_malformed:
                        _reject(token)
                    continue
            # Path fragments like /issues/not-a-number must not hide behind a
            # valid sibling URL on the same declaration line.
            for path_match in re.finditer(r"(?i)/issues?/([^\s/]+)", residual):
                token = path_match.group(0)
                if not path_match.group(1).isdigit():
                    if reject_malformed:
                        _reject(token)
                    continue
                if reject_malformed:
                    # Bare /issues/N without host is incomplete.
                    _reject(token)

            for token in residual.split():
                cleaned = token.strip().rstrip(").,;\"'")
                if not cleaned or _EMPTY_BLOCKER_LINE_RE.fullmatch(cleaned):
                    continue
                if not (
                    _line_looks_like_ref_attempt(cleaned)
                    or cleaned.startswith(("#", "/"))
                    or cleaned.casefold().startswith("issues/")
                ):
                    # Non-ref residual prose on a mixed line is ignored once
                    # every ref-shaped token has been considered.
                    continue
                try:
                    url = resolve_issue_ref(cleaned, repository_slug=repository_slug)
                except HelmetEpicError:
                    if reject_malformed:
                        raise
                    url = None
                if url is not None:
                    _add(url)
                    line_resolved = True
                elif reject_malformed:
                    _reject(cleaned)

        if not line_resolved and reject_malformed and (
            is_bullet or _line_looks_like_ref_attempt(bullet)
        ):
            _reject(bullet[:80])
    return found


def parse_parent_links(
    body: str,
    *,
    repository_slug: str,
    reject_malformed: bool = True,
) -> tuple[str, ...]:
    """Return explicit Parent issue URLs (full URL or same-repo short refs)."""

    urls: list[str] = []
    seen: set[str] = set()
    section = _section_body(body or "", _PARENT_HEADING_RE)
    if section is not None:
        for url in _urls_from_block(
            section,
            repository_slug=repository_slug,
            section="parent",
            reject_malformed=reject_malformed,
        ):
            if url not in seen:
                seen.add(url)
                urls.append(url)
    for match in _PARENT_LINE_RE.finditer(body or ""):
        raw = match.group(1).strip().rstrip(").,;")
        url = resolve_issue_ref(raw, repository_slug=repository_slug)
        if url is None:
            if reject_malformed and raw:
                raise HelmetEpicError(
                    checkpoint_note("err", "malformed_issue_link", "parent", raw[:80])
                )
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return tuple(urls)


def parse_blocked_by_links(
    body: str,
    *,
    repository_slug: str,
    reject_malformed: bool = True,
) -> tuple[str, ...]:
    """Return explicit Blocked-by issue URLs (full URL or same-repo short refs)."""

    urls: list[str] = []
    seen: set[str] = set()
    section = _section_body(body or "", _BLOCKED_BY_HEADING_RE)
    if section is not None:
        for url in _urls_from_block(
            section,
            repository_slug=repository_slug,
            section="blocked_by",
            reject_malformed=reject_malformed,
        ):
            if url not in seen:
                seen.add(url)
                urls.append(url)
    for match in _BLOCKED_BY_LINE_RE.finditer(body or ""):
        raw = match.group(1).strip().rstrip(").,;")
        if _EMPTY_BLOCKER_LINE_RE.fullmatch(raw):
            continue
        url = resolve_issue_ref(raw, repository_slug=repository_slug)
        if url is None:
            if reject_malformed and raw:
                raise HelmetEpicError(
                    checkpoint_note(
                        "err", "malformed_issue_link", "blocked_by", raw[:80]
                    )
                )
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return tuple(urls)


def is_human_only_issue(node: EpicNode) -> bool:
    """True only when title/body carries an operative never-dispatch instruction."""

    title = (node.title or "").strip()
    if _HUMAN_ONLY_PAREN_RE.search(title):
        # Parenthetical/bracket owner markers are always operative, e.g. "(human only)".
        return True
    if _HUMAN_ONLY_TITLE_RE.search(title):
        # Titles that merely mention another human-only gate use longer prose;
        # require the marker to dominate a short title or start the title.
        compact = re.sub(r"\s+", " ", title).strip()
        if len(compact) <= 80 or compact.casefold().startswith("human-only"):
            return True
    body = node.body or ""
    if _HUMAN_ONLY_HEADING_RE.search(body):
        return True
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Strip simple emphasis wrappers for whole-line matches.
        candidate = re.sub(r"^\*\*(.*)\*\*$", r"\1", line).strip()
        candidate = re.sub(r"^__(.*)__$", r"\1", candidate).strip()
        if _HUMAN_ONLY_LINE_RE.match(candidate):
            return True
    return False


def graph_fingerprint(
    root_url: str,
    children: Sequence[EpicNode],
    blocked_by: dict[str, frozenset[str]],
) -> str:
    child_urls = sorted(node.url for node in children)
    edges: list[str] = []
    for child in child_urls:
        blockers = sorted(blocked_by.get(child, frozenset()))
        edges.append(f"{child}|{'|'.join(blockers)}")
    payload = json.dumps(
        {"root": root_url, "children": child_urls, "edges": edges},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _issue_ref_to_node(
    issue: IssueRef,
    *,
    is_root: bool,
    parent_urls: tuple[str, ...] = (),
    blocked_by_urls: tuple[str, ...] = (),
    source: str = "body",
    state_reason: str | None = None,
) -> EpicNode:
    return EpicNode(
        url=issue.url,
        number=issue.number,
        repository_slug=issue.repository.slug,
        title=issue.title,
        body=issue.body,
        state=issue.state,
        state_reason=state_reason,
        labels=issue.labels,
        is_root=is_root,
        parent_urls=parent_urls,
        blocked_by_urls=blocked_by_urls,
        source=source,
    )


def load_issue_node(
    policy: Policy,
    issue_url: str,
    runner: Runner,
    *,
    gh: str = DEFAULT_GH,
    is_root: bool = False,
    source: str = "body",
) -> EpicNode:
    issue = load_issue(policy, issue_url, runner, gh=gh)
    # Fetch state_reason via API fields (REST includes it on modern payloads).
    slug, number = parse_issue_url(issue_url)
    raw = _github_json(runner, gh, f"repos/{slug}/issues/{number}")
    state_reason = None
    if isinstance(raw, dict):
        reason = raw.get("state_reason")
        if isinstance(reason, str) and reason.strip():
            state_reason = reason.strip()
    parents = parse_parent_links(issue.body, repository_slug=issue.repository.slug)
    blocked = parse_blocked_by_links(issue.body, repository_slug=issue.repository.slug)
    return _issue_ref_to_node(
        issue,
        is_root=is_root,
        parent_urls=parents,
        blocked_by_urls=blocked,
        source=source,
        state_reason=state_reason,
    )


def _paginate_github_list(
    runner: Runner,
    gh: str,
    endpoint: str,
    *,
    per_page: int = 100,
    max_pages: int = 20,
) -> list[object]:
    """Fetch a GitHub list endpoint with explicit pagination.

    Distinguishes transport failure (raises) from a genuinely empty list.
    """

    items: list[object] = []
    page = 1
    separator = "&" if "?" in endpoint else "?"
    while page <= max_pages:
        page_endpoint = f"{endpoint}{separator}per_page={per_page}&page={page}"
        payload = _github_json(runner, gh, page_endpoint)
        if payload is None:
            break
        if isinstance(payload, list):
            batch = payload
        elif isinstance(payload, dict):
            nested = (
                payload.get("sub_issues")
                or payload.get("dependencies")
                or payload.get("blocked_by")
                or payload.get("data")
                or payload.get("items")
            )
            if nested is None and not payload:
                batch = []
            elif isinstance(nested, list):
                batch = nested
            else:
                raise HelmetEpicError(
                    checkpoint_note("err", "native_list_payload_ambiguous", endpoint)
                )
        else:
            raise HelmetEpicError(
                checkpoint_note("err", "native_list_payload_invalid", endpoint)
            )
        if not batch:
            break
        items.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    if page > max_pages:
        raise HelmetEpicError(
            checkpoint_note("err", "native_list_incomplete", endpoint)
        )
    return items


def _issue_urls_from_native_items(items: list[object], *, endpoint: str) -> list[str]:
    urls: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise HelmetEpicError(
                checkpoint_note("err", "native_list_item_invalid", endpoint)
            )
        html = item.get("html_url") or item.get("url")
        if isinstance(html, str) and ISSUE_URL_RE.fullmatch(html.strip().rstrip("/")):
            urls.append(normalize_issue_url(html))
            continue
        nested_issue = item.get("issue") if isinstance(item.get("issue"), dict) else item
        if isinstance(nested_issue, dict):
            html2 = nested_issue.get("html_url")
            if isinstance(html2, str) and ISSUE_URL_RE.fullmatch(
                html2.strip().rstrip("/")
            ):
                urls.append(normalize_issue_url(html2))
                continue
        raise HelmetEpicError(
            checkpoint_note("err", "native_list_link_ambiguous", endpoint)
        )
    # Deduplicate preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        if url in seen:
            raise HelmetEpicError(
                checkpoint_note("err", "duplicate_native_child", url)
            )
        seen.add(url)
        ordered.append(url)
    return ordered


def _github_http_status(exc: BaseException) -> int | None:
    """Extract typed httpNNN status from a sanitized command_failed note."""

    match = re.search(r"(?i)(?:^|:)http(\d{3})(?:$|:)", str(exc))
    if match is None:
        return None
    return int(match.group(1))


def _is_native_relationship_unavailable(exc: BaseException) -> bool:
    """True only for unsupported/not-found native relationship endpoints.

    Denied (401/403), malformed, and generic transport failures must fail closed
    rather than being treated as an empty/unavailable relationship.
    """

    status = _github_http_status(exc)
    if status == 404:
        return True
    if status is not None:
        return False
    text = str(exc).casefold()
    # Typed not-found markers without a conflicting auth denial.
    if "http404" in text or "not found" in text:
        return "http401" not in text and "http403" not in text
    return False


def _try_native_sub_issues(
    root: EpicNode,
    runner: Runner,
    *,
    gh: str,
) -> list[str] | None:
    """Return child issue URLs from native sub-issue membership APIs.

    Returns None when the endpoint is unavailable (404/empty-unavailable), an
    empty list when the API explicitly returns zero membership rows, and raises
    on denied/malformed/incomplete reads. Does **not** consult dependency
    endpoints — those are prerequisite edges, not membership.
    """

    slug = root.repository_slug
    number = root.number
    endpoint = f"repos/{slug}/issues/{number}/sub_issues"
    try:
        items = _paginate_github_list(runner, gh, endpoint)
    except HelmetIssueError as exc:
        if _is_native_relationship_unavailable(exc):
            return None
        raise HelmetEpicError(
            checkpoint_note("err", "native_sub_issue_read_failed", endpoint)
        ) from exc
    try:
        return _issue_urls_from_native_items(items, endpoint=endpoint)
    except HelmetEpicError:
        raise


def child_is_native_sub_issue_of(
    parent_url: str,
    child_url: str,
    runner: Runner,
    *,
    gh: str = DEFAULT_GH,
) -> bool | None:
    """Return whether ``child_url`` is a native sub-issue of ``parent_url``.

    ``True`` when the parent's sub-issue membership list includes the child.
    ``False`` when the API returns a complete list that does not include the
    child. ``None`` when the membership endpoint is unavailable (404). Denied
    or malformed reads raise ``HelmetEpicError`` (fail closed).
    """

    parent = normalize_issue_url(parent_url)
    child = normalize_issue_url(child_url)
    slug, number_s = parse_issue_url(parent)
    number = int(number_s)
    root = EpicNode(
        url=parent,
        number=number,
        repository_slug=slug,
        title="",
        body="",
        state="open",
        state_reason=None,
        labels=(),
        source="native_probe",
    )
    members = _try_native_sub_issues(root, runner, gh=gh)
    if members is None:
        return None
    return child in members


def native_parent_of(
    child_url: str,
    runner: Runner,
    *,
    gh: str = DEFAULT_GH,
) -> str | None:
    """Return the child's live native parent issue URL, if any.

    Uses ``GET /repos/{owner}/{repo}/issues/{n}/parent``. Returns ``None`` when
    the endpoint reports no parent (HTTP 404) or is otherwise unavailable as a
    relationship. Denied/malformed reads raise ``HelmetEpicError`` (fail closed).
    A present native parent is authoritative over stale body ``Parent`` links.
    """

    child = normalize_issue_url(child_url)
    slug, number_s = parse_issue_url(child)
    endpoint = f"repos/{slug}/issues/{int(number_s)}/parent"
    try:
        payload = _github_json(runner, gh, endpoint)
    except HelmetIssueError as exc:
        if _is_native_relationship_unavailable(exc):
            return None
        raise HelmetEpicError(
            checkpoint_note("err", "native_parent_read_failed", endpoint)
        ) from exc
    if not isinstance(payload, dict):
        raise HelmetEpicError(
            checkpoint_note("err", "native_parent_payload_invalid", endpoint)
        )
    html = payload.get("html_url")
    if not isinstance(html, str) or not ISSUE_URL_RE.fullmatch(html.strip().rstrip("/")):
        # Some payloads nest the issue; accept a single clear html_url only.
        nested = payload.get("issue") if isinstance(payload.get("issue"), dict) else None
        if isinstance(nested, dict):
            html = nested.get("html_url")
        if not isinstance(html, str) or not ISSUE_URL_RE.fullmatch(
            html.strip().rstrip("/")
        ):
            raise HelmetEpicError(
                checkpoint_note("err", "native_parent_link_ambiguous", endpoint)
            )
    parent = normalize_issue_url(html)
    if parent == child:
        raise HelmetEpicError(checkpoint_note("err", "native_parent_self", child))
    return parent


def _try_native_blocked_by(
    node: EpicNode,
    runner: Runner,
    *,
    gh: str,
) -> list[str] | None:
    """Return native blocked-by prerequisite URLs for one issue, or None if N/A."""

    endpoint = (
        f"repos/{node.repository_slug}/issues/{node.number}/dependencies/blocked_by"
    )
    try:
        items = _paginate_github_list(runner, gh, endpoint)
    except HelmetIssueError as exc:
        if _is_native_relationship_unavailable(exc):
            return None
        raise HelmetEpicError(
            checkpoint_note("err", "native_blocked_by_read_failed", endpoint)
        ) from exc
    try:
        return _issue_urls_from_native_items(items, endpoint=endpoint)
    except HelmetEpicError:
        raise


def _search_parent_children(
    policy: Policy,
    root_url: str,
    runner: Runner,
    *,
    gh: str,
) -> list[str]:
    """Discover child issues via search for explicit Parent body links.

    Searches each allowlisted repository for the full epic URL and the same-repo
    ``#N`` short form. Distinguishes failed/incomplete search from empty hits.
    Does not invent edges from prose; membership still requires a validated
    Parent declaration on each candidate. Any failed query fails closed — a
    successful unrelated query does not complete missing discovery.
    """

    found: list[str] = []
    seen: set[str] = set()
    root_match = ISSUE_URL_RE.fullmatch(root_url)
    if root_match is None:
        raise HelmetEpicError(checkpoint_note("err", "issue_url_malformed"))
    root_number = root_match.group("number")
    root_slug = root_match.group("slug")
    queries: list[str] = [f'"{root_url}"']
    # Same-repo short Parent form (e.g. "#42") only meaningful inside root repo,
    # but we still search all allowlisted repos and validate Parent afterward.
    queries.append(f'"{root_slug}#{root_number}"')
    queries.append(f'"#{root_number}"')

    search_errors: list[str] = []
    for repo in policy.repositories:
        for query in queries:
            quoted = quote(query, safe="")
            endpoint = (
                f"search/issues?q=repo:{repo.slug}+is:issue+{quoted}&per_page=100"
            )
            try:
                payload = _github_json(runner, gh, endpoint)
            except HelmetIssueError as exc:
                search_errors.append(f"{repo.slug}:{query}:{type(exc).__name__}")
                continue
            if not isinstance(payload, dict):
                raise HelmetEpicError(checkpoint_note("err", "search_payload_invalid"))
            if payload.get("incomplete_results") is True:
                raise HelmetEpicError(
                    checkpoint_note("err", "search_incomplete_results", repo.slug)
                )
            total = payload.get("total_count")
            items = payload.get("items")
            if items is None:
                raise HelmetEpicError(checkpoint_note("err", "search_items_missing"))
            if not isinstance(items, list):
                raise HelmetEpicError(checkpoint_note("err", "search_items_invalid"))
            if isinstance(total, int) and total > len(items) and total > 100:
                # Single-page cap — fail closed rather than silently shrink graph.
                raise HelmetEpicError(
                    checkpoint_note(
                        "err",
                        "search_results_truncated",
                        repo.slug,
                        str(total),
                    )
                )
            for item in items:
                if not isinstance(item, dict):
                    raise HelmetEpicError(checkpoint_note("err", "search_item_invalid"))
                html = item.get("html_url")
                if not isinstance(html, str):
                    continue
                try:
                    url = normalize_issue_url(html)
                except HelmetEpicError:
                    continue
                if url == root_url:
                    continue
                if url not in seen:
                    seen.add(url)
                    found.append(url)
    if search_errors:
        # Partial success must not hide a failed discovery query.
        raise HelmetEpicError(
            checkpoint_note("err", "search_unavailable", str(len(search_errors)))
        )
    return found


def _filter_search_candidates_by_parent(
    policy: Policy,
    root_url: str,
    candidates: Sequence[str],
    runner: Runner,
    *,
    gh: str,
    existing: set[str],
) -> list[str]:
    """Keep search hits only when live Parent membership points at the epic.

    Ordinary mentions of the epic URL or ``#N`` are skipped. Explicit/native
    seeds are not routed through this filter and retain strict validation.
    Ambiguous multi-Parent declarations that include the epic still fail closed.
    """

    kept: list[str] = []
    for raw in candidates:
        url = normalize_issue_url(raw)
        if url == root_url or url in existing:
            continue
        node = load_issue_node(policy, url, runner, gh=gh, is_root=False, source="body")
        parents = tuple(node.parent_urls)
        if not parents:
            continue
        if root_url not in parents:
            continue
        if len(set(parents)) > 1:
            raise HelmetEpicError(
                checkpoint_note("err", "child_ambiguous_parents", url)
            )
        kept.append(url)
    return kept


def build_epic_graph(
    policy: Policy,
    epic_url: str,
    runner: Runner,
    *,
    gh: str = DEFAULT_GH,
    extra_child_urls: Sequence[str] = (),
) -> EpicGraph:
    """Load and validate the epic graph from GitHub + explicit body links."""

    root_url = normalize_issue_url(epic_url)
    root = load_issue_node(policy, root_url, runner, gh=gh, is_root=True, source="root")

    native_children_result = _try_native_sub_issues(root, runner, gh=gh)
    native_children = list(native_children_result or ())
    search_hits = _search_parent_children(policy, root_url, runner, gh=gh)

    # Explicit operator seeds: duplicates in the same seed list are rejected.
    seed_from_extra: list[str] = []
    seen_extra: set[str] = set()
    for url in extra_child_urls:
        norm = normalize_issue_url(url)
        if norm in seen_extra:
            raise HelmetEpicError(checkpoint_note("err", "duplicate_explicit_child", norm))
        seen_extra.add(norm)
        seed_from_extra.append(norm)

    # Declared membership seeds (native + explicit) are strict.
    seed_urls: list[str] = []
    seen_seed: set[str] = set()
    for url in [*native_children, *seed_from_extra]:
        norm = normalize_issue_url(url)
        if norm == root_url:
            raise HelmetEpicError(checkpoint_note("err", "self_edge_root_as_child"))
        if norm not in seen_seed:
            seen_seed.add(norm)
            seed_urls.append(norm)

    # Search hits are candidates only — filter by Parent before membership.
    search_children = _filter_search_candidates_by_parent(
        policy,
        root_url,
        search_hits,
        runner,
        gh=gh,
        existing=seen_seed,
    )
    for url in search_children:
        if url not in seen_seed:
            seen_seed.add(url)
            seed_urls.append(url)

    if not seed_urls:
        raise HelmetEpicError(checkpoint_note("err", "epic_has_no_children"))

    nodes: dict[str, EpicNode] = {}
    source_tags: set[str] = set()
    native_set = set(native_children)
    for url in seed_urls:
        if url in native_set:
            src = "native"
        else:
            src = "body"
        source_tags.add(src)
        node = load_issue_node(policy, url, runner, gh=gh, is_root=False, source=src)
        nodes[url] = node

    # Validate membership for body-discovered / explicit children.
    # Native parent association (GET …/parent) is authoritative when present: a
    # stale body Parent must not admit a child that has moved under another
    # native parent. When no native parent exists, body Parent remains the
    # fallback (empty-native plans). Search candidates were already
    # Parent-filtered; re-check so explicit seeds and concurrent edits fail closed.
    for url, node in list(nodes.items()):
        if node.source == "native":
            # Native membership already selected this child for this epic.
            continue
        try:
            live_native_parent = native_parent_of(url, runner, gh=gh)
        except HelmetEpicError:
            raise
        if live_native_parent is not None:
            if live_native_parent != root_url:
                raise HelmetEpicError(
                    checkpoint_note("err", "child_native_parent_not_epic", url)
                )
            # Live native parent is this epic — body Parent is informational only.
            continue
        parents = node.parent_urls
        if not parents:
            raise HelmetEpicError(
                checkpoint_note("err", "child_missing_parent_link", url)
            )
        if root_url not in parents:
            raise HelmetEpicError(
                checkpoint_note("err", "child_parent_not_epic", url)
            )
        if len(set(parents)) > 1:
            # Multiple distinct parents — ambiguous ownership for this epic skill.
            raise HelmetEpicError(
                checkpoint_note("err", "child_ambiguous_parents", url)
            )

    # Collect blocked-by edges from body + each child's native dependencies.
    blocked_by: dict[str, frozenset[str]] = {}
    external: dict[str, EpicNode] = {}
    for url, node in nodes.items():
        blockers = list(node.blocked_by_urls)
        native_blockers = _try_native_blocked_by(node, runner, gh=gh)
        if native_blockers:
            for burl in native_blockers:
                if burl not in blockers:
                    blockers.append(burl)
        normalized_blockers: list[str] = []
        for blocker in blockers:
            burl = normalize_issue_url(blocker)
            if burl == url:
                raise HelmetEpicError(checkpoint_note("err", "self_edge", url))
            if burl == root_url:
                # Root is orchestration-only; blocking on root is meaningless.
                raise HelmetEpicError(
                    checkpoint_note("err", "blocked_by_epic_root", url)
                )
            normalized_blockers.append(burl)
            if burl not in nodes and burl not in external:
                # Blockers may live on allowlisted repos even if not epic children.
                try:
                    external[burl] = load_issue_node(
                        policy, burl, runner, gh=gh, is_root=False, source="blocker"
                    )
                except HelmetIssueError as exc:
                    raise HelmetEpicError(
                        checkpoint_note("err", "blocker_missing_or_denied", burl)
                    ) from exc
        # Reject duplicate blocker entries as ambiguity.
        if len(normalized_blockers) != len(set(normalized_blockers)):
            raise HelmetEpicError(checkpoint_note("err", "duplicate_blocker_links", url))
        blocked_by[url] = frozenset(normalized_blockers)

    # Incomplete closed blockers fail validation (cannot satisfy edge).
    for child, blockers in blocked_by.items():
        for burl in blockers:
            node = nodes.get(burl) or external.get(burl)
            if node is None:
                raise HelmetEpicError(checkpoint_note("err", "blocker_unresolved", burl))
            if node.closed_incomplete:
                raise HelmetEpicError(
                    checkpoint_note("err", "blocker_closed_incomplete", burl)
                )

    children = tuple(sorted(nodes.values(), key=lambda n: (n.repository_slug, n.number)))

    # Cycle detection among children (external completed blockers are ok).
    child_urls = {n.url for n in children}
    adjacency = {
        url: frozenset(b for b in blocked_by.get(url, frozenset()) if b in child_urls)
        for url in child_urls
    }
    _assert_acyclic(adjacency)

    fingerprint = graph_fingerprint(root_url, children, blocked_by)
    if source_tags == {"native"}:
        source = "native"
    elif source_tags == {"body"}:
        source = "body"
    else:
        source = "mixed"
    return EpicGraph(
        root_url=root_url,
        root=root,
        children=children,
        blocked_by=blocked_by,
        fingerprint=fingerprint,
        source=source,
        external_blockers=dict(external),
    )


def _assert_acyclic(adjacency: dict[str, frozenset[str]]) -> None:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in adjacency}

    def visit(node: str) -> None:
        color[node] = GRAY
        for nxt in adjacency.get(node, frozenset()):
            if nxt not in color:
                continue
            if color[nxt] == GRAY:
                raise HelmetEpicError(checkpoint_note("err", "dependency_cycle", node))
            if color[nxt] == WHITE:
                visit(nxt)
        color[node] = BLACK

    for node in adjacency:
        if color[node] == WHITE:
            visit(node)


def blockers_satisfied(
    child_url: str,
    graph: EpicGraph,
    *,
    completed_urls: set[str],
) -> bool:
    blockers = graph.blocked_by.get(child_url, frozenset())
    if not blockers:
        return True
    for burl in blockers:
        if burl in completed_urls:
            continue
        node = graph.blocker_node(burl)
        if node is None:
            return False
        if node.closed_complete:
            continue
        # Child blockers may also be completed via helmet-issue DONE without
        # GitHub close yet — only completed_urls covers that path above.
        return False
    return True


def _merge_gate_awaits_human(merge_gate: str | None) -> bool:
    """True when status merge_gate means Captain must act before merge.

    Unattended authority modes remain machine-progress even when the suffix is
    ``:awaiting`` (waiting on clean checks), and must not consume a human slot.
    """

    text = (merge_gate or "").strip().casefold()
    if not text:
        return False
    mode, sep, rest = text.partition(":")
    if mode == "unattended_when_clean":
        return False
    if text in {
        "stop_for_approval",
        "awaiting_captain_approval",
        "explicit_captain_approval",
    }:
        return True
    if mode == "explicit_captain_approval":
        return rest in {"awaiting", "stop_for_approval", ""} or "stop_for_approval" in rest
    # Status emits ``{mode}:awaiting`` (and similar) for merge-gate stops.
    if text.endswith(":awaiting") or text.endswith(":stop_for_approval"):
        return True
    if "stop_for_approval" in text:
        return True
    return False


def classify_children(
    policy: Policy,
    graph: EpicGraph,
    *,
    checkpoint_dir: Path,
    runner: Runner,
    gh: str,
    hermes: str,
    ledger: Path,
    worker_runtime: Sequence[str],
    epic_body: str,
) -> dict[str, list[str]]:
    """Classify each child into status buckets using live GitHub + issue checkpoints."""

    buckets: dict[str, list[str]] = {name: [] for name in CHILD_BUCKETS}
    completed: set[str] = set()
    child_map = graph.child_map()

    # First pass: closed-complete children.
    for url, node in child_map.items():
        if node.closed_complete:
            completed.add(url)
            buckets["completed"].append(url)

    # Second pass: checkpoints / human-only / blocked-by.
    for url, node in child_map.items():
        if url in completed:
            continue
        if is_human_only_issue(node):
            buckets["awaiting_human"].append(url)
            continue
        if not blockers_satisfied(url, graph, completed_urls=completed):
            buckets["blocked"].append(url)
            continue

        # Inspect helmet-issue checkpoint / live status when present.
        try:
            report = status_issue(
                policy,
                url,
                ledger=ledger,
                checkpoint_dir=checkpoint_dir,
                runner=runner,
                gh=gh,
                hermes=hermes,
                worker_runtime=worker_runtime,
            )
        except HelmetIssueError:
            # No status yet — ready for dispatch if open and labeled appropriately.
            if node.state != "open":
                buckets["failed"].append(url)
            else:
                buckets["ready"].append(url)
            continue

        state = (report.state or "").upper()
        if report.terminal and state == "DONE":
            buckets["completed"].append(url)
            completed.add(url)
            continue
        if report.terminal and state == "FAILED":
            buckets["failed"].append(url)
            continue
        if report.terminal and state == "BLOCKED":
            buckets["awaiting_human"].append(url)
            continue

        # Authoritative recovered root ownership counts as in-flight before any
        # local PREFLIGHT/UNKNOWN checkpoint is treated as freshly ready.
        if report.root_task_id and state in {"PREFLIGHT", "UNKNOWN", ""}:
            buckets["active"].append(url)
            continue
        if report.root_task_id and not report.terminal and state not in {
            "DONE",
            "FAILED",
            "BLOCKED",
        }:
            # Fall through to state-specific active/awaiting handling below, but
            # never classify a live owned root as ready solely due to UNKNOWN.
            pass

        if state in {
            "DISPATCH",
            "WAIT_PR",
            "REVIEW_HEAD",
            "REQUEST_REPAIR",
            "WAIT_REPAIR",
            "READY",
            "MERGE_GATE",
            "MERGE",
            "VERIFY_MERGED",
        }:
            # Merge gate stop for approval is awaiting human when not unattended.
            if state in {"READY", "MERGE_GATE"} and _merge_gate_awaits_human(
                report.merge_gate
            ):
                buckets["awaiting_human"].append(url)
            else:
                buckets["active"].append(url)
            continue
        if state in {"PREFLIGHT", "UNKNOWN", ""}:
            if report.root_task_id:
                buckets["active"].append(url)
            else:
                buckets["ready"].append(url)
            continue
        # Fallback: treat non-terminal unknown as active if root task exists.
        if report.root_task_id:
            buckets["active"].append(url)
        else:
            buckets["ready"].append(url)

    # Stable ordering.
    for key in buckets:
        buckets[key] = sorted(buckets[key])
    return buckets


def compute_ready_frontier(
    buckets: dict[str, list[str]],
    *,
    max_parallelism: int,
    continue_independent_branches: bool = True,
) -> list[str]:
    """Return child URLs to invoke next (not yet active), capped by free slots."""

    active = list(buckets.get("active") or [])
    ready = list(buckets.get("ready") or [])
    failed = list(buckets.get("failed") or [])
    if failed and not continue_independent_branches:
        return []
    free = max(0, max_parallelism - len(active))
    if free <= 0:
        return []
    return ready[:free]


def compute_resume_children(buckets: dict[str, list[str]]) -> list[str]:
    """Return in-flight children that must be re-entered before new dispatches."""

    return list(buckets.get("active") or [])


def preflight_epic(
    policy: Policy,
    epic_url: str,
    runner: Runner,
    *,
    gh: str = DEFAULT_GH,
) -> tuple[EpicNode, str, str]:
    """Validate Captain identity, allowlist, epic root state, budgets, labels config."""

    if policy.version < 2:
        raise HelmetEpicError(checkpoint_note("err", "policy_version_unsupported"))
    if not policy.captain_github_login or not policy.github_identity:
        raise HelmetEpicError(checkpoint_note("err", "policy_identities_missing"))
    if policy.captain_github_login.casefold() == policy.github_identity.casefold():
        raise HelmetEpicError(checkpoint_note("err", "captain_worker_must_differ"))
    if policy.budgets.max_issue_runtime_minutes < 1 or policy.budgets.max_repair_rounds < 1:
        raise HelmetEpicError(checkpoint_note("err", "policy_budgets_invalid"))
    if policy.max_epic_parallelism < 1:
        raise HelmetEpicError(checkpoint_note("err", "max_epic_parallelism_invalid"))
    if not policy.ready_label or not policy.dispatch_label:
        raise HelmetEpicError(checkpoint_note("err", "policy_labels_missing"))
    if policy.ready_label.casefold() == policy.dispatch_label.casefold():
        raise HelmetEpicError(checkpoint_note("err", "ready_dispatch_labels_collide"))

    observed = observed_github_login(runner, gh=gh)
    try:
        verify_captain_identity(policy, observed)
    except AuthorityError as exc:
        raise HelmetEpicError(checkpoint_note("err", "captain_identity_mismatch")) from exc

    root = load_issue_node(policy, epic_url, runner, gh=gh, is_root=True, source="root")
    if root.state != "open":
        raise HelmetEpicError(checkpoint_note("err", "epic_root_not_open"))
    # Root must NEVER carry dispatch label (orchestration record only).
    known = {label.casefold() for label in root.labels}
    if policy.dispatch_label.casefold() in known:
        raise HelmetEpicError(checkpoint_note("err", "epic_root_has_dispatch_label"))
    merge_mode = merge_authority_for(policy, issue_body=root.body)
    return root, observed, merge_mode


def _ensure_root_not_dispatched(
    policy: Policy,
    root: EpicNode,
    runner: Runner,
    *,
    gh: str,
) -> None:
    """Hard guard: never add dispatch label to epic root; fail if present."""

    known = {label.casefold() for label in root.labels}
    if policy.dispatch_label.casefold() in known:
        raise HelmetEpicError(checkpoint_note("err", "epic_root_has_dispatch_label"))
    # Defense in depth: do not call any label mutation API for the root.


def invoke_child_issue(
    policy: Policy,
    child_url: str,
    *,
    epic_url: str,
    epic_body: str,
    ledger: Path,
    checkpoint_dir: Path,
    runner: Runner,
    gh: str,
    hermes: str,
    host_continuation: str,
    one_pass_only: bool,
    apply_dispatch: bool,
    worker_runtime: Sequence[str],
) -> dict[str, object]:
    """Invoke one helmet-issue pass for a child, propagating epic merge authority."""

    checkpoint, report = run_preflight_and_adopt(
        policy,
        child_url,
        ledger=ledger,
        checkpoint_dir=checkpoint_dir,
        runner=runner,
        gh=gh,
        hermes=hermes,
        host_continuation=host_continuation,
        one_pass_only=one_pass_only,
        apply_dispatch=apply_dispatch,
        worker_runtime=worker_runtime,
        epic_body=epic_body,
        parent_epic_url=epic_url,
    )
    return {
        "issue_url": child_url,
        "checkpoint": checkpoint.to_public_dict(),
        "status": report.to_public_dict(),
    }


def run_epic_pass(
    policy: Policy,
    epic_url: str,
    *,
    ledger: Path = DEFAULT_LEDGER,
    checkpoint_dir: Path = DEFAULT_CHECKPOINT_DIR,
    runner: Runner | None = None,
    gh: str = DEFAULT_GH,
    hermes: str = DEFAULT_HERMES,
    host_continuation: str = "unknown",
    one_pass_only: bool = False,
    apply_dispatch: bool = True,
    accept_graph: bool = False,
    continue_independent_branches: bool = True,
    max_parallelism: int | None = None,
    extra_child_urls: Sequence[str] = (),
    worker_runtime: Sequence[str] = (),
    child_invoker=None,
) -> tuple[EpicCheckpoint, EpicStatusReport, EpicGraph | None]:
    """One truthful epic orchestration pass.

    Loads/validates the graph, classifies children, and optionally invokes
    helmet-issue for ready frontier children up to max parallelism. Never
    dispatch-labels the epic root. Leaves the epic open by default.
    """

    runner = runner or SubprocessRunner(gh=gh, hermes=hermes)
    runtime = parse_worker_runtime(worker_runtime) or DEFAULT_WORKER_RUNTIME
    epic_url = normalize_issue_url(epic_url)
    path = epic_checkpoint_path(checkpoint_dir, epic_url)
    checkpoint = load_epic_checkpoint(path, expected_epic_url=epic_url)
    if checkpoint is None:
        checkpoint = EpicCheckpoint(
            version=EPIC_CHECKPOINT_VERSION,
            epic_url=epic_url,
            state="PREFLIGHT",
        )
    checkpoint.host_continuation = host_continuation
    checkpoint.one_pass_only = one_pass_only
    checkpoint.continue_independent_branches = continue_independent_branches
    parallelism = (
        max_parallelism
        if max_parallelism is not None
        else max(1, int(policy.max_epic_parallelism))
    )
    checkpoint.max_parallelism = parallelism

    graph: EpicGraph | None = None
    try:
        root, _observed, epic_merge_mode = preflight_epic(
            policy, epic_url, runner, gh=gh
        )
        checkpoint.epic_merge_mode = epic_merge_mode
        _ensure_root_not_dispatched(policy, root, runner, gh=gh)

        graph = build_epic_graph(
            policy,
            epic_url,
            runner,
            gh=gh,
            extra_child_urls=extra_child_urls,
        )
        checkpoint.graph_fingerprint = graph.fingerprint

        # Graph change gate: pause new dispatch until Captain accepts.
        if checkpoint.accepted_fingerprint is None:
            if accept_graph or checkpoint.state == "PREFLIGHT":
                checkpoint.accepted_fingerprint = graph.fingerprint
                checkpoint.state = "GRAPH_READY"
                checkpoint.last_blocker = None
                checkpoint.notes.append(
                    checkpoint_note("op", "accepted_graph", graph.fingerprint[:12])
                )
            else:
                checkpoint.state = "GRAPH_CHANGED"
                checkpoint.last_blocker = checkpoint_note(
                    "err", "graph_acceptance_required", graph.fingerprint[:12]
                )
                save_epic_checkpoint(path, checkpoint)
                report = build_epic_status(
                    policy,
                    graph,
                    checkpoint,
                    ledger=ledger,
                    checkpoint_dir=checkpoint_dir,
                    runner=runner,
                    gh=gh,
                    hermes=hermes,
                    worker_runtime=runtime,
                )
                return checkpoint, report, graph
        elif checkpoint.accepted_fingerprint != graph.fingerprint:
            if accept_graph:
                checkpoint.accepted_fingerprint = graph.fingerprint
                checkpoint.state = "GRAPH_READY"
                checkpoint.last_blocker = None
                checkpoint.notes.append(
                    checkpoint_note("op", "accepted_refreshed_graph", graph.fingerprint[:12])
                )
            else:
                checkpoint.state = "GRAPH_CHANGED"
                checkpoint.last_blocker = checkpoint_note(
                    "err", "graph_changed_pause_dispatch", graph.fingerprint[:12]
                )
                # Still refresh classification for status, but do not invoke children.
                save_epic_checkpoint(path, checkpoint)
                report = build_epic_status(
                    policy,
                    graph,
                    checkpoint,
                    ledger=ledger,
                    checkpoint_dir=checkpoint_dir,
                    runner=runner,
                    gh=gh,
                    hermes=hermes,
                    worker_runtime=runtime,
                )
                return checkpoint, report, graph
        else:
            # Fingerprint still accepted; clear a stale pause if operator re-runs.
            if checkpoint.state == "GRAPH_CHANGED":
                checkpoint.state = "GRAPH_READY"
                checkpoint.last_blocker = None

        buckets = classify_children(
            policy,
            graph,
            checkpoint_dir=checkpoint_dir,
            runner=runner,
            gh=gh,
            hermes=hermes,
            ledger=ledger,
            worker_runtime=runtime,
            epic_body=root.body,
        )
        checkpoint.completed_children = list(buckets["completed"])
        checkpoint.active_children = list(buckets["active"])
        checkpoint.failed_children = list(buckets["failed"])
        checkpoint.blocked_children = list(buckets["blocked"])
        checkpoint.awaiting_human_children = list(buckets["awaiting_human"])

        # All children terminal success and none failed/awaiting → DONE (root stays open).
        child_urls = {c.url for c in graph.children}
        settled = set(buckets["completed"]) | set(buckets["awaiting_human"])
        # Human-only children remain awaiting forever; epic DONE when every
        # non-human-only child is completed and none failed/active/ready/blocked.
        non_human = [
            c.url for c in graph.children if not is_human_only_issue(c)
        ]
        if (
            non_human
            and all(url in set(buckets["completed"]) for url in non_human)
            and not buckets["failed"]
            and not buckets["active"]
            and not buckets["ready"]
            and not buckets["blocked"]
        ):
            checkpoint.state = "DONE"
            checkpoint.last_blocker = None
            checkpoint.notes.append(checkpoint_note("op", "epic_children_complete"))
            save_epic_checkpoint(path, checkpoint)
            report = build_epic_status(
                policy,
                graph,
                checkpoint,
                buckets=buckets,
                ledger=ledger,
                checkpoint_dir=checkpoint_dir,
                runner=runner,
                gh=gh,
                hermes=hermes,
                worker_runtime=runtime,
            )
            return checkpoint, report, graph

        if buckets["failed"] and not continue_independent_branches:
            checkpoint.state = "FAILED"
            checkpoint.last_blocker = checkpoint_note(
                "err", "child_failed_stop", buckets["failed"][0]
            )
            save_epic_checkpoint(path, checkpoint)
            report = build_epic_status(
                policy,
                graph,
                checkpoint,
                buckets=buckets,
                ledger=ledger,
                checkpoint_dir=checkpoint_dir,
                runner=runner,
                gh=gh,
                hermes=hermes,
                worker_runtime=runtime,
            )
            return checkpoint, report, graph

        frontier = compute_ready_frontier(
            buckets,
            max_parallelism=parallelism,
            continue_independent_branches=continue_independent_branches,
        )
        # Re-enter existing in-flight children before allocating new ready work.
        resume = compute_resume_children(buckets)
        to_invoke: list[str] = []
        seen_invoke: set[str] = set()
        for child_url in [*resume, *frontier]:
            if child_url in seen_invoke:
                continue
            seen_invoke.add(child_url)
            to_invoke.append(child_url)

        invoker = child_invoker or invoke_child_issue
        if apply_dispatch and to_invoke and checkpoint.state != "GRAPH_CHANGED":
            checkpoint.state = "DISPATCHING"
            for child_url in to_invoke:
                # Never allow root URL into child invoker.
                if child_url == epic_url:
                    raise HelmetEpicError(
                        checkpoint_note("err", "refusing_dispatch_epic_root")
                    )
                child_node = graph.child_map()[child_url]
                if is_human_only_issue(child_node):
                    continue
                result = invoker(
                    policy,
                    child_url,
                    epic_url=epic_url,
                    epic_body=root.body,
                    ledger=ledger,
                    checkpoint_dir=checkpoint_dir,
                    runner=runner,
                    gh=gh,
                    hermes=hermes,
                    host_continuation=host_continuation,
                    one_pass_only=one_pass_only,
                    apply_dispatch=apply_dispatch,
                    worker_runtime=runtime,
                )
                checkpoint.child_invocations += 1
                status_obj = result.get("status") if isinstance(result, dict) else None
                child_state = "unknown"
                if isinstance(status_obj, dict):
                    child_state = str(status_obj.get("state") or "unknown")
                checkpoint.notes.append(
                    checkpoint_note(
                        "op",
                        "invoked_child",
                        child_url.split("/")[-1],
                        child_state,
                    )
                )
            # Reclassify after invocations.
            buckets = classify_children(
                policy,
                graph,
                checkpoint_dir=checkpoint_dir,
                runner=runner,
                gh=gh,
                hermes=hermes,
                ledger=ledger,
                worker_runtime=runtime,
                epic_body=root.body,
            )
            checkpoint.completed_children = list(buckets["completed"])
            checkpoint.active_children = list(buckets["active"])
            checkpoint.failed_children = list(buckets["failed"])
            checkpoint.blocked_children = list(buckets["blocked"])
            checkpoint.awaiting_human_children = list(buckets["awaiting_human"])

        if buckets["active"] or buckets["ready"] or buckets["blocked"]:
            checkpoint.state = "WAITING"
            checkpoint.last_blocker = None
        elif buckets["awaiting_human"] and not buckets["failed"]:
            checkpoint.state = "WAITING"
            checkpoint.last_blocker = checkpoint_note("op", "awaiting_human_children")
        elif buckets["failed"]:
            checkpoint.state = "WAITING" if continue_independent_branches else "FAILED"
            checkpoint.last_blocker = checkpoint_note(
                "err", "child_failed", buckets["failed"][0]
            )
        else:
            checkpoint.state = "GRAPH_READY"

        # Root stays open — never close epic from this skill by default.
        _ensure_root_not_dispatched(policy, root, runner, gh=gh)
        save_epic_checkpoint(path, checkpoint)
        report = build_epic_status(
            policy,
            graph,
            checkpoint,
            buckets=buckets,
            ledger=ledger,
            checkpoint_dir=checkpoint_dir,
            runner=runner,
            gh=gh,
            hermes=hermes,
            worker_runtime=runtime,
        )
        return checkpoint, report, graph
    except (HelmetEpicError, HelmetIssueError) as exc:
        code = str(exc)
        if not code.startswith("err:"):
            code = checkpoint_note("err", "epic_failed")
        # Live graph/preflight uncertainty must never report terminal success.
        # Mirror status_epic: leave on-disk DONE (and other terminal history)
        # unchanged when the current pass cannot verify completion; demote the
        # live report only. New/non-terminal runs still record FAILED.
        historical = checkpoint.state
        report_state = historical
        if historical == "DONE":
            report_state = "WAITING"
        elif historical not in EPIC_TERMINAL:
            checkpoint.state = "FAILED"
            checkpoint.last_blocker = code
            checkpoint.notes.append(code)
            save_epic_checkpoint(path, checkpoint)
            report_state = "FAILED"
        else:
            # BLOCKED/FAILED historical: keep bytes; still nonterminal live view
            # when we could not re-verify (graph is None).
            report_state = historical if graph is not None else "WAITING"
        report = EpicStatusReport(
            epic_url=epic_url,
            state=report_state,
            fingerprint=checkpoint.graph_fingerprint,
            max_parallelism=checkpoint.max_parallelism,
            continue_independent_branches=checkpoint.continue_independent_branches,
            completed=tuple(checkpoint.completed_children),
            active=tuple(checkpoint.active_children),
            ready=(),
            blocked=tuple(checkpoint.blocked_children),
            failed=tuple(checkpoint.failed_children),
            awaiting_human=tuple(checkpoint.awaiting_human_children),
            root_open=True,
            blocker=code,
            terminal=False,
            details={"error": code, "checkpoint_state": historical},
        )
        return checkpoint, report, graph


def build_epic_status(
    policy: Policy,
    graph: EpicGraph,
    checkpoint: EpicCheckpoint,
    *,
    buckets: dict[str, list[str]] | None = None,
    ledger: Path = DEFAULT_LEDGER,
    checkpoint_dir: Path = DEFAULT_CHECKPOINT_DIR,
    runner: Runner | None = None,
    gh: str = DEFAULT_GH,
    hermes: str = DEFAULT_HERMES,
    worker_runtime: Sequence[str] = (),
    persist_state: bool = False,
) -> EpicStatusReport:
    """Build status from the live graph + buckets.

    When the live graph fingerprint differs from the accepted checkpoint, report
    GRAPH_CHANGED rather than a stale terminal DONE. Does not write checkpoints
    unless ``persist_state`` is True (epic pass owns writes).
    """

    del persist_state  # reserved; status path never mutates by default
    runner = runner or SubprocessRunner(gh=gh, hermes=hermes)
    runtime = parse_worker_runtime(worker_runtime) or DEFAULT_WORKER_RUNTIME
    if buckets is None:
        buckets = classify_children(
            policy,
            graph,
            checkpoint_dir=checkpoint_dir,
            runner=runner,
            gh=gh,
            hermes=hermes,
            ledger=ledger,
            worker_runtime=runtime,
            epic_body=graph.root.body,
        )

    state = checkpoint.state
    blocker = checkpoint.last_blocker
    accepted = checkpoint.accepted_fingerprint
    if accepted is not None and accepted != graph.fingerprint:
        state = "GRAPH_CHANGED"
        blocker = checkpoint_note(
            "err", "graph_changed_pause_dispatch", graph.fingerprint[:12]
        )
    else:
        # Derive completion from live buckets so a stale DONE checkpoint cannot
        # mask newly ready/blocked children after graph membership changes that
        # still share a fingerprint edge case — or after resume without accept.
        non_human = [c.url for c in graph.children if not is_human_only_issue(c)]
        if buckets.get("failed") and not checkpoint.continue_independent_branches:
            state = "FAILED"
            blocker = checkpoint_note("err", "child_failed", buckets["failed"][0])
        elif (
            non_human
            and all(url in set(buckets.get("completed") or ()) for url in non_human)
            and not buckets.get("failed")
            and not buckets.get("active")
            and not buckets.get("ready")
            and not buckets.get("blocked")
        ):
            state = "DONE"
            blocker = None
        elif state == "DONE" and (
            buckets.get("ready")
            or buckets.get("active")
            or buckets.get("blocked")
            or buckets.get("failed")
        ):
            state = "WAITING"
            blocker = None
        elif state in EPIC_TERMINAL and state != "DONE":
            pass
        elif (
            buckets.get("active")
            or buckets.get("ready")
            or buckets.get("blocked")
            or buckets.get("awaiting_human")
            or buckets.get("failed")
        ):
            if state not in {"GRAPH_CHANGED", "DISPATCHING", "FAILED", "BLOCKED"}:
                state = "WAITING" if state != "PREFLIGHT" else state

    return EpicStatusReport(
        epic_url=graph.root_url,
        state=state,
        fingerprint=graph.fingerprint,
        max_parallelism=checkpoint.max_parallelism,
        continue_independent_branches=checkpoint.continue_independent_branches,
        completed=tuple(buckets.get("completed") or ()),
        active=tuple(buckets.get("active") or ()),
        ready=tuple(buckets.get("ready") or ()),
        blocked=tuple(buckets.get("blocked") or ()),
        failed=tuple(buckets.get("failed") or ()),
        awaiting_human=tuple(buckets.get("awaiting_human") or ()),
        root_open=graph.root.state == "open",
        blocker=blocker,
        terminal=state in EPIC_TERMINAL,
        details={
            "graph_source": graph.source,
            "child_count": len(graph.children),
            "epic_merge_mode": checkpoint.epic_merge_mode,
            "accepted_fingerprint": checkpoint.accepted_fingerprint,
            "child_invocations": checkpoint.child_invocations,
            "checkpoint_state": checkpoint.state,
        },
    )


def status_epic(
    policy: Policy,
    epic_url: str,
    *,
    ledger: Path = DEFAULT_LEDGER,
    checkpoint_dir: Path = DEFAULT_CHECKPOINT_DIR,
    runner: Runner | None = None,
    gh: str = DEFAULT_GH,
    hermes: str = DEFAULT_HERMES,
    extra_child_urls: Sequence[str] = (),
    worker_runtime: Sequence[str] = (),
) -> EpicStatusReport:
    """Read-oriented epic status. Rebuilds graph from GitHub; no child dispatch."""

    runner = runner or SubprocessRunner(gh=gh, hermes=hermes)
    runtime = parse_worker_runtime(worker_runtime) or DEFAULT_WORKER_RUNTIME
    epic_url = normalize_issue_url(epic_url)
    path = epic_checkpoint_path(checkpoint_dir, epic_url)
    checkpoint = load_epic_checkpoint(path, expected_epic_url=epic_url)
    if checkpoint is None:
        checkpoint = EpicCheckpoint(
            version=EPIC_CHECKPOINT_VERSION,
            epic_url=epic_url,
            state="UNKNOWN",
        )
    try:
        graph = build_epic_graph(
            policy,
            epic_url,
            runner,
            gh=gh,
            extra_child_urls=extra_child_urls,
        )
        checkpoint.graph_fingerprint = graph.fingerprint
        return build_epic_status(
            policy,
            graph,
            checkpoint,
            ledger=ledger,
            checkpoint_dir=checkpoint_dir,
            runner=runner,
            gh=gh,
            hermes=hermes,
            worker_runtime=runtime,
        )
    except (HelmetEpicError, HelmetIssueError) as exc:
        code = str(exc)
        if not code.startswith("err:"):
            code = checkpoint_note("err", "status_uncertain")
        # Graph-read uncertainty must never report terminal success. Keep the
        # on-disk checkpoint bytes unchanged; only the live status view demotes.
        state = checkpoint.state
        if state == "DONE":
            state = "WAITING"
        return EpicStatusReport(
            epic_url=epic_url,
            state=state,
            fingerprint=checkpoint.graph_fingerprint,
            max_parallelism=checkpoint.max_parallelism,
            continue_independent_branches=checkpoint.continue_independent_branches,
            completed=tuple(checkpoint.completed_children),
            active=tuple(checkpoint.active_children),
            ready=(),
            blocked=tuple(checkpoint.blocked_children),
            failed=tuple(checkpoint.failed_children),
            awaiting_human=tuple(checkpoint.awaiting_human_children),
            root_open=True,
            blocker=code,
            terminal=False,
            details={"error": code, "checkpoint_state": checkpoint.state},
        )
