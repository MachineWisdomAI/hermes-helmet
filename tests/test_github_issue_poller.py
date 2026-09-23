from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hermes_helmet import github_issue_poller as poller
from hermes_helmet import install_poller as installer


FIXTURE_ORG = "example-org"
FIXTURE_REPO = "demo-repo"
FIXTURE_SLUG = f"{FIXTURE_ORG}/{FIXTURE_REPO}"
FIXTURE_ISSUE_URL = f"https://github.com/{FIXTURE_SLUG}/issues/48"
FIXTURE_PR_URL = f"https://github.com/{FIXTURE_SLUG}/pull/49"
FIXTURE_IDENTITY = "agent-bot"
FIXTURE_ASSIGNEE = "builder"
FIXTURE_BRANCH = "automation/demo-repo-48"


class FakeRunner:
    def __init__(self, records: list[object]) -> None:
        self.records = records
        self.calls: list[list[str]] = []
        self.created: list[str] = []
        self.identity = "agent-bot"

    def run(self, command: list[str]) -> str:
        self.calls.append(command)
        if command[:3] == [poller.GH, "api", "user"]:
            return self.identity + "\n"
        if command[:3] == [poller.GH, "api", "--paginate"]:
            if "--slurp" in command:
                raise poller.PollerError("unknown flag: --slurp")
            return json.dumps(self.records)
        if command[:4] == ["git", "-C", command[2], "rev-parse"]:
            return "true\n"
        if command[:5] == [
            poller.HERMES,
            "kanban",
            "--board",
            "default",
            "show",
        ]:
            return json.dumps(
                {
                    "task": {
                        "id": command[5],
                        "status": "running",
                        "workspace_kind": "worktree",
                        "workspace_path": "/unused",
                        "branch_name": "automation/demo-repo-48",
                    },
                    "comments": [],
                    "runs": [],
                }
            )
        if command[:4] == [poller.HERMES, "kanban", "--board", "default"] and "create" in command:
            url = command[command.index("--idempotency-key") + 1]
            self.created.append(url)
            return json.dumps({"id": f"t_{len(self.created)}"})
        raise AssertionError(command)


class ReviewWatchRunner:
    def __init__(
        self,
        workspace: Path,
        *,
        open_pulls: list[object],
        timeline: list[object],
        root_status: str = "done",
        root_pull_url: str | None = "https://github.com/example-org/demo-repo/pull/49",
        root_pull_metadata: dict[str, object] | None = None,
    ) -> None:
        self.workspace = workspace
        self.open_pulls = open_pulls
        self.timeline = timeline
        self.root_status = root_status
        self.root_pull_url = root_pull_url
        self.root_pull_metadata = root_pull_metadata
        self.calls: list[list[str]] = []
        self.create_commands: list[list[str]] = []
        self.idempotent_tasks: dict[str, str] = {}
        self.task_statuses: dict[str, str] = {}

    def run(self, command: list[str]) -> str:
        self.calls.append(command)
        if command[:3] == [poller.GH, "api", "user"]:
            return "agent-bot\n"
        if command[:3] == [poller.GH, "api", "--paginate"]:
            endpoint = command[-1]
            if endpoint.startswith("repos/example-org/demo-repo/issues?"):
                return "[]"
            if endpoint.startswith("repos/example-org/demo-repo/pulls?"):
                return json.dumps(self.open_pulls)
            if endpoint == "repos/example-org/demo-repo/issues/49/timeline?per_page=100":
                return json.dumps(self.timeline)
        if command[:5] == [
            poller.HERMES,
            "kanban",
            "--board",
            "default",
            "show",
        ]:
            task_id = command[5]
            if task_id == "t_root":
                runs = []
                metadata: dict[str, object] | None
                if self.root_pull_metadata is not None:
                    metadata = self.root_pull_metadata
                elif self.root_pull_url is not None:
                    metadata = {"pr_url": self.root_pull_url}
                else:
                    metadata = None
                if metadata is not None:
                    runs.append(
                        {
                            "id": 14,
                            "status": "done",
                            "outcome": "completed",
                            "metadata": metadata,
                        }
                    )
                return json.dumps(
                    {
                        "task": {
                            "id": task_id,
                            "status": self.root_status,
                            "assignee": "builder",
                            "workspace_kind": "worktree",
                            "workspace_path": str(self.workspace),
                            "branch_name": "automation/demo-repo-48",
                        },
                        "comments": [],
                        "runs": runs,
                    }
                )
            return json.dumps(
                {
                    "task": {
                        "id": task_id,
                        "status": self.task_statuses[task_id],
                        "workspace_kind": "worktree",
                        "workspace_path": str(self.workspace),
                        "branch_name": "automation/demo-repo-48",
                    },
                    "comments": [],
                    "runs": [],
                }
            )
        if command[:4] == [
            poller.HERMES,
            "kanban",
            "--board",
            "default",
        ] and "create" in command:
            self.create_commands.append(command)
            key = command[command.index("--idempotency-key") + 1]
            task_id = self.idempotent_tasks.setdefault(
                key,
                f"t_review_{len(self.idempotent_tasks) + 1}",
            )
            self.task_statuses.setdefault(task_id, "ready")
            return json.dumps({"id": task_id})
        raise AssertionError(command)


class SubprocessRunnerBoundaryTests(unittest.TestCase):
    def test_failure_reports_executable_and_exit_without_command_output(self) -> None:
        secret = "synthetic-secret-that-must-not-escape"
        runner = poller.SubprocessRunner()
        with self.assertRaises(poller.PollerError) as raised:
            runner.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys; "
                        f"print({secret!r}); "
                        f"print({secret!r}, file=sys.stderr); "
                        "raise SystemExit(23)"
                    ),
                ]
            )
        message = str(raised.exception)
        self.assertIn(Path(sys.executable).name, message)
        self.assertIn("exit 23", message)
        self.assertIn("output withheld", message)
        self.assertNotIn(secret, message)


