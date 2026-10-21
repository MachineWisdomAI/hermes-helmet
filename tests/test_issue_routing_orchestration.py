"""Routed tracking issues through helmet-issue: discovery, review, merge, acceptance.

``example-org/planning`` is the (internal) tracking repository,
``example-org/product`` the (public) implementation repository. The fake GitHub
below is an in-memory fixture; behavior that needs real GitHub (issue transfer
redirects, native dependency endpoints) is listed in docs/issue-routing.md as
later live qualification.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hermes_helmet import authority  # noqa: E402
from hermes_helmet import cli as helmet_cli  # noqa: E402
from hermes_helmet import github_issue_poller as poller  # noqa: E402
from hermes_helmet import helmet_epic as he  # noqa: E402
from hermes_helmet import helmet_issue as hi  # noqa: E402

_WORKER_RUNTIME_PATCHER = mock.patch.object(hi, "DEFAULT_WORKER_RUNTIME", ())


def setUpModule() -> None:
    _WORKER_RUNTIME_PATCHER.start()


def tearDownModule() -> None:
    _WORKER_RUNTIME_PATCHER.stop()


ORG = "example-org"
BACKLOG = f"{ORG}/backlog"
PLANNING = f"{ORG}/planning"
PRODUCT = f"{ORG}/product"
OTHER = f"{ORG}/other"
ROUTE_LABEL = "route:product"
CAPTAIN = "example-captain"
WORKER = "example-agent"
TRACKING_URL = f"https://github.com/{PLANNING}/issues/7"
OLD_TRACKING_URL = f"https://github.com/{BACKLOG}/issues/3"
PR_URL = f"https://github.com/{PRODUCT}/pull/31"
BRANCH = authority.routed_branch(TRACKING_URL)
OLD_BRANCH = authority.routed_branch(OLD_TRACKING_URL)
HEAD = "abc123deadbeef"
NODE_ID = "I_kwDOexample7"


def policy_dict(root: Path | None = None) -> dict:
    base = root or Path("/opt/data/repos")
    return {
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
        "github_owners": [ORG],
        "budgets": {"max_issue_runtime_minutes": 120, "max_repair_rounds": 10},
        "repositories": [
            {"slug": slug, "worktree": str(base / slug.split("/")[1])}
            for slug in (BACKLOG, PLANNING, PRODUCT)
        ],
        "issue_routes": [
            {"label": ROUTE_LABEL, "tracking_repository": PLANNING, "implementation_repository": PRODUCT},
            {"label": ROUTE_LABEL, "tracking_repository": BACKLOG, "implementation_repository": PRODUCT},
        ],
    }


def make_policy(root: Path | None = None, **overrides: object) -> authority.Policy:
    raw = policy_dict(root)
    raw.update(overrides)
    return authority.policy_from_mapping(raw)


def seed_ledger(path: Path, issue_url: str = TRACKING_URL, task_id: str = "t_root", *, pull_url=None) -> None:
    with sqlite3.connect(path) as connection:
        poller._ensure_ledger(connection)
        connection.execute(
            "INSERT INTO issue_tasks(issue_url, task_id) VALUES (?, ?)", (issue_url, task_id)
        )
        if pull_url:
            connection.execute(
                "INSERT INTO pull_request_watches(issue_url, pull_url, discovery_complete,"
                " last_event_at, last_event_kind, last_event_id, last_repair_task_id)"
                " VALUES (?, ?, 1, '2026-10-01T00:00:00Z', 'reviewed', 55, 't_repair')",
                (issue_url, pull_url),
            )


class RoutedGitHub:
    """In-memory GitHub + Kanban for one routed tracking issue."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.identity = CAPTAIN
        self.calls: list[list[str]] = []
        self.issues: dict[str, dict] = {
            f"repos/{PLANNING}/issues/7": self._issue(PLANNING, 7, TRACKING_URL),
        }
        self.pull: dict = {
            "number": 31,
            "html_url": PR_URL,
            "state": "open",
            "merged": False,
            "draft": False,
            "mergeable_state": "clean",
            "body": "Adds the widget. Public description only.\n",
            "user": {"login": WORKER},
            "head": {"sha": HEAD, "ref": BRANCH},
            "base": {"ref": "main"},
        }
        self.extra_pulls: list[dict] = []
        self.reviews = [
            {
                "id": 9,
                "user": {"login": CAPTAIN},
                "state": "APPROVED",
                "commit_id": HEAD,
                "submitted_at": "2026-10-01T00:00:00Z",
            }
        ]
        self.rules = [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "ci"}]},
            }
        ]
        self.check_runs = [
            {
                "name": "ci",
                "status": "completed",
                "conclusion": "success",
                "started_at": "2026-10-01T00:00:00Z",
                "completed_at": "2026-10-01T00:01:00Z",
            }
        ]
        self.task_status = "done"
        self.repair_status = "done"
        self.task_workspace = str(root / "product" / ".worktrees" / "t_root")
        self.task_branch = BRANCH
        self.task_pr: str | None = None
        self.mutations: list[tuple[str, str]] = []
        self.merge_puts = 0
        self.kanban_creates: list[list[str]] = []

    @staticmethod
    def _issue(slug: str, number: int, url: str, *, labels=(ROUTE_LABEL,), node_id=NODE_ID) -> dict:
        return {
            "number": number,
            "title": "Private planning title",
            "body": "Private acceptance criteria.\n",
            "state": "open",
            "html_url": url,
            "node_id": node_id,
            "labels": [{"name": n} for n in ("ready-for-agent", "hermes-kanban-go", *labels)],
        }

    def run(self, command):
        command = list(command)
        self.calls.append(command)
        exe = Path(command[0]).name
        if exe == "git":
            return "true\n"
        if exe == "gh" and command[1:3] == ["auth", "status"]:
            return json.dumps({"hosts": {"github.com": [{"login": CAPTAIN, "active": True}]}})
        if exe == "hermes":
            return self._hermes(command)
        if exe == "gh" and command[1] == "api":
            return self._api(command)
        raise AssertionError(command)

    def _hermes(self, command: list[str]) -> str:
        if "show" in command:
            task_id = command[command.index("show") + 1]
            runs = []
            if self.task_pr:
                runs.append({"id": 1, "status": "done", "metadata": {"published_pr": self.task_pr}})
            return json.dumps(
                {
                    "task": {
                        "id": task_id,
                        "status": (
                            self.task_status
                            if task_id == "t_root"
                            else self.repair_status if task_id == "t_repair" else "done"
                        ),
                        "assignee": "builder",
                        "workspace_kind": "worktree",
                        "workspace_path": self.task_workspace,
                        "branch_name": self.task_branch,
                    },
                    "runs": runs,
                }
            )
        if "create" in command:
            self.kanban_creates.append(command)
            return json.dumps({"id": f"t_new_{len(self.kanban_creates)}"})
        raise AssertionError(command)

    def _pulls_all(self) -> list[dict]:
        return [self.pull, *self.extra_pulls]

    def _api(self, command: list[str]) -> str:
        method = command[command.index("--method") + 1] if "--method" in command else "GET"
        endpoint = command[-1]
        if method != "GET":
            endpoint = command[command.index("--method") + 2]
            return self._mutation(method, endpoint, command)
        if endpoint == "user":
            return json.dumps({"login": self.identity})
        path = urlparse(endpoint)
        query = parse_qs(path.query)
        if path.path in self.issues:
            return json.dumps(self.issues[path.path])
        if path.path == f"repos/{PRODUCT}/pulls":
            head = query.get("head", [None])[0]
            rows = [
                pr for pr in self._pulls_all()
                if head is None or f"{ORG}:{pr['head']['ref']}" == head
            ]
            return json.dumps(rows)
        if path.path.startswith(f"repos/{PRODUCT}/pulls/") and path.path.endswith("/reviews"):
            return json.dumps(self.reviews)
        if path.path.startswith(f"repos/{PRODUCT}/pulls/"):
            number = int(path.path.rsplit("/", 1)[1])
            for pr in self._pulls_all():
                if pr["number"] == number:
                    return json.dumps(pr)
        if path.path.startswith(f"repos/{PRODUCT}/rules/branches/"):
            return json.dumps(self.rules)
        if "/check-runs" in path.path and path.path.startswith(f"repos/{PRODUCT}/"):
            return json.dumps({"total_count": len(self.check_runs), "check_runs": self.check_runs})
        if path.path.endswith("/status") and path.path.startswith(f"repos/{PRODUCT}/commits/"):
            return json.dumps({"state": "success", "statuses": []})
        if (
            path.path.endswith("/parent")
            or "sub_issues" in path.path
            or re.fullmatch(r"repos/[^/]+/[^/]+/issues/\d+", path.path)
        ):
            raise hi.HelmetIssueError("err:command_failed:gh:exit1:http404")
        raise AssertionError(command)

    def _mutation(self, method: str, endpoint: str, command: list[str]) -> str:
        self.mutations.append((method, endpoint))
        if method == "PUT" and endpoint == f"repos/{PRODUCT}/pulls/31/merge":
            self.merge_puts += 1
            sha = next(a for a in command if a.startswith("sha=")).split("=", 1)[1]
            assert sha == self.pull["head"]["sha"], "merge must be SHA-bound"
            self.pull = dict(self.pull, merged=True, state="closed")
            return json.dumps({"merged": True, "sha": sha})
        if method == "PATCH" and endpoint == f"repos/{PLANNING}/issues/7":
            fields = dict(
                a.split("=", 1) for a in command[command.index("--method") + 3:] if "=" in a
            )
            self.issues[endpoint] = dict(
                self.issues[endpoint], state=fields["state"], state_reason=fields.get("state_reason")
            )
            return json.dumps(self.issues[endpoint])
        if method == "POST" and endpoint in {
            f"repos/{PLANNING}/issues/7/comments",
            f"repos/{PLANNING}/issues/7/labels",
        }:
            return "{}"
        raise AssertionError((method, endpoint))

    # convenience -------------------------------------------------------
    def merge_prs(self) -> None:
        self.pull = dict(self.pull, merged=True, state="closed")

    def writes_to(self, slug: str) -> list[tuple[str, str]]:
        return [m for m in self.mutations if m[1].startswith(f"repos/{slug}/")]


class RoutedBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Policy resolves worktree paths; canonicalize the fixture root so
        # expectations match on macOS, where /var is a link to /private/var.
        self.root = Path(self._tmp.name).resolve()
        for name in ("backlog", "planning", "product"):
            (self.root / name).mkdir()
        self.policy = make_policy(self.root)
        self.gh = RoutedGitHub(self.root)
        self.ledger = self.root / "ledger.sqlite3"
        self.checkpoints = self.root / "cp"

    def adopt(self, url: str = TRACKING_URL, **kwargs):
        return hi.run_preflight_and_adopt(
            self.policy,
            url,
            ledger=self.ledger,
            checkpoint_dir=self.checkpoints,
            runner=self.gh,
            gh="gh",
            hermes="hermes",
            host_continuation="cron",
            **kwargs,
        )

    def ready_checkpoint(self, url: str = TRACKING_URL, **overrides) -> hi.Checkpoint:
        payload = {
            "version": 1,
            "issue_url": url,
            "state": "READY",
            "clean_head": HEAD,
            "reviewed_head": HEAD,
            "pr_url": PR_URL,
            "pr_number": 31,
            "root_task_id": "t_root",
            "started_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "merge_mode": "unattended_when_clean",
            "merge_mode_choice": "unattended_when_clean",
            "merge_mode_source": "policy",
        }
        payload.update(overrides)
        checkpoint = hi.Checkpoint(**payload)  # type: ignore[arg-type]
        hi.save_checkpoint(hi.checkpoint_path(self.checkpoints, url), checkpoint)
        return checkpoint

    def merge(self, url: str = TRACKING_URL, expected: str = HEAD):
        return hi.merge_issue(
            url,
            expected_head=expected,
            policy=self.policy,
            ledger=self.ledger,
            checkpoint_dir=self.checkpoints,
            runner=self.gh,
            gh="gh",
            hermes="hermes",
        )

    def accept(self, url: str = TRACKING_URL, expected: str = HEAD):
        return hi.close_tracking_issue(
            url,
            expected_head=expected,
            policy=self.policy,
            ledger=self.ledger,
            checkpoint_dir=self.checkpoints,
            runner=self.gh,
            gh="gh",
            hermes="hermes",
        )


