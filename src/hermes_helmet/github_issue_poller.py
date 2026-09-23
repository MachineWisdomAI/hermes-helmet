#!/usr/bin/env python3
"""Create one Hermes Kanban task for each eligible allowlisted GitHub issue.

Hermes Helmet's control loop is designed to run inside the containerized Hermes
runtime (typically via a no-agent cron job). It uses the runtime ``gh`` binary
for GitHub API access and the ``hermes`` CLI for Kanban operations. The poller
never reads, logs, or persists token values itself.
"""


from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from typing import Protocol, Sequence
from urllib.parse import urlencode

from hermes_helmet.authority import (
    AuthorityError,
    Policy,
    Repository,
    is_trusted_bot,
    is_trusted_human,
    load_policy as load_authority_policy,
    render_issue_task_body,
    render_repair_task_body,
    verify_worker_identity,
)


DEFAULT_CONFIG = Path("/opt/data/github-issue-poller/policy.json")
DEFAULT_LEDGER = Path("/opt/data/github-issue-poller/ledger.sqlite3")
DEFAULT_GH = "/usr/local/bin/gh"
DEFAULT_HERMES = "/opt/hermes/.venv/bin/hermes"
# Overridable for tests and alternate layouts.
GH = DEFAULT_GH
HERMES = DEFAULT_HERMES
CREATED_BY = "github-issue-poller"
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ISSUE_URL_RE = re.compile(
    r"^https://github\.com/(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(?P<number>\d+)$"
)
PULL_REQUEST_URL_RE = re.compile(
    r"https://github\.com/(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(?P<number>\d+)"
)
TRUSTED_REVIEW_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
REVIEW_EVENT_KINDS = frozenset({"reviewed", "commented"})
TERMINAL_TASK_STATUSES = frozenset({"done", "archived"})
GITHUB_TIMELINE_ACCEPT = "Accept: application/vnd.github+json"


class PollerError(RuntimeError):
    """The poller cannot safely complete one pass."""


@dataclass(frozen=True)
class Issue:
    repository: Repository
    number: int
    title: str
    body: str

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repository.slug}/issues/{self.number}"

    @property
    def branch(self) -> str:
        name = self.repository.slug.rsplit("/", 1)[1].lower()
        return f"automation/{name}-{self.number}"


@dataclass(frozen=True)
class PullRequestContext:
    repository: Repository
    issue_number: int
    pull_number: int
    pull_url: str
    root_task_id: str
    assignee: str
    workspace_path: Path
    branch_name: str


@dataclass(frozen=True)
class ReviewEvent:
    event_id: int
    kind: str
    state: str
    actor: str
    url: str
    timestamp: str

    @property
    def sort_key(self) -> tuple[str, str, int]:
        return (self.timestamp, self.kind, self.event_id)

    def idempotency_key(self, context: PullRequestContext) -> str:
        return (
            f"github-pr-event:{context.repository.slug}:{context.pull_number}:"
            f"{self.kind}:{self.event_id}"
        )


@dataclass
class PollResult:
    created: list[tuple[Issue, str]]
    warnings: list[str]
    errors: list[str]


@dataclass(frozen=True)
class PullRequestWatch:
    pull_url: str | None
    discovery_complete: bool
    cursor: tuple[str, str, int] | None
    last_repair_task_id: str | None


class Runner(Protocol):
    def run(self, command: Sequence[str]) -> str: ...


