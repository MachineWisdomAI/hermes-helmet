from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hermes_helmet.authority import (  # noqa: E402
    AuthorityError,
    policy_from_mapping,
    verify_captain_identity,
)
from hermes_helmet import helmet_issue as hi  # noqa: E402
from hermes_helmet import install_skills as skills  # noqa: E402
from hermes_helmet import cli as helmet_cli  # noqa: E402


FIXTURE_ORG = "example-org"
FIXTURE_REPO = "demo-repo"
FIXTURE_SLUG = f"{FIXTURE_ORG}/{FIXTURE_REPO}"
ISSUE_URL = f"https://github.com/{FIXTURE_SLUG}/issues/2"
PR_URL = f"https://github.com/{FIXTURE_SLUG}/pull/10"
CAPTAIN = "example-captain"
WORKER = "example-agent"


def _policy_dict(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "version": 2,
        "company": {"display_name": "ExampleCo", "slug": "exampleco"},
        "captain_github_login": CAPTAIN,
        "worker_github_login": WORKER,
        "schedule": "every 15m",
        "board": "default",
        "assignee": "builder",
        "inference_provider": "openai",
        "inference_model": "gpt-4.1",
        "worker_max_turns": 50,
        "ready_label": "ready-for-agent",
        "dispatch_label": "hermes-kanban-go",
        "cron_deliver": "local",
        "github_owners": [FIXTURE_ORG],
        "trusted_review_bots": ["github-code-quality[bot]"],
        "merge": {
            "default_mode": "explicit_captain_approval",
            "unattended_marker": "Merge when clean: yes",
            "narrow_marker": "Merge when clean: no",
        },
        "budgets": {"max_issue_runtime_minutes": 120, "max_repair_rounds": 10},
        "repositories": [
            {"slug": FIXTURE_SLUG, "worktree": "/opt/data/repos/demo-repo"}
        ],
    }
    base.update(overrides)
    return base


def _policy(**overrides: object):
    return policy_from_mapping(_policy_dict(**overrides))


class FakeRunner:
    def __init__(self) -> None:
        self.identity = CAPTAIN
        self.issue = {
            "number": 2,
            "title": "H1 extract",
            "body": "Ship the loop.\n",
            "state": "open",
            "html_url": ISSUE_URL,
            "labels": [{"name": "ready-for-agent"}, {"name": "hermes-kanban-go"}],
        }
        self.pull = {
            "number": 10,
            "html_url": PR_URL,
            "state": "open",
            "merged": False,
            "draft": False,
            "mergeable_state": "clean",
            "body": f"Closes #2\n\n{ISSUE_URL}\n",
            "user": {"login": WORKER},
            "head": {"sha": "abc123deadbeef", "ref": "automation/demo-repo-2"},
            "base": {"ref": "main"},
        }
        self.open_pulls = [self.pull]
        self.pulls: dict[int, dict[str, object]] = {}
        self.timeline: list[object] = []
        self.calls: list[list[str]] = []
        self.labels_posted: list[str] = []
        self.kanban_tasks: dict[str, dict[str, object]] = {
            "t_root": {
                "task": {
                    "id": "t_root",
                    "status": "done",
                    "assignee": "builder",
                    "workspace_kind": "worktree",
                    "workspace_path": "/opt/data/repos/demo-repo",
                    "branch_name": "automation/demo-repo-2",
                },
                "runs": [
                    {
                        "id": 1,
                        "status": "done",
                        "metadata": {"pr_url": PR_URL},
                    }
                ],
            }
        }
        self.created_tasks: list[str] = []
        self.reviews: list[dict[str, object]] = []

    def run(self, command: list[str]) -> str:
        self.calls.append(command)
        if command[:3] == ["gh", "api", "user"] or (
            len(command) >= 3 and command[0].endswith("gh") and command[1] == "api" and command[2] == "user"
        ):
            return json.dumps({"login": self.identity})
        if command[0].endswith("gh") and command[1] == "api":
            # Normalize endpoint as last non-flag token-ish
            if "--method" in command and "labels" in command[-1]:
                self.labels_posted.append(command[-1])
                return "[]"
            endpoint = command[-1]
            if endpoint == "user":
                return json.dumps({"login": self.identity})
            if endpoint == f"repos/{FIXTURE_SLUG}/issues/2":
                return json.dumps(self.issue)
            if endpoint.startswith(f"repos/{FIXTURE_SLUG}/issues/2/timeline"):
                return json.dumps(self.timeline)
            if endpoint.startswith(f"repos/{FIXTURE_SLUG}/pulls?"):
                return json.dumps(self.open_pulls)
            reviews_prefix = f"repos/{FIXTURE_SLUG}/pulls/"
            if "/reviews" in endpoint and endpoint.startswith(reviews_prefix):
                return json.dumps(self.reviews)
            if endpoint.startswith(reviews_prefix) and endpoint.count("/") >= 4:
                # repos/{slug}/pulls/{n}
                try:
                    number = int(endpoint.rsplit("/", 1)[-1])
                except ValueError as exc:
                    raise AssertionError(command) from exc
                if number in self.pulls:
                    return json.dumps(self.pulls[number])
                if number == int(self.pull.get("number") or 10):
                    return json.dumps(self.pull)
                if number == 11:
                    other = dict(self.pull)
                    other.update(
                        {
                            "number": 11,
                            "html_url": f"https://github.com/{FIXTURE_SLUG}/pull/11",
                            "head": {"sha": "fff", "ref": "other-branch"},
                            "user": {"login": WORKER},
                        }
                    )
                    return json.dumps(other)
            if "--method" in command and command[command.index("--method") + 1] == "PUT":
                # merge
                self.pull = dict(self.pull)
                self.pull["merged"] = True
                self.pull["state"] = "closed"
                return json.dumps({"merged": True})
            raise AssertionError(command)
        if command[0].endswith("hermes") and "kanban" in command:
            if "show" in command:
                task_id = command[command.index("show") + 1]
                return json.dumps(self.kanban_tasks[task_id])
            if "create" in command:
                key = command[command.index("--idempotency-key") + 1]
                self.created_tasks.append(key)
                task_id = f"t_{len(self.created_tasks)}"
                self.kanban_tasks[task_id] = {
                    "task": {
                        "id": task_id,
                        "status": "ready",
                        "workspace_kind": "worktree",
                        "workspace_path": "/opt/data/repos/demo-repo",
                        "branch_name": "automation/demo-repo-2",
                    },
                    "runs": [],
                }
                return json.dumps({"id": task_id})
        if command[:2] == ["git", "-C"]:
            return "true\n"
        raise AssertionError(command)