class RoutedDiscoveryTests(RoutedBase):
    def test_public_pr_is_discovered_by_canonical_branch_without_private_links(self) -> None:
        seed_ledger(self.ledger)
        checkpoint, report = self.adopt(apply_dispatch=False)
        self.assertEqual(checkpoint.state, "REVIEW_HEAD")
        self.assertEqual(report.pr_url, PR_URL)
        self.assertEqual(report.root_task_id, "t_root")
        self.assertEqual(report.details["route"]["implementation_repository"], PRODUCT)
        self.assertEqual(report.issue_url, TRACKING_URL)
        endpoints = " ".join(c[-1] for c in self.gh.calls if Path(c[0]).name == "gh")
        self.assertNotIn(f"repos/{PLANNING}/issues/7/timeline", endpoints)
        self.assertNotIn(f"repos/{PLANNING}/pulls", endpoints)

    def test_closing_keyword_or_issue_number_on_a_non_canonical_pr_is_not_ownership(self) -> None:
        seed_ledger(self.ledger)
        self.gh.pull = dict(
            self.gh.pull,
            body="Closes #7\nFixes " + TRACKING_URL,
            head={"sha": HEAD, "ref": "someone/else"},
        )
        checkpoint, report = self.adopt(apply_dispatch=False)
        self.assertEqual(checkpoint.state, "WAIT_PR")
        self.assertIsNone(report.pr_url)

    def test_kanban_adopted_pr_in_the_configured_target_is_canonical(self) -> None:
        seed_ledger(self.ledger)
        self.gh.task_pr = PR_URL
        self.gh.pull = dict(self.gh.pull, head={"sha": HEAD, "ref": "late-renamed-branch"})
        # The branch no longer matches, but exact-URL adoption by the task is authoritative.
        checkpoint, report = self.adopt(apply_dispatch=False)
        self.assertEqual(report.pr_url, PR_URL)
        self.assertEqual(checkpoint.state, "REVIEW_HEAD")

    def test_adopted_pr_outside_the_configured_target_fails_closed(self) -> None:
        seed_ledger(self.ledger)
        self.gh.task_pr = f"https://github.com/{OTHER}/pull/5"
        checkpoint, _ = self.adopt(apply_dispatch=False)
        self.assertEqual(checkpoint.state, "FAILED")
        self.assertIn("pr_outside_repository", checkpoint.last_blocker or "")

    def test_unrouted_label_keeps_the_same_repository_contract(self) -> None:
        labels = [{"name": "ready-for-agent"}, {"name": "hermes-kanban-go"}]
        self.gh.issues[f"repos/{PLANNING}/issues/7"]["labels"] = labels
        issue = hi.load_issue(self.policy, TRACKING_URL, self.gh, gh="gh")
        self.assertIsNone(issue.route)
        self.assertEqual(issue.implementation_repository.slug, PLANNING)
        self.assertEqual(issue.branch, "automation/planning-7")

    def test_dispatch_creates_one_root_in_the_target_checkout(self) -> None:
        self.gh.pull = dict(self.gh.pull, head={"sha": HEAD, "ref": "unrelated"})
        checkpoint, _ = self.adopt()
        (create,) = self.gh.kanban_creates
        self.assertEqual(create[create.index("--workspace") + 1], f"worktree:{self.root / 'product'}")
        self.assertEqual(create[create.index("--branch") + 1], BRANCH)
        self.assertEqual(create[create.index("--completion-contract") + 1], PRODUCT)
        self.assertEqual(create[create.index("--idempotency-key") + 1], TRACKING_URL)
        self.assertIn("Public pull-request privacy contract", create[create.index("--body") + 1])
        # The dispatch label was already on the tracking issue: no label write.
        self.assertEqual(self.gh.writes_to(PLANNING), [])
        self.assertTrue(checkpoint.root_task_id)
        # A second pass adopts the same root; no second Kanban create.
        self.adopt()
        self.assertEqual(len(self.gh.kanban_creates), 1)

    def test_dispatch_to_an_unconfigured_target_is_impossible_through_policy(self) -> None:
        raw = policy_dict(self.root)
        raw["issue_routes"][0]["implementation_repository"] = OTHER
        with self.assertRaises(authority.AuthorityError):
            authority.policy_from_mapping(raw)