class GitHubIssuePollerTests(unittest.TestCase):
    def policy(
        self,
        root: Path,
        *,
        trusted_review_bots: tuple[str, ...] = (),
        worker_completion_contract: str = "github-pr",
    ) -> poller.Policy:
        checkout = root / "demo-repo"
        checkout.mkdir()
        return poller.Policy(
            schedule="every 15m",
            board="default",
            assignee="builder",
            github_identity="agent-bot",
            inference_provider="openai",
            inference_model="gpt-4.1",
            worker_max_turns=100,
            required_label="hermes-kanban-go",
            repositories=(poller.Repository("example-org/demo-repo", checkout),),
            trusted_review_bots=trusted_review_bots,
            worker_completion_contract=worker_completion_contract,
        )

    def eligible(self) -> dict[str, object]:
        return {
            "number": 48,
            "title": "Unify structured data",
            "body": "- [ ] build\n- [ ] test",
            "state": "open",
            "labels": [{"name": "hermes-kanban-go"}],
        }

    def seed_task(self, ledger: Path, task_id: str = "t_root") -> None:
        with sqlite3.connect(ledger) as connection:
            poller._ensure_ledger(connection)
            connection.execute(
                "INSERT INTO issue_tasks(issue_url, task_id) VALUES (?, ?)",
                ("https://github.com/example-org/demo-repo/issues/48", task_id),
            )

    def root_payload(
        self,
        workspace: Path,
        metadata: dict[str, object],
        *,
        task_id: str = "t_root",
    ) -> dict[str, object]:
        return {
            "task": {
                "id": task_id,
                "status": "done",
                "assignee": "builder",
                "workspace_kind": "worktree",
                "workspace_path": str(workspace),
                "branch_name": "automation/demo-repo-48",
            },
            "runs": [
                {
                    "id": 14,
                    "status": "done",
                    "outcome": "completed",
                    "metadata": metadata,
                }
            ],
        }

    def pull_context(
        self,
        policy: poller.Policy,
        workspace: Path,
        metadata: dict[str, object],
    ) -> poller.PullRequestContext | None:
        return poller._pull_request_context_from_payload(
            "https://github.com/example-org/demo-repo/issues/48",
            "t_root",
            policy,
            self.root_payload(workspace, metadata),
        )

    def test_creates_one_worktree_task_and_persists_canonical_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner([self.eligible()])
            result = poller.run_once(self.policy(root), root / "ledger.sqlite3", runner)
            self.assertEqual(result.warnings, [])
            self.assertEqual(result.errors, [])
            self.assertEqual(
                [(issue.url, task_id) for issue, task_id in result.created],
                [("https://github.com/example-org/demo-repo/issues/48", "t_1")],
            )
            command = next(call for call in runner.calls if call[0] == poller.HERMES)
            self.assertIn("worktree:" + str(root / "demo-repo"), command)
            self.assertIn("automation/demo-repo-48", command)
            self.assertIn("builder", command)
            self.assertEqual(command[command.index("--created-by") + 1], poller.CREATED_BY)
            self.assertEqual(
                command[command.index("--completion-contract") + 1],
                FIXTURE_SLUG,
            )
            self.assertIn("hermes-kanban-go", json.dumps(runner.records))
            with sqlite3.connect(root / "ledger.sqlite3") as ledger:
                self.assertEqual(
                    ledger.execute("SELECT issue_url, task_id FROM issue_tasks").fetchall(),
                    [("https://github.com/example-org/demo-repo/issues/48", "t_1")],
                )

    def test_repeated_polls_do_not_create_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner([self.eligible()])
            policy = self.policy(root)
            ledger = root / "ledger.sqlite3"
            poller.run_once(policy, ledger, runner)
            result = poller.run_once(policy, ledger, runner)
            self.assertEqual(result.created, [])
            self.assertEqual(runner.created, ["https://github.com/example-org/demo-repo/issues/48"])

    def test_publication_and_repair_fixtures_in_both_completion_modes(self) -> None:
        modes = (("github-pr", FIXTURE_SLUG), ("local-only", "local-only"))
        for mode, expected_contract in modes:
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    policy = self.policy(
                        root, worker_completion_contract=mode
                    )
                    runner = FakeRunner([self.eligible()])
                    created = poller.run_once(
                        policy, root / "ledger.sqlite3", runner
                    )
                    self.assertEqual(created.errors, [])
                    command = next(
                        call for call in runner.calls if call[0] == poller.HERMES
                    )
                    self.assertEqual(
                        command[command.index("--completion-contract") + 1],
                        expected_contract,
                    )
                    self.assertIn("metadata.published_pr", command[command.index("--body") + 1])

                    workspace = root / "demo-repo" / ".worktrees" / "t_root"
                    workspace.mkdir(parents=True)
                    ledger = root / "repair.sqlite3"
                    self.seed_task(ledger)
                    review_runner = ReviewWatchRunner(
                        workspace,
                        open_pulls=[
                            {
                                "number": 49,
                                "state": "open",
                                "html_url": FIXTURE_PR_URL,
                            }
                        ],
                        timeline=[
                            {
                                "id": 4823248879,
                                "event": "reviewed",
                                "state": "changes_requested",
                                "submitted_at": "2026-07-30T21:09:29Z",
                                "html_url": (
                                    "https://github.com/example-org/demo-repo/"
                                    "pull/49#pullrequestreview-4823248879"
                                ),
                                "user": {
                                    "login": "trusted-reviewer",
                                    "type": "User",
                                },
                                "author_association": "MEMBER",
                            }
                        ],
                        root_pull_url=None,
                        root_pull_metadata={"published_pr": FIXTURE_PR_URL},
                    )
                    first = poller.run_once(policy, ledger, review_runner)
                    second = poller.run_once(policy, ledger, review_runner)
                    self.assertEqual(first.errors, [])
                    self.assertEqual(second.errors, [])
                    self.assertEqual(len(review_runner.create_commands), 1)
                    repair = review_runner.create_commands[0]
                    self.assertEqual(
                        repair[repair.index("--completion-contract") + 1],
                        expected_contract,
                    )
                    self.assertIn(
                        "metadata.published_pr",
                        repair[repair.index("--body") + 1],
                    )

                    with self.assertRaises(poller.PollerError) as raised:
                        self.pull_context(
                            policy,
                            workspace,
                            {
                                "pr_url": FIXTURE_PR_URL,
                                "published_pr": (
                                    "https://github.com/example-org/demo-repo/pull/50"
                                ),
                            },
                        )
                    self.assertIn(
                        "disagreeing pull request metadata",
                        str(raised.exception),
                    )
                    self.assertIsNone(
                        self.pull_context(policy, workspace, {})
                    )

    def test_changes_requested_review_creates_one_dependent_same_pr_repair_task(self) -> None:
        class ReviewRunner:
            def __init__(self, workspace: Path) -> None:
                self.workspace = workspace
                self.calls: list[list[str]] = []
                self.repair_commands: list[list[str]] = []

            def run(self, command: list[str]) -> str:
                self.calls.append(command)
                if command[:3] == [poller.GH, "api", "user"]:
                    return "agent-bot\n"
                if command[:3] == [poller.GH, "api", "--paginate"]:
                    endpoint = command[-1]
                    if endpoint.startswith("repos/example-org/demo-repo/issues?"):
                        return "[]"
                    if endpoint.startswith("repos/example-org/demo-repo/pulls?"):
                        return json.dumps(
                            [
                                {
                                    "number": 49,
                                    "state": "open",
                                    "html_url": (
                                        "https://github.com/"
                                        "example-org/demo-repo/pull/49"
                                    ),
                                }
                            ]
                        )
                    if endpoint == "repos/example-org/demo-repo/issues/49/timeline?per_page=100":
                        return json.dumps(
                            [
                                {
                                    "id": 4823248879,
                                    "event": "reviewed",
                                    "state": "changes_requested",
                                    "submitted_at": "2026-07-30T21:09:29Z",
                                    "html_url": (
                                        "https://github.com/example-org/demo-repo/"
                                        "pull/49#pullrequestreview-4823248879"
                                    ),
                                    "user": {
                                        "login": "trusted-reviewer",
                                        "type": "User",
                                    },
                                    "author_association": "MEMBER",
                                }
                            ]
                        )
                if command[:5] == [
                    poller.HERMES,
                    "kanban",
                    "--board",
                    "default",
                    "show",
                ]:
                    return json.dumps(
                        {
                            "task": {
                                "id": "t_root",
                                "status": "done",
                                "workspace_kind": "worktree",
                                "workspace_path": str(self.workspace),
                                "branch_name": "automation/demo-repo-48",
                            },
                            "comments": [],
                            "runs": [
                                {
                                    "id": 14,
                                    "status": "done",
                                    "outcome": "completed",
                                    "metadata": {
                                        "pr_url": (
                                            "https://github.com/"
                                            "example-org/demo-repo/pull/49"
                                        )
                                    },
                                }
                            ],
                        }
                    )
                if command[:4] == [
                    poller.HERMES,
                    "kanban",
                    "--board",
                    "default",
                ] and "create" in command:
                    self.repair_commands.append(command)
                    return json.dumps({"id": "t_repair"})
                raise AssertionError(command)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            ledger = root / "ledger.sqlite3"
            workspace = root / "demo-repo" / ".worktrees" / "t_root"
            workspace.mkdir(parents=True)
            with sqlite3.connect(ledger) as connection:
                poller._ensure_ledger(connection)
                connection.execute(
                    "INSERT INTO issue_tasks(issue_url, task_id) VALUES (?, ?)",
                    ("https://github.com/example-org/demo-repo/issues/48", "t_root"),
                )
            runner = ReviewRunner(workspace)

            result = poller.run_once(policy, ledger, runner)

            self.assertEqual(result.errors, [])
            self.assertEqual(len(runner.repair_commands), 1)
            command = runner.repair_commands[0]
            self.assertEqual(
                command[command.index("--idempotency-key") + 1],
                "github-pr-event:example-org/demo-repo:49:reviewed:4823248879",
            )
            self.assertEqual(command[command.index("--parent") + 1], "t_root")
            self.assertEqual(
                command[command.index("--workspace") + 1],
                f"worktree:{workspace}",
            )
            self.assertEqual(
                command[command.index("--branch") + 1],
                "automation/demo-repo-48",
            )
            body = command[command.index("--body") + 1]
            self.assertIn("pull/49#pullrequestreview-4823248879", body)
            self.assertIn("update the existing branch and pull request", body)
            self.assertNotIn("Unsloth", body)

    def test_closed_pull_request_does_not_fetch_timeline_or_create_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            ledger = root / "ledger.sqlite3"
            workspace = root / "demo-repo" / ".worktrees" / "t_root"
            workspace.mkdir(parents=True)
            self.seed_task(ledger)
            runner = ReviewWatchRunner(
                workspace,
                open_pulls=[],
                timeline=[
                    {
                        "id": 4823248879,
                        "event": "reviewed",
                        "state": "changes_requested",
                        "submitted_at": "2026-07-30T21:09:29Z",
                        "html_url": (
                            "https://github.com/example-org/demo-repo/"
                            "pull/49#pullrequestreview-4823248879"
                        ),
                        "user": {"login": "trusted-reviewer", "type": "User"},
                        "author_association": "MEMBER",
                    }
                ],
            )

            first = poller.run_once(policy, ledger, runner)
            second = poller.run_once(policy, ledger, runner)

            self.assertEqual(first.errors, [])
            self.assertEqual(second.errors, [])
            self.assertEqual(runner.create_commands, [])
            self.assertFalse(any("timeline" in call[-1] for call in runner.calls))
            # Closed metadata must not persist a non-null watch; keep the
            # recoverable null path so a later open URL can still be adopted.
            with sqlite3.connect(ledger) as connection:
                row = connection.execute(
                    """
                    SELECT pull_url, discovery_complete
                    FROM pull_request_watches
                    WHERE issue_url = ?
                    """,
                    ("https://github.com/example-org/demo-repo/issues/48",),
                ).fetchone()
            self.assertTrue(row is None or row[0] is None)
            root_shows = [
                call
                for call in runner.calls
                if call[:5]
                == [poller.HERMES, "kanban", "--board", "default", "show"]
                and call[5] == "t_root"
            ]
            self.assertEqual(len(root_shows), 2)

    def test_repeated_review_poll_does_not_repeat_hermes_create(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            ledger = root / "ledger.sqlite3"
            workspace = root / "demo-repo" / ".worktrees" / "t_root"
            workspace.mkdir(parents=True)
            self.seed_task(ledger)
            runner = ReviewWatchRunner(
                workspace,
                open_pulls=[
                    {
                        "number": 49,
                        "state": "open",
                        "html_url": "https://github.com/example-org/demo-repo/pull/49",
                    }
                ],
                timeline=[
                    {
                        "id": 4823248879,
                        "event": "reviewed",
                        "state": "changes_requested",
                        "submitted_at": "2026-07-30T21:09:29Z",
                        "html_url": (
                            "https://github.com/example-org/demo-repo/"
                            "pull/49#pullrequestreview-4823248879"
                        ),
                        "user": {"login": "trusted-reviewer", "type": "User"},
                        "author_association": "MEMBER",
                    }
                ],
            )

            first = poller.run_once(policy, ledger, runner)
            second = poller.run_once(policy, ledger, runner)

            self.assertEqual(first.errors, [])
            self.assertEqual(second.errors, [])
            self.assertEqual(len(runner.create_commands), 1)

    def test_allowlisted_bot_comment_creates_a_repair_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(
                root,
                trusted_review_bots=("github-code-quality[bot]",),
            )
            ledger = root / "ledger.sqlite3"
            workspace = root / "demo-repo" / ".worktrees" / "t_root"
            workspace.mkdir(parents=True)
            self.seed_task(ledger)
            runner = ReviewWatchRunner(
                workspace,
                open_pulls=[
                    {
                        "number": 49,
                        "state": "open",
                        "html_url": "https://github.com/example-org/demo-repo/pull/49",
                    }
                ],
                timeline=[
                    {
                        "id": 4823248879,
                        "event": "reviewed",
                        "state": "commented",
                        "submitted_at": "2026-07-30T21:09:29Z",
                        "html_url": (
                            "https://github.com/example-org/demo-repo/"
                            "pull/49#pullrequestreview-4823248879"
                        ),
                        "user": {
                            "login": "GITHUB-CODE-QUALITY[BOT]",
                            "type": "Bot",
                        },
                        "author_association": "CONTRIBUTOR",
                    }
                ],
            )

            result = poller.run_once(policy, ledger, runner)

            self.assertEqual(result.errors, [])
            self.assertEqual(len(runner.create_commands), 1)
            command = runner.create_commands[0]
            body = command[command.index("--body") + 1]
            self.assertIn(
                "reviewed (commented) by @GITHUB-CODE-QUALITY[BOT]",
                body,
            )
            self.assertEqual(
                command[command.index("--completion-contract") + 1],
                FIXTURE_SLUG,
            )
            self.assertIn("metadata.published_pr", body)

    def test_terminal_root_without_pull_request_remains_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            ledger = root / "ledger.sqlite3"
            workspace = root / "demo-repo" / ".worktrees" / "t_root"
            workspace.mkdir(parents=True)
            self.seed_task(ledger)
            runner = ReviewWatchRunner(
                workspace,
                open_pulls=[],
                timeline=[],
                root_pull_url=None,
            )

            first = poller.run_once(policy, ledger, runner)
            second = poller.run_once(policy, ledger, runner)

            self.assertEqual(first.errors, [])
            self.assertEqual(second.errors, [])
            self.assertEqual(runner.create_commands, [])
            with sqlite3.connect(ledger) as connection:
                row = connection.execute(
                    """
                    SELECT pull_url, discovery_complete, last_event_id,
                           last_repair_task_id
                    FROM pull_request_watches
                    WHERE issue_url = ?
                    """,
                    ("https://github.com/example-org/demo-repo/issues/48",),
                ).fetchone()
            self.assertEqual(row, (None, 1, None, None))
            root_shows = [
                call
                for call in runner.calls
                if call[:5]
                == [poller.HERMES, "kanban", "--board", "default", "show"]
                and call[5] == "t_root"
            ]
            # Null terminal discovery still re-reads once per poll so late
            # metadata can land without a second control path.
            self.assertEqual(len(root_shows), 2)

    def test_late_pr_metadata_after_null_terminal_discovery_creates_one_repair(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            ledger = root / "ledger.sqlite3"
            workspace = root / "demo-repo" / ".worktrees" / "t_root"
            workspace.mkdir(parents=True)
            self.seed_task(ledger)
            review = {
                "id": 4823248879,
                "event": "reviewed",
                "state": "changes_requested",
                "submitted_at": "2026-07-30T21:09:29Z",
                "html_url": (
                    "https://github.com/example-org/demo-repo/"
                    "pull/49#pullrequestreview-4823248879"
                ),
                "user": {"login": "trusted-reviewer", "type": "User"},
                "author_association": "MEMBER",
            }
            runner = ReviewWatchRunner(
                workspace,
                open_pulls=[],
                timeline=[review],
                root_pull_url=None,
            )

            first = poller.run_once(policy, ledger, runner)
            self.assertEqual(first.errors, [])
            self.assertEqual(runner.create_commands, [])
            with sqlite3.connect(ledger) as connection:
                row = connection.execute(
                    """
                    SELECT pull_url, discovery_complete
                    FROM pull_request_watches
                    WHERE issue_url = ?
                    """,
                    ("https://github.com/example-org/demo-repo/issues/48",),
                ).fetchone()
            self.assertEqual(row, (None, 1))

            runner.root_pull_url = (
                "https://github.com/example-org/demo-repo/pull/49"
            )
            runner.open_pulls = [
                {
                    "number": 49,
                    "state": "open",
                    "html_url": (
                        "https://github.com/example-org/demo-repo/pull/49"
                    ),
                }
            ]

            second = poller.run_once(policy, ledger, runner)
            third = poller.run_once(policy, ledger, runner)

            self.assertEqual(second.errors, [])
            self.assertEqual(third.errors, [])
            self.assertEqual(len(runner.create_commands), 1)
            command = runner.create_commands[0]
            self.assertEqual(
                command[command.index("--idempotency-key") + 1],
                "github-pr-event:example-org/demo-repo:49:reviewed:4823248879",
            )
            self.assertEqual(command[command.index("--parent") + 1], "t_root")
            with sqlite3.connect(ledger) as connection:
                watches = connection.execute(
                    "SELECT COUNT(*) FROM pull_request_watches"
                ).fetchone()
                row = connection.execute(
                    """
                    SELECT pull_url, discovery_complete, last_event_id,
                           last_repair_task_id
                    FROM pull_request_watches
                    WHERE issue_url = ?
                    """,
                    ("https://github.com/example-org/demo-repo/issues/48",),
                ).fetchone()
            self.assertEqual(watches, (1,))
            self.assertEqual(
                row,
                (
                    "https://github.com/example-org/demo-repo/pull/49",
                    1,
                    4823248879,
                    "t_review_1",
                ),
            )

    def test_late_closed_pr_metadata_does_not_wedge_null_discovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            ledger = root / "ledger.sqlite3"
            workspace = root / "demo-repo" / ".worktrees" / "t_root"
            workspace.mkdir(parents=True)
            self.seed_task(ledger)
            review = {
                "id": 4823248879,
                "event": "reviewed",
                "state": "changes_requested",
                "submitted_at": "2026-07-30T21:09:29Z",
                "html_url": (
                    "https://github.com/example-org/demo-repo/"
                    "pull/49#pullrequestreview-4823248879"
                ),
                "user": {"login": "trusted-reviewer", "type": "User"},
                "author_association": "MEMBER",
            }
            runner = ReviewWatchRunner(
                workspace,
                open_pulls=[],
                timeline=[review],
                root_pull_url=None,
            )

            first = poller.run_once(policy, ledger, runner)
            self.assertEqual(first.errors, [])
            self.assertEqual(runner.create_commands, [])
            with sqlite3.connect(ledger) as connection:
                row = connection.execute(
                    """
                    SELECT pull_url, discovery_complete
                    FROM pull_request_watches
                    WHERE issue_url = ?
                    """,
                    ("https://github.com/example-org/demo-repo/issues/48",),
                ).fetchone()
            self.assertEqual(row, (None, 1))

            # Temporary same-repo closed PR URL must not replace the null
            # sentinel; otherwise later open-PR correction wedges again.
            runner.root_pull_url = (
                "https://github.com/example-org/demo-repo/pull/50"
            )
            runner.open_pulls = []

            second = poller.run_once(policy, ledger, runner)
            self.assertEqual(second.errors, [])
            self.assertEqual(runner.create_commands, [])
            with sqlite3.connect(ledger) as connection:
                row = connection.execute(
                    """
                    SELECT pull_url, discovery_complete
                    FROM pull_request_watches
                    WHERE issue_url = ?
                    """,
                    ("https://github.com/example-org/demo-repo/issues/48",),
                ).fetchone()
            self.assertEqual(row, (None, 1))

            runner.root_pull_url = (
                "https://github.com/example-org/demo-repo/pull/49"
            )
            runner.open_pulls = [
                {
                    "number": 49,
                    "state": "open",
                    "html_url": (
                        "https://github.com/example-org/demo-repo/pull/49"
                    ),
                }
            ]

            third = poller.run_once(policy, ledger, runner)
            fourth = poller.run_once(policy, ledger, runner)

            self.assertEqual(third.errors, [])
            self.assertEqual(fourth.errors, [])
            self.assertEqual(len(runner.create_commands), 1)
            command = runner.create_commands[0]
            self.assertEqual(
                command[command.index("--idempotency-key") + 1],
                "github-pr-event:example-org/demo-repo:49:reviewed:4823248879",
            )
            self.assertEqual(command[command.index("--parent") + 1], "t_root")
            with sqlite3.connect(ledger) as connection:
                watches = connection.execute(
                    "SELECT COUNT(*) FROM pull_request_watches"
                ).fetchone()
                row = connection.execute(
                    """
                    SELECT pull_url, discovery_complete, last_event_id,
                           last_repair_task_id
                    FROM pull_request_watches
                    WHERE issue_url = ?
                    """,
                    ("https://github.com/example-org/demo-repo/issues/48",),
                ).fetchone()
            self.assertEqual(watches, (1,))
            self.assertEqual(
                row,
                (
                    "https://github.com/example-org/demo-repo/pull/49",
                    1,
                    4823248879,
                    "t_review_1",
                ),
            )

    def test_pr_only_metadata_is_not_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            workspace = root / "worktree"
            self.assertIsNone(
                self.pull_context(policy, workspace, {"pr": FIXTURE_PR_URL})
            )

    def test_published_pr_metadata_is_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            workspace = root / "worktree"
            context = self.pull_context(
                policy,
                workspace,
                {"published_pr": FIXTURE_PR_URL},
            )
            assert context is not None
            self.assertEqual(context.pull_url, FIXTURE_PR_URL)
            self.assertEqual(context.pull_number, 49)

    def test_legacy_pr_url_metadata_is_still_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            workspace = root / "worktree"
            context = self.pull_context(
                policy,
                workspace,
                {"pr_url": FIXTURE_PR_URL},
            )
            assert context is not None
            self.assertEqual(context.pull_url, FIXTURE_PR_URL)
            self.assertEqual(context.pull_number, 49)

    def test_matching_pr_url_and_published_pr_are_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            workspace = root / "worktree"
            context = self.pull_context(
                policy,
                workspace,
                {"pr_url": FIXTURE_PR_URL, "published_pr": FIXTURE_PR_URL},
            )
            assert context is not None
            self.assertEqual(context.pull_url, FIXTURE_PR_URL)

    def test_disagreeing_pr_url_and_published_pr_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            workspace = root / "worktree"
            with self.assertRaises(poller.PollerError) as raised:
                self.pull_context(
                    policy,
                    workspace,
                    {
                        "pr_url": FIXTURE_PR_URL,
                        "published_pr": (
                            "https://github.com/example-org/demo-repo/pull/50"
                        ),
                    },
                )
            self.assertIn("disagreeing pull request metadata", str(raised.exception))
            self.assertIn("t_root", str(raised.exception))

    def test_malformed_pull_request_metadata_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            workspace = root / "worktree"
            self.assertIsNone(
                self.pull_context(
                    policy,
                    workspace,
                    {"published_pr": "not-a-pull-request", "pr_url": "also bad"},
                )
            )
            context = self.pull_context(
                policy,
                workspace,
                {
                    "published_pr": "not-a-pull-request",
                    "pr_url": FIXTURE_PR_URL,
                },
            )
            assert context is not None
            self.assertEqual(context.pull_url, FIXTURE_PR_URL)

    def test_outside_repository_published_pr_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            workspace = root / "worktree"
            with self.assertRaises(poller.PollerError) as raised:
                self.pull_context(
                    policy,
                    workspace,
                    {
                        "published_pr": (
                            "https://github.com/other-org/other-repo/pull/1"
                        )
                    },
                )
            self.assertIn("outside example-org/demo-repo", str(raised.exception))

    def test_terminal_null_discovery_published_pr_creates_one_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            ledger = root / "ledger.sqlite3"
            workspace = root / "demo-repo" / ".worktrees" / "t_root"
            workspace.mkdir(parents=True)
            self.seed_task(ledger)
            with sqlite3.connect(ledger) as connection:
                poller._save_watch_discovery(
                    connection,
                    "https://github.com/example-org/demo-repo/issues/48",
                    pull_url=None,
                    complete=True,
                )
            runner = ReviewWatchRunner(
                workspace,
                open_pulls=[
                    {
                        "number": 49,
                        "state": "open",
                        "html_url": FIXTURE_PR_URL,
                    }
                ],
                timeline=[
                    {
                        "id": 4823248879,
                        "event": "reviewed",
                        "state": "changes_requested",
                        "submitted_at": "2026-07-30T21:09:29Z",
                        "html_url": (
                            "https://github.com/example-org/demo-repo/"
                            "pull/49#pullrequestreview-4823248879"
                        ),
                        "user": {"login": "trusted-reviewer", "type": "User"},
                        "author_association": "MEMBER",
                    }
                ],
                root_pull_url=None,
                root_pull_metadata={"published_pr": FIXTURE_PR_URL},
            )

            first = poller.run_once(policy, ledger, runner)
            second = poller.run_once(policy, ledger, runner)

            self.assertEqual(first.errors, [])
            self.assertEqual(second.errors, [])
            self.assertEqual(len(runner.create_commands), 1)
            command = runner.create_commands[0]
            self.assertEqual(
                command[command.index("--idempotency-key") + 1],
                "github-pr-event:example-org/demo-repo:49:reviewed:4823248879",
            )
            self.assertEqual(command[command.index("--parent") + 1], "t_root")
            with sqlite3.connect(ledger) as connection:
                row = connection.execute(
                    """
                    SELECT pull_url, discovery_complete, last_event_id,
                           last_repair_task_id
                    FROM pull_request_watches
                    WHERE issue_url = ?
                    """,
                    ("https://github.com/example-org/demo-repo/issues/48",),
                ).fetchone()
            self.assertEqual(
                row,
                (FIXTURE_PR_URL, 1, 4823248879, "t_review_1"),
            )
            self.assertEqual(
                runner.create_commands[0][
                    runner.create_commands[0].index("--completion-contract") + 1
                ],
                FIXTURE_SLUG,
            )

    def test_terminal_pr_only_metadata_does_not_create_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            ledger = root / "ledger.sqlite3"
            workspace = root / "demo-repo" / ".worktrees" / "t_root"
            workspace.mkdir(parents=True)
            self.seed_task(ledger)
            with sqlite3.connect(ledger) as connection:
                poller._save_watch_discovery(
                    connection,
                    "https://github.com/example-org/demo-repo/issues/48",
                    pull_url=None,
                    complete=True,
                )
            runner = ReviewWatchRunner(
                workspace,
                open_pulls=[
                    {
                        "number": 49,
                        "state": "open",
                        "html_url": FIXTURE_PR_URL,
                    }
                ],
                timeline=[
                    {
                        "id": 4823248879,
                        "event": "reviewed",
                        "state": "changes_requested",
                        "submitted_at": "2026-07-30T21:09:29Z",
                        "html_url": (
                            "https://github.com/example-org/demo-repo/"
                            "pull/49#pullrequestreview-4823248879"
                        ),
                        "user": {"login": "trusted-reviewer", "type": "User"},
                        "author_association": "MEMBER",
                    }
                ],
                root_pull_url=None,
                root_pull_metadata={"pr": FIXTURE_PR_URL},
            )

            first = poller.run_once(policy, ledger, runner)
            second = poller.run_once(policy, ledger, runner)

            self.assertEqual(first.errors, [])
            self.assertEqual(second.errors, [])
            self.assertEqual(runner.create_commands, [])
            with sqlite3.connect(ledger) as connection:
                row = connection.execute(
                    """
                    SELECT pull_url, discovery_complete, last_event_id,
                           last_repair_task_id
                    FROM pull_request_watches
                    WHERE issue_url = ?
                    """,
                    ("https://github.com/example-org/demo-repo/issues/48",),
                ).fetchone()
            self.assertEqual(row, (None, 1, None, None))

    def test_open_pull_listing_failure_does_not_mutate_watch_state(self) -> None:
        class FailingOpenPullRunner(ReviewWatchRunner):
            def run(self, command: list[str]) -> str:
                if (
                    command[:3] == [poller.GH, "api", "--paginate"]
                    and "/pulls?" in command[-1]
                ):
                    raise poller.PollerError("GitHub unavailable")
                return super().run(command)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            ledger = root / "ledger.sqlite3"
            workspace = root / "demo-repo" / ".worktrees" / "t_root"
            workspace.mkdir(parents=True)
            self.seed_task(ledger)
            runner = FailingOpenPullRunner(
                workspace,
                open_pulls=[],
                timeline=[],
            )

            result = poller.run_once(policy, ledger, runner)

            self.assertEqual(
                result.errors,
                [
                    "Failed to list open pull requests for example-org/demo-repo: "
                    "GitHub unavailable"
                ],
            )
            self.assertFalse(any(call[0] == poller.HERMES for call in runner.calls))
            with sqlite3.connect(ledger) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT * FROM pull_request_watches"
                    ).fetchall(),
                    [],
                )

    def test_review_activity_coalesces_behind_one_outstanding_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            ledger = root / "ledger.sqlite3"
            workspace = root / "demo-repo" / ".worktrees" / "t_root"
            workspace.mkdir(parents=True)
            self.seed_task(ledger)
            first_event = {
                "id": 100,
                "event": "reviewed",
                "state": "changes_requested",
                "submitted_at": "2026-07-30T20:00:00Z",
                "html_url": (
                    "https://github.com/example-org/demo-repo/"
                    "pull/49#pullrequestreview-100"
                ),
                "user": {"login": "trusted-reviewer", "type": "User"},
                "author_association": "MEMBER",
            }
            runner = ReviewWatchRunner(
                workspace,
                open_pulls=[
                    {
                        "number": 49,
                        "state": "open",
                        "html_url": "https://github.com/example-org/demo-repo/pull/49",
                    }
                ],
                timeline=[first_event],
            )

            first = poller.run_once(policy, ledger, runner)
            first_task_id = runner.create_commands[0][
                runner.create_commands[0].index("--idempotency-key") + 1
            ]
            runner.timeline.extend(
                [
                    {
                        "id": 101,
                        "event": "commented",
                        "created_at": "2026-07-30T20:05:00Z",
                        "html_url": (
                            "https://github.com/example-org/demo-repo/"
                            "pull/49#issuecomment-101"
                        ),
                        "user": {"login": "trusted-reviewer", "type": "User"},
                        "author_association": "MEMBER",
                    },
                    {
                        "id": 102,
                        "event": "commented",
                        "created_at": "2026-07-30T20:06:00Z",
                        "html_url": (
                            "https://github.com/example-org/demo-repo/"
                            "pull/49#issuecomment-102"
                        ),
                        "user": {"login": "trusted-reviewer", "type": "User"},
                        "author_association": "MEMBER",
                    },
                ]
            )

            while_ready = poller.run_once(policy, ledger, runner)
            self.assertEqual(len(runner.create_commands), 1)

            repair_id = runner.idempotent_tasks[first_task_id]
            runner.task_statuses[repair_id] = "done"
            after_done = poller.run_once(policy, ledger, runner)

            self.assertEqual(first.errors, [])
            self.assertEqual(while_ready.errors, [])
            self.assertEqual(after_done.errors, [])
            self.assertEqual(len(runner.create_commands), 2)
            successor = runner.create_commands[-1]
            self.assertEqual(
                successor[successor.index("--idempotency-key") + 1],
                "github-pr-event:example-org/demo-repo:49:commented:102",
            )
            self.assertEqual(successor[successor.index("--parent") + 1], repair_id)

    def test_archived_repair_does_not_block_later_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            workspace = root / "demo-repo" / ".worktrees" / "t_root"
            workspace.mkdir(parents=True)
            runner = ReviewWatchRunner(
                workspace,
                open_pulls=[],
                timeline=[],
            )
            runner.task_statuses["t_archived"] = "archived"

            self.assertFalse(
                poller._repair_is_outstanding(policy, "t_archived", runner)
            )

    def test_review_events_accept_only_actionable_trusted_human_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            context = poller.PullRequestContext(
                repository=policy.repositories[0],
                issue_number=48,
                pull_number=49,
                pull_url="https://github.com/example-org/demo-repo/pull/49",
                root_task_id="t_root",
                assignee=policy.assignee,
                workspace_path=root / "worktree",
                branch_name="automation/demo-repo-48",
            )
            base = {
                "event": "commented",
                "created_at": "2026-07-30T20:00:00Z",
                "html_url": context.pull_url + "#issuecomment-101",
                "user": {"login": "reviewer", "type": "User"},
                "author_association": "MEMBER",
            }
            records = [
                base | {"id": 101},
                base | {
                    "id": 102,
                    "user": {"login": "random-builder[bot]", "type": "Bot"},
                },
                base | {
                    "id": 103,
                    "user": {"login": "AGENT-BOT", "type": "User"},
                },
                base | {"id": 104, "author_association": "NONE"},
                base | {"id": "105"},
                base | {"id": 106, "created_at": None},
                base | {
                    "id": 107,
                    "event": "reviewed",
                    "state": "approved",
                    "submitted_at": "2026-07-30T20:01:00Z",
                },
                base | {
                    "id": 108,
                    "event": "reviewed",
                    "state": "changes_requested",
                    "submitted_at": "2026-07-30T20:02:00Z",
                    "html_url": context.pull_url + "#pullrequestreview-108",
                },
                base | {"id": 109, "user": {"login": "reviewer"}},
                base | {
                    "id": 110,
                    "user": {"login": "reviewer", "type": "Organization"},
                },
                base | {
                    "id": 111,
                    "user": {
                        "login": "github-code-quality[bot]",
                        "type": "Bot",
                    },
                    "author_association": "CONTRIBUTOR",
                },
                base | {
                    "id": 112,
                    "user": {
                        "login": "GITHUB-CODE-QUALITY[BOT]",
                        "type": "Bot",
                    },
                    "author_association": "NONE",
                },
            ]

            class TimelineRunner:
                def run(self, _command: list[str]) -> str:
                    return json.dumps(records)

            events = poller._review_events(
                context,
                ("github-code-quality[bot]",),
                "agent-bot",
                TimelineRunner(),
            )

            self.assertEqual(
                [event.event_id for event in events],
                [101, 111, 112, 108],
            )

    def test_review_reconciliation_failure_does_not_block_later_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            context = poller.PullRequestContext(
                repository=policy.repositories[0],
                issue_number=49,
                pull_number=50,
                pull_url="https://github.com/example-org/demo-repo/pull/50",
                root_task_id="t_good",
                assignee=policy.assignee,
                workspace_path=root / "worktree",
                branch_name="automation/demo-repo-49",
            )
            event = poller.ReviewEvent(
                event_id=200,
                kind="reviewed",
                state="approved",
                actor="reviewer",
                url=context.pull_url + "#pullrequestreview-200",
                timestamp="2026-07-30T21:00:00Z",
            )

            def context_for(
                _issue_url: str,
                task_id: str,
                _policy: poller.Policy,
                _runner: poller.Runner,
            ) -> poller.PullRequestContext:
                if task_id == "t_bad":
                    raise poller.PollerError("malformed task")
                return context

            with (
                mock.patch.object(
                    poller,
                    "_tracked_tasks",
                    return_value=[
                        ("https://github.com/example-org/demo-repo/issues/48", "t_bad"),
                        ("https://github.com/example-org/demo-repo/issues/49", "t_good"),
                    ],
                ),
                mock.patch.object(
                    poller,
                    "_pull_request_context",
                    side_effect=context_for,
                ),
                mock.patch.object(
                    poller,
                    "_watch_state",
                    return_value=poller.PullRequestWatch(
                        pull_url=context.pull_url,
                        discovery_complete=True,
                        cursor=None,
                        last_repair_task_id=None,
                    ),
                ),
                mock.patch.object(
                    poller,
                    "_open_pull_urls",
                    return_value={context.pull_url},
                ),
                mock.patch.object(poller, "_review_events", return_value=[event]),
                mock.patch.object(
                    poller,
                    "_create_review_task",
                    return_value="t_repair",
                ) as create,
            ):
                task_ids, errors = poller.reconcile_review_tasks(
                    policy,
                    root / "ledger",
                    mock.Mock(),
                )

            self.assertEqual(task_ids, ["t_repair"])
            self.assertEqual(len(errors), 1)
            self.assertIn("issues/48", errors[0])
            create.assert_called_once()

    def test_concurrent_polls_create_only_one_task(self) -> None:
        class SlowRunner(FakeRunner):
            def run(self, command: list[str]) -> str:
                if command and command[0] == poller.HERMES:
                    time.sleep(0.05)
                return super().run(command)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            ledger = root / "ledger.sqlite3"
            runners = [SlowRunner([self.eligible()]), SlowRunner([self.eligible()])]
            failures: list[BaseException] = []

            def poll(runner: SlowRunner) -> None:
                try:
                    poller.run_once(policy, ledger, runner)
                except BaseException as exc:  # pragma: no cover - assertion below
                    failures.append(exc)

            threads = [threading.Thread(target=poll, args=(runner,)) for runner in runners]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(failures, [])
            self.assertEqual(sum(len(runner.created) for runner in runners), 1)

    def test_concurrent_review_polls_create_only_one_outstanding_repair(self) -> None:
        class SlowReviewRunner(ReviewWatchRunner):
            def run(self, command: list[str]) -> str:
                if command and command[0] == poller.HERMES and "create" in command:
                    time.sleep(0.05)
                return super().run(command)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            ledger = root / "ledger.sqlite3"
            workspace = root / "demo-repo" / ".worktrees" / "t_root"
            workspace.mkdir(parents=True)
            self.seed_task(ledger)
            runner = SlowReviewRunner(
                workspace,
                open_pulls=[
                    {
                        "number": 49,
                        "state": "open",
                        "html_url": "https://github.com/example-org/demo-repo/pull/49",
                    }
                ],
                timeline=[
                    {
                        "id": 4823248879,
                        "event": "reviewed",
                        "state": "changes_requested",
                        "submitted_at": "2026-07-30T21:09:29Z",
                        "html_url": (
                            "https://github.com/example-org/demo-repo/"
                            "pull/49#pullrequestreview-4823248879"
                        ),
                        "user": {"login": "trusted-reviewer", "type": "User"},
                        "author_association": "MEMBER",
                    }
                ],
            )
            failures: list[BaseException] = []

            def poll() -> None:
                try:
                    poller.run_once(policy, ledger, runner)
                except BaseException as exc:  # pragma: no cover - assertion below
                    failures.append(exc)

            threads = [threading.Thread(target=poll) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(failures, [])
            self.assertEqual(len(runner.create_commands), 1)

    def test_filters_pull_requests_closed_unlabeled_and_malformed_records(self) -> None:
        closed = self.eligible() | {"state": "closed"}
        pull_request = self.eligible() | {"pull_request": {"url": "api"}}
        unlabeled = self.eligible() | {"labels": [{"name": "enhancement"}]}
        triage_only = self.eligible() | {"labels": [{"name": "ready-for-agent"}]}
        malformed = {
            "number": "48", "title": "", "state": "open",
            "labels": [{"name": "hermes-kanban-go"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner([closed, pull_request, unlabeled, triage_only, malformed])
            result = poller.run_once(self.policy(root), root / "ledger.sqlite3", runner)
            self.assertEqual(result.created, [])
            self.assertEqual(
                result.warnings,
                ["Skipped malformed eligible issue from example-org/demo-repo."],
            )
            self.assertEqual(result.errors, [])
            self.assertEqual(runner.created, [])

    def test_paginated_json_stream_is_merged_without_slurp(self) -> None:
        class PaginatedRunner:
            def run(self, command: list[str]) -> str:
                self_command = [poller.GH, "api", "--paginate", "issues"]
                if command != self_command:
                    raise AssertionError(command)
                return json.dumps([{"number": 1}]) + "\n" + json.dumps([{"number": 2}])

        self.assertEqual(
            poller._github_api(PaginatedRunner(), "issues"),
            [{"number": 1}, {"number": 2}],
        )

    def test_identity_mismatch_fails_before_reading_issues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            runner = FakeRunner([self.eligible()])
            runner.identity = "wrong-user"
            with self.assertRaisesRegex(
                poller.PollerError,
                "worker identity: observed GitHub login does not match worker_github_login",
            ):
                poller.run_once(policy, root / "ledger.sqlite3", runner)
            self.assertEqual(len(runner.calls), 1)
            # Fail closed without leaking configured or observed login values.
            with self.assertRaises(poller.PollerError) as ctx:
                poller.run_once(policy, root / "ledger.sqlite3", runner)
            self.assertNotIn("agent-bot", str(ctx.exception))
            self.assertNotIn("wrong-user", str(ctx.exception))
            self.assertEqual(len(runner.calls), 2)

    def test_failed_task_creation_does_not_commit_a_ledger_entry(self) -> None:
        class FailingRunner(FakeRunner):
            def run(self, command: list[str]) -> str:
                if command and command[0] == poller.HERMES:
                    raise poller.PollerError("kanban unavailable")
                return super().run(command)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FailingRunner([self.eligible()])
            ledger = root / "ledger.sqlite3"
            result = poller.run_once(self.policy(root), ledger, runner)
            self.assertEqual(result.created, [])
            self.assertEqual(
                result.errors,
                [
                    "Failed to queue https://github.com/example-org/demo-repo/issues/48: "
                    "kanban unavailable"
                ],
            )
            with sqlite3.connect(ledger) as connection:
                self.assertEqual(connection.execute("SELECT task_id FROM issue_tasks").fetchall(), [])

    def test_one_failing_issue_does_not_block_later_allowlisted_issue(self) -> None:
        class PartiallyFailingRunner(FakeRunner):
            def run(self, command: list[str]) -> str:
                if command and command[0] == poller.HERMES and "create" in command:
                    url = command[command.index("--idempotency-key") + 1]
                    if url.endswith("/48"):
                        raise poller.PollerError("kanban unavailable")
                return super().run(command)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = PartiallyFailingRunner([self.eligible(), self.eligible() | {"number": 49}])
            result = poller.run_once(self.policy(root), root / "ledger.sqlite3", runner)
            self.assertEqual([(issue.number, task_id) for issue, task_id in result.created], [(49, "t_1")])
            self.assertEqual(len(result.errors), 1)

    def test_malformed_repository_response_does_not_block_a_later_repository(self) -> None:
        eligible = self.eligible()

        class RepositoryRunner(FakeRunner):
            def run(self, command: list[str]) -> str:
                if command[:3] == [poller.GH, "api", "--paginate"]:
                    if "example-org/bad" in command[-1]:
                        return json.dumps({"message": "not an array"})
                    return json.dumps([eligible])
                return super().run(command)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = root / "bad"
            good = root / "demo-repo"
            bad.mkdir()
            good.mkdir()
            policy = poller.Policy(
                schedule="every 15m",
                board="default",
                assignee="builder",
                github_identity="agent-bot",
                inference_provider="openai",
                inference_model="gpt-4.1",
                worker_max_turns=100,
                required_label="hermes-kanban-go",
                repositories=(
                    poller.Repository("example-org/bad", bad),
                    poller.Repository("example-org/demo-repo", good),
                ),
            )
            result = poller.run_once(policy, root / "ledger.sqlite3", RepositoryRunner([]))
            self.assertEqual(
                [(issue.repository.slug, task_id) for issue, task_id in result.created],
                [("example-org/demo-repo", "t_1")],
            )
            self.assertEqual(
                result.errors,
                ["Failed to list example-org/bad: GitHub API returned a non-list page"],
            )

    def test_example_policy_is_generic_and_explicit(self) -> None:
        policy = poller.load_policy(ROOT / "config/policy.example.json")
        self.assertEqual(policy.required_label, "hermes-kanban-go")
        self.assertEqual(policy.dispatch_label, "hermes-kanban-go")
        self.assertEqual(policy.github_identity, "example-agent")
        self.assertEqual(policy.worker_github_login, "example-agent")
        self.assertEqual(policy.captain_github_login, "example-captain")
        self.assertEqual(policy.company_display_name, "ExampleCo")
        self.assertEqual(policy.assignee, "builder")
        self.assertEqual(policy.cron_deliver, "local")
        self.assertEqual(
            [(repository.slug, str(repository.worktree)) for repository in policy.repositories],
            [("example-org/demo-repo", "/opt/data/repos/demo-repo")],
        )
        self.assertEqual(
            (policy.inference_provider, policy.inference_model),
            ("openai", "gpt-4.1"),
        )
        self.assertEqual(policy.worker_max_turns, 100)
        self.assertEqual(
            policy.trusted_review_bots,
            ("github-code-quality[bot]",),
        )
        self.assertEqual(policy.version, 2)
        self.assertEqual(policy.max_epic_parallelism, 2)


    def test_policy_rejects_invalid_or_duplicate_trusted_review_bots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "demo-repo"
            checkout.mkdir()
            policy_path = root / "policy.json"
            base = {
                "version": 1,
                "schedule": "every 15m",
                "board": "default",
                "assignee": "builder",
                "github_identity": "agent-bot",
                "inference_provider": "openai",
                "inference_model": "gpt-4.1",
                "worker_max_turns": 100,
                "required_label": "hermes-kanban-go",
                "repositories": [
                    {
                        "slug": "example-org/demo-repo",
                        "worktree": str(checkout),
                    }
                ],
            }
            for invalid in (
                "github-code-quality[bot]",
                [""],
                ["github-code-quality[bot]", "GITHUB-CODE-QUALITY[BOT]"],
            ):
                with self.subTest(invalid=invalid):
                    policy_path.write_text(
                        json.dumps(base | {"trusted_review_bots": invalid}),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        poller.PollerError,
                        "trusted_review_bots",
                    ):
                        poller.load_policy(policy_path)

            policy_path.write_text(json.dumps(base), encoding="utf-8")
            self.assertEqual(
                poller.load_policy(policy_path).trusted_review_bots,
                (),
            )

    def test_policy_rejects_invalid_or_duplicate_repositories(self) -> None:
        base = json.loads(
            (ROOT / "config/policy.example.json").read_text(
                encoding="utf-8"
            )
        )
        invalid_repositories = (
            ([{"slug": "not-a-slug", "worktree": "/opt/data/repos/bad"}], "slug"),
            ([{"slug": "example-org/bad", "worktree": "relative"}], "absolute"),
            (
                [
                    {"slug": "example-org/bad", "worktree": "/opt/data/repos/bad"},
                    {"slug": "example-org/bad", "worktree": "/opt/data/repos/other"},
                ],
                "unique",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.json"
            for repositories, error in invalid_repositories:
                with self.subTest(error=error):
                    policy_path.write_text(
                        json.dumps(base | {"repositories": repositories}),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(poller.PollerError, error):
                        poller.load_policy(policy_path)

    def test_no_code_path_merges_or_force_pushes(self) -> None:
        source = Path(poller.__file__).read_text(encoding="utf-8")
        forbidden = (
            "gh pr merge",
            "gh api .*merge",
            "git push --force",
            "git push -f",
            "git push --force-with-lease",
            "allow_empty_merge",
            "merge_method",
        )
        for needle in forbidden:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, source)

    def test_task_bodies_forbid_merge_and_force_push(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            issue = poller.Issue(
                repository=policy.repositories[0],
                number=48,
                title="demo",
                body="criteria",
            )
            root_body = poller._task_body(issue, policy)
            self.assertIn("never merge", root_body)
            self.assertIn("never force-push", root_body)
            self.assertIn("agent-bot", root_body)
            self.assertIn("metadata.published_pr", root_body)
            self.assertNotIn("WisdomHelm", root_body)
            context = poller.PullRequestContext(
                repository=policy.repositories[0],
                issue_number=48,
                pull_number=49,
                pull_url="https://github.com/example-org/demo-repo/pull/49",
                root_task_id="t_root",
                assignee=policy.assignee,
                workspace_path=root / "worktree",
                branch_name="automation/demo-repo-48",
            )
            event = poller.ReviewEvent(
                event_id=1,
                kind="reviewed",
                state="changes_requested",
                actor="trusted-reviewer",
                url=context.pull_url + "#pullrequestreview-1",
                timestamp="2026-07-30T21:00:00Z",
            )
            repair_body = poller._review_task_body(context, event)
            self.assertIn("checks, and mergeability", repair_body)
            self.assertIn("inline review comments", repair_body)
            self.assertIn("force-push", repair_body)
            self.assertIn("merge", repair_body.lower())
            self.assertIn("update the existing branch and pull request", repair_body)
            self.assertIn("metadata.published_pr", repair_body)

    def test_install_helper_validates_worktrees_without_compose(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "demo-repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            policy_path = root / "policy.json"
            script_path = root / "scripts" / "github_issue_poller.py"
            policy_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "schedule": "every 15m",
                        "board": "default",
                        "assignee": "builder",
                        "github_identity": "agent-bot",
                        "inference_provider": "openai",
                        "inference_model": "gpt-4.1",
                        "worker_max_turns": 100,
                        "required_label": "hermes-kanban-go",
                        "cron_deliver": "local",
                        "repositories": [
                            {"slug": "example-org/demo-repo", "worktree": str(repo)}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            installer.install(
                policy_path=policy_path,
                ledger_path=root / "ledger.sqlite3",
                hermes_bin=Path("/nonexistent/hermes"),
                script_path=script_path,
                configure_model=False,
                reconcile_cron=False,
            )
            self.assertTrue(script_path.is_file())
            self.assertNotIn("/opt/data", script_path.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(installer.InstallError, "unavailable"):
                missing = poller.load_policy(policy_path)
                bad = poller.Policy(
                    schedule=missing.schedule,
                    board=missing.board,
                    assignee=missing.assignee,
                    github_identity=missing.github_identity,
                    inference_provider=missing.inference_provider,
                    inference_model=missing.inference_model,
                    worker_max_turns=missing.worker_max_turns,
                    required_label=missing.required_label,
                    repositories=(
                        poller.Repository("example-org/missing", root / "missing"),
                    ),
                    trusted_review_bots=missing.trusted_review_bots,
                    cron_deliver=missing.cron_deliver,
                )
                # write temp policy with bad worktree
                bad_path = root / "bad-policy.json"
                bad_script = root / "scripts" / "bad_github_issue_poller.py"
                bad_path.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "schedule": "every 15m",
                            "board": "default",
                            "assignee": "builder",
                            "github_identity": "agent-bot",
                            "inference_provider": "openai",
                            "inference_model": "gpt-4.1",
                            "worker_max_turns": 100,
                            "required_label": "hermes-kanban-go",
                            "repositories": [
                                {
                                    "slug": "example-org/missing",
                                    "worktree": str(root / "missing"),
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                installer.install(
                    policy_path=bad_path,
                    ledger_path=root / "ledger2.sqlite3",
                    hermes_bin=Path("/nonexistent/hermes"),
                    script_path=bad_script,
                    configure_model=False,
                    reconcile_cron=False,
                )


class GitHubIssuePollerProcessIntegrationTests(unittest.TestCase):
    def eligible(self) -> dict[str, object]:
        return {
            "number": 48,
            "title": "Unify structured data",
            "body": "acceptance criteria",
            "state": "open",
            "labels": [{"name": "hermes-kanban-go"}],
        }

    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o700)

    def test_real_process_handles_flat_api_filtering_concurrency_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "demo-repo"
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            policy_path = root / "policy.json"
            ledger = root / "ledger.sqlite3"
            policy_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "schedule": "every 15m",
                        "board": "default",
                        "assignee": "builder",
                        "github_identity": "agent-bot",
                        "inference_provider": "openai",
                        "inference_model": "gpt-4.1",
                        "worker_max_turns": 100,
                        "required_label": "hermes-kanban-go",
                        "repositories": [
                            {"slug": "example-org/demo-repo", "worktree": str(checkout)}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            gh = root / "gh"
            hermes = root / "hermes"
            created = root / "created.jsonl"
            self._write_executable(
                gh,
                """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args == ['api', 'user', '--jq', '.login']:
    print('agent-bot')
elif args[:2] == ['api', '--paginate'] and '--slurp' not in args:
    print(os.environ['TEST_GH_ISSUES'])
else:
    print('unsupported gh argv: ' + repr(args), file=sys.stderr)
    raise SystemExit(2)
""",
            )
            self._write_executable(
                hermes,
                """#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
if 'show' in args:
    task_id = args[args.index('show') + 1]
    print(json.dumps({
        'task': {
            'id': task_id,
            'status': 'running',
            'workspace_kind': 'worktree',
            'workspace_path': '/unused',
            'branch_name': 'automation/demo-repo-48',
        },
        'comments': [],
        'runs': [],
    }))
    raise SystemExit(0)
if '--created-by' not in args or args[args.index('--created-by') + 1] != 'github-issue-poller':
    print('missing explicit automation attribution', file=sys.stderr)
    raise SystemExit(2)
with pathlib.Path(os.environ['TEST_CREATED_LOG']).open('a', encoding='utf-8') as handle:
    handle.write(json.dumps(args) + '\\n')
print(json.dumps({'id': 'task-1'}))
""",
            )
            harness = """
import sys
sys.path.insert(0, sys.argv[1])
from hermes_helmet import github_issue_poller as poller
poller.GH = sys.argv[2]
poller.HERMES = sys.argv[3]
raise SystemExit(poller.main(['--config', sys.argv[4], '--ledger', sys.argv[5]]))
"""
            records = [
                self.eligible(),
                self.eligible() | {"state": "closed"},
                self.eligible() | {"pull_request": {"url": "api"}},
                self.eligible() | {"labels": [{"name": "ready-for-agent"}]},
            ]
            environment = {
                **os.environ,
                "TEST_GH_ISSUES": json.dumps(records),
                "TEST_CREATED_LOG": str(created),
            }
            command = [
                sys.executable, "-c", harness, str(ROOT / "src"), str(gh), str(hermes),
                str(policy_path), str(ledger),
            ]
            first = subprocess.Popen(
                command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment
            )
            second = subprocess.Popen(
                command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment
            )
            first_output = first.communicate()
            second_output = second.communicate()
            self.assertEqual(first.returncode, 0, first_output)
            self.assertEqual(second.returncode, 0, second_output)
            self.assertEqual(created.read_text(encoding="utf-8").count("\n"), 1)

            restarted = subprocess.run(command, text=True, capture_output=True, env=environment)
            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            self.assertEqual(created.read_text(encoding="utf-8").count("\n"), 1)
            with sqlite3.connect(ledger) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM issue_tasks").fetchone(), (1,))


class RuntimeBoundaryTests(unittest.TestCase):
    """Deploy-time boundaries: non-root runtime, token scrub, policy mount."""

    def test_dockerfile_switches_to_non_root_user(self) -> None:
        dockerfile = (ROOT / "deploy/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("USER root", dockerfile)
        self.assertRegex(dockerfile, r"USER \$\{HERMES_UID\}:\$\{HERMES_GID\}|USER 10000:10000")
        self.assertGreater(
            dockerfile.rfind("USER ${HERMES_UID}:${HERMES_GID}"),
            dockerfile.find("USER root"),
        )

    def test_dockerfile_keeps_control_plane_root_owned_and_worker_non_writable(self) -> None:
        dockerfile = (ROOT / "deploy/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY skills /opt/hermes-helmet/src/hermes_helmet/bundled_skills", dockerfile)
        # Mutable runtime state only.
        self.assertIn('chown -R "${HERMES_UID}:${HERMES_GID}" /opt/data', dockerfile)
        # Control plane must remain root-owned so UID 10000 cannot rewrite the
        # entrypoint, poller, or SOURCE_COMMIT across restart: unless-stopped.
        self.assertIn("chown -R root:root /opt/hermes-helmet", dockerfile)
        self.assertIn("chmod -R go-w /opt/hermes-helmet", dockerfile)
        self.assertIn("chmod -R a+rX /opt/hermes-helmet", dockerfile)
        self.assertNotIn(
            'chown -R "${HERMES_UID}:${HERMES_GID}" /opt/hermes-helmet',
            dockerfile,
        )
        self.assertNotRegex(
            dockerfile,
            r"chown\s+-R\s+\"?\$\{HERMES_UID\}:\$\{HERMES_GID\}\"?\s+/opt/hermes-helmet",
        )
        # SOURCE_COMMIT is written as a regular file then mode-locked readable.
        self.assertIn("/opt/hermes-helmet/SOURCE_COMMIT", dockerfile)
        self.assertIn("chmod 644 /opt/hermes-helmet/SOURCE_COMMIT", dockerfile)

    def test_worker_uid_cannot_modify_control_plane_paths(self) -> None:
        """Simulate image perms: root-owned tree readable but not writable by worker."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "opt" / "hermes-helmet"
            src_dir = root / "src" / "hermes_helmet"
            src_dir.mkdir(parents=True)
            entrypoint = root / "runtime-entrypoint.sh"
            source_commit = root / "SOURCE_COMMIT"
            poller = src_dir / "github_issue_poller.py"
            entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
            source_commit.write_text("abc123\n", encoding="utf-8")
            poller.write_text("print('poller')\n", encoding="utf-8")

            # Match the Dockerfile contract: world-readable/executable, group/other non-writable.
            # Drop owner-write as well so the test process (standing in for UID 10000)
            # cannot mutate protected paths even when it owns the temp tree.
            for path in (root, root / "src", src_dir):
                path.chmod(0o555)
            entrypoint.chmod(0o555)
            source_commit.chmod(0o444)
            poller.chmod(0o444)

            for path in (entrypoint, source_commit, poller):
                self.assertTrue(os.access(path, os.R_OK), path)
                self.assertFalse(os.access(path, os.W_OK), path)
                with self.assertRaises(PermissionError):
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write("tamper\n")
                with self.assertRaises(OSError):
                    path.write_text("tamper\n", encoding="utf-8")

            # Worker must still be able to execute the entrypoint and read sources.
            self.assertTrue(os.access(entrypoint, os.X_OK))
            self.assertEqual(source_commit.read_text(encoding="utf-8"), "abc123\n")
            self.assertEqual(poller.read_text(encoding="utf-8"), "print('poller')\n")

    def test_compose_mounts_documented_policy_and_non_root_user(self) -> None:
        compose = (ROOT / "deploy/compose.yaml").read_text(encoding="utf-8")
        self.assertIn("../config/policy.json", compose)
        self.assertNotIn("./config/policy.example.json", compose)
        self.assertIn('user: "${HERMES_UID:-10000}:${HERMES_GID:-10000}"', compose)
        self.assertIn("os.getuid()!=0", compose.replace(" ", ""))
        # Relative to deploy/, the default host path is the documented quickstart file.
        default_host = (ROOT / "deploy" / "../config/policy.json").resolve()
        self.assertEqual(default_host, (ROOT / "config/policy.json").resolve())
        self.assertTrue((ROOT / "config/policy.example.json").is_file())

    def test_runtime_entrypoint_scrubs_tokens_before_upstream_init(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_dir = root / "run"
            home = root / "data"
            policy_source = root / "policy.source.json"
            marker = root / "hermes-env.json"
            init_marker = root / "init-env.json"
            fake_init = root / "init"
            fake_main_wrapper = root / "main-wrapper.sh"
            hermes_bin = root / "opt" / "hermes" / ".venv" / "bin" / "hermes"
            hermes_bin.parent.mkdir(parents=True)
            runtime_dir.mkdir()
            home.mkdir()
            policy_source.write_text('{"version":1}\n', encoding="utf-8")
            hermes_bin.write_text(
                """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
Path(os.environ["TEST_MARKER"]).write_text(
    json.dumps({
        "uid": os.getuid(),
        "gh_token": os.environ.get("GH_TOKEN"),
        "github_token": os.environ.get("GITHUB_TOKEN"),
        "argv": sys.argv[1:],
    }),
    encoding="utf-8",
)
raise SystemExit(0)
""",
                encoding="utf-8",
            )
            hermes_bin.chmod(0o700)
            fake_init.write_text(
                f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
Path(os.environ["TEST_INIT_MARKER"]).write_text(
    json.dumps({{
        "gh_token": os.environ.get("GH_TOKEN"),
        "github_token": os.environ.get("GITHUB_TOKEN"),
        "argv": sys.argv[1:],
    }}),
    encoding="utf-8",
)
os.execv(sys.argv[1], sys.argv[1:])
""",
                encoding="utf-8",
            )
            fake_init.chmod(0o700)
            fake_main_wrapper.write_text(
                "#!/bin/sh\nexec \"$TEST_HERMES_BIN\" \"$@\"\n",
                encoding="utf-8",
            )
            fake_main_wrapper.chmod(0o700)

            entrypoint = root / "runtime-entrypoint.sh"
            entrypoint.write_text(
                (ROOT / "deploy/hermes/runtime-entrypoint.sh").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            entrypoint.chmod(0o755)

            # Point the entrypoint at the fake upstream init and main wrapper by
            # rewriting their absolute paths in a local copy.
            local = entrypoint.read_text(encoding="utf-8").replace(
                "/opt/hermes/docker/main-wrapper.sh",
                str(fake_main_wrapper),
            ).replace(
                "/init",
                str(fake_init),
            ).replace(
                "/opt/data/github-issue-poller/policy.json",
                str(home / "github-issue-poller" / "policy.json"),
            )
            entrypoint.write_text(local, encoding="utf-8")

            env = {
                **os.environ,
                "HOME": str(home),
                "HERMES_HOME": str(home),
                "HERMES_UID": str(os.getuid()),
                "HERMES_GID": str(os.getgid()),
                "HERMES_HELMET_RUNTIME_DIR": str(runtime_dir),
                "HERMES_HELMET_POLICY_SOURCE": str(policy_source),
                "GH_TOKEN": "secret-pat-value",
                "GITHUB_TOKEN": "secret-pat-value",
                "TEST_MARKER": str(marker),
                "TEST_INIT_MARKER": str(init_marker),
                "TEST_HERMES_BIN": str(hermes_bin),
            }
            # Ensure a real git is available for the optional credential helper path.
            completed = subprocess.run(
                [str(entrypoint), "gateway", "run"],
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            payload = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(payload["uid"], os.getuid())
            self.assertNotEqual(payload["uid"], 0)
            self.assertIsNone(payload["gh_token"])
            self.assertIsNone(payload["github_token"])
            self.assertEqual(payload["argv"], ["gateway", "run"])
            init_payload = json.loads(init_marker.read_text(encoding="utf-8"))
            self.assertIsNone(init_payload["gh_token"])
            self.assertIsNone(init_payload["github_token"])
            self.assertEqual(
                init_payload["argv"],
                [str(fake_main_wrapper), "gateway", "run"],
            )
            token_file = runtime_dir / "github-token"
            self.assertTrue(token_file.is_file())
            self.assertEqual(token_file.read_text(encoding="utf-8"), "secret-pat-value")
            self.assertEqual(stat_mode := (token_file.stat().st_mode & 0o777), 0o600, oct(stat_mode))
            installed_policy = home / "github-issue-poller" / "policy.json"
            self.assertTrue(installed_policy.is_file())
            self.assertEqual(installed_policy.read_text(encoding="utf-8"), '{"version":1}\n')


if __name__ == "__main__":
    unittest.main()