def _ledger_with_root(path: Path, issue_url: str = ISSUE_URL, task_id: str = "t_root") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE issue_tasks (
            issue_url TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE pull_request_watches (
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
    conn.execute(
        "INSERT INTO issue_tasks(issue_url, task_id) VALUES (?, ?)",
        (issue_url, task_id),
    )
    conn.commit()
    conn.close()


class CaptainIdentityTests(unittest.TestCase):
    def test_captain_ok(self) -> None:
        verify_captain_identity(_policy(), CAPTAIN)

    def test_worker_rejected(self) -> None:
        with self.assertRaises(AuthorityError):
            verify_captain_identity(_policy(), WORKER)

    def test_unknown_rejected(self) -> None:
        with self.assertRaises(AuthorityError):
            verify_captain_identity(_policy(), "someone-else")


class CheckpointTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = hi.checkpoint_path(Path(tmp), ISSUE_URL)
            cp = hi.Checkpoint(version=1, issue_url=ISSUE_URL, state="WAIT_PR")
            hi.save_checkpoint(path, cp)
            loaded = hi.load_checkpoint(path)
            assert loaded is not None
            self.assertEqual(loaded.state, "WAIT_PR")
            self.assertEqual(loaded.issue_url, ISSUE_URL)


class RepairPolicyTests(unittest.TestCase):
    def test_never_create_repair_from_non_actionable(self) -> None:
        cases = [
            dict(
                activity_kind="reviewed",
                actor_login=CAPTAIN,
                actor_is_worker=False,
                trusted=True,
                has_actionable_findings=True,
                is_approval_only=True,
                is_ci_failure_only=False,
                is_repeat_poll=False,
            ),
            dict(
                activity_kind="commented",
                actor_login="random",
                actor_is_worker=False,
                trusted=False,
                has_actionable_findings=True,
                is_approval_only=False,
                is_ci_failure_only=False,
                is_repeat_poll=False,
            ),
            dict(
                activity_kind="reviewed",
                actor_login=WORKER,
                actor_is_worker=True,
                trusted=True,
                has_actionable_findings=True,
                is_approval_only=False,
                is_ci_failure_only=False,
                is_repeat_poll=False,
            ),
            dict(
                activity_kind="check",
                actor_login="ci",
                actor_is_worker=False,
                trusted=True,
                has_actionable_findings=False,
                is_approval_only=False,
                is_ci_failure_only=True,
                is_repeat_poll=False,
            ),
            dict(
                activity_kind="poll",
                actor_login=CAPTAIN,
                actor_is_worker=False,
                trusted=True,
                has_actionable_findings=True,
                is_approval_only=False,
                is_ci_failure_only=False,
                is_repeat_poll=True,
            ),
        ]
        for case in cases:
            self.assertFalse(hi.should_create_repair_from_activity(**case))

    def test_even_actionable_trusted_findings_do_not_create_here(self) -> None:
        # helmet-issue never creates repair cards; H1 owns that.
        self.assertFalse(
            hi.should_create_repair_from_activity(
                activity_kind="reviewed",
                actor_login=CAPTAIN,
                actor_is_worker=False,
                trusted=True,
                has_actionable_findings=True,
                is_approval_only=False,
                is_ci_failure_only=False,
                is_repeat_poll=False,
            )
        )


class DiscoveryTests(unittest.TestCase):
    def test_scenario_issue2_pr10_already_open_green_pr(self) -> None:
        """Historical H1 shape: open worker PR discovered without user nudge."""
        policy = _policy()
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            _ledger_with_root(ledger)
            conn = sqlite3.connect(ledger)
            conn.execute(
                """
                INSERT INTO pull_request_watches(issue_url, pull_url, discovery_complete)
                VALUES (?, ?, 1)
                """,
                (ISSUE_URL, PR_URL),
            )
            conn.commit()
            conn.close()
            issue = hi.load_issue(policy, ISSUE_URL, runner, gh="gh")
            root, pull, repair = hi.discover_progress(
                policy, issue, ledger, runner, gh="gh", hermes="hermes"
            )
            self.assertEqual(root, "t_root")
            assert pull is not None
            self.assertEqual(pull.url, PR_URL)
            self.assertEqual(pull.author_login, WORKER)
            self.assertEqual(pull.head_sha, "abc123deadbeef")
            self.assertIsNone(repair)

    def test_kanban_pr_only_metadata_is_not_adopted(self) -> None:
        self.assertIsNone(
            hi._pr_url_from_kanban(
                {"runs": [{"id": 1, "metadata": {"pr": PR_URL}}]},
                task_id="t_root",
            )
        )

    def test_kanban_published_pr_metadata_is_adopted(self) -> None:
        self.assertEqual(
            hi._pr_url_from_kanban(
                {"runs": [{"id": 1, "metadata": {"published_pr": PR_URL}}]},
                task_id="t_root",
            ),
            PR_URL,
        )

    def test_kanban_legacy_pr_url_metadata_is_still_adopted(self) -> None:
        self.assertEqual(
            hi._pr_url_from_kanban(
                {"runs": [{"id": 1, "metadata": {"pr_url": PR_URL}}]},
                task_id="t_root",
            ),
            PR_URL,
        )

    def test_kanban_matching_pr_url_and_published_pr_are_adopted(self) -> None:
        self.assertEqual(
            hi._pr_url_from_kanban(
                {
                    "runs": [
                        {
                            "id": 1,
                            "metadata": {"pr_url": PR_URL, "published_pr": PR_URL},
                        }
                    ]
                },
                task_id="t_root",
            ),
            PR_URL,
        )

    def test_kanban_disagreeing_pr_url_and_published_pr_fail_closed(self) -> None:
        with self.assertRaises(hi.HelmetIssueError) as raised:
            hi._pr_url_from_kanban(
                {
                    "runs": [
                        {
                            "id": 1,
                            "metadata": {
                                "pr_url": PR_URL,
                                "published_pr": (
                                    f"https://github.com/{FIXTURE_SLUG}/pull/11"
                                ),
                            },
                        }
                    ]
                },
                task_id="t_root",
            )
        self.assertIn("kanban_pr_metadata_disagree", str(raised.exception))
        self.assertIn("t_root", str(raised.exception))

    def test_kanban_malformed_pull_request_metadata_is_ignored(self) -> None:
        self.assertIsNone(
            hi._pr_url_from_kanban(
                {
                    "runs": [
                        {
                            "id": 1,
                            "metadata": {
                                "published_pr": "not-a-pull-request",
                                "pr_url": "also bad",
                            },
                        }
                    ]
                },
                task_id="t_root",
            )
        )
        self.assertEqual(
            hi._pr_url_from_kanban(
                {
                    "runs": [
                        {
                            "id": 1,
                            "metadata": {
                                "published_pr": "not-a-pull-request",
                                "pr_url": PR_URL,
                            },
                        }
                    ]
                },
                task_id="t_root",
            ),
            PR_URL,
        )

    def test_discover_progress_adopts_published_pr_when_github_has_no_link(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        runner.timeline = []
        runner.open_pulls = []
        runner.kanban_tasks["t_root"] = {
            "task": {
                "id": "t_root",
                "status": "done",
                "assignee": "builder",
                "workspace_kind": "worktree",
                "workspace_path": "/opt/data/repos/demo-repo",
                "branch_name": "automation/demo-repo-2",
            },
            "runs": [
                {
                    "id": 1,
                    "status": "done",
                    "metadata": {"published_pr": PR_URL},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            _ledger_with_root(ledger)
            issue = hi.load_issue(policy, ISSUE_URL, runner, gh="gh")
            root, pull, repair = hi.discover_progress(
                policy, issue, ledger, runner, gh="gh", hermes="hermes"
            )
            self.assertEqual(root, "t_root")
            assert pull is not None
            self.assertEqual(pull.url, PR_URL)
            self.assertIsNone(repair)

    def test_adopt_existing_root_never_duplicates(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            _ledger_with_root(ledger)
            issue = hi.load_issue(policy, ISSUE_URL, runner, gh="gh")
            task_id, created = hi.adopt_or_dispatch_root_task(
                policy, issue, ledger, runner, hermes="hermes", gh="gh"
            )
            self.assertEqual(task_id, "t_root")
            self.assertFalse(created)
            self.assertEqual(runner.created_tasks, [])

    def test_ambiguous_open_worker_prs_block(self) -> None:
        policy = _policy()
        issue = hi.IssueRef(
            repository=policy.repositories[0],
            number=2,
            title="x",
            body="",
            state="open",
            labels=("ready-for-agent",),
            html_url=ISSUE_URL,
        )
        a = hi.PullRequestRef(
            FIXTURE_SLUG,
            10,
            PR_URL,
            "aaa",
            "branch-a",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
            body="Closes #2\n",
        )
        b = hi.PullRequestRef(
            FIXTURE_SLUG,
            11,
            f"https://github.com/{FIXTURE_SLUG}/pull/11",
            "bbb",
            "branch-b",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
            body=f"Fixes {ISSUE_URL}\n",
        )
        with self.assertRaises(hi.HelmetIssueError):
            hi.select_canonical_pull_request(policy, issue, [a, b])

    def test_non_worker_author_rejected(self) -> None:
        policy = _policy()
        issue = hi.IssueRef(
            repository=policy.repositories[0],
            number=2,
            title="x",
            body="",
            state="open",
            labels=("ready-for-agent",),
            html_url=ISSUE_URL,
        )
        pr = hi.PullRequestRef(
            FIXTURE_SLUG,
            10,
            PR_URL,
            "aaa",
            "automation/demo-repo-2",
            "main",
            "stranger",
            "open",
            False,
            "clean",
            False,
        )
        with self.assertRaises(hi.HelmetIssueError):
            hi.select_canonical_pull_request(policy, issue, [pr])

    def test_incidental_merged_worker_timeline_pr_is_not_canonical(self) -> None:
        """Discovery leads alone must not become fulfillment evidence."""
        policy = _policy()
        issue = hi.IssueRef(
            repository=policy.repositories[0],
            number=14,
            title="open work",
            body="",
            state="open",
            labels=("ready-for-agent",),
            html_url=f"https://github.com/{FIXTURE_SLUG}/issues/14",
        )
        # Merged worker PR for a different issue — only an incidental reference.
        other = hi.PullRequestRef(
            FIXTURE_SLUG,
            22,
            f"https://github.com/{FIXTURE_SLUG}/pull/22",
            "mergedsha0001",
            "automation/demo-repo-13",
            "main",
            WORKER,
            "closed",
            True,
            "unknown",
            False,
            issue_urls=(f"https://github.com/{FIXTURE_SLUG}/issues/13",),
            body="Closes #13\n\nReview mentioned #14 in passing.\n",
        )
        self.assertFalse(hi.pull_associates_with_issue(issue, other))
        self.assertIsNone(hi.select_canonical_pull_request(policy, issue, [other]))

    def test_canonical_branch_and_closing_ref_still_associate(self) -> None:
        policy = _policy()
        issue = hi.IssueRef(
            repository=policy.repositories[0],
            number=2,
            title="x",
            body="",
            state="open",
            labels=("ready-for-agent",),
            html_url=ISSUE_URL,
        )
        by_branch = hi.PullRequestRef(
            FIXTURE_SLUG,
            10,
            PR_URL,
            "abc",
            "automation/demo-repo-2",
            "main",
            WORKER,
            "closed",
            True,
            "unknown",
            False,
            body="Implementation without closing keyword yet.\n",
        )
        by_close = hi.PullRequestRef(
            FIXTURE_SLUG,
            11,
            f"https://github.com/{FIXTURE_SLUG}/pull/11",
            "def",
            "feature/misc",
            "main",
            WORKER,
            "closed",
            True,
            "unknown",
            False,
            body="Closes #2\n",
        )
        bare_mention = hi.PullRequestRef(
            FIXTURE_SLUG,
            12,
            f"https://github.com/{FIXTURE_SLUG}/pull/12",
            "ghi",
            "feature/other",
            "main",
            WORKER,
            "closed",
            True,
            "unknown",
            False,
            body="See also #2 for context.\n",
        )
        self.assertTrue(hi.pull_associates_with_issue(issue, by_branch))
        self.assertTrue(hi.pull_associates_with_issue(issue, by_close))
        self.assertFalse(hi.pull_associates_with_issue(issue, bare_mention))
        self.assertEqual(
            hi.select_canonical_pull_request(policy, issue, [by_branch]), by_branch
        )
        self.assertEqual(
            hi.select_canonical_pull_request(policy, issue, [by_close]), by_close
        )
        self.assertIsNone(
            hi.select_canonical_pull_request(policy, issue, [bare_mention])
        )

    def test_incidental_full_url_and_markdown_link_are_not_ownership(self) -> None:
        """Ordinary body/Markdown issue links are discovery leads, not ownership."""
        policy = _policy()
        issue = hi.IssueRef(
            repository=policy.repositories[0],
            number=14,
            title="open work",
            body="",
            state="open",
            labels=("ready-for-agent",),
            html_url=f"https://github.com/{FIXTURE_SLUG}/issues/14",
        )
        deferred_url = hi.PullRequestRef(
            FIXTURE_SLUG,
            22,
            f"https://github.com/{FIXTURE_SLUG}/pull/22",
            "mergedsha0001",
            "automation/demo-repo-13",
            "main",
            WORKER,
            "closed",
            True,
            "unknown",
            False,
            issue_urls=(f"https://github.com/{FIXTURE_SLUG}/issues/14",),
            body=(
                "Closes #13\n\n"
                f"Deferred context: {issue.html_url} is separate work.\n"
            ),
        )
        markdown_link = hi.PullRequestRef(
            FIXTURE_SLUG,
            23,
            f"https://github.com/{FIXTURE_SLUG}/pull/23",
            "mergedsha0002",
            "feature/notes",
            "main",
            WORKER,
            "closed",
            True,
            "unknown",
            False,
            issue_urls=(issue.html_url,),
            body=f"See also [open work]({issue.html_url}) for context.\n",
        )
        self.assertFalse(hi.pull_associates_with_issue(issue, deferred_url))
        self.assertFalse(hi.pull_associates_with_issue(issue, markdown_link))
        self.assertIsNone(
            hi.select_canonical_pull_request(policy, issue, [deferred_url, markdown_link])
        )

    def test_supported_closing_forms_on_ordinary_branch(self) -> None:
        """Optional colon, repo-qualified short refs, and closing full URLs associate."""
        policy = _policy()
        issue = hi.IssueRef(
            repository=policy.repositories[0],
            number=2,
            title="x",
            body="",
            state="open",
            labels=("ready-for-agent",),
            html_url=ISSUE_URL,
        )
        closes_colon = hi.PullRequestRef(
            FIXTURE_SLUG,
            31,
            f"https://github.com/{FIXTURE_SLUG}/pull/31",
            "sha31",
            "feature/colon-close",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
            body="Closes: #2\n",
        )
        repo_qualified = hi.PullRequestRef(
            FIXTURE_SLUG,
            32,
            f"https://github.com/{FIXTURE_SLUG}/pull/32",
            "sha32",
            "feature/qualified-close",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
            body=f"Fixes {FIXTURE_SLUG}#2\n",
        )
        closes_full_url = hi.PullRequestRef(
            FIXTURE_SLUG,
            33,
            f"https://github.com/{FIXTURE_SLUG}/pull/33",
            "sha33",
            "feature/url-close",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
            body=f"Resolves {ISSUE_URL}\n",
        )
        other_repo = hi.PullRequestRef(
            FIXTURE_SLUG,
            34,
            f"https://github.com/{FIXTURE_SLUG}/pull/34",
            "sha34",
            "feature/other-repo",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
            body="Fixes other-org/other-repo#2\n",
        )
        neighbor = hi.PullRequestRef(
            FIXTURE_SLUG,
            35,
            f"https://github.com/{FIXTURE_SLUG}/pull/35",
            "sha35",
            "feature/neighbor",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
            body="Closes: #20\n",
        )
        for pull in (closes_colon, repo_qualified, closes_full_url):
            self.assertTrue(hi.pull_associates_with_issue(issue, pull))
            self.assertEqual(
                hi.select_canonical_pull_request(policy, issue, [pull]), pull
            )
        self.assertFalse(hi.pull_associates_with_issue(issue, other_repo))
        self.assertFalse(hi.pull_associates_with_issue(issue, neighbor))
        self.assertIsNone(
            hi.select_canonical_pull_request(policy, issue, [other_repo, neighbor])
        )
        self.assertEqual(
            hi.closing_references("Closes: #2 and Fixes example-org/demo-repo#20"),
            frozenset({2, 20}),
        )
        self.assertEqual(
            hi.closing_references(
                "Fixes other-org/other-repo#2",
                repository_slug=FIXTURE_SLUG,
            ),
            frozenset(),
        )

    def test_valid_watch_not_ambiguous_with_incidental_url_candidate(self) -> None:
        """Ledger watch stays sole owner when another candidate only links the issue."""
        policy = _policy()
        issue = hi.IssueRef(
            repository=policy.repositories[0],
            number=2,
            title="x",
            body="",
            state="open",
            labels=("ready-for-agent",),
            html_url=ISSUE_URL,
        )
        watched = hi.PullRequestRef(
            FIXTURE_SLUG,
            10,
            PR_URL,
            "abc",
            "worker-delivery-branch",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
            body="Worker delivery without closing keyword.\n",
        )
        incidental = hi.PullRequestRef(
            FIXTURE_SLUG,
            99,
            f"https://github.com/{FIXTURE_SLUG}/pull/99",
            "incidental99",
            "feature/notes",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
            issue_urls=(ISSUE_URL,),
            body=f"Notes about {ISSUE_URL} while other work continues.\n",
        )
        chosen = hi.select_canonical_pull_request(
            policy, issue, [watched, incidental], ledger_pull_url=PR_URL
        )
        self.assertEqual(chosen, watched)

    def test_ledger_adoption_still_selects_without_body_association(self) -> None:
        policy = _policy()
        issue = hi.IssueRef(
            repository=policy.repositories[0],
            number=2,
            title="x",
            body="",
            state="open",
            labels=("ready-for-agent",),
            html_url=ISSUE_URL,
        )
        delivered = hi.PullRequestRef(
            FIXTURE_SLUG,
            10,
            PR_URL,
            "abc",
            "worker-delivery-branch",
            "main",
            WORKER,
            "closed",
            True,
            "unknown",
            False,
            body="Worker delivery without closing keyword.\n",
        )
        chosen = hi.select_canonical_pull_request(
            policy, issue, [delivered], ledger_pull_url=PR_URL
        )
        self.assertEqual(chosen, delivered)


class HeadAndMergeTests(unittest.TestCase):
    def test_changed_head_invalidates_clean(self) -> None:
        cp = hi.Checkpoint(
            version=1,
            issue_url=ISSUE_URL,
            state="READY",
            clean_head="oldsha",
            reviewed_head="oldsha",
        )
        self.assertTrue(hi.head_changed(cp, "newsha"))
        hi.record_review_result(cp, head_sha="newsha", outcome="clean", summary="ok")
        self.assertEqual(cp.clean_head, "newsha")
        self.assertEqual(cp.state, "READY")

    def test_default_merge_stops_for_approval(self) -> None:
        policy = _policy()
        issue = hi.IssueRef(
            repository=policy.repositories[0],
            number=2,
            title="x",
            body="No merge directive here.\n",
            state="open",
            labels=("ready-for-agent",),
            html_url=ISSUE_URL,
        )
        pull = hi.PullRequestRef(
            FIXTURE_SLUG,
            10,
            PR_URL,
            "abc",
            "automation/demo-repo-2",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
        )
        cp = hi.Checkpoint(
            version=1, issue_url=ISSUE_URL, state="READY", clean_head="abc"
        )
        decision, blocker = hi.evaluate_merge_gate(
            policy,
            issue,
            pull,
            cp,
            required_checks_green=True,
            mergeable=True,
        )
        self.assertEqual(decision, "stop_for_approval")
        self.assertIsNotNone(blocker)

    def test_merge_when_clean_allows_captain_merge(self) -> None:
        policy = _policy()
        issue = hi.IssueRef(
            repository=policy.repositories[0],
            number=2,
            title="x",
            body="Merge when clean: yes\n",
            state="open",
            labels=("ready-for-agent",),
            html_url=ISSUE_URL,
        )
        pull = hi.PullRequestRef(
            FIXTURE_SLUG,
            10,
            PR_URL,
            "abc",
            "automation/demo-repo-2",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
        )
        cp = hi.Checkpoint(
            version=1, issue_url=ISSUE_URL, state="READY", clean_head="abc"
        )
        decision, blocker = hi.evaluate_merge_gate(
            policy,
            issue,
            pull,
            cp,
            required_checks_green=True,
            mergeable=True,
        )
        self.assertEqual(decision, "merge_allowed")
        self.assertIsNone(blocker)

    def test_local_only_does_not_unattended_merge_on_checkless_clean(self) -> None:
        policy = _policy(worker_completion_contract="local-only")
        issue = hi.IssueRef(
            repository=policy.repositories[0],
            number=2,
            title="x",
            body="Merge when clean: yes\n",
            state="open",
            labels=("ready-for-agent",),
            html_url=ISSUE_URL,
        )
        pull = hi.PullRequestRef(
            FIXTURE_SLUG,
            10,
            PR_URL,
            "abc",
            "automation/demo-repo-2",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
        )
        cp = hi.Checkpoint(
            version=1, issue_url=ISSUE_URL, state="READY", clean_head="abc"
        )
        decision, blocker = hi.evaluate_merge_gate(
            policy,
            issue,
            pull,
            cp,
            mergeable=True,
        )
        self.assertEqual(decision, "stop_for_approval")
        self.assertIn("explicit_captain_approval_required", blocker or "")

    def test_local_only_unattended_merge_with_independently_verified_checks(self) -> None:
        policy = _policy(worker_completion_contract="local-only")
        issue = hi.IssueRef(
            repository=policy.repositories[0],
            number=2,
            title="x",
            body="Merge when clean: yes\n",
            state="open",
            labels=("ready-for-agent",),
            html_url=ISSUE_URL,
        )
        pull = hi.PullRequestRef(
            FIXTURE_SLUG,
            10,
            PR_URL,
            "abc",
            "automation/demo-repo-2",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
        )
        cp = hi.Checkpoint(
            version=1, issue_url=ISSUE_URL, state="READY", clean_head="abc"
        )
        decision, blocker = hi.evaluate_merge_gate(
            policy,
            issue,
            pull,
            cp,
            required_checks_green=True,
            mergeable=True,
        )
        self.assertEqual(decision, "merge_allowed")
        self.assertIsNone(blocker)

    def test_github_pr_infers_required_checks_from_live_clean(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        runner.reviews = [
            {
                "user": {"login": CAPTAIN},
                "state": "APPROVED",
                "commit_id": "abc123deadbeef",
                "submitted_at": "2026-01-01T00:00:00Z",
            }
        ]
        pull = hi.PullRequestRef(
            FIXTURE_SLUG,
            10,
            PR_URL,
            "abc123deadbeef",
            "automation/demo-repo-2",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
        )
        cp = hi.Checkpoint(
            version=1,
            issue_url=ISSUE_URL,
            state="READY",
            clean_head="abc123deadbeef",
        )
        issue = hi.IssueRef(
            repository=policy.repositories[0],
            number=2,
            title="x",
            body="Merge when clean: yes\n",
            state="open",
            labels=("ready-for-agent",),
            html_url=ISSUE_URL,
        )
        decision, blocker = hi.evaluate_merge_gate(
            policy,
            issue,
            pull,
            cp,
            runner=runner,
            gh="gh",
        )
        self.assertEqual(decision, "merge_allowed")
        self.assertIsNone(blocker)

    def test_local_only_live_clean_does_not_infer_required_checks(self) -> None:
        policy = _policy(worker_completion_contract="local-only")
        runner = FakeRunner()
        runner.reviews = [
            {
                "user": {"login": CAPTAIN},
                "state": "APPROVED",
                "commit_id": "abc123deadbeef",
                "submitted_at": "2026-01-01T00:00:00Z",
            }
        ]
        pull = hi.PullRequestRef(
            FIXTURE_SLUG,
            10,
            PR_URL,
            "abc123deadbeef",
            "automation/demo-repo-2",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
        )
        cp = hi.Checkpoint(
            version=1,
            issue_url=ISSUE_URL,
            state="READY",
            clean_head="abc123deadbeef",
        )
        issue = hi.IssueRef(
            repository=policy.repositories[0],
            number=2,
            title="x",
            body="Merge when clean: yes\n",
            state="open",
            labels=("ready-for-agent",),
            html_url=ISSUE_URL,
        )
        decision, blocker = hi.evaluate_merge_gate(
            policy,
            issue,
            pull,
            cp,
            runner=runner,
            gh="gh",
        )
        self.assertEqual(decision, "stop_for_approval")
        self.assertIn("explicit_captain_approval_required", blocker or "")

    def test_merge_not_ready_without_clean_current_head(self) -> None:
        policy = _policy()
        issue = hi.IssueRef(
            repository=policy.repositories[0],
            number=2,
            title="x",
            body="Merge when clean: yes\n",
            state="open",
            labels=("ready-for-agent",),
            html_url=ISSUE_URL,
        )
        pull = hi.PullRequestRef(
            FIXTURE_SLUG,
            10,
            PR_URL,
            "new",
            "automation/demo-repo-2",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
        )
        cp = hi.Checkpoint(
            version=1, issue_url=ISSUE_URL, state="READY", clean_head="old"
        )
        decision, blocker = hi.evaluate_merge_gate(
            policy,
            issue,
            pull,
            cp,
            required_checks_green=True,
            mergeable=True,
        )
        self.assertEqual(decision, "not_ready")
        self.assertIn("head_lacks_clean_captain_review", blocker or "")

    def test_merge_failure_requeries(self) -> None:
        runner = FakeRunner()
        original_run = runner.run
        merge_calls = {"n": 0}

        def flaky(command: list[str]) -> str:
            joined = " ".join(command)
            if "--method" in command and "/merge" in joined:
                merge_calls["n"] += 1
                raise hi.HelmetIssueError("merge failed: required status check")
            return original_run(command)

        runner.run = flaky  # type: ignore[method-assign]
        pull = hi.PullRequestRef(
            FIXTURE_SLUG,
            10,
            PR_URL,
            "abc123deadbeef",
            "automation/demo-repo-2",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
        )
        with self.assertRaises(hi.HelmetIssueError):
            hi.merge_pull_request(pull, runner, gh="gh")
        self.assertEqual(merge_calls["n"], 1)
        # Merge helper already re-queried once; PR remains unmerged.
        runner.run = original_run  # type: ignore[method-assign]
        verified = hi.verify_merged(pull, runner, gh="gh")
        self.assertFalse(verified.merged)

        # If the PUT fails but GitHub shows merged, treat as success (no retry PUT).
        runner.pull = dict(runner.pull)
        runner.pull["merged"] = True
        runner.pull["state"] = "closed"

        def fail_put_then_live(command: list[str]) -> str:
            joined = " ".join(command)
            if "--method" in command and "/merge" in joined:
                raise hi.HelmetIssueError("merge failed: race")
            return original_run(command)

        runner.run = fail_put_then_live  # type: ignore[method-assign]
        result = hi.merge_pull_request(pull, runner, gh="gh")
        self.assertTrue(result.merged)


class OrchestrationPassTests(unittest.TestCase):
    def test_preflight_rejects_worker_identity(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        runner.identity = WORKER
        with self.assertRaises(hi.HelmetIssueError):
            hi.preflight(policy, ISSUE_URL, runner, gh="gh")

    def test_preflight_rejects_missing_labels(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        runner.issue = dict(runner.issue)
        runner.issue["labels"] = []
        with self.assertRaises(hi.HelmetIssueError):
            hi.preflight(policy, ISSUE_URL, runner, gh="gh")

    def test_run_pass_adopts_open_pr_for_review(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            cp, report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                host_continuation="none",
                one_pass_only=True,
                apply_dispatch=False,
            )
            self.assertEqual(cp.root_task_id, "t_root")
            self.assertEqual(cp.pr_url, PR_URL)
            self.assertEqual(cp.state, "REVIEW_HEAD")
            self.assertTrue(cp.one_pass_only)
            self.assertIn(
                "op:one_pass_only:continuation_unavailable",
                cp.notes,
            )
            self.assertEqual(report.pr_url, PR_URL)
            self.assertEqual(
                report.details["continuation"],
                "one_pass_only:continuation_unavailable",
            )
            # status is read-only and does not require mutation
            status = hi.status_issue(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(status.state, "REVIEW_HEAD")
            self.assertEqual(status.root_task_id, "t_root")

    def test_review_outcome_and_head_change(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        runner.reviews = [
            {
                "user": {"login": CAPTAIN},
                "state": "APPROVED",
                "commit_id": "abc123deadbeef",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            hi.apply_review_outcome(
                ISSUE_URL,
                head_sha="abc123deadbeef",
                outcome="clean",
                summary="lgtm",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            # New head invalidates on next adopt/discover pass.
            runner.pull = dict(runner.pull)
            runner.pull["head"] = {
                "sha": "newheadsha0001",
                "ref": "automation/demo-repo-2",
            }
            runner.open_pulls = [runner.pull]
            cp, _report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            self.assertEqual(cp.state, "REVIEW_HEAD")
            self.assertIsNone(cp.clean_head)

    def test_changes_requested_waits_for_h1_not_local_repair(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        runner.reviews = [
            {
                "user": {"login": CAPTAIN},
                "state": "CHANGES_REQUESTED",
                "commit_id": "abc123deadbeef",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            cp = hi.apply_review_outcome(
                ISSUE_URL,
                head_sha="abc123deadbeef",
                outcome="changes_requested",
                summary="fix the boundary",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(cp.state, "WAIT_REPAIR")
            self.assertTrue(cp.pending_repair)
            # No kanban create for repair from helmet-issue path.
            self.assertEqual(runner.created_tasks, [])

    def test_no_dispatch_does_not_create_root(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            # Empty ledger file with tables but no root row.
            _ledger_with_root(ledger)
            conn = sqlite3.connect(ledger)
            conn.execute("DELETE FROM issue_tasks")
            conn.commit()
            conn.close()
            cp, _report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            self.assertIsNone(cp.root_task_id)
            self.assertEqual(runner.created_tasks, [])
            self.assertEqual(runner.labels_posted, [])

    def test_forged_review_sha_rejected(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        runner.reviews = [
            {
                "user": {"login": CAPTAIN},
                "state": "APPROVED",
                "commit_id": "abc123deadbeef",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            with self.assertRaises(hi.HelmetIssueError):
                hi.apply_review_outcome(
                    ISSUE_URL,
                    head_sha="bogus-sha-not-live",
                    outcome="clean",
                    summary="forged",
                    checkpoint_dir=checkpoints,
                    policy=policy,
                    ledger=ledger,
                    runner=runner,
                    gh="gh",
                    hermes="hermes",
                )

    def test_exact_issue_ref_not_prefix(self) -> None:
        self.assertTrue(hi.issue_number_mentioned("Closes #2\n", 2))
        self.assertFalse(hi.issue_number_mentioned("Closes #20\n", 2))
        self.assertEqual(hi.closing_references("Fixes #20 and closes #2"), frozenset({20, 2}))
        self.assertEqual(hi.closing_references("Closes: #2"), frozenset({2}))
        self.assertTrue(
            hi.body_closes_issue(
                f"Fixes {FIXTURE_SLUG}#2",
                repository_slug=FIXTURE_SLUG,
                number=2,
            )
        )
        self.assertFalse(
            hi.body_closes_issue(
                "Fixes other-org/other-repo#2",
                repository_slug=FIXTURE_SLUG,
                number=2,
            )
        )

    def test_checkpoint_rejects_bad_version_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = hi.checkpoint_path(Path(tmp), ISSUE_URL)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "version": 99,
                        "issue_url": ISSUE_URL,
                        "state": "READY",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(hi.HelmetIssueError):
                hi.load_checkpoint(path, expected_issue_url=ISSUE_URL)

    def test_terminal_repair_does_not_pin_wait_forever(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            conn = sqlite3.connect(ledger)
            conn.execute(
                "INSERT INTO pull_request_watches(issue_url, pull_url, last_repair_task_id) VALUES (?,?,?)",
                (ISSUE_URL, PR_URL, "t_repair_done"),
            )
            conn.commit()
            conn.close()
            runner.kanban_tasks["t_repair_done"] = {
                "task": {
                    "id": "t_repair_done",
                    "status": "done",
                    "assignee": "builder",
                    "workspace_kind": "worktree",
                    "workspace_path": "/opt/data/repos/demo-repo",
                },
                "runs": [],
            }
            cp, _report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            self.assertEqual(cp.state, "REVIEW_HEAD")
            self.assertFalse(cp.pending_repair)

    def test_pending_repair_unchanged_head_stays_wait(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            path = hi.checkpoint_path(checkpoints, ISSUE_URL)
            hi.save_checkpoint(
                path,
                hi.Checkpoint(
                    version=1,
                    issue_url=ISSUE_URL,
                    state="WAIT_REPAIR",
                    pending_repair=True,
                    root_task_id="t_root",
                    pr_url=PR_URL,
                    pr_number=10,
                    reviewed_head="abc123deadbeef",
                ),
            )
            cp, _report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            self.assertEqual(cp.state, "WAIT_REPAIR")
            self.assertTrue(cp.pending_repair)

    def test_status_surfaces_live_uncertainty_on_done_checkpoint(self) -> None:
        policy = _policy()
        runner = FakeRunner()

        def boom(command: list[str]) -> str:
            raise hi.HelmetIssueError("GitHub API failed for user")

        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            path = hi.checkpoint_path(checkpoints, ISSUE_URL)
            hi.save_checkpoint(
                path,
                hi.Checkpoint(
                    version=1,
                    issue_url=ISSUE_URL,
                    state="DONE",
                    root_task_id="t_root",
                    pr_url=PR_URL,
                    clean_head="abc123deadbeef",
                ),
            )
            runner.run = boom  # type: ignore[method-assign]
            status = hi.status_issue(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(status.state, "DONE")
            self.assertFalse(status.terminal)
            self.assertIn("live_uncertain", status.blocker or "")

    def test_budget_before_dispatch_mutation(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            path = hi.checkpoint_path(checkpoints, ISSUE_URL)
            # Exhausted repair budget before any mutation.
            hi.save_checkpoint(
                path,
                hi.Checkpoint(
                    version=1,
                    issue_url=ISSUE_URL,
                    state="PREFLIGHT",
                    repair_rounds=99,
                    started_at=hi._utc_now(),
                ),
            )
            cp, _report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=True,
            )
            self.assertEqual(cp.state, "FAILED")
            self.assertEqual(runner.created_tasks, [])
            self.assertEqual(runner.labels_posted, [])

    def test_repeated_identical_blocker_stops(self) -> None:
        cp = hi.Checkpoint(version=1, issue_url=ISSUE_URL, state="WAIT_PR")
        hi.set_blocker(cp, "still waiting")
        hi.set_blocker(cp, "still waiting")
        hi.set_blocker(cp, "still waiting")
        self.assertEqual(cp.state, "BLOCKED")
        self.assertGreaterEqual(cp.identical_blocker_count, 3)

    def test_budget_repair_rounds(self) -> None:
        policy = _policy()
        cp = hi.Checkpoint(
            version=1,
            issue_url=ISSUE_URL,
            state="WAIT_REPAIR",
            repair_rounds=11,
            started_at=hi._utc_now(),
        )
        self.assertIsNotNone(hi.budget_exhausted(cp, policy))


class InstallSkillsTests(unittest.TestCase):
    def test_static_validate_and_install_all_targets(self) -> None:
        messages = skills.static_validate_all_targets()
        self.assertTrue(any("helmet-issue" in item for item in messages))
        self.assertTrue(any("helmet-epic" in item for item in messages))
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp)
            installed = skills.install_skills(
                targets=["codex", "claude", "hermes"],
                prefix=prefix,
            )
            # Three bundled skills and all reference assets reach every target.
            self.assertEqual(len(installed), 9)
            for skill_name in ("helmet-issue", "helmet-epic", "setup-helmet"):
                source = ROOT / "skills" / skill_name
                bundled = skills.default_skills_source() / skill_name
                expected = {
                    path.relative_to(source): path.read_bytes()
                    for path in source.rglob("*") if path.is_file()
                }
                self.assertEqual(expected, {
                    path.relative_to(bundled): path.read_bytes()
                    for path in bundled.rglob("*") if path.is_file()
                }, msg=f"{skill_name}: source/package asset drift")
                for target, rel in (
                    ("codex", Path(f".codex/skills/{skill_name}/SKILL.md")),
                    ("claude", Path(f".claude/skills/{skill_name}/SKILL.md")),
                    ("hermes", Path(f".hermes/skills/hermes-helmet/{skill_name}/SKILL.md")),
                ):
                    path = prefix / rel
                    self.assertTrue(path.is_file(), msg=f"{target}/{skill_name}: {path}")
                    text = path.read_text(encoding="utf-8")
                    self.assertTrue(text.startswith("---"))
                    self.assertIn(f"name: {skill_name}", text)
                    self.assertIn("hermes-helmet", text)
                    self.assertEqual(expected, {
                        asset.relative_to(path.parent): asset.read_bytes()
                        for asset in path.parent.rglob("*") if asset.is_file()
                    }, msg=f"{target}/{skill_name}: installed assets differ")


class CliSmokeTests(unittest.TestCase):
    def test_wait_forwards_cursor_and_returns_changed(self) -> None:
        runner = FakeRunner()
        seen: list[list[str]] = []
        before = "v1." + "a" * 64
        after = "v1." + "b" * 64

        def wait_run(command: list[str]) -> str:
            seen.append(command)
            return json.dumps(
                {
                    "outcome": "changed",
                    "reason": "task_terminal",
                    "cursor": after,
                }
            )

        runner.run = wait_run  # type: ignore[method-assign]
        report = hi.wait_for_issue_change(
            ISSUE_URL,
            runner,
            worker_runtime=("worker-rt",),
            timeout_seconds=45,
            cursor=before,
        )
        self.assertEqual(report.outcome, "changed")
        self.assertEqual(report.reason, "task_terminal")
        self.assertEqual(
            seen,
            [[
                "worker-rt", "wait", ISSUE_URL, "--timeout-seconds", "45",
                "--json", "--cursor", before,
            ]],
        )

    def test_wait_timeout_is_resumable_and_cli_returns_two(self) -> None:
        runner = FakeRunner()
        same = "v1." + "c" * 64
        runner.run = lambda command: json.dumps(  # type: ignore[method-assign]
            {"outcome": "timeout", "reason": "timeout", "cursor": same}
        )
        report = hi.wait_for_issue_change(
            ISSUE_URL, runner, worker_runtime=("worker-rt",), timeout_seconds=10
        )
        self.assertEqual(report.cursor, same)
        with mock.patch.object(helmet_cli, "SubprocessRunner", return_value=runner):
            args = helmet_cli.build_parser().parse_args(
                ["wait", ISSUE_URL, "--worker-runtime", "worker-rt", "--timeout-seconds", "10"]
            )
            self.assertEqual(args.func(args), 2)

    def test_wait_rejects_invalid_runtime_output(self) -> None:
        runner = FakeRunner()
        bad_payloads = (
            "not-json",
            json.dumps({"outcome": "changed", "reason": "made_up", "cursor": "v1." + "d" * 64}),
            json.dumps({"outcome": "timeout", "reason": "task_terminal", "cursor": "v1." + "d" * 64}),
            json.dumps({"outcome": "changed", "reason": "task_terminal", "cursor": "secret value"}),
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                runner.run = lambda command, value=payload: value  # type: ignore[method-assign]
                with self.assertRaises(hi.HelmetIssueError):
                    hi.wait_for_issue_change(
                        ISSUE_URL, runner, worker_runtime=("worker-rt",), timeout_seconds=10
                    )

    def test_wait_rejects_missing_runtime_without_running_commands(self) -> None:
        runner = FakeRunner()
        with mock.patch.object(hi, "DEFAULT_WORKER_RUNTIME", ()):
            with self.assertRaises(hi.HelmetIssueError):
                hi.wait_for_issue_change(ISSUE_URL, runner, timeout_seconds=10)

    def test_status_cli_json(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            config = Path(tmp) / "policy.json"
            config.write_text(json.dumps(_policy_dict()), encoding="utf-8")
            _ledger_with_root(ledger)
            hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            with mock.patch.object(hi, "SubprocessRunner", return_value=runner):
                # Direct function path already covered; ensure parser accepts.
                parser = helmet_cli.build_parser()
                args = parser.parse_args(
                    [
                        "status",
                        ISSUE_URL,
                        "--config",
                        str(config),
                        "--ledger",
                        str(ledger),
                        "--checkpoint-dir",
                        str(checkpoints),
                        "--json",
                    ]
                )
                self.assertEqual(args.command, "status")


class SourceGuardTests(unittest.TestCase):
    def test_no_merge_force_push_in_helmet_issue_helpers_except_captain_merge_api(self) -> None:
        source = (ROOT / "src/hermes_helmet/helmet_issue.py").read_text(encoding="utf-8")
        for needle in ("git push --force", "git push -f", "git push --force-with-lease"):
            self.assertNotIn(needle, source)
        # Captain merge uses the pulls merge API only, never gh pr merge helper text
        # in the poller sense — allow the API path string.
        self.assertIn("/merge", source)

    def test_skill_forbids_second_relay(self) -> None:
        text = (ROOT / "skills/helmet-issue/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("second watcher", text.casefold())
        self.assertIn("H1 poller", text)


class ResidualReviewRegressionTests(unittest.TestCase):
    """Captain residual findings against ebbe5c1 (PR #19 review 5134469606)."""

    def test_discover_before_dispatch_mutation(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            # Corrupt ledger: file exists but wrong schema → fail before labels.
            ledger.write_text("not-a-sqlite-database", encoding="utf-8")
            cp, _report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=True,
            )
            self.assertEqual(cp.state, "FAILED")
            self.assertEqual(runner.labels_posted, [])
            self.assertEqual(runner.created_tasks, [])

    def test_open_pr_discovered_before_create_on_empty_ledger(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "missing-ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            # No local ledger; open worker PR must still be discovered without
            # requiring a prior root. apply_dispatch=False keeps mutation-free.
            cp, report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            self.assertEqual(cp.pr_url, PR_URL)
            self.assertEqual(cp.state, "REVIEW_HEAD")
            self.assertIsNone(cp.root_task_id)
            self.assertEqual(report.pr_url, PR_URL)
            self.assertEqual(runner.labels_posted, [])
            self.assertEqual(runner.created_tasks, [])

    def test_effective_review_supersedes_historical_approval(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        runner.reviews = [
            {
                "user": {"login": CAPTAIN},
                "state": "APPROVED",
                "commit_id": "abc123deadbeef",
                "submitted_at": "2026-01-01T00:00:00Z",
            },
            {
                "user": {"login": CAPTAIN},
                "state": "CHANGES_REQUESTED",
                "commit_id": "abc123deadbeef",
                "submitted_at": "2026-01-02T00:00:00Z",
            },
        ]
        pull = hi.PullRequestRef(
            FIXTURE_SLUG,
            10,
            PR_URL,
            "abc123deadbeef",
            "automation/demo-repo-2",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
        )
        effective = hi.effective_captain_review_state(
            policy=policy,
            pull=pull,
            head_sha="abc123deadbeef",
            runner=runner,
            gh="gh",
        )
        self.assertEqual(effective, "CHANGES_REQUESTED")
        cp = hi.Checkpoint(
            version=1,
            issue_url=ISSUE_URL,
            state="READY",
            clean_head="abc123deadbeef",
        )
        issue = hi.IssueRef(
            repository=policy.repositories[0],
            number=2,
            title="x",
            body="Merge when clean: yes\n",
            state="open",
            labels=("ready-for-agent",),
            html_url=ISSUE_URL,
        )
        decision, blocker = hi.evaluate_merge_gate(
            policy,
            issue,
            pull,
            cp,
            required_checks_green=True,
            mergeable=True,
            runner=runner,
            gh="gh",
        )
        self.assertEqual(decision, "not_ready")
        self.assertIn("merge_gate_review_not_approved", blocker or "")

    def test_live_dirty_overrides_caller_true_flags(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        runner.pull = dict(runner.pull)
        runner.pull["mergeable_state"] = "dirty"
        runner.reviews = [
            {
                "user": {"login": CAPTAIN},
                "state": "APPROVED",
                "commit_id": "abc123deadbeef",
                "submitted_at": "2026-01-01T00:00:00Z",
            }
        ]
        pull = hi.PullRequestRef(
            FIXTURE_SLUG,
            10,
            PR_URL,
            "abc123deadbeef",
            "automation/demo-repo-2",
            "main",
            WORKER,
            "open",
            False,
            "dirty",
            False,
        )
        cp = hi.Checkpoint(
            version=1,
            issue_url=ISSUE_URL,
            state="READY",
            clean_head="abc123deadbeef",
        )
        issue = hi.IssueRef(
            repository=policy.repositories[0],
            number=2,
            title="x",
            body="Merge when clean: yes\n",
            state="open",
            labels=("ready-for-agent",),
            html_url=ISSUE_URL,
        )
        decision, blocker = hi.evaluate_merge_gate(
            policy,
            issue,
            pull,
            cp,
            required_checks_green=True,
            mergeable=True,
            runner=runner,
            gh="gh",
        )
        self.assertEqual(decision, "not_ready")
        self.assertIn("pr_not_mergeable", blocker or "")

    def test_full_issue_url_not_prefix_of_longer_number(self) -> None:
        issue20 = "https://github.com/example-org/demo-repo/issues/20"
        body = f"Closes {issue20}\n"
        self.assertFalse(hi.issue_url_mentioned(body, ISSUE_URL))
        self.assertTrue(hi.issue_url_mentioned(body, issue20))
        self.assertTrue(hi.issue_url_mentioned(f"See {ISSUE_URL} please", ISSUE_URL))
        # Substring trap from the review: bare `in` would false-positive.
        self.assertFalse(ISSUE_URL in issue20 and hi.issue_url_mentioned(issue20, ISSUE_URL))

    def test_pending_repair_new_head_forces_review(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            path = hi.checkpoint_path(checkpoints, ISSUE_URL)
            hi.save_checkpoint(
                path,
                hi.Checkpoint(
                    version=1,
                    issue_url=ISSUE_URL,
                    state="WAIT_REPAIR",
                    pending_repair=True,
                    root_task_id="t_root",
                    pr_url=PR_URL,
                    pr_number=10,
                    reviewed_head="abc123deadbeef",
                    clean_head=None,
                ),
            )
            runner.pull = dict(runner.pull)
            runner.pull["head"] = {
                "sha": "brandnewhead001",
                "ref": "automation/demo-repo-2",
            }
            runner.open_pulls = [runner.pull]
            cp, _report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            self.assertEqual(cp.state, "REVIEW_HEAD")
            self.assertFalse(cp.pending_repair)

    def test_repair_round_idempotent_on_same_head(self) -> None:
        cp = hi.Checkpoint(version=1, issue_url=ISSUE_URL, state="REVIEW_HEAD")
        hi.record_review_result(
            cp, head_sha="abc123deadbeef", outcome="changes_requested", summary="one"
        )
        self.assertEqual(cp.repair_rounds, 1)
        for _ in range(10):
            hi.record_review_result(
                cp,
                head_sha="abc123deadbeef",
                outcome="changes_requested",
                summary="repeat",
            )
        self.assertEqual(cp.repair_rounds, 1)
        hi.record_review_result(
            cp, head_sha="newheadsha0002", outcome="changes_requested", summary="next"
        )
        self.assertEqual(cp.repair_rounds, 2)

    def test_same_outcome_idempotent_after_pending_cleared(self) -> None:
        """Re-record changes_requested after pending was cleared must not burn rounds."""
        cp = hi.Checkpoint(version=1, issue_url=ISSUE_URL, state="REVIEW_HEAD")
        hi.record_review_result(
            cp, head_sha="abc123deadbeef", outcome="changes_requested", summary="one"
        )
        self.assertEqual(cp.repair_rounds, 1)
        self.assertTrue(cp.pending_repair)
        # Intermediate adoption may clear the transient pending flag while the
        # formal same-head outcome remains authoritative.
        cp.pending_repair = False
        cp.state = "REVIEW_HEAD"
        hi.record_review_result(
            cp,
            head_sha="abc123deadbeef",
            outcome="changes_requested",
            summary="repeat after adoption",
        )
        self.assertEqual(cp.repair_rounds, 1)
        self.assertTrue(cp.pending_repair)
        self.assertEqual(cp.state, "REQUEST_REPAIR")

    def test_previous_done_repair_does_not_satisfy_new_changes_requested(self) -> None:
        """Issue #27: stale Done repair must not clear a fresh same-head review cycle."""
        policy = _policy(budgets={"max_issue_runtime_minutes": 120, "max_repair_rounds": 3})
        runner = FakeRunner()
        runner.reviews = [
            {
                "user": {"login": CAPTAIN},
                "state": "CHANGES_REQUESTED",
                "commit_id": "abc123deadbeef",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            conn = sqlite3.connect(ledger)
            conn.execute(
                "INSERT INTO pull_request_watches(issue_url, pull_url, last_repair_task_id) VALUES (?,?,?)",
                (ISSUE_URL, PR_URL, "t_old_repair_done"),
            )
            conn.commit()
            conn.close()
            runner.kanban_tasks["t_old_repair_done"] = {
                "task": {
                    "id": "t_old_repair_done",
                    "status": "done",
                    "assignee": "builder",
                    "workspace_kind": "worktree",
                    "workspace_path": "/opt/data/repos/demo-repo",
                },
                "runs": [],
            }
            # First adoption establishes PR/watch context without a pending review.
            hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            rounds_before = 0
            for cycle in range(1, 5):
                cp = hi.apply_review_outcome(
                    ISSUE_URL,
                    head_sha="abc123deadbeef",
                    outcome="changes_requested",
                    summary=f"cycle-{cycle}",
                    checkpoint_dir=checkpoints,
                    policy=policy,
                    ledger=ledger,
                    runner=runner,
                    gh="gh",
                    hermes="hermes",
                )
                self.assertTrue(cp.pending_repair, msg=f"cycle {cycle} after review")
                self.assertEqual(cp.repair_rounds, 1, msg=f"cycle {cycle} rounds")
                rounds_before = cp.repair_rounds
                cp, report = hi.run_preflight_and_adopt(
                    policy,
                    ISSUE_URL,
                    ledger=ledger,
                    checkpoint_dir=checkpoints,
                    runner=runner,
                    gh="gh",
                    hermes="hermes",
                    apply_dispatch=False,
                )
                # Stale Done repair is not delivery for this unchanged-head cycle.
                self.assertTrue(cp.pending_repair, msg=f"cycle {cycle} after adopt")
                self.assertIn(cp.state, {"WAIT_REPAIR", "REQUEST_REPAIR"})
                self.assertEqual(cp.repair_rounds, rounds_before)
                self.assertIsNone(cp.clean_head)
                self.assertNotEqual(cp.state, "READY")
                self.assertNotEqual(cp.state, "DONE")
                self.assertIn(
                    report.merge_gate.split(":")[-1]
                    if ":" in (report.merge_gate or "")
                    else report.merge_gate,
                    {"explicit_captain_approval", "not_ready", "awaiting"},
                )
            # Budget must not false-exhaust from re-recording the same formal outcome.
            self.assertIsNone(hi.budget_exhausted(cp, policy))
            self.assertEqual(cp.repair_rounds, 1)

    def test_no_prior_repair_changes_requested_stays_waiting(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        runner.reviews = [
            {
                "user": {"login": CAPTAIN},
                "state": "CHANGES_REQUESTED",
                "commit_id": "abc123deadbeef",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            hi.apply_review_outcome(
                ISSUE_URL,
                head_sha="abc123deadbeef",
                outcome="changes_requested",
                summary="need repair",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            cp, _report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            self.assertTrue(cp.pending_repair)
            self.assertEqual(cp.state, "WAIT_REPAIR")

    def test_current_cycle_terminal_repair_with_new_head_returns_for_review(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            conn = sqlite3.connect(ledger)
            conn.execute(
                "INSERT INTO pull_request_watches(issue_url, pull_url, last_repair_task_id) VALUES (?,?,?)",
                (ISSUE_URL, PR_URL, "t_current_repair_done"),
            )
            conn.commit()
            conn.close()
            runner.kanban_tasks["t_current_repair_done"] = {
                "task": {
                    "id": "t_current_repair_done",
                    "status": "done",
                    "assignee": "builder",
                    "workspace_kind": "worktree",
                    "workspace_path": "/opt/data/repos/demo-repo",
                },
                "runs": [],
            }
            path = hi.checkpoint_path(checkpoints, ISSUE_URL)
            hi.save_checkpoint(
                path,
                hi.Checkpoint(
                    version=1,
                    issue_url=ISSUE_URL,
                    state="WAIT_REPAIR",
                    pending_repair=True,
                    root_task_id="t_root",
                    pr_url=PR_URL,
                    pr_number=10,
                    reviewed_head="abc123deadbeef",
                    repair_rounds=1,
                ),
            )
            runner.pull = dict(runner.pull)
            runner.pull["head"] = {
                "sha": "repairedhead0001",
                "ref": "automation/demo-repo-2",
            }
            runner.open_pulls = [runner.pull]
            cp, _report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            self.assertEqual(cp.state, "REVIEW_HEAD")
            self.assertFalse(cp.pending_repair)
            self.assertIsNone(cp.clean_head)
            self.assertEqual(cp.repair_rounds, 1)

    def test_current_cycle_terminal_repair_clean_head_stays_ready(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            conn = sqlite3.connect(ledger)
            conn.execute(
                "INSERT INTO pull_request_watches(issue_url, pull_url, last_repair_task_id) VALUES (?,?,?)",
                (ISSUE_URL, PR_URL, "t_clean_repair_done"),
            )
            conn.commit()
            conn.close()
            runner.kanban_tasks["t_clean_repair_done"] = {
                "task": {
                    "id": "t_clean_repair_done",
                    "status": "done",
                    "assignee": "builder",
                    "workspace_kind": "worktree",
                    "workspace_path": "/opt/data/repos/demo-repo",
                },
                "runs": [],
            }
            path = hi.checkpoint_path(checkpoints, ISSUE_URL)
            hi.save_checkpoint(
                path,
                hi.Checkpoint(
                    version=1,
                    issue_url=ISSUE_URL,
                    state="READY",
                    pending_repair=False,
                    root_task_id="t_root",
                    pr_url=PR_URL,
                    pr_number=10,
                    reviewed_head="abc123deadbeef",
                    clean_head="abc123deadbeef",
                    repair_rounds=2,
                ),
            )
            cp, report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            self.assertEqual(cp.state, "READY")
            self.assertFalse(cp.pending_repair)
            self.assertEqual(cp.clean_head, "abc123deadbeef")
            self.assertEqual(cp.repair_rounds, 2)
            self.assertFalse(report.details.get("head_needs_review"))

    def test_distinct_same_head_review_events_consume_new_rounds(self) -> None:
        """Issue #27 follow-up: distinct formal CR events on one SHA each count."""
        policy = _policy(budgets={"max_issue_runtime_minutes": 120, "max_repair_rounds": 3})
        runner = FakeRunner()
        head = "abc123deadbeef"
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            # First formal CR event.
            runner.reviews = [
                {
                    "id": 200,
                    "user": {"login": CAPTAIN},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T01:00:00Z",
                }
            ]
            cp = hi.apply_review_outcome(
                ISSUE_URL,
                head_sha=head,
                outcome="changes_requested",
                summary="event-200",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(cp.repair_rounds, 1)
            # Replay the same verified event — no extra round.
            cp = hi.apply_review_outcome(
                ISSUE_URL,
                head_sha=head,
                outcome="changes_requested",
                summary="event-200-replay",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(cp.repair_rounds, 1)
            # Intermediate clean on same head.
            runner.reviews.append(
                {
                    "id": 201,
                    "user": {"login": CAPTAIN},
                    "state": "APPROVED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T02:00:00Z",
                }
            )
            cp = hi.apply_review_outcome(
                ISSUE_URL,
                head_sha=head,
                outcome="clean",
                summary="event-201",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(cp.state, "READY")
            self.assertFalse(cp.pending_repair)
            self.assertEqual(cp.repair_rounds, 1)
            # Later distinct CR on unchanged SHA must consume a new round.
            runner.reviews.append(
                {
                    "id": 202,
                    "user": {"login": CAPTAIN},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T03:00:00Z",
                }
            )
            cp = hi.apply_review_outcome(
                ISSUE_URL,
                head_sha=head,
                outcome="changes_requested",
                summary="event-202",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(cp.repair_rounds, 2)
            self.assertTrue(cp.pending_repair)
            # Another distinct CR advances again (budget stop path).
            runner.reviews.append(
                {
                    "id": 203,
                    "user": {"login": CAPTAIN},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T04:00:00Z",
                }
            )
            cp = hi.apply_review_outcome(
                ISSUE_URL,
                head_sha=head,
                outcome="changes_requested",
                summary="event-203",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(cp.repair_rounds, 3)
            runner.reviews.append(
                {
                    "id": 204,
                    "user": {"login": CAPTAIN},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T05:00:00Z",
                }
            )
            cp = hi.apply_review_outcome(
                ISSUE_URL,
                head_sha=head,
                outcome="changes_requested",
                summary="event-204",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(cp.repair_rounds, 4)
            self.assertIsNotNone(hi.budget_exhausted(cp, policy))

    def test_record_review_result_distinct_event_ids_on_same_head(self) -> None:
        cp = hi.Checkpoint(version=1, issue_url=ISSUE_URL, state="REVIEW_HEAD")
        hi.record_review_result(
            cp,
            head_sha="abc123deadbeef",
            outcome="changes_requested",
            review_event_id=200,
        )
        self.assertEqual(cp.repair_rounds, 1)
        hi.record_review_result(
            cp,
            head_sha="abc123deadbeef",
            outcome="changes_requested",
            review_event_id=200,
        )
        self.assertEqual(cp.repair_rounds, 1)
        hi.record_review_result(
            cp,
            head_sha="abc123deadbeef",
            outcome="clean",
            review_event_id=201,
        )
        self.assertFalse(cp.pending_repair)
        hi.record_review_result(
            cp,
            head_sha="abc123deadbeef",
            outcome="changes_requested",
            review_event_id=202,
        )
        self.assertEqual(cp.repair_rounds, 2)
        self.assertTrue(cp.pending_repair)

    def test_legacy_head_only_note_idempotent_for_correlated_event(self) -> None:
        """Upgrade: replaying the already-counted formal event must not burn rounds."""
        head = "abc123deadbeef"
        cp = hi.Checkpoint(
            version=1,
            issue_url=ISSUE_URL,
            state="WAIT_REPAIR",
            pending_repair=True,
            reviewed_head=head,
            repair_rounds=3,
            notes=[hi.checkpoint_note("review", "changes_requested", head[:12])],
        )
        hi.record_review_result(
            cp,
            head_sha=head,
            outcome="changes_requested",
            review_event_id=200,
            prior_cycle_event_id=200,
        )
        self.assertEqual(cp.repair_rounds, 3)
        self.assertTrue(cp.pending_repair)
        scoped = hi.checkpoint_note("review", "changes_requested", head[:12], 200)
        self.assertIn(scoped, cp.notes)
        # Replay after migration still idempotent.
        hi.record_review_result(
            cp,
            head_sha=head,
            outcome="changes_requested",
            review_event_id=200,
            prior_cycle_event_id=200,
        )
        self.assertEqual(cp.repair_rounds, 3)

    def test_legacy_exemption_not_reused_after_event_scoped_migration(self) -> None:
        """After migrating event200, a later event201 with watch naming 201 must count."""
        head = "abc123deadbeef"
        cp = hi.Checkpoint(
            version=1,
            issue_url=ISSUE_URL,
            state="WAIT_REPAIR",
            pending_repair=True,
            reviewed_head=head,
            repair_rounds=3,
            notes=[hi.checkpoint_note("review", "changes_requested", head[:12])],
        )
        hi.record_review_result(
            cp,
            head_sha=head,
            outcome="changes_requested",
            review_event_id=200,
            prior_cycle_event_id=200,
        )
        self.assertEqual(cp.repair_rounds, 3)
        # Poller updates watch/body to the new formal event before Captain records.
        hi.record_review_result(
            cp,
            head_sha=head,
            outcome="changes_requested",
            review_event_id=201,
            prior_cycle_event_id=201,
        )
        self.assertEqual(cp.repair_rounds, 4)
        self.assertTrue(cp.pending_repair)
        scoped201 = hi.checkpoint_note("review", "changes_requested", head[:12], 201)
        self.assertIn(scoped201, cp.notes)
        # Replay of 200 remains idempotent; replaying 201 does not double-count.
        hi.record_review_result(
            cp,
            head_sha=head,
            outcome="changes_requested",
            review_event_id=200,
            prior_cycle_event_id=200,
        )
        self.assertEqual(cp.repair_rounds, 4)
        hi.record_review_result(
            cp,
            head_sha=head,
            outcome="changes_requested",
            review_event_id=201,
            prior_cycle_event_id=201,
        )
        self.assertEqual(cp.repair_rounds, 4)

    def test_legacy_exemption_rejects_review_after_checkpoint_updated_at(self) -> None:
        """Chronology: review submitted after legacy checkpoint save must count a new round."""
        head = "abc123deadbeef"
        checkpoint_saved_at = "2026-09-08T01:30:00+00:00"
        cp = hi.Checkpoint(
            version=1,
            issue_url=ISSUE_URL,
            state="WAIT_REPAIR",
            pending_repair=True,
            reviewed_head=head,
            repair_rounds=3,
            updated_at=checkpoint_saved_at,
            notes=[hi.checkpoint_note("review", "changes_requested", head[:12])],
        )
        # Already-counted event200 (before save) still migrates without burning a round.
        hi.record_review_result(
            cp,
            head_sha=head,
            outcome="changes_requested",
            review_event_id=200,
            prior_cycle_event_id=200,
            review_submitted_at="2026-09-08T01:00:00Z",
        )
        self.assertEqual(cp.repair_rounds, 3)
        # Fresh checkpoint state for the first-upgrade path where watch already names 201.
        cp201 = hi.Checkpoint(
            version=1,
            issue_url=ISSUE_URL,
            state="WAIT_REPAIR",
            pending_repair=True,
            reviewed_head=head,
            repair_rounds=3,
            updated_at=checkpoint_saved_at,
            notes=[hi.checkpoint_note("review", "changes_requested", head[:12])],
        )
        hi.record_review_result(
            cp201,
            head_sha=head,
            outcome="changes_requested",
            review_event_id=201,
            prior_cycle_event_id=201,
            review_submitted_at="2026-09-08T01:31:00Z",
        )
        self.assertEqual(cp201.repair_rounds, 4)
        self.assertTrue(cp201.pending_repair)
        scoped201 = hi.checkpoint_note("review", "changes_requested", head[:12], 201)
        self.assertIn(scoped201, cp201.notes)

    def test_legacy_head_only_note_idempotent_after_pending_cleared(self) -> None:
        """Legacy checkpoint with consumed pending still migrates the same event."""
        head = "abc123deadbeef"
        cp = hi.Checkpoint(
            version=1,
            issue_url=ISSUE_URL,
            state="REVIEW_HEAD",
            pending_repair=False,
            reviewed_head=head,
            repair_rounds=3,
            notes=[hi.checkpoint_note("review", "changes_requested", head[:12])],
        )
        hi.record_review_result(
            cp,
            head_sha=head,
            outcome="changes_requested",
            review_event_id=200,
            prior_cycle_event_id=200,
        )
        self.assertEqual(cp.repair_rounds, 3)
        self.assertTrue(cp.pending_repair)

    def test_legacy_head_only_note_does_not_cover_distinct_later_event(self) -> None:
        """Distinct later CR on same head still consumes a round after legacy marker."""
        head = "abc123deadbeef"
        cp = hi.Checkpoint(
            version=1,
            issue_url=ISSUE_URL,
            state="WAIT_REPAIR",
            pending_repair=True,
            reviewed_head=head,
            repair_rounds=3,
            notes=[hi.checkpoint_note("review", "changes_requested", head[:12])],
        )
        hi.record_review_result(
            cp,
            head_sha=head,
            outcome="changes_requested",
            review_event_id=201,
            prior_cycle_event_id=200,
        )
        self.assertEqual(cp.repair_rounds, 4)
        self.assertTrue(cp.pending_repair)

    def test_legacy_checkpoint_rerecord_same_event_preserves_budget(self) -> None:
        """Issue #27 upgrade: legacy CR note + event200 watch must not exhaust limit3."""
        policy = _policy(budgets={"max_issue_runtime_minutes": 120, "max_repair_rounds": 3})
        runner = FakeRunner()
        head = "abc123deadbeef"
        event_id = 200
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            conn = sqlite3.connect(ledger)
            conn.execute(
                """
                INSERT INTO pull_request_watches(
                    issue_url, pull_url, discovery_complete,
                    last_event_at, last_event_kind, last_event_id, last_repair_task_id
                ) VALUES (?,?,1,?,?,?,?)
                """,
                (
                    ISSUE_URL,
                    PR_URL,
                    "2026-09-08T01:00:00Z",
                    "reviewed",
                    event_id,
                    "t_legacy_budget",
                ),
            )
            conn.commit()
            conn.close()
            runner.reviews = [
                {
                    "id": event_id,
                    "user": {"login": CAPTAIN},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T01:00:00Z",
                }
            ]
            runner.kanban_tasks["t_legacy_budget"] = {
                "task": {
                    "id": "t_legacy_budget",
                    "status": "ready",
                    "assignee": "builder",
                    "workspace_kind": "worktree",
                    "workspace_path": "/opt/data/repos/demo-repo",
                    "body": (
                        "## GitHub review event\n\n"
                        f"- Pull request: {PR_URL}\n"
                        f"- Event: reviewed (changes_requested) by @{CAPTAIN}\n"
                        f"- Authoritative event: {PR_URL}#pullrequestreview-{event_id}\n"
                    ),
                },
                "runs": [],
            }
            path = hi.checkpoint_path(checkpoints, ISSUE_URL)
            hi.save_checkpoint(
                path,
                hi.Checkpoint(
                    version=1,
                    issue_url=ISSUE_URL,
                    state="WAIT_REPAIR",
                    pending_repair=True,
                    root_task_id="t_root",
                    pr_url=PR_URL,
                    pr_number=10,
                    reviewed_head=head,
                    repair_rounds=3,
                    notes=[hi.checkpoint_note("review", "changes_requested", head[:12])],
                ),
            )
            cp = hi.apply_review_outcome(
                ISSUE_URL,
                head_sha=head,
                outcome="changes_requested",
                summary="legacy-replay-200",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(cp.repair_rounds, 3)
            self.assertTrue(cp.pending_repair)
            self.assertIsNone(hi.budget_exhausted(cp, policy))
            # No-dispatch adopt must not stop on false budget exhaustion.
            cp, report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            self.assertNotEqual(cp.state, "BLOCKED")
            self.assertIsNone(hi.budget_exhausted(cp, policy))
            self.assertEqual(cp.repair_rounds, 3)
            # Distinct later event still consumes a round and can exhaust.
            # Control: watch still names 200 while recording 201.
            runner.reviews.append(
                {
                    "id": 201,
                    "user": {"login": CAPTAIN},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T02:00:00Z",
                }
            )
            cp = hi.apply_review_outcome(
                ISSUE_URL,
                head_sha=head,
                outcome="changes_requested",
                summary="event-201",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(cp.repair_rounds, 4)
            self.assertIsNotNone(hi.budget_exhausted(cp, policy))

    def test_legacy_migration_then_watch_updated_event_counts_round(self) -> None:
        """After migrating event200, poller-updated watch naming 201 must count round4."""
        policy = _policy(budgets={"max_issue_runtime_minutes": 120, "max_repair_rounds": 3})
        runner = FakeRunner()
        head = "abc123deadbeef"
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            conn = sqlite3.connect(ledger)
            conn.execute(
                """
                INSERT INTO pull_request_watches(
                    issue_url, pull_url, discovery_complete,
                    last_event_at, last_event_kind, last_event_id, last_repair_task_id
                ) VALUES (?,?,1,?,?,?,?)
                """,
                (
                    ISSUE_URL,
                    PR_URL,
                    "2026-09-08T01:00:00Z",
                    "reviewed",
                    200,
                    "t_legacy_migrate_then_201",
                ),
            )
            conn.commit()
            conn.close()
            runner.reviews = [
                {
                    "id": 200,
                    "user": {"login": CAPTAIN},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T01:00:00Z",
                }
            ]
            runner.kanban_tasks["t_legacy_migrate_then_201"] = {
                "task": {
                    "id": "t_legacy_migrate_then_201",
                    "status": "ready",
                    "assignee": "builder",
                    "workspace_kind": "worktree",
                    "workspace_path": "/opt/data/repos/demo-repo",
                    "body": (
                        "## GitHub review event\n\n"
                        f"- Pull request: {PR_URL}\n"
                        f"- Event: reviewed (changes_requested) by @{CAPTAIN}\n"
                        f"- Authoritative event: {PR_URL}#pullrequestreview-200\n"
                    ),
                },
                "runs": [],
            }
            path = hi.checkpoint_path(checkpoints, ISSUE_URL)
            hi.save_checkpoint(
                path,
                hi.Checkpoint(
                    version=1,
                    issue_url=ISSUE_URL,
                    state="WAIT_REPAIR",
                    pending_repair=True,
                    root_task_id="t_root",
                    pr_url=PR_URL,
                    pr_number=10,
                    reviewed_head=head,
                    repair_rounds=3,
                    notes=[hi.checkpoint_note("review", "changes_requested", head[:12])],
                ),
            )
            # First: migrate already-counted event200 (round stays 3).
            cp = hi.apply_review_outcome(
                ISSUE_URL,
                head_sha=head,
                outcome="changes_requested",
                summary="legacy-replay-200",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(cp.repair_rounds, 3)
            self.assertIsNone(hi.budget_exhausted(cp, policy))
            scoped200 = hi.checkpoint_note("review", "changes_requested", head[:12], 200)
            self.assertIn(scoped200, cp.notes)
            # Poller _save_review_progress updates watch + generated repair body to 201.
            runner.reviews.append(
                {
                    "id": 201,
                    "user": {"login": CAPTAIN},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T02:00:00Z",
                }
            )
            conn = sqlite3.connect(ledger)
            conn.execute(
                """
                UPDATE pull_request_watches
                SET last_event_at=?, last_event_kind=?, last_event_id=?, last_repair_task_id=?
                WHERE issue_url=?
                """,
                (
                    "2026-09-08T02:00:00Z",
                    "reviewed",
                    201,
                    "t_legacy_migrate_then_201b",
                    ISSUE_URL,
                ),
            )
            conn.commit()
            conn.close()
            runner.kanban_tasks["t_legacy_migrate_then_201b"] = {
                "task": {
                    "id": "t_legacy_migrate_then_201b",
                    "status": "ready",
                    "assignee": "builder",
                    "workspace_kind": "worktree",
                    "workspace_path": "/opt/data/repos/demo-repo",
                    "body": (
                        "## GitHub review event\n\n"
                        f"- Pull request: {PR_URL}\n"
                        f"- Event: reviewed (changes_requested) by @{CAPTAIN}\n"
                        f"- Authoritative event: {PR_URL}#pullrequestreview-201\n"
                    ),
                },
                "runs": [],
            }
            cp = hi.apply_review_outcome(
                ISSUE_URL,
                head_sha=head,
                outcome="changes_requested",
                summary="event-201-after-migration",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(cp.repair_rounds, 4)
            self.assertTrue(cp.pending_repair)
            self.assertIsNotNone(hi.budget_exhausted(cp, policy))
            scoped201 = hi.checkpoint_note("review", "changes_requested", head[:12], 201)
            self.assertIn(scoped201, cp.notes)
            # Replay of 200 stays at 4; missing count is not recoverable by re-record.
            conn = sqlite3.connect(ledger)
            conn.execute(
                """
                UPDATE pull_request_watches
                SET last_event_at=?, last_event_kind=?, last_event_id=?, last_repair_task_id=?
                WHERE issue_url=?
                """,
                (
                    "2026-09-08T01:00:00Z",
                    "reviewed",
                    200,
                    "t_legacy_migrate_then_201",
                    ISSUE_URL,
                ),
            )
            conn.commit()
            conn.close()
            cp = hi.apply_review_outcome(
                ISSUE_URL,
                head_sha=head,
                outcome="changes_requested",
                summary="replay-200-after-201",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(cp.repair_rounds, 4)

    def test_legacy_post_checkpoint_review_counts_on_first_upgrade(self) -> None:
        """review200 < checkpoint.updated_at < review201: first record of 201 counts round4."""
        policy = _policy(
            budgets={"max_issue_runtime_minutes": 5256000, "max_repair_rounds": 3}
        )
        runner = FakeRunner()
        head = "abc123deadbeef"
        checkpoint_saved_at = "2026-09-08T01:30:00+00:00"
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            # Poller already advanced watch/body to later event201 before first upgrade.
            conn = sqlite3.connect(ledger)
            conn.execute(
                """
                INSERT INTO pull_request_watches(
                    issue_url, pull_url, discovery_complete,
                    last_event_at, last_event_kind, last_event_id, last_repair_task_id
                ) VALUES (?,?,1,?,?,?,?)
                """,
                (
                    ISSUE_URL,
                    PR_URL,
                    "2026-09-08T01:31:00Z",
                    "reviewed",
                    201,
                    "t_legacy_post_cp_201",
                ),
            )
            conn.commit()
            conn.close()
            runner.reviews = [
                {
                    "id": 200,
                    "user": {"login": CAPTAIN},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T01:00:00Z",
                },
                {
                    "id": 201,
                    "user": {"login": CAPTAIN},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T01:31:00Z",
                },
            ]
            runner.kanban_tasks["t_legacy_post_cp_201"] = {
                "task": {
                    "id": "t_legacy_post_cp_201",
                    "status": "ready",
                    "assignee": "builder",
                    "workspace_kind": "worktree",
                    "workspace_path": "/opt/data/repos/demo-repo",
                    "body": (
                        "## GitHub review event\n\n"
                        f"- Pull request: {PR_URL}\n"
                        f"- Event: reviewed (changes_requested) by @{CAPTAIN}\n"
                        f"- Authoritative event: {PR_URL}#pullrequestreview-201\n"
                    ),
                },
                "runs": [],
            }
            path = hi.checkpoint_path(checkpoints, ISSUE_URL)
            with mock.patch.object(hi, "_utc_now", return_value=checkpoint_saved_at):
                hi.save_checkpoint(
                    path,
                    hi.Checkpoint(
                        version=1,
                        issue_url=ISSUE_URL,
                        state="WAIT_REPAIR",
                        pending_repair=True,
                        root_task_id="t_root",
                        pr_url=PR_URL,
                        pr_number=10,
                        reviewed_head=head,
                        repair_rounds=3,
                        notes=[hi.checkpoint_note("review", "changes_requested", head[:12])],
                    ),
                )
            loaded = hi.load_checkpoint(path, expected_issue_url=ISSUE_URL)
            assert loaded is not None
            self.assertEqual(loaded.updated_at, checkpoint_saved_at)
            # First candidate invocation records the post-save formal event201.
            cp = hi.apply_review_outcome(
                ISSUE_URL,
                head_sha=head,
                outcome="changes_requested",
                summary="event-201-after-checkpoint",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(cp.repair_rounds, 4)
            self.assertTrue(cp.pending_repair)
            self.assertIsNotNone(hi.budget_exhausted(cp, policy))
            scoped201 = hi.checkpoint_note("review", "changes_requested", head[:12], 201)
            self.assertIn(scoped201, cp.notes)
            # Control: already-counted event200 on a fresh legacy checkpoint stays at 3.
            path200 = hi.checkpoint_path(checkpoints, ISSUE_URL)
            conn = sqlite3.connect(ledger)
            conn.execute(
                """
                UPDATE pull_request_watches
                SET last_event_at=?, last_event_kind=?, last_event_id=?, last_repair_task_id=?
                WHERE issue_url=?
                """,
                (
                    "2026-09-08T01:00:00Z",
                    "reviewed",
                    200,
                    "t_legacy_post_cp_200",
                    ISSUE_URL,
                ),
            )
            conn.commit()
            conn.close()
            runner.kanban_tasks["t_legacy_post_cp_200"] = {
                "task": {
                    "id": "t_legacy_post_cp_200",
                    "status": "ready",
                    "assignee": "builder",
                    "workspace_kind": "worktree",
                    "workspace_path": "/opt/data/repos/demo-repo",
                    "body": (
                        "## GitHub review event\n\n"
                        f"- Pull request: {PR_URL}\n"
                        f"- Event: reviewed (changes_requested) by @{CAPTAIN}\n"
                        f"- Authoritative event: {PR_URL}#pullrequestreview-200\n"
                    ),
                },
                "runs": [],
            }
            # Drop 201 so effective review is 200.
            runner.reviews = [
                {
                    "id": 200,
                    "user": {"login": CAPTAIN},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T01:00:00Z",
                }
            ]
            with mock.patch.object(hi, "_utc_now", return_value=checkpoint_saved_at):
                hi.save_checkpoint(
                    path200,
                    hi.Checkpoint(
                        version=1,
                        issue_url=ISSUE_URL,
                        state="WAIT_REPAIR",
                        pending_repair=True,
                        root_task_id="t_root",
                        pr_url=PR_URL,
                        pr_number=10,
                        reviewed_head=head,
                        repair_rounds=3,
                        notes=[hi.checkpoint_note("review", "changes_requested", head[:12])],
                    ),
                )
            cp200 = hi.apply_review_outcome(
                ISSUE_URL,
                head_sha=head,
                outcome="changes_requested",
                summary="legacy-replay-200-control",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(cp200.repair_rounds, 3)
            self.assertIsNone(hi.budget_exhausted(cp200, policy))

    def test_legacy_post_checkpoint_review_survives_adopt_then_record(self) -> None:
        """Adopt before record must keep review200 < updated_at < review201 chronology."""
        policy = _policy(
            budgets={"max_issue_runtime_minutes": 5256000, "max_repair_rounds": 3}
        )
        runner = FakeRunner()
        head = "abc123deadbeef"
        checkpoint_saved_at = "2026-09-08T01:30:00+00:00"
        adopt_at = "2026-09-08T02:00:00+00:00"
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            conn = sqlite3.connect(ledger)
            conn.execute(
                """
                INSERT INTO pull_request_watches(
                    issue_url, pull_url, discovery_complete,
                    last_event_at, last_event_kind, last_event_id, last_repair_task_id
                ) VALUES (?,?,1,?,?,?,?)
                """,
                (
                    ISSUE_URL,
                    PR_URL,
                    "2026-09-08T01:31:00Z",
                    "reviewed",
                    201,
                    "t_legacy_adopt_then_201",
                ),
            )
            conn.commit()
            conn.close()
            runner.reviews = [
                {
                    "id": 200,
                    "user": {"login": CAPTAIN},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T01:00:00Z",
                },
                {
                    "id": 201,
                    "user": {"login": CAPTAIN},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T01:31:00Z",
                },
            ]
            runner.kanban_tasks["t_legacy_adopt_then_201"] = {
                "task": {
                    "id": "t_legacy_adopt_then_201",
                    "status": "ready",
                    "assignee": "builder",
                    "workspace_kind": "worktree",
                    "workspace_path": "/opt/data/repos/demo-repo",
                    "body": (
                        "## GitHub review event\n\n"
                        f"- Pull request: {PR_URL}\n"
                        f"- Event: reviewed (changes_requested) by @{CAPTAIN}\n"
                        f"- Authoritative event: {PR_URL}#pullrequestreview-201\n"
                    ),
                },
                "runs": [],
            }
            path = hi.checkpoint_path(checkpoints, ISSUE_URL)
            with mock.patch.object(hi, "_utc_now", return_value=checkpoint_saved_at):
                hi.save_checkpoint(
                    path,
                    hi.Checkpoint(
                        version=1,
                        issue_url=ISSUE_URL,
                        state="WAIT_REPAIR",
                        pending_repair=True,
                        root_task_id="t_root",
                        pr_url=PR_URL,
                        pr_number=10,
                        reviewed_head=head,
                        repair_rounds=3,
                        notes=[hi.checkpoint_note("review", "changes_requested", head[:12])],
                    ),
                )
            # Supported resume path: adopt/discover before recording the formal review.
            with mock.patch.object(hi, "_utc_now", return_value=adopt_at):
                adopted, _report = hi.run_preflight_and_adopt(
                    policy,
                    ISSUE_URL,
                    ledger=ledger,
                    checkpoint_dir=checkpoints,
                    runner=runner,
                    gh="gh",
                    hermes="hermes",
                    host_continuation="session",
                    apply_dispatch=False,
                )
            self.assertEqual(adopted.updated_at, checkpoint_saved_at)
            self.assertEqual(adopted.repair_rounds, 3)
            self.assertIsNone(hi.budget_exhausted(adopted, policy))
            # After adoption, first record of post-save event201 still counts round4.
            cp = hi.apply_review_outcome(
                ISSUE_URL,
                head_sha=head,
                outcome="changes_requested",
                summary="event-201-after-adopt",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(cp.repair_rounds, 4)
            self.assertTrue(cp.pending_repair)
            self.assertIsNotNone(hi.budget_exhausted(cp, policy))
            scoped201 = hi.checkpoint_note("review", "changes_requested", head[:12], 201)
            self.assertIn(scoped201, cp.notes)

            # Control: same adopt-then-record path for already-counted event200 stays at 3.
            conn = sqlite3.connect(ledger)
            conn.execute(
                """
                UPDATE pull_request_watches
                SET last_event_at=?, last_event_kind=?, last_event_id=?, last_repair_task_id=?
                WHERE issue_url=?
                """,
                (
                    "2026-09-08T01:00:00Z",
                    "reviewed",
                    200,
                    "t_legacy_adopt_then_200",
                    ISSUE_URL,
                ),
            )
            conn.commit()
            conn.close()
            runner.kanban_tasks["t_legacy_adopt_then_200"] = {
                "task": {
                    "id": "t_legacy_adopt_then_200",
                    "status": "ready",
                    "assignee": "builder",
                    "workspace_kind": "worktree",
                    "workspace_path": "/opt/data/repos/demo-repo",
                    "body": (
                        "## GitHub review event\n\n"
                        f"- Pull request: {PR_URL}\n"
                        f"- Event: reviewed (changes_requested) by @{CAPTAIN}\n"
                        f"- Authoritative event: {PR_URL}#pullrequestreview-200\n"
                    ),
                },
                "runs": [],
            }
            runner.reviews = [
                {
                    "id": 200,
                    "user": {"login": CAPTAIN},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T01:00:00Z",
                }
            ]
            with mock.patch.object(hi, "_utc_now", return_value=checkpoint_saved_at):
                hi.save_checkpoint(
                    path,
                    hi.Checkpoint(
                        version=1,
                        issue_url=ISSUE_URL,
                        state="WAIT_REPAIR",
                        pending_repair=True,
                        root_task_id="t_root",
                        pr_url=PR_URL,
                        pr_number=10,
                        reviewed_head=head,
                        repair_rounds=3,
                        notes=[hi.checkpoint_note("review", "changes_requested", head[:12])],
                    ),
                )
            with mock.patch.object(hi, "_utc_now", return_value=adopt_at):
                adopted200, _ = hi.run_preflight_and_adopt(
                    policy,
                    ISSUE_URL,
                    ledger=ledger,
                    checkpoint_dir=checkpoints,
                    runner=runner,
                    gh="gh",
                    hermes="hermes",
                    host_continuation="session",
                    apply_dispatch=False,
                )
            self.assertEqual(adopted200.updated_at, checkpoint_saved_at)
            cp200 = hi.apply_review_outcome(
                ISSUE_URL,
                head_sha=head,
                outcome="changes_requested",
                summary="legacy-replay-200-after-adopt",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(cp200.repair_rounds, 3)
            self.assertIsNone(hi.budget_exhausted(cp200, policy))

    def test_current_cycle_terminal_same_head_repair_returns_for_review(self) -> None:
        """Correlated Done repair for current event returns REVIEW_HEAD on same SHA."""
        policy = _policy()
        runner = FakeRunner()
        head = "abc123deadbeef"
        event_id = 200
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            conn = sqlite3.connect(ledger)
            conn.execute(
                """
                INSERT INTO pull_request_watches(
                    issue_url, pull_url, discovery_complete,
                    last_event_at, last_event_kind, last_event_id, last_repair_task_id
                ) VALUES (?,?,1,?,?,?,?)
                """,
                (
                    ISSUE_URL,
                    PR_URL,
                    "2026-09-08T01:00:00Z",
                    "reviewed",
                    event_id,
                    "t_current_same_head",
                ),
            )
            conn.commit()
            conn.close()
            runner.reviews = [
                {
                    "id": event_id,
                    "user": {"login": CAPTAIN},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T01:00:00Z",
                }
            ]
            runner.kanban_tasks["t_current_same_head"] = {
                "task": {
                    "id": "t_current_same_head",
                    "status": "done",
                    "assignee": "builder",
                    "workspace_kind": "worktree",
                    "workspace_path": "/opt/data/repos/demo-repo",
                    "body": (
                        "## GitHub review event\n\n"
                        f"- Pull request: {PR_URL}\n"
                        f"- Event: reviewed (changes_requested) by @{CAPTAIN}\n"
                        f"- Authoritative event: {PR_URL}#pullrequestreview-{event_id}\n"
                    ),
                },
                "runs": [],
            }
            path = hi.checkpoint_path(checkpoints, ISSUE_URL)
            hi.save_checkpoint(
                path,
                hi.Checkpoint(
                    version=1,
                    issue_url=ISSUE_URL,
                    state="WAIT_REPAIR",
                    pending_repair=True,
                    root_task_id="t_root",
                    pr_url=PR_URL,
                    pr_number=10,
                    reviewed_head=head,
                    repair_rounds=1,
                    notes=[hi.checkpoint_note("review", "changes_requested", head[:12], event_id)],
                ),
            )
            cp, _report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            self.assertEqual(cp.state, "REVIEW_HEAD")
            self.assertFalse(cp.pending_repair)
            self.assertIsNone(cp.clean_head)
            self.assertEqual(cp.repair_rounds, 1)

    def test_legacy_checkpoint_current_cycle_terminal_same_head_returns(self) -> None:
        """Legacy notes without event id still return when live review matches watch."""
        policy = _policy()
        runner = FakeRunner()
        head = "abc123deadbeef"
        event_id = 200
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            conn = sqlite3.connect(ledger)
            conn.execute(
                """
                INSERT INTO pull_request_watches(
                    issue_url, pull_url, discovery_complete,
                    last_event_at, last_event_kind, last_event_id, last_repair_task_id
                ) VALUES (?,?,1,?,?,?,?)
                """,
                (
                    ISSUE_URL,
                    PR_URL,
                    "2026-09-08T01:00:00Z",
                    "reviewed",
                    event_id,
                    "t_legacy_same_head",
                ),
            )
            conn.commit()
            conn.close()
            runner.reviews = [
                {
                    "id": event_id,
                    "user": {"login": CAPTAIN},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T01:00:00Z",
                }
            ]
            runner.kanban_tasks["t_legacy_same_head"] = {
                "task": {
                    "id": "t_legacy_same_head",
                    "status": "done",
                    "assignee": "builder",
                    "workspace_kind": "worktree",
                    "workspace_path": "/opt/data/repos/demo-repo",
                    "body": (
                        f"Authoritative event: {PR_URL}#pullrequestreview-{event_id}\n"
                    ),
                },
                "runs": [],
            }
            path = hi.checkpoint_path(checkpoints, ISSUE_URL)
            hi.save_checkpoint(
                path,
                hi.Checkpoint(
                    version=1,
                    issue_url=ISSUE_URL,
                    state="WAIT_REPAIR",
                    pending_repair=True,
                    root_task_id="t_root",
                    pr_url=PR_URL,
                    pr_number=10,
                    reviewed_head=head,
                    repair_rounds=1,
                    # Head-only legacy marker — no event id suffix.
                    notes=[hi.checkpoint_note("review", "changes_requested", head[:12])],
                ),
            )
            cp, _report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            self.assertEqual(cp.state, "REVIEW_HEAD")
            self.assertFalse(cp.pending_repair)

    def test_stale_event100_done_keeps_wait_for_event200(self) -> None:
        """Older Done repair for event100 must not satisfy a later event200 cycle."""
        policy = _policy()
        runner = FakeRunner()
        head = "abc123deadbeef"
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            conn = sqlite3.connect(ledger)
            conn.execute(
                """
                INSERT INTO pull_request_watches(
                    issue_url, pull_url, discovery_complete,
                    last_event_at, last_event_kind, last_event_id, last_repair_task_id
                ) VALUES (?,?,1,?,?,?,?)
                """,
                (
                    ISSUE_URL,
                    PR_URL,
                    "2026-09-08T00:00:00Z",
                    "reviewed",
                    100,
                    "t_old_event100",
                ),
            )
            conn.commit()
            conn.close()
            runner.reviews = [
                {
                    "id": 100,
                    "user": {"login": CAPTAIN},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T00:00:00Z",
                },
                {
                    "id": 200,
                    "user": {"login": CAPTAIN},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": head,
                    "submitted_at": "2026-09-08T02:00:00Z",
                },
            ]
            runner.kanban_tasks["t_old_event100"] = {
                "task": {
                    "id": "t_old_event100",
                    "status": "done",
                    "assignee": "builder",
                    "workspace_kind": "worktree",
                    "workspace_path": "/opt/data/repos/demo-repo",
                    "body": f"Authoritative event: {PR_URL}#pullrequestreview-100\n",
                },
                "runs": [],
            }
            path = hi.checkpoint_path(checkpoints, ISSUE_URL)
            hi.save_checkpoint(
                path,
                hi.Checkpoint(
                    version=1,
                    issue_url=ISSUE_URL,
                    state="WAIT_REPAIR",
                    pending_repair=True,
                    root_task_id="t_root",
                    pr_url=PR_URL,
                    pr_number=10,
                    reviewed_head=head,
                    repair_rounds=2,
                    notes=[
                        hi.checkpoint_note("review", "changes_requested", head[:12], 100),
                        hi.checkpoint_note("review", "changes_requested", head[:12], 200),
                    ],
                ),
            )
            cp, _report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            self.assertEqual(cp.state, "WAIT_REPAIR")
            self.assertTrue(cp.pending_repair)
            self.assertEqual(cp.repair_rounds, 2)

    def test_closed_merged_issue_completes_done(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        runner.issue = dict(runner.issue)
        runner.issue["state"] = "closed"
        runner.pull = dict(runner.pull)
        runner.pull["merged"] = True
        runner.pull["state"] = "closed"
        runner.open_pulls = []
        runner.timeline = [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 10,
                        "pull_request": {"url": PR_URL},
                    }
                },
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            conn = sqlite3.connect(ledger)
            conn.execute(
                "INSERT INTO pull_request_watches(issue_url, pull_url, discovery_complete) VALUES (?,?,1)",
                (ISSUE_URL, PR_URL),
            )
            conn.commit()
            conn.close()
            path = hi.checkpoint_path(checkpoints, ISSUE_URL)
            hi.save_checkpoint(
                path,
                hi.Checkpoint(
                    version=1,
                    issue_url=ISSUE_URL,
                    state="DONE",
                    root_task_id="t_root",
                    pr_url=PR_URL,
                    clean_head="abc123deadbeef",
                ),
            )
            cp, report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=True,
            )
            self.assertEqual(cp.state, "DONE")
            self.assertEqual(report.state, "DONE")
            self.assertEqual(runner.labels_posted, [])

    def test_status_rejects_incidental_timeline_merged_pr(self) -> None:
        """Open issue must not report DONE from an unrelated merged worker PR."""
        policy = _policy()
        runner = FakeRunner()
        unrelated = {
            "number": 22,
            "html_url": f"https://github.com/{FIXTURE_SLUG}/pull/22",
            "state": "closed",
            "merged": True,
            "draft": False,
            "mergeable_state": "unknown",
            "body": "Closes #13\n\nEarlier review mentioned #2 in passing.\n",
            "user": {"login": WORKER},
            "head": {"sha": "mergedsha0013", "ref": "automation/demo-repo-13"},
            "base": {"ref": "main"},
        }
        runner.pull = unrelated
        runner.pulls[22] = unrelated
        runner.open_pulls = []
        runner.timeline = [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 22,
                        "pull_request": {
                            "url": f"https://api.github.com/repos/{FIXTURE_SLUG}/pulls/22"
                        },
                    }
                },
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            # No root, no watch, no checkpoint — pure live discovery path.
            status = hi.status_issue(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertIsNone(status.pr_url)
            self.assertNotEqual(status.state, "DONE")
            self.assertFalse(status.terminal)
            # Read-only: no checkpoint file created.
            self.assertEqual(list(checkpoints.glob("**/*")), [])

    def test_status_rejects_incidental_full_url_body_merged_pr(self) -> None:
        """Deferred full issue URL in an unrelated merged PR is not DONE."""
        policy = _policy()
        runner = FakeRunner()
        issue14 = f"https://github.com/{FIXTURE_SLUG}/issues/14"
        unrelated = {
            "number": 22,
            "html_url": f"https://github.com/{FIXTURE_SLUG}/pull/22",
            "state": "closed",
            "merged": True,
            "draft": False,
            "mergeable_state": "unknown",
            "body": (
                "Closes #13\n\n"
                f"Context only: {issue14} remains deferred.\n"
            ),
            "user": {"login": WORKER},
            "head": {"sha": "mergedsha0013", "ref": "automation/demo-repo-13"},
            "base": {"ref": "main"},
        }
        runner.issue = {
            "number": 14,
            "title": "Open prerequisite",
            "body": "Ship later.\n",
            "state": "open",
            "html_url": issue14,
            "labels": [{"name": "ready-for-agent"}, {"name": "hermes-kanban-go"}],
        }
        runner.pull = unrelated
        runner.pulls[22] = unrelated
        runner.open_pulls = []
        runner.timeline = [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 22,
                        "pull_request": {
                            "url": f"https://api.github.com/repos/{FIXTURE_SLUG}/pulls/22"
                        },
                    }
                },
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            status = hi.status_issue(
                policy,
                issue14,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertIsNone(status.pr_url)
            self.assertNotEqual(status.state, "DONE")
            self.assertFalse(status.terminal)
            self.assertEqual(list(checkpoints.glob("**/*")), [])

    def test_status_accepts_colon_and_repo_qualified_closing_forms(self) -> None:
        """Supported closing associations work on ordinary noncanonical branches."""
        policy = _policy()
        for body in (
            "Closes: #2\n",
            f"Fixes {FIXTURE_SLUG}#2\n",
            f"Resolves {ISSUE_URL}\n",
        ):
            runner = FakeRunner()
            runner.pull = dict(runner.pull)
            runner.pull["body"] = body
            runner.pull["head"] = {"sha": "featurehead0001", "ref": "feature/misc"}
            runner.pull["merged"] = True
            runner.pull["state"] = "closed"
            runner.open_pulls = []
            runner.timeline = [
                {
                    "event": "cross-referenced",
                    "source": {
                        "issue": {
                            "number": 10,
                            "pull_request": {"url": PR_URL},
                        }
                    },
                }
            ]
            with tempfile.TemporaryDirectory() as tmp:
                ledger = Path(tmp) / "ledger.sqlite3"
                checkpoints = Path(tmp) / "cp"
                status = hi.status_issue(
                    policy,
                    ISSUE_URL,
                    ledger=ledger,
                    checkpoint_dir=checkpoints,
                    runner=runner,
                    gh="gh",
                    hermes="hermes",
                )
                self.assertEqual(status.pr_url, PR_URL)
                self.assertEqual(status.state, "DONE")
                self.assertTrue(status.terminal)

    def test_status_accepts_merged_canonical_before_issue_closure(self) -> None:
        """Merged implementation on the issue branch is DONE even while issue stays open."""
        policy = _policy()
        runner = FakeRunner()
        runner.pull = dict(runner.pull)
        runner.pull["merged"] = True
        runner.pull["state"] = "closed"
        runner.pull["body"] = "Implementation landed; closure may lag.\n"
        runner.open_pulls = []
        runner.timeline = [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 10,
                        "pull_request": {"url": PR_URL},
                    }
                },
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            status = hi.status_issue(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(status.pr_url, PR_URL)
            self.assertEqual(status.state, "DONE")
            self.assertTrue(status.terminal)

    def test_status_surfaces_unreviewed_head_without_mutating_ready(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            path = hi.checkpoint_path(checkpoints, ISSUE_URL)
            hi.save_checkpoint(
                path,
                hi.Checkpoint(
                    version=1,
                    issue_url=ISSUE_URL,
                    state="READY",
                    root_task_id="t_root",
                    pr_url=PR_URL,
                    pr_number=10,
                    clean_head="oldcleanhead01",
                    reviewed_head="oldcleanhead01",
                ),
            )
            status = hi.status_issue(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(status.state, "REVIEW_HEAD")
            self.assertIn("current_head_needs_review", status.blocker or "")
            self.assertTrue(status.details.get("head_needs_review"))
            loaded = hi.load_checkpoint(path, expected_issue_url=ISSUE_URL)
            assert loaded is not None
            self.assertEqual(loaded.state, "READY")
            self.assertEqual(loaded.clean_head, "oldcleanhead01")

    def test_worker_runtime_seam_adopts_without_local_ledger(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        original = runner.run

        def with_runtime(command: list[str]) -> str:
            if command[:1] == ["worker-rt"] or (
                len(command) >= 2 and command[0] == "worker-rt"
            ):
                if command[1] == "ledger-root":
                    return "t_remote_root\n"
                if command[1] == "ledger-watch":
                    return json.dumps(
                        {
                            "pull_url": PR_URL,
                            "discovery_complete": True,
                            "last_repair_task_id": None,
                        }
                    )
                if command[1] == "dispatch-root":
                    return json.dumps({"task_id": "t_remote_root", "created": False})
            return original(command)

        runner.run = with_runtime  # type: ignore[method-assign]
        runner.kanban_tasks["t_remote_root"] = {
            "task": {
                "id": "t_remote_root",
                "status": "done",
                "assignee": "builder",
                "workspace_kind": "worktree",
                "workspace_path": "/opt/data/repos/demo-repo",
            },
            "runs": [{"id": 1, "status": "done", "metadata": {"pr_url": PR_URL}}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "no-such-ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            cp, report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
                worker_runtime=("worker-rt",),
            )
            self.assertEqual(cp.root_task_id, "t_remote_root")
            self.assertEqual(cp.pr_url, PR_URL)
            self.assertEqual(report.root_task_id, "t_remote_root")
            self.assertEqual(runner.created_tasks, [])

    def test_checkpoint_never_persists_command_output_or_summary_prose(self) -> None:
        secret = "FAVA_API_KEY=synthetic-fava-key-NOT-A-REAL-SECRET"
        with tempfile.TemporaryDirectory() as tmp:
            path = hi.checkpoint_path(Path(tmp), ISSUE_URL)
            cp = hi.Checkpoint(version=1, issue_url=ISSUE_URL, state="WAIT_PR")
            hi.set_blocker(cp, secret, fatal=True)
            self.assertTrue((cp.last_blocker or "").startswith("err:"))
            self.assertNotIn("FAVA", cp.last_blocker or "")
            hi.save_checkpoint(path, cp)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("FAVA_API_KEY", text)
            self.assertNotIn("synthetic-fava-key", text)

            cp2 = hi.Checkpoint(version=1, issue_url=ISSUE_URL, state="REVIEW_HEAD")
            hi.record_review_result(
                cp2,
                head_sha="abc123deadbeef",
                outcome="changes_requested",
                summary=f"please rotate {secret} immediately",
            )
            path2 = path.with_name("review.json")
            # save via dedicated path helper shape
            hi.save_checkpoint(path, cp2)
            text2 = path.read_text(encoding="utf-8")
            self.assertNotIn("FAVA_API_KEY", text2)
            self.assertNotIn("synthetic-fava-key", text2)
            self.assertNotIn("please rotate", text2)
            loaded = hi.load_checkpoint(path, expected_issue_url=ISSUE_URL)
            assert loaded is not None
            self.assertTrue(
                any(n.startswith("review:changes_requested:") for n in loaded.notes)
            )
            del path2

    def test_subprocess_runner_exception_has_no_stderr_body(self) -> None:
        import subprocess

        runner = hi.SubprocessRunner(gh="gh", hermes="hermes")
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["gh", "api", "user"],
                returncode=1,
                stdout="",
                stderr="FAVA_API_KEY=synthetic-fava-key-NOT-A-REAL-SECRET\nbad",
            )
            with self.assertRaises(hi.HelmetIssueError) as ctx:
                runner.run(["gh", "api", "user"])
            message = str(ctx.exception)
            self.assertNotIn("FAVA_API_KEY", message)
            self.assertNotIn("synthetic-fava-key", message)
            self.assertIn("command_failed", message)

    def test_subprocess_runner_preserves_typed_http_status(self) -> None:
        import subprocess

        runner = hi.SubprocessRunner(gh="gh", hermes="hermes")
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["gh", "api", "repos/x/y/issues/1/dependencies/blocked_by"],
                returncode=1,
                stdout='{"message":"Resource not accessible","status":"403"}',
                stderr="gh: Resource not accessible by personal access token (HTTP 403)\n",
            )
            with self.assertRaises(hi.HelmetIssueError) as ctx:
                runner.run(["gh", "api", "repos/x/y/issues/1/dependencies/blocked_by"])
            message = str(ctx.exception)
            self.assertIn("http403", message)
            self.assertNotIn("Resource not accessible", message)
            self.assertNotIn("personal access token", message)


class ResidualReview575bf70RegressionTests(unittest.TestCase):
    """Captain residual findings against 575bf70 (PR #19 review 5134620371)."""

    def test_default_dispatch_does_not_create_root_when_pr_exists_without_ledger(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "missing-ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            # Default apply_dispatch=True: existing canonical worker PR + no root
            # must fail closed without Kanban create or intake label mutation.
            cp, _report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=True,
            )
            self.assertEqual(cp.state, "FAILED")
            self.assertIn("pr_without_recoverable_root", cp.last_blocker or "")
            self.assertIsNone(cp.root_task_id)
            self.assertEqual(runner.created_tasks, [])
            self.assertEqual(runner.labels_posted, [])

    def test_blocked_review_invalidates_clean_and_blocks_merge_and_resume(self) -> None:
        policy = _policy()
        runner = FakeRunner()
        runner.reviews = [
            {
                "user": {"login": CAPTAIN},
                "state": "APPROVED",
                "commit_id": "abc123deadbeef",
                "submitted_at": "2026-01-01T00:00:00Z",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.sqlite3"
            checkpoints = Path(tmp) / "cp"
            _ledger_with_root(ledger)
            hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            # Prior clean authority on this head.
            clean_cp = hi.apply_review_outcome(
                ISSUE_URL,
                head_sha="abc123deadbeef",
                outcome="clean",
                summary="lgtm",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(clean_cp.state, "READY")
            self.assertEqual(clean_cp.clean_head, "abc123deadbeef")

            # Explicit blocked on the same head clears clean and stops merge.
            blocked = hi.apply_review_outcome(
                ISSUE_URL,
                head_sha="abc123deadbeef",
                outcome="blocked",
                summary="unsafe",
                checkpoint_dir=checkpoints,
                policy=policy,
                ledger=ledger,
                runner=runner,
                gh="gh",
                hermes="hermes",
            )
            self.assertEqual(blocked.state, "BLOCKED")
            self.assertIsNone(blocked.clean_head)
            self.assertEqual(blocked.reviewed_head, "abc123deadbeef")

            issue = hi.IssueRef(
                repository=policy.repositories[0],
                number=2,
                title="x",
                body="Merge when clean: yes\n",
                state="open",
                labels=("ready-for-agent",),
                html_url=ISSUE_URL,
            )
            pull = hi.PullRequestRef(
                FIXTURE_SLUG,
                10,
                PR_URL,
                "abc123deadbeef",
                "automation/demo-repo-2",
                "main",
                WORKER,
                "open",
                False,
                "clean",
                False,
            )
            decision, blocker = hi.evaluate_merge_gate(
                policy,
                issue,
                pull,
                blocked,
                required_checks_green=True,
                mergeable=True,
                runner=runner,
                gh="gh",
            )
            self.assertEqual(decision, "not_ready")
            self.assertIn("blocked", (blocker or "").casefold())

            # Unchanged-head resume must not silently promote to READY.
            resumed, _report = hi.run_preflight_and_adopt(
                policy,
                ISSUE_URL,
                ledger=ledger,
                checkpoint_dir=checkpoints,
                runner=runner,
                gh="gh",
                hermes="hermes",
                apply_dispatch=False,
            )
            self.assertEqual(resumed.state, "BLOCKED")
            self.assertIsNone(resumed.clean_head)

            decision2, _ = hi.evaluate_merge_gate(
                policy,
                issue,
                pull,
                resumed,
                required_checks_green=True,
                mergeable=True,
                runner=runner,
                gh="gh",
            )
            self.assertEqual(decision2, "not_ready")


if __name__ == "__main__":
    unittest.main()