class RoutedReviewAndMergeTests(RoutedBase):
    def test_clean_review_of_the_exact_head_records_ready_without_inventing_a_finding(self) -> None:
        seed_ledger(self.ledger)
        self.adopt(apply_dispatch=False)
        checkpoint = hi.apply_review_outcome(
            TRACKING_URL,
            head_sha=HEAD,
            outcome="clean",
            checkpoint_dir=self.checkpoints,
            policy=self.policy,
            ledger=self.ledger,
            runner=self.gh,
            gh="gh",
            hermes="hermes",
        )
        self.assertEqual(checkpoint.state, "READY")
        self.assertEqual(checkpoint.clean_head, HEAD)
        self.assertFalse(checkpoint.pending_repair)
        self.assertEqual(checkpoint.repair_rounds, 0)

    def test_merge_is_sha_bound_in_the_target_repository_and_leaves_acceptance_open(self) -> None:
        seed_ledger(self.ledger)
        self.ready_checkpoint()
        report = self.merge()
        self.assertEqual(report.decision, "merged")
        self.assertTrue(report.merged)
        self.assertEqual(report.checkpoint_state, "AWAIT_ACCEPTANCE")
        self.assertEqual(self.gh.merge_puts, 1)
        self.assertEqual(self.gh.writes_to(PLANNING), [])
        # Required checks come from the repository that is merged.
        endpoints = [c[-1] for c in self.gh.calls if Path(c[0]).name == "gh"]
        self.assertTrue(any(e.startswith(f"repos/{PRODUCT}/rules/branches/") for e in endpoints))
        self.assertFalse(any(e.startswith(f"repos/{PLANNING}/rules") for e in endpoints))
        # The tracking issue is still open and no Kanban task was created.
        self.assertEqual(self.gh.issues[f"repos/{PLANNING}/issues/7"]["state"], "open")
        self.assertEqual(self.gh.kanban_creates, [])

    def test_merge_is_withheld_when_the_target_repository_checks_are_red(self) -> None:
        seed_ledger(self.ledger)
        self.ready_checkpoint()
        self.gh.check_runs[0]["conclusion"] = "failure"
        report = self.merge()
        self.assertFalse(report.merged)
        self.assertEqual(self.gh.merge_puts, 0)

    def test_merge_withholds_a_stale_expected_head(self) -> None:
        seed_ledger(self.ledger)
        self.ready_checkpoint()
        report = self.merge(expected="0" * 14)
        self.assertEqual(report.decision, "withheld")
        self.assertEqual(self.gh.merge_puts, 0)

    def test_merge_gate_refuses_a_pull_request_outside_the_configured_target(self) -> None:
        seed_ledger(self.ledger)
        checkpoint = self.ready_checkpoint()
        issue = hi.load_issue(self.policy, TRACKING_URL, self.gh, gh="gh")
        foreign = hi.PullRequestRef(
            repository_slug=OTHER, number=5, url=f"https://github.com/{OTHER}/pull/5",
            head_sha=HEAD, head_ref=BRANCH, base_ref="main", author_login=WORKER,
            state="open", merged=False, mergeable_state="clean", draft=False,
        )
        decision, blocker = hi.evaluate_merge_gate(
            self.policy, issue, foreign, checkpoint, required_checks_green=True, mergeable=True
        )
        self.assertEqual(decision, "not_ready")
        self.assertIn("pr_outside_repository", blocker or "")

    def test_manual_merge_hold_is_preserved_for_routed_issues(self) -> None:
        seed_ledger(self.ledger)
        self.gh.issues[f"repos/{PLANNING}/issues/7"]["body"] = "Merge when clean: no\n"
        self.ready_checkpoint(
            merge_mode="explicit_captain_approval",
            merge_mode_choice="explicit_captain_approval",
            merge_mode_source="interactive",
        )
        report = self.merge()
        self.assertFalse(report.merged)
        self.assertEqual(self.gh.merge_puts, 0)
        self.assertIn("explicit_captain_approval_required", report.blocker or "")

    def test_status_after_merge_awaits_explicit_acceptance(self) -> None:
        seed_ledger(self.ledger)
        self.ready_checkpoint()
        self.merge()
        report = hi.status_issue(
            self.policy, TRACKING_URL, ledger=self.ledger, checkpoint_dir=self.checkpoints,
            runner=self.gh, gh="gh", hermes="hermes",
        )
        self.assertEqual(report.state, "AWAIT_ACCEPTANCE")
        self.assertFalse(report.terminal)
        self.assertEqual(report.details["acceptance"], "pending_explicit")
        self.assertTrue(report.merge_gate.endswith(":merged"))
        # Adoption keeps waiting: merged code never closes the tracking issue.
        checkpoint, _ = self.adopt(apply_dispatch=False)
        self.assertEqual(checkpoint.state, "AWAIT_ACCEPTANCE")
        self.assertEqual(self.gh.writes_to(PLANNING), [])