class SubprocessRunner:
    def run(self, command: Sequence[str]) -> str:
        completed = subprocess.run(
            list(command), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if completed.returncode:
            executable = Path(command[0]).name
            raise PollerError(
                f"command failed ({executable}, exit {completed.returncode}); "
                "subprocess output withheld"
            )
        return completed.stdout


def load_policy(path: Path) -> Policy:
    """Load the adopter authority/policy document used by the control loop."""

    try:
        return load_authority_policy(path)
    except AuthorityError as exc:
        raise PollerError(str(exc)) from exc


def _github_api(
    runner: Runner,
    endpoint: str,
    *,
    headers: Sequence[str] = (),
) -> list[object]:
    command = [GH, "api", "--paginate"]
    for header in headers:
        command.extend(["-H", header])
    command.append(endpoint)
    output = runner.run(command)
    decoder = json.JSONDecoder()
    records: list[object] = []
    index = 0
    while index < len(output):
        while index < len(output) and output[index].isspace():
            index += 1
        if index == len(output):
            break
        try:
            page, index = decoder.raw_decode(output, index)
        except json.JSONDecodeError as exc:
            raise PollerError("GitHub API returned invalid JSON") from exc
        if not isinstance(page, list):
            raise PollerError("GitHub API returned a non-list page")
        records.extend(page)
    return records


def verify_identity(policy: Policy, runner: Runner) -> None:
    """Require the live GitHub login to be the configured worker identity."""

    identity = runner.run([GH, "api", "user", "--jq", ".login"]).strip()
    try:
        verify_worker_identity(policy, identity)
    except AuthorityError as exc:
        raise PollerError(str(exc)) from exc


def _label_names(raw: object) -> set[str]:
    if not isinstance(raw, list):
        return set()
    return {
        item["name"]
        for item in raw
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def list_eligible_issues(
    policy: Policy, runner: Runner
) -> tuple[list[Issue], list[str], list[str]]:
    issues: list[Issue] = []
    warnings: list[str] = []
    errors: list[str] = []
    for repository in policy.repositories:
        query = urlencode(
            {"state": "open", "labels": policy.required_label, "per_page": "100"}
        )
        try:
            records = _github_api(runner, f"repos/{repository.slug}/issues?{query}")
        except PollerError as exc:
            errors.append(f"Failed to list {repository.slug}: {exc}")
            continue
        for raw in records:
            if not isinstance(raw, dict):
                warnings.append(f"Skipped malformed issue record from {repository.slug}.")
                continue
            if "pull_request" in raw or raw.get("state") != "open":
                continue
            if policy.required_label not in _label_names(raw.get("labels")):
                continue
            number = raw.get("number")
            title = raw.get("title")
            body = raw.get("body")
            if not isinstance(number, int) or number < 1 or not isinstance(title, str) or not title.strip():
                warnings.append(f"Skipped malformed eligible issue from {repository.slug}.")
                continue
            if body is not None and not isinstance(body, str):
                warnings.append(f"Skipped malformed eligible issue from {repository.slug}.")
                continue
            issues.append(Issue(repository, number, title.strip(), body or ""))
    return issues, warnings, errors


def _task_body(issue: Issue, policy: Policy) -> str:
    return render_issue_task_body(
        issue_url=issue.url,
        issue_body=issue.body,
        policy=policy,
    )


def _ensure_ledger(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS issue_tasks (
            issue_url TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS pull_request_watches (
            issue_url TEXT PRIMARY KEY,
            pull_url TEXT,
            discovery_complete INTEGER NOT NULL DEFAULT 0,
            last_event_at TEXT,
            last_event_kind TEXT,
            last_event_id INTEGER,
            last_repair_task_id TEXT
        )
        """
    )


def _watch_state(
    connection: sqlite3.Connection,
    issue_url: str,
) -> PullRequestWatch:
    row = connection.execute(
        """
        SELECT pull_url, discovery_complete, last_event_at, last_event_kind,
               last_event_id, last_repair_task_id
        FROM pull_request_watches
        WHERE issue_url = ?
        """,
        (issue_url,),
    ).fetchone()
    if row is None:
        return PullRequestWatch(None, False, None, None)
    cursor = None
    if row[2] is not None and row[3] is not None and row[4] is not None:
        cursor = (str(row[2]), str(row[3]), int(row[4]))
    return PullRequestWatch(
        pull_url=str(row[0]) if row[0] is not None else None,
        discovery_complete=bool(row[1]),
        cursor=cursor,
        last_repair_task_id=str(row[5]) if row[5] is not None else None,
    )


def _save_watch_discovery(
    connection: sqlite3.Connection,
    issue_url: str,
    *,
    pull_url: str | None,
    complete: bool,
) -> None:
    connection.execute(
        """
        INSERT INTO pull_request_watches(issue_url, pull_url, discovery_complete)
        VALUES (?, ?, ?)
        ON CONFLICT(issue_url) DO UPDATE SET
            pull_url = excluded.pull_url,
            discovery_complete = excluded.discovery_complete
        """,
        (issue_url, pull_url, int(complete)),
    )


def _save_review_progress(
    connection: sqlite3.Connection,
    issue_url: str,
    event: ReviewEvent,
    task_id: str,
) -> None:
    connection.execute(
        """
        INSERT INTO pull_request_watches(
            issue_url, discovery_complete, last_event_at, last_event_kind,
            last_event_id, last_repair_task_id
        )
        VALUES (?, 1, ?, ?, ?, ?)
        ON CONFLICT(issue_url) DO UPDATE SET
            last_event_at = excluded.last_event_at,
            last_event_kind = excluded.last_event_kind,
            last_event_id = excluded.last_event_id,
            last_repair_task_id = excluded.last_repair_task_id
        """,
        (issue_url, event.timestamp, event.kind, event.event_id, task_id),
    )


def _tracked_tasks(ledger: Path) -> list[tuple[str, str]]:
    ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(ledger, timeout=30)
    try:
        _ensure_ledger(connection)
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT issue_url, task_id FROM issue_tasks ORDER BY created_at, issue_url"
            ).fetchall()
        ]
    finally:
        connection.close()


def _kanban_task(policy: Policy, task_id: str, runner: Runner) -> dict[str, object]:
    output = runner.run(
        [
            HERMES,
            "kanban",
            "--board",
            policy.board,
            "show",
            task_id,
            "--json",
        ]
    )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise PollerError(f"Kanban show returned invalid JSON for {task_id}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("task"), dict):
        raise PollerError(f"Kanban show returned invalid task data for {task_id}")
    return payload


PULL_REQUEST_METADATA_KEYS = ("pr_url", "published_pr")


def _pull_request_url(payload: dict[str, object], *, task_id: str) -> str | None:
    runs = payload.get("runs")
    if not isinstance(runs, list):
        return None
    for run in reversed(runs):
        if not isinstance(run, dict) or not isinstance(run.get("metadata"), dict):
            continue
        found: list[str] = []
        for key in PULL_REQUEST_METADATA_KEYS:
            value = run["metadata"].get(key)
            if not isinstance(value, str):
                continue
            stripped = value.strip()
            if not PULL_REQUEST_URL_RE.fullmatch(stripped):
                continue
            if stripped not in found:
                found.append(stripped)
        if len(found) > 1:
            raise PollerError(f"task {task_id} has disagreeing pull request metadata")
        if found:
            return found[0]
    return None


def _pull_request_context(
    issue_url: str,
    task_id: str,
    policy: Policy,
    runner: Runner,
) -> PullRequestContext | None:
    return _pull_request_context_from_payload(
        issue_url,
        task_id,
        policy,
        _kanban_task(policy, task_id, runner),
    )


def _pull_request_context_from_payload(
    issue_url: str,
    task_id: str,
    policy: Policy,
    payload: dict[str, object],
) -> PullRequestContext | None:
    issue_match = ISSUE_URL_RE.fullmatch(issue_url)
    if issue_match is None:
        raise PollerError(f"ledger contains an invalid issue URL: {issue_url}")
    repository = next(
        (
            candidate
            for candidate in policy.repositories
            if candidate.slug == issue_match.group("slug")
        ),
        None,
    )
    if repository is None:
        return None

    pull_url = _pull_request_url(payload, task_id=task_id)
    if pull_url is None:
        return None
    pull_match = PULL_REQUEST_URL_RE.fullmatch(pull_url)
    if pull_match is None or pull_match.group("slug") != repository.slug:
        raise PollerError(f"task {task_id} references a pull request outside {repository.slug}")

    task = payload["task"]
    assert isinstance(task, dict)
    workspace_kind = task.get("workspace_kind")
    workspace_path = task.get("workspace_path")
    branch_name = task.get("branch_name")
    if workspace_kind != "worktree":
        raise PollerError(f"task {task_id} pull request is not backed by a worktree")
    if not isinstance(workspace_path, str) or not Path(workspace_path).is_absolute():
        raise PollerError(f"task {task_id} has no absolute pull-request worktree path")
    if not isinstance(branch_name, str) or not branch_name.strip():
        raise PollerError(f"task {task_id} has no pull-request branch")
    assignee = task.get("assignee")
    if not isinstance(assignee, str) or not assignee.strip():
        assignee = policy.assignee

    return PullRequestContext(
        repository=repository,
        issue_number=int(issue_match.group("number")),
        pull_number=int(pull_match.group("number")),
        pull_url=pull_match.group(0),
        root_task_id=task_id,
        assignee=assignee.strip(),
        workspace_path=Path(workspace_path),
        branch_name=branch_name.strip(),
    )


def _review_events(
    context: PullRequestContext,
    trusted_review_bots: Sequence[str],
    github_identity: str,
    runner: Runner,
    *,
    trusted_human_associations: Sequence[str] = tuple(TRUSTED_REVIEW_ASSOCIATIONS),
) -> list[ReviewEvent]:
    endpoint = (
        f"repos/{context.repository.slug}/issues/{context.pull_number}/"
        "timeline?per_page=100"
    )
    records = _github_api(runner, endpoint, headers=(GITHUB_TIMELINE_ACCEPT,))
    events: list[ReviewEvent] = []
    # Build a temporary policy view so trust helpers stay authoritative.
    trust_policy = Policy(
        schedule="unused",
        board="unused",
        assignee="unused",
        github_identity=github_identity,
        inference_provider="unused",
        inference_model="unused",
        worker_max_turns=1,
        required_label="unused",
        repositories=(context.repository,),
        trusted_review_bots=tuple(trusted_review_bots),
        trusted_human_associations=tuple(trusted_human_associations)
        or tuple(TRUSTED_REVIEW_ASSOCIATIONS),
    )
    for raw in records:
        if not isinstance(raw, dict) or raw.get("event") not in REVIEW_EVENT_KINDS:
            continue
        actor = raw.get("user")
        if not isinstance(actor, dict):
            actor = raw.get("actor")
        if not isinstance(actor, dict):
            continue
        login = actor.get("login")
        actor_type = actor.get("type")
        association = raw.get("author_association")
        if (
            not isinstance(login, str)
            or not login.strip()
            or login.casefold() == github_identity.casefold()
            or not isinstance(actor_type, str)
        ):
            continue
        trusted_human = is_trusted_human(
            actor_type=actor_type,
            author_association=association,
            policy=trust_policy,
        )
        trusted_bot = is_trusted_bot(
            actor_type=actor_type,
            login=login,
            policy=trust_policy,
        )
        if not trusted_human and not trusted_bot:
            continue
        event_id = raw.get("id")
        url = raw.get("html_url")
        timestamp = (
            raw.get("submitted_at")
            or raw.get("created_at")
            or raw.get("updated_at")
        )
        if (
            not isinstance(event_id, int)
            or isinstance(event_id, bool)
            or event_id < 1
            or not isinstance(url, str)
            or not url.startswith(context.pull_url)
            or not isinstance(timestamp, str)
            or not timestamp.strip()
        ):
            continue
        state = raw.get("state")
        if raw["event"] == "reviewed" and (
            not isinstance(state, str)
            or state.casefold() not in {"changes_requested", "commented"}
        ):
            continue
        events.append(
            ReviewEvent(
                event_id=event_id,
                kind=str(raw["event"]),
                state=str(state).lower() if isinstance(state, str) else "",
                actor=login.strip(),
                url=url.strip(),
                timestamp=timestamp.strip(),
            )
        )
    return sorted(events, key=lambda event: event.sort_key)


def _open_pull_urls(repository: Repository, runner: Runner) -> set[str]:
    query = urlencode({"state": "open", "per_page": "100"})
    records = _github_api(runner, f"repos/{repository.slug}/pulls?{query}")
    urls: set[str] = set()
    for raw in records:
        if not isinstance(raw, dict) or raw.get("state") != "open":
            continue
        url = raw.get("html_url")
        match = PULL_REQUEST_URL_RE.fullmatch(url.strip()) if isinstance(url, str) else None
        if match is not None and match.group("slug") == repository.slug:
            urls.add(match.group(0))
    return urls


def _review_task_body(context: PullRequestContext, event: ReviewEvent) -> str:
    return render_repair_task_body(
        pull_url=context.pull_url,
        event_kind=event.kind,
        event_state=event.state,
        event_actor=event.actor,
        event_url=event.url,
    )


def _create_review_task(
    context: PullRequestContext,
    event: ReviewEvent,
    parent_id: str,
    policy: Policy,
    runner: Runner,
) -> str:
    output = runner.run(
        [
            HERMES,
            "kanban",
            "--board",
            policy.board,
            "create",
            (
                f"{context.repository.slug}#{context.issue_number}: "
                f"review follow-up for PR #{context.pull_number}"
            ),
            "--body",
            _review_task_body(context, event),
            "--assignee",
            context.assignee,
            "--created-by",
            CREATED_BY,
            "--parent",
            parent_id,
            "--workspace",
            f"worktree:{context.workspace_path}",
            "--branch",
            context.branch_name,
            "--completion-contract",
            policy.kanban_completion_contract(context.repository.slug),
            "--idempotency-key",
            event.idempotency_key(context),
            "--json",
        ]
    )
    try:
        task = json.loads(output)
        task_id = task["id"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PollerError(
            f"Kanban create returned invalid review task JSON for {event.url}"
        ) from exc
    if not isinstance(task_id, str) or not task_id:
        raise PollerError(f"Kanban create returned invalid review task id for {event.url}")
    return task_id


def _repair_is_outstanding(policy: Policy, task_id: str, runner: Runner) -> bool:
    payload = _kanban_task(policy, task_id, runner)
    task = payload["task"]
    assert isinstance(task, dict)
    status = task.get("status")
    if not isinstance(status, str) or not status:
        raise PollerError(f"repair task {task_id} has no status")
    return status not in TERMINAL_TASK_STATUSES


def _reconcile_review_task(
    policy: Policy,
    connection: sqlite3.Connection,
    issue_url: str,
    root_task_id: str,
    open_pulls: dict[str, set[str]],
    runner: Runner,
) -> str | None:
    issue_match = ISSUE_URL_RE.fullmatch(issue_url)
    if issue_match is None:
        raise PollerError(f"ledger contains an invalid issue URL: {issue_url}")
    repository_slug = issue_match.group("slug")
    watch = _watch_state(connection, issue_url)
    if (
        watch.pull_url is not None
        and watch.pull_url not in open_pulls[repository_slug]
    ):
        return None
    if watch.pull_url is None:
        # Terminal null discovery is not final: re-read the root task each
        # poll so late canonical metadata.pr_url or metadata.published_pr
        # can be adopted without a second watcher or ledger surgery. Absent
        # metadata stays quiet and leaves the null sentinel unchanged.
        payload = _kanban_task(policy, root_task_id, runner)
        context = _pull_request_context_from_payload(
            issue_url,
            root_task_id,
            policy,
            payload,
        )
        if context is None:
            task = payload["task"]
            assert isinstance(task, dict)
            if (
                task.get("status") in TERMINAL_TASK_STATUSES
                and not watch.discovery_complete
            ):
                _save_watch_discovery(
                    connection,
                    issue_url,
                    pull_url=None,
                    complete=True,
                )
            return None
    else:
        context = _pull_request_context(
            issue_url,
            root_task_id,
            policy,
            runner,
        )
    if context is None:
        return None
    if context.pull_url not in open_pulls[context.repository.slug]:
        # Keep a null discovery sentinel when late metadata points at a
        # closed/non-open same-repo PR so a later open URL can still be
        # adopted without ledger surgery.
        return None
    if watch.pull_url is None:
        _save_watch_discovery(
            connection,
            issue_url,
            pull_url=context.pull_url,
            complete=True,
        )
        watch = _watch_state(connection, issue_url)
    if (
        watch.last_repair_task_id is not None
        and _repair_is_outstanding(
            policy,
            watch.last_repair_task_id,
            runner,
        )
    ):
        return None
    events = [
        event
        for event in _review_events(
            context,
            policy.trusted_review_bots,
            policy.github_identity,
            runner,
            trusted_human_associations=policy.trusted_human_associations,
        )
        if watch.cursor is None or event.sort_key > watch.cursor
    ]
    if not events:
        return None
    event = events[-1]
    task_id = _create_review_task(
        context,
        event,
        watch.last_repair_task_id or context.root_task_id,
        policy,
        runner,
    )
    _save_review_progress(connection, issue_url, event, task_id)
    return task_id


def reconcile_review_tasks(
    policy: Policy,
    ledger: Path,
    runner: Runner,
) -> tuple[list[str], list[str]]:
    task_ids: list[str] = []
    errors: list[str] = []
    tracked_tasks = _tracked_tasks(ledger)
    tracked_repositories = {
        match.group("slug")
        for issue_url, _ in tracked_tasks
        if (match := ISSUE_URL_RE.fullmatch(issue_url)) is not None
    }
    open_pulls: dict[str, set[str]] = {}
    for repository in policy.repositories:
        if repository.slug not in tracked_repositories:
            continue
        try:
            open_pulls[repository.slug] = _open_pull_urls(repository, runner)
        except PollerError as exc:
            errors.append(f"Failed to list open pull requests for {repository.slug}: {exc}")

    for issue_url, root_task_id in tracked_tasks:
        issue_match = ISSUE_URL_RE.fullmatch(issue_url)
        repository_slug = issue_match.group("slug") if issue_match is not None else None
        if repository_slug is not None and repository_slug not in open_pulls:
            continue
        connection = sqlite3.connect(ledger, timeout=30, isolation_level=None)
        try:
            _ensure_ledger(connection)
            connection.execute("BEGIN IMMEDIATE")
            task_id = _reconcile_review_task(
                policy,
                connection,
                issue_url,
                root_task_id,
                open_pulls,
                runner,
            )
            connection.execute("COMMIT")
            if task_id is not None:
                task_ids.append(task_id)
        except PollerError as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            errors.append(
                f"Failed to reconcile pull-request reviews for {issue_url}: {exc}"
            )
            continue
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
    return task_ids, errors


def create_task_once(issue: Issue, policy: Policy, ledger: Path, runner: Runner) -> tuple[str, bool]:
    ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(ledger, timeout=30, isolation_level=None)
    try:
        _ensure_ledger(connection)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT task_id FROM issue_tasks WHERE issue_url = ?", (issue.url,)
        ).fetchone()
        if row:
            connection.execute("COMMIT")
            return str(row[0]), False
        if not issue.repository.worktree.is_dir():
            raise PollerError(f"allowlisted worktree is unavailable: {issue.repository.worktree}")
        git_state = runner.run(
            ["git", "-C", str(issue.repository.worktree), "rev-parse", "--is-inside-work-tree"]
        ).strip()
        if git_state != "true":
            raise PollerError(f"allowlisted worktree is not a Git repository: {issue.repository.worktree}")
        output = runner.run(
            [
                HERMES,
                "kanban",
                "--board",
                policy.board,
                "create",
                f"{issue.repository.slug}#{issue.number}: {issue.title}",
                "--body",
                _task_body(issue, policy),
                "--assignee",
                policy.assignee,
                "--created-by",
                CREATED_BY,
                "--workspace",
                f"worktree:{issue.repository.worktree}",
                "--branch",
                issue.branch,
                "--completion-contract",
                policy.kanban_completion_contract(issue.repository.slug),
                "--idempotency-key",
                issue.url,
                "--json",
            ]
        )
        try:
            task = json.loads(output)
            task_id = task["id"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise PollerError(f"Kanban create returned invalid task JSON for {issue.url}") from exc
        if not isinstance(task_id, str) or not task_id:
            raise PollerError(f"Kanban create returned invalid task id for {issue.url}")
        connection.execute(
            "INSERT INTO issue_tasks(issue_url, task_id) VALUES (?, ?)", (issue.url, task_id)
        )
        connection.execute("COMMIT")
        return task_id, True
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def run_once(policy: Policy, ledger: Path, runner: Runner) -> PollResult:
    verify_identity(policy, runner)
    issues, warnings, errors = list_eligible_issues(policy, runner)
    created: list[tuple[Issue, str]] = []
    for issue in issues:
        try:
            task_id, is_new = create_task_once(issue, policy, ledger, runner)
        except PollerError as exc:
            errors.append(f"Failed to queue {issue.url}: {exc}")
            continue
        if is_new:
            created.append((issue, task_id))
    _, review_errors = reconcile_review_tasks(policy, ledger, runner)
    errors.extend(review_errors)
    return PollResult(created=created, warnings=warnings, errors=errors)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    args = parser.parse_args(argv)
    try:
        result = run_once(load_policy(args.config), args.ledger, SubprocessRunner())
    except PollerError as exc:
        print(f"GitHub issue poller failed: {exc}", file=sys.stderr)
        return 1
    for issue, task_id in result.created:
        print(f"Queued {issue.url} as Kanban task {task_id}.")
    for warning in result.warnings:
        print(f"Warning: {warning}")
    for error in result.errors:
        print(f"GitHub issue poller failed: {error}", file=sys.stderr)
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