class ExplicitAcceptanceTests(RoutedBase):
    def merged(self) -> None:
        seed_ledger(self.ledger)
        self.ready_checkpoint()
        self.merge()

    def test_acceptance_closes_the_tracking_issue_after_a_sha_bound_merge(self) -> None:
        self.merged()
        report = self.accept()
        self.assertEqual(report.decision, "closed")
        self.assertTrue(report.closed)
        self.assertEqual(report.checkpoint_state, "DONE")
        issue = self.gh.issues[f"repos/{PLANNING}/issues/7"]
        self.assertEqual((issue["state"], issue["state_reason"]), ("closed", "completed"))
        methods = [m for m in self.gh.writes_to(PLANNING)]
        self.assertEqual(
            methods,
            [("POST", f"repos/{PLANNING}/issues/7/comments"), ("PATCH", f"repos/{PLANNING}/issues/7")],
        )
        self.assertEqual(self.gh.writes_to(PRODUCT)[0][0], "PUT")  # only the merge itself
        self.assertEqual(len(self.gh.writes_to(PRODUCT)), 1)
        status = hi.status_issue(
            self.policy, TRACKING_URL, ledger=self.ledger, checkpoint_dir=self.checkpoints,
            runner=self.gh, gh="gh", hermes="hermes",
        )
        self.assertEqual(status.state, "DONE")
        self.assertTrue(status.terminal)

    def test_manual_close_of_the_tracking_issue_completes_the_merged_work(self) -> None:
        self.merged()
        self.gh.issues[f"repos/{PLANNING}/issues/7"]["state"] = "closed"
        status = hi.status_issue(
            self.policy, TRACKING_URL, ledger=self.ledger, checkpoint_dir=self.checkpoints,
            runner=self.gh, gh="gh", hermes="hermes",
        )
        self.assertEqual(status.state, "DONE")
        checkpoint, _ = self.adopt(apply_dispatch=False)
        self.assertEqual(checkpoint.state, "DONE")
        self.assertEqual(self.gh.writes_to(PLANNING), [])

    def test_acceptance_is_idempotent(self) -> None:
        self.merged()
        self.accept()
        writes = len(self.gh.mutations)
        again = self.accept()
        self.assertEqual(again.decision, "already_closed")
        self.assertEqual(len(self.gh.mutations), writes)

    def test_acceptance_requires_a_merged_pr_and_the_reviewed_head(self) -> None:
        seed_ledger(self.ledger)
        self.ready_checkpoint()
        unmerged = self.accept()
        self.assertEqual(unmerged.decision, "withheld")
        self.assertIn("pr_not_merged", unmerged.blocker or "")
        self.assertEqual(self.gh.writes_to(PLANNING), [])
        self.gh.merge_prs()
        wrong_head = self.accept(expected="f" * 14)
        self.assertIn("merge_head_mismatch", wrong_head.blocker or "")
        unreviewed = self.ready_checkpoint(clean_head=None, state="REVIEW_HEAD")
        del unreviewed
        no_review = self.accept()
        self.assertIn("head_lacks_clean_captain_review", no_review.blocker or "")
        self.assertEqual(self.gh.writes_to(PLANNING), [])

    def test_acceptance_is_refused_for_same_repository_issues(self) -> None:
        self.gh.issues[f"repos/{PLANNING}/issues/7"]["labels"] = [{"name": "ready-for-agent"}]
        seed_ledger(self.ledger)
        self.ready_checkpoint()
        with self.assertRaises(hi.HelmetIssueError) as raised:
            self.accept()
        self.assertIn("acceptance_not_routed", str(raised.exception))

    def test_cli_accept_requires_expected_head_and_reports_json(self) -> None:
        self.merged()
        config = self.root / "policy.json"
        config.write_text(json.dumps(policy_dict(self.root)), encoding="utf-8")
        with mock.patch.object(helmet_cli, "SubprocessRunner", return_value=self.gh):
            code = helmet_cli.main(
                [
                    "accept", TRACKING_URL, "--expected-head", HEAD, "--config", str(config),
                    "--ledger", str(self.ledger), "--checkpoint-dir", str(self.checkpoints), "--json",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(self.gh.issues[f"repos/{PLANNING}/issues/7"]["state"], "closed")

    def classify(self, tracking: he.EpicNode, dependent: he.EpicNode):
        helper = EpicDependencyAssumptionTests()
        graph = helper.graph(tracking, dependent)
        return he.classify_children(
            self.policy, graph, checkpoint_dir=self.checkpoints, runner=self.gh, gh="gh",
            hermes="hermes", ledger=self.ledger, worker_runtime=(), epic_body="",
        )

    def test_epic_waits_for_acceptance_and_dependents_stay_blocked_on_the_tracking_issue(self) -> None:
        self.merged()
        helper = EpicDependencyAssumptionTests()
        dependent = helper.node(f"https://github.com/{PLANNING}/issues/8")
        buckets = self.classify(helper.node(TRACKING_URL), dependent)
        self.assertEqual(buckets["awaiting_human"], [TRACKING_URL])
        self.assertEqual(buckets["blocked"], [dependent.url])
        self.assertEqual(buckets["active"], [])
        self.assertEqual(buckets["completed"], [])
        self.accept()
        buckets = self.classify(helper.node(TRACKING_URL, "closed", "completed"), dependent)
        self.assertEqual(buckets["completed"], [TRACKING_URL])
        self.assertEqual(buckets["blocked"], [])


class MergedDiscoveryEvidenceTests(RoutedBase):
    """Discovering a merged routed PR must not mint Captain review evidence."""

    def discover_merged(self, **checkpoint_overrides):
        seed_ledger(self.ledger)
        if checkpoint_overrides:
            self.ready_checkpoint(**checkpoint_overrides)
        self.gh.merge_prs()
        return self.adopt(apply_dispatch=False)

    def planning_writes(self):
        return self.gh.writes_to(PLANNING)

    def test_externally_merged_unreviewed_pr_awaits_acceptance_without_clean_evidence(self) -> None:
        checkpoint, _ = self.discover_merged()
        self.assertEqual(checkpoint.state, "AWAIT_ACCEPTANCE")
        self.assertIsNone(checkpoint.clean_head)
        self.assertIsNone(checkpoint.reviewed_head)
        self.assertIn(f"op:merged_head_not_reviewed_clean:{HEAD[:12]}", checkpoint.notes)
        report = self.accept()
        self.assertEqual(report.decision, "withheld")
        self.assertFalse(report.closed)
        self.assertIn("head_lacks_clean_captain_review", report.blocker or "")
        self.assertEqual(self.gh.issues[f"repos/{PLANNING}/issues/7"]["state"], "open")
        self.assertEqual(self.planning_writes(), [])

    def test_merged_head_that_differs_from_the_reviewed_head_is_not_accepted(self) -> None:
        old_head = "1" * 14
        checkpoint, _ = self.discover_merged(clean_head=old_head, reviewed_head=old_head)
        self.assertEqual(checkpoint.state, "AWAIT_ACCEPTANCE")
        self.assertEqual((checkpoint.clean_head, checkpoint.reviewed_head), (old_head, old_head))
        report = self.accept()
        self.assertIn("head_lacks_clean_captain_review", report.blocker or "")
        self.assertEqual(self.planning_writes(), [])

    def test_merged_head_after_changes_requested_is_not_accepted(self) -> None:
        checkpoint, _ = self.discover_merged(
            state="REQUEST_REPAIR", clean_head=None, reviewed_head=HEAD, pending_repair=True
        )
        self.assertIsNone(checkpoint.clean_head)
        report = self.accept()
        self.assertIn("head_lacks_clean_captain_review", report.blocker or "")
        self.assertEqual(self.planning_writes(), [])

    def test_genuinely_reviewed_merged_head_is_preserved_and_can_be_accepted(self) -> None:
        checkpoint, _ = self.discover_merged(state="READY")
        self.assertEqual(checkpoint.state, "AWAIT_ACCEPTANCE")
        self.assertEqual((checkpoint.clean_head, checkpoint.reviewed_head), (HEAD, HEAD))
        self.assertFalse(any("merged_head_not_reviewed_clean" in n for n in checkpoint.notes))
        report = self.accept()
        self.assertEqual(report.decision, "closed")
        self.assertEqual(self.gh.issues[f"repos/{PLANNING}/issues/7"]["state"], "closed")

    def test_closed_tracking_issue_with_unreviewed_merge_records_no_clean_evidence(self) -> None:
        seed_ledger(self.ledger)
        self.gh.merge_prs()
        self.gh.issues[f"repos/{PLANNING}/issues/7"]["state"] = "closed"
        checkpoint, _ = self.adopt(apply_dispatch=False)
        self.assertEqual(checkpoint.state, "DONE")
        self.assertIsNone(checkpoint.clean_head)
        self.assertIsNone(checkpoint.reviewed_head)

    def test_same_repository_issue_still_records_the_merged_head(self) -> None:
        self.gh.issues[f"repos/{PLANNING}/issues/7"]["labels"] = [
            {"name": "ready-for-agent"}, {"name": "hermes-kanban-go"}
        ]
        issue = hi.load_issue(self.policy, TRACKING_URL, self.gh, gh="gh")
        self.assertIsNone(issue.route)
        checkpoint = hi.Checkpoint(version=1, issue_url=TRACKING_URL, state="WAIT_PR")
        pull = hi.load_pull_request(PRODUCT, 31, self.gh, gh="gh")
        hi._record_merged_discovery(checkpoint, issue, pull)
        self.assertEqual((checkpoint.clean_head, checkpoint.reviewed_head), (HEAD, HEAD))


class EpicDependencyAssumptionTests(unittest.TestCase):
    """Dependents wait on the tracking issue's closure, not on merged code."""

    def node(self, url: str, state: str = "open", reason=None) -> he.EpicNode:
        slug, number = url.removeprefix("https://github.com/").split("/issues/")
        return he.EpicNode(
            url=url, number=int(number), repository_slug=slug, title="t", body="",
            state=state, state_reason=reason, labels=(),
        )

    def graph(self, tracking: he.EpicNode, dependent: he.EpicNode) -> he.EpicGraph:
        root = self.node(f"https://github.com/{PLANNING}/issues/1")
        return he.EpicGraph(
            root_url=root.url, root=root, children=(tracking, dependent),
            blocked_by={dependent.url: frozenset({tracking.url})},
            fingerprint="f", source="native",
        )

    def test_dependent_stays_blocked_until_the_tracking_issue_is_closed_complete(self) -> None:
        dependent = self.node(f"https://github.com/{PLANNING}/issues/8")
        open_tracking = self.node(TRACKING_URL)
        self.assertFalse(
            he.blockers_satisfied(dependent.url, self.graph(open_tracking, dependent), completed_urls=set())
        )
        closed = self.node(TRACKING_URL, "closed", "completed")
        self.assertTrue(
            he.blockers_satisfied(dependent.url, self.graph(closed, dependent), completed_urls=set())
        )



class TransferAdoptionTests(RoutedBase):
    """A tracking issue transferred to another allowlisted repository.

    The fixture models GitHub resolving the retired URL to the same issue
    (same ``node_id``). That redirect and node-id continuity are assumptions
    to qualify live; see docs/issue-routing.md.
    """

    def setUp(self) -> None:
        super().setUp()
        old_started = "2026-10-20T01:00:00+00:00"
        self.old_started = old_started
        self.gh.issues[f"repos/{PLANNING}/issues/7"] = RoutedGitHub._issue(PLANNING, 7, TRACKING_URL)
        self.gh.issues[f"repos/{BACKLOG}/issues/3"] = RoutedGitHub._issue(PLANNING, 7, TRACKING_URL)
        self.gh.pull = dict(self.gh.pull, head={"sha": HEAD, "ref": OLD_BRANCH})
        self.gh.task_branch = OLD_BRANCH
        self.gh.task_pr = PR_URL
        seed_ledger(self.ledger, OLD_TRACKING_URL, pull_url=PR_URL)
        self.old_checkpoint = hi.Checkpoint(
            version=1, issue_url=OLD_TRACKING_URL, state="READY", root_task_id="t_root",
            pr_url=PR_URL, pr_number=31, clean_head=HEAD, reviewed_head=HEAD,
            repair_rounds=3, started_at=old_started,
            merge_mode="explicit_captain_approval", merge_mode_choice="explicit_captain_approval",
            merge_mode_source="interactive", notes=["op:adopted_root:t_root"],
        )
        hi.save_checkpoint(hi.checkpoint_path(self.checkpoints, OLD_TRACKING_URL), self.old_checkpoint)

    def adopt_transfer(self, old: str = OLD_TRACKING_URL, new: str = TRACKING_URL, policy=None):
        return hi.adopt_transferred_issue(
            old, new, policy=policy or self.policy, ledger=self.ledger,
            checkpoint_dir=self.checkpoints, runner=self.gh, gh="gh", hermes="hermes",
        )

    def ledger_rows(self):
        with sqlite3.connect(self.ledger) as connection:
            return (
                connection.execute("SELECT issue_url, task_id FROM issue_tasks").fetchall(),
                connection.execute(
                    "SELECT issue_url, pull_url, last_event_id, last_repair_task_id"
                    " FROM pull_request_watches"
                ).fetchall(),
            )

    def kanban_writes(self):
        return [c for c in self.gh.calls if Path(c[0]).name == "hermes" and "show" not in c]

    def test_adoption_preserves_root_pr_watch_budget_and_acceptance_hold(self) -> None:
        report = self.adopt_transfer()
        self.assertEqual(report.outcome, "adopted")
        self.assertEqual((report.root_task_id, report.root_status), ("t_root", "done"))
        self.assertEqual(report.implementation_repository, PRODUCT)
        self.assertEqual(report.pr_url, PR_URL)
        self.assertEqual(report.checkpoint, "migrated")
        tasks, watches = self.ledger_rows()
        self.assertEqual(tasks, [(TRACKING_URL, "t_root")])
        self.assertEqual(watches, [(TRACKING_URL, PR_URL, 55, "t_repair")])
        moved = hi.load_checkpoint(hi.checkpoint_path(self.checkpoints, TRACKING_URL))
        assert moved is not None
        self.assertEqual(moved.issue_url, TRACKING_URL)
        for field in ("started_at", "updated_at", "repair_rounds", "merge_mode_choice",
                      "merge_mode_source", "merge_mode", "clean_head", "reviewed_head",
                      "root_task_id", "pr_url", "state"):
            self.assertEqual(getattr(moved, field), getattr(
                hi.load_checkpoint(hi.checkpoint_path(self.checkpoints, OLD_TRACKING_URL)), field), field)
        self.assertEqual(moved.started_at, self.old_started)
        self.assertEqual(moved.repair_rounds, 3)
        self.assertIn("op:transfer_adopted", moved.notes)
        # The old checkpoint is not deleted or rewritten.
        self.assertTrue(hi.checkpoint_path(self.checkpoints, OLD_TRACKING_URL).is_file())
        # Kanban was only read.
        self.assertEqual(self.kanban_writes(), [])
        self.assertEqual(self.gh.mutations, [])

    def test_retained_old_url_and_new_url_never_create_a_second_root(self) -> None:
        self.adopt_transfer()
        for slug, number in ((BACKLOG, 3), (PLANNING, 7)):
            issue = poller.Issue(self.policy.repository_for(slug), number, "t", "b")
            task_id, created = poller.create_task_once(issue, self.policy, self.ledger, self.gh)
            self.assertEqual((task_id, created), ("t_root", False), slug)
        self.assertEqual(self.gh.kanban_creates, [])
        self.assertEqual(self.ledger_rows()[0], [(TRACKING_URL, "t_root")])

    def test_old_url_resolves_to_the_one_current_identity_for_status_and_resume(self) -> None:
        self.adopt_transfer()
        report = hi.status_issue(
            self.policy, OLD_TRACKING_URL, ledger=self.ledger, checkpoint_dir=self.checkpoints,
            runner=self.gh, gh="gh", hermes="hermes",
        )
        self.assertEqual(report.issue_url, TRACKING_URL)
        self.assertEqual(report.root_task_id, "t_root")
        self.assertEqual(report.pr_url, PR_URL)  # ledger-adopted despite the retained branch
        checkpoint, _ = self.adopt(OLD_TRACKING_URL, apply_dispatch=True)
        self.assertEqual(checkpoint.issue_url, TRACKING_URL)
        self.assertEqual(checkpoint.root_task_id, "t_root")
        self.assertEqual(self.gh.kanban_creates, [])
        self.assertEqual(checkpoint.started_at, self.old_started)

    def test_original_budget_is_not_reset_by_adoption(self) -> None:
        old = hi.load_checkpoint(hi.checkpoint_path(self.checkpoints, OLD_TRACKING_URL))
        assert old is not None
        old.started_at = "2026-10-19T00:00:00+00:00"  # long past the 120 minute window
        hi.save_checkpoint(hi.checkpoint_path(self.checkpoints, OLD_TRACKING_URL), old)
        self.adopt_transfer()
        checkpoint, report = self.adopt(TRACKING_URL, apply_dispatch=False)
        self.assertEqual(checkpoint.state, "FAILED")
        self.assertIn("max_issue_runtime_minutes", checkpoint.last_blocker or "")
        self.assertEqual(checkpoint.started_at, "2026-10-19T00:00:00+00:00")

    def test_blocked_do_not_restart_task_stays_blocked_and_untouched(self) -> None:
        self.gh.task_status = "blocked"
        report = self.adopt_transfer()
        self.assertEqual(report.root_status, "blocked")
        self.assertEqual(self.kanban_writes(), [])
        shown = json.loads(self.gh.run(["hermes", "kanban", "--board", "default", "show", "t_root", "--json"]))
        self.assertEqual(shown["task"]["status"], "blocked")

    def test_active_root_is_refused_without_changing_anything(self) -> None:
        for status in ("running", "ready", "todo", "triage"):
            with self.subTest(status=status):
                self.gh.task_status = status
                with self.assertRaises(hi.HelmetIssueError) as raised:
                    self.adopt_transfer()
                self.assertIn("transfer_active_task_unsupported", str(raised.exception))
                self.assertEqual(self.ledger_rows()[0], [(OLD_TRACKING_URL, "t_root")])
                self.assertFalse(hi.checkpoint_path(self.checkpoints, TRACKING_URL).exists())
        self.assertEqual(self.kanban_writes(), [])

    def test_unproven_identity_is_refused(self) -> None:
        cases = {
            "different issue behind the old URL": dict(html_url=f"https://github.com/{PLANNING}/issues/9"),
            "node id mismatch": dict(node_id="I_other"),
            "no node id": dict(node_id=None),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.gh.issues[f"repos/{BACKLOG}/issues/3"] = dict(
                    RoutedGitHub._issue(PLANNING, 7, TRACKING_URL), **change
                )
                with self.assertRaises(hi.HelmetIssueError) as raised:
                    self.adopt_transfer()
                self.assertIn("transfer_identity_unproven", str(raised.exception))
                self.assertEqual(self.ledger_rows()[0], [(OLD_TRACKING_URL, "t_root")])

    def test_destination_must_be_allowlisted_and_routed_to_the_roots_checkout(self) -> None:
        with self.assertRaises(hi.HelmetIssueError) as raised:
            self.adopt_transfer(new=f"https://github.com/{OTHER}/issues/7")
        self.assertIn("transfer_not_allowlisted", str(raised.exception))
        raw = policy_dict(self.root)
        raw["issue_routes"] = [raw["issue_routes"][1]]  # planning -> product removed
        with self.assertRaises(hi.HelmetIssueError) as raised:
            self.adopt_transfer(policy=authority.policy_from_mapping(raw))
        self.assertIn("transfer_route_unconfigured", str(raised.exception))
        self.assertEqual(self.ledger_rows()[0], [(OLD_TRACKING_URL, "t_root")])

    def test_conflicting_roots_and_missing_roots_are_refused(self) -> None:
        with sqlite3.connect(self.ledger) as connection:
            connection.execute(
                "INSERT INTO issue_tasks(issue_url, task_id) VALUES (?, 't_dup')", (TRACKING_URL,)
            )
        with self.assertRaises(hi.HelmetIssueError) as raised:
            self.adopt_transfer()
        self.assertIn("transfer_conflicting_root", str(raised.exception))
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("DELETE FROM issue_tasks WHERE task_id = 't_dup'")
        with self.assertRaises(hi.HelmetIssueError) as raised:
            self.adopt_transfer(old=f"https://github.com/{BACKLOG}/issues/99")
        self.assertIn("transfer_no_root", str(raised.exception))

    def test_pr_outside_the_target_is_refused(self) -> None:
        self.gh.task_pr = f"https://github.com/{OTHER}/pull/5"
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("UPDATE pull_request_watches SET pull_url = ?", (self.gh.task_pr,))
        with self.assertRaises(hi.HelmetIssueError) as raised:
            self.adopt_transfer()
        self.assertIn("transfer_pr_outside_target", str(raised.exception))

    def test_ledger_unavailable_names_the_runtime_prerequisite_without_claiming_migration(self) -> None:
        with self.assertRaises(hi.HelmetIssueError) as raised:
            hi.adopt_transferred_issue(
                OLD_TRACKING_URL, TRACKING_URL, policy=self.policy,
                ledger=self.root / "absent.sqlite3", checkpoint_dir=self.checkpoints,
                runner=self.gh, gh="gh", hermes="hermes",
            )
        self.assertIn("transfer_ledger_unavailable", str(raised.exception))

    def test_retry_is_idempotent_and_completes_an_interrupted_checkpoint_move(self) -> None:
        first = self.adopt_transfer()
        hi.checkpoint_path(self.checkpoints, TRACKING_URL).unlink()  # interrupted after the ledger commit
        second = self.adopt_transfer()
        self.assertEqual((first.outcome, second.outcome), ("adopted", "already_adopted"))
        self.assertEqual(second.checkpoint, "migrated")
        third = self.adopt_transfer()
        self.assertEqual((third.outcome, third.checkpoint), ("already_adopted", "present"))
        moved = hi.load_checkpoint(hi.checkpoint_path(self.checkpoints, TRACKING_URL))
        assert moved is not None
        self.assertEqual(moved.started_at, self.old_started)
        self.assertEqual(moved.notes.count("op:transfer_adopted"), 1)
        self.assertEqual(self.ledger_rows()[0], [(TRACKING_URL, "t_root")])

    def alias_rows(self):
        with sqlite3.connect(self.ledger) as connection:
            return connection.execute("SELECT retired_url, current_url FROM issue_aliases").fetchall()

    def snapshot(self):
        old_file = hi.checkpoint_path(self.checkpoints, OLD_TRACKING_URL)
        new_file = hi.checkpoint_path(self.checkpoints, TRACKING_URL)
        return (
            self.ledger_rows(),
            self.alias_rows(),
            old_file.read_bytes(),
            new_file.read_bytes() if new_file.exists() else None,
        )

    def test_active_same_pr_repair_is_refused_without_changing_anything(self) -> None:
        # The root is done (adoptable) but its latest repair is still in flight.
        before = self.snapshot()
        for status in ("in_progress", "running", "ready", "todo", "triage"):
            with self.subTest(status=status):
                self.gh.repair_status = status
                with self.assertRaises(hi.HelmetIssueError) as raised:
                    self.adopt_transfer()
                self.assertIn("transfer_active_repair_unsupported", str(raised.exception))
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.kanban_writes(), [])
        self.assertEqual(self.gh.mutations, [])
        # The repair task was actually inspected (not only the root).
        shown = [c for c in self.gh.calls if Path(c[0]).name == "hermes" and "t_repair" in c]
        self.assertTrue(shown)

    def test_finished_or_held_repair_does_not_block_adoption(self) -> None:
        for status in ("done", "archived", "blocked"):
            with self.subTest(status=status):
                self.setUp()
                self.gh.repair_status = status
                self.assertEqual(self.adopt_transfer().outcome, "adopted")

    def test_unreadable_repair_status_is_refused_before_any_change(self) -> None:
        before = self.snapshot()
        self.gh.repair_status = ""
        with self.assertRaises(hi.HelmetIssueError):
            self.adopt_transfer()
        self.assertEqual(self.snapshot(), before)

    def seed_destination_checkpoint(self, **overrides) -> bytes:
        from dataclasses import replace

        old = hi.load_checkpoint(hi.checkpoint_path(self.checkpoints, OLD_TRACKING_URL))
        assert old is not None
        destination = replace(old, issue_url=TRACKING_URL, notes=[*old.notes], **overrides)
        path = hi.checkpoint_path(self.checkpoints, TRACKING_URL)
        hi.save_checkpoint(path, destination)
        return path.read_bytes()

    def test_conflicting_destination_checkpoint_is_refused_before_the_ledger_moves(self) -> None:
        conflicts = {
            "started_at": dict(started_at="2026-10-20T02:00:00+00:00"),
            "root_task_id": dict(root_task_id="t_other"),
            "merge hold": dict(
                merge_mode="unattended_when_clean",
                merge_mode_choice="unattended_when_clean",
                merge_mode_source="policy",
            ),
            "review head": dict(reviewed_head="2" * 14, clean_head=None),
            "state": dict(state="REVIEW_HEAD"),
            "repair budget": dict(repair_rounds=0),
        }
        for name, overrides in conflicts.items():
            with self.subTest(name):
                self.setUp()
                seeded = self.seed_destination_checkpoint(**overrides)
                before = self.snapshot()
                with self.assertRaises(hi.HelmetIssueError) as raised:
                    self.adopt_transfer()
                self.assertIn("transfer_conflicting_checkpoint", str(raised.exception))
                # Nothing moved: no re-keyed rows, no alias, destination untouched.
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.ledger_rows()[0], [(OLD_TRACKING_URL, "t_root")])
                self.assertEqual(self.alias_rows(), [])
                self.assertEqual(
                    hi.checkpoint_path(self.checkpoints, TRACKING_URL).read_bytes(), seeded
                )
                self.assertEqual(self.kanban_writes(), [])
        # The old URL keeps resolving to the old identity, not the conflict.
        self.assertEqual(hi._current_tracking_url(self.ledger, OLD_TRACKING_URL), OLD_TRACKING_URL)

    def test_unreadable_destination_checkpoint_is_refused_before_the_ledger_moves(self) -> None:
        hi.checkpoint_path(self.checkpoints, TRACKING_URL).write_text("{not json", encoding="utf-8")
        before = self.snapshot()
        with self.assertRaises(hi.HelmetIssueError):
            self.adopt_transfer()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.alias_rows(), [])

    def test_agreeing_preexisting_destination_checkpoint_is_accepted(self) -> None:
        seeded = self.seed_destination_checkpoint()
        report = self.adopt_transfer()
        self.assertEqual((report.outcome, report.checkpoint), ("adopted", "present"))
        self.assertEqual(hi.checkpoint_path(self.checkpoints, TRACKING_URL).read_bytes(), seeded)

    def test_progressed_destination_after_adoption_is_not_a_conflict_on_retry(self) -> None:
        self.adopt_transfer()
        moved = hi.load_checkpoint(hi.checkpoint_path(self.checkpoints, TRACKING_URL))
        assert moved is not None
        moved.state = "REVIEW_HEAD"
        moved.repair_rounds = 4
        hi.save_checkpoint(hi.checkpoint_path(self.checkpoints, TRACKING_URL), moved)
        again = self.adopt_transfer()
        self.assertEqual((again.outcome, again.checkpoint), ("already_adopted", "present"))
        # An identity mismatch is still refused on retry.
        moved.started_at = "2026-10-20T05:00:00+00:00"
        hi.save_checkpoint(hi.checkpoint_path(self.checkpoints, TRACKING_URL), moved)
        with self.assertRaises(hi.HelmetIssueError):
            self.adopt_transfer()

    def test_cli_adopt_transfer(self) -> None:
        config = self.root / "policy.json"
        config.write_text(json.dumps(policy_dict(self.root)), encoding="utf-8")
        with mock.patch.object(helmet_cli, "SubprocessRunner", return_value=self.gh):
            code = helmet_cli.main(
                [
                    "adopt-transfer", OLD_TRACKING_URL, TRACKING_URL, "--config", str(config),
                    "--ledger", str(self.ledger), "--checkpoint-dir", str(self.checkpoints), "--json",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(self.ledger_rows()[0], [(TRACKING_URL, "t_root")])

    def test_acceptance_after_transfer_uses_the_adopted_identity(self) -> None:
        self.gh.merge_prs()
        self.adopt_transfer()
        report = self.accept(url=OLD_TRACKING_URL)
        self.assertEqual(report.decision, "closed")
        self.assertEqual(report.issue_url, TRACKING_URL)


if __name__ == "__main__":
    unittest.main()
