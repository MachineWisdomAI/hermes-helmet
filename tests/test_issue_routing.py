"""Explicit tracking-issue -> implementation-repository routing (policy + poller).

Fixtures use generic organizations only. ``example-org/planning`` is the
tracking repository that holds internal acceptance; ``example-org/product`` is
the implementation repository that receives the code pull request.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hermes_helmet import authority  # noqa: E402
from hermes_helmet import github_issue_poller as poller  # noqa: E402

ORG = "example-org"
PLANNING = f"{ORG}/planning"
PRODUCT = f"{ORG}/product"
OTHER = f"{ORG}/other"
ROUTE_LABEL = "route:product"
TRACKING_URL = f"https://github.com/{PLANNING}/issues/7"
PRODUCT_PR = f"https://github.com/{PRODUCT}/pull/31"
WORKER = "agent-bot"


def policy_dict(*, routes: object = "default", root: Path | None = None, **extra: object) -> dict:
    base = root or Path("/opt/data/repos")
    raw: dict[str, object] = {
        "version": 2,
        "company": {"display_name": "ExampleCo", "slug": "exampleco"},
        "captain_github_login": "example-captain",
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
        "repositories": [
            {"slug": PLANNING, "worktree": str(base / "planning")},
            {"slug": PRODUCT, "worktree": str(base / "product")},
        ],
    }
    if routes == "default":
        routes = [
            {
                "label": ROUTE_LABEL,
                "tracking_repository": PLANNING,
                "implementation_repository": PRODUCT,
            }
        ]
    if routes is not None:
        raw["issue_routes"] = routes
    raw.update(extra)
    return raw


def make_policy(**kwargs: object) -> authority.Policy:
    return authority.policy_from_mapping(policy_dict(**kwargs))


class RoutePolicyTests(unittest.TestCase):
    def test_default_policy_has_no_routes_and_keeps_same_repository_behavior(self) -> None:
        policy = make_policy(routes=None)
        self.assertEqual(policy.issue_routes, ())
        self.assertIsNone(policy.route_for_labels(PLANNING, [ROUTE_LABEL]))
        self.assertNotIn("issue_routes", authority.authority_public_dict(policy))

    def test_route_applies_only_to_labeled_tracking_issues(self) -> None:
        policy = make_policy()
        route = policy.route_for_labels(PLANNING, ["hermes-kanban-go", ROUTE_LABEL.upper()])
        assert route is not None
        self.assertEqual(route.tracking.slug, PLANNING)
        self.assertEqual(route.implementation.slug, PRODUCT)
        # A planning-repository documentation issue keeps its own repository.
        self.assertIsNone(policy.route_for_labels(PLANNING, ["hermes-kanban-go"]))
        # The route belongs to the tracking repository only.
        self.assertIsNone(policy.route_for_labels(PRODUCT, [ROUTE_LABEL]))

    def test_adoption_target_must_be_a_configured_route_target(self) -> None:
        policy = make_policy()
        route = policy.route_for_target(PLANNING, PRODUCT.upper())
        assert route is not None and route.implementation.slug == PRODUCT
        self.assertIsNone(policy.route_for_target(PLANNING, OTHER))
        self.assertIsNone(policy.route_for_target(PRODUCT, PLANNING))

    def test_round_trips_through_public_authority_document(self) -> None:
        policy = make_policy()
        public = authority.authority_public_dict(policy)
        self.assertEqual(
            public["issue_routes"],
            [
                {
                    "label": ROUTE_LABEL,
                    "tracking_repository": PLANNING,
                    "implementation_repository": PRODUCT,
                }
            ],
        )
        self.assertEqual(authority.policy_from_mapping(public).issue_routes, policy.issue_routes)

    def test_rejects_unconfigured_or_unsafe_routes(self) -> None:
        bad_routes = {
            "unallowlisted implementation": {
                "label": ROUTE_LABEL,
                "tracking_repository": PLANNING,
                "implementation_repository": OTHER,
            },
            "unallowlisted tracking": {
                "label": ROUTE_LABEL,
                "tracking_repository": OTHER,
                "implementation_repository": PRODUCT,
            },
            "same repository": {
                "label": ROUTE_LABEL,
                "tracking_repository": PLANNING,
                "implementation_repository": PLANNING,
            },
            "dispatch label as selector": {
                "label": "hermes-kanban-go",
                "tracking_repository": PLANNING,
                "implementation_repository": PRODUCT,
            },
            "ready label as selector": {
                "label": "Ready-For-Agent",
                "tracking_repository": PLANNING,
                "implementation_repository": PRODUCT,
            },
            "blank label": {
                "label": "  ",
                "tracking_repository": PLANNING,
                "implementation_repository": PRODUCT,
            },
            "unknown key": {
                "label": ROUTE_LABEL,
                "tracking_repository": PLANNING,
                "implementation_repository": PRODUCT,
                "pr_title": "x",
            },
            "secret-shaped key": {
                "label": ROUTE_LABEL,
                "tracking_repository": PLANNING,
                "implementation_repository": PRODUCT,
                "token": "x",
            },
            "malformed slug": {
                "label": ROUTE_LABEL,
                "tracking_repository": "planning",
                "implementation_repository": PRODUCT,
            },
        }
        for name, route in bad_routes.items():
            with self.subTest(name):
                with self.assertRaises(authority.AuthorityError) as raised:
                    make_policy(routes=[route])
                self.assertIn("issue_routes", str(raised.exception))
        with self.assertRaises(authority.AuthorityError):
            make_policy(routes={"label": ROUTE_LABEL})
        duplicate = [
            {"label": ROUTE_LABEL, "tracking_repository": PLANNING, "implementation_repository": PRODUCT},
            {"label": ROUTE_LABEL.upper(), "tracking_repository": PLANNING.upper(), "implementation_repository": PRODUCT},
        ]
        with self.assertRaises(authority.AuthorityError):
            make_policy(routes=duplicate)

    def test_two_labels_selecting_different_targets_are_refused(self) -> None:
        raw = policy_dict(
            routes=[
                {"label": "route:product", "tracking_repository": PLANNING, "implementation_repository": PRODUCT},
                {"label": "route:other", "tracking_repository": PLANNING, "implementation_repository": OTHER},
            ]
        )
        raw["repositories"].append({"slug": OTHER, "worktree": "/opt/data/repos/other"})
        policy = authority.policy_from_mapping(raw)
        with self.assertRaises(authority.AuthorityError):
            policy.route_for_labels(PLANNING, ["route:product", "route:other"])
        only = policy.route_for_labels(PLANNING, ["route:other"])
        assert only is not None and only.implementation.slug == OTHER

    def test_routed_branch_is_opaque_and_stable(self) -> None:
        branch = authority.routed_branch(TRACKING_URL)
        self.assertRegex(branch, r"^automation/tracked-[0-9a-f]{12}$")
        self.assertEqual(branch, authority.routed_branch(TRACKING_URL))
        self.assertNotEqual(branch, authority.routed_branch(TRACKING_URL.replace("/7", "/8")))
        for private in ("planning", ORG, "issues"):
            self.assertNotIn(private, branch)


class RouteInstructionTests(unittest.TestCase):
    def test_routed_task_body_names_route_and_forbids_public_exposure(self) -> None:
        policy = make_policy()
        route = policy.route_for_labels(PLANNING, [ROUTE_LABEL])
        assert route is not None
        body = authority.render_issue_task_body(
            issue_url=TRACKING_URL,
            issue_body="Private acceptance criteria.",
            policy=policy,
            route=route,
            branch=authority.routed_branch(TRACKING_URL),
        )
        self.assertIn(TRACKING_URL, body)  # the private Kanban card keeps its source
        self.assertIn("Private acceptance criteria.", body)
        self.assertIn(f"Implementation repository: {PRODUCT}", body)
        self.assertIn(authority.routed_branch(TRACKING_URL), body)
        public_section = body.split("## Public pull-request privacy contract", 1)[1]
        self.assertNotIn(TRACKING_URL, public_section)
        self.assertNotIn("Private acceptance criteria.", public_section)
        for needle in ("title", "body", "commit message", "branch", "closing keyword", "approval"):
            self.assertIn(needle, public_section)
        self.assertIn("do not close the tracking issue", public_section.casefold())

    def test_same_repository_task_body_is_unchanged(self) -> None:
        policy = make_policy(routes=None)
        body = authority.render_issue_task_body(
            issue_url=TRACKING_URL, issue_body="Do it.", policy=policy
        )
        self.assertNotIn("Implementation route", body)
        self.assertNotIn("privacy contract", body)

    def test_repair_body_repeats_privacy_contract_for_routed_pull_requests(self) -> None:
        plain = authority.render_repair_task_body(
            pull_url=PRODUCT_PR, event_kind="reviewed", event_state="changes_requested",
            event_actor="reviewer", event_url=PRODUCT_PR + "#pullrequestreview-1",
        )
        self.assertNotIn("privacy", plain)
        routed = authority.render_repair_task_body(
            pull_url=PRODUCT_PR, event_kind="reviewed", event_state="changes_requested",
            event_actor="reviewer", event_url=PRODUCT_PR + "#pullrequestreview-1",
            routed=True,
        )
        self.assertIn("privacy", routed)
        self.assertNotIn(TRACKING_URL, routed)


class PollerRoutingRunner:
    """Minimal gh/git/hermes fake for the routed poller paths."""

    def __init__(self, root: Path, *, planning_issues=(), product_issues=(), open_pulls=None,
                 timeline=None, root_status="done", root_workspace=None, root_pr=PRODUCT_PR) -> None:
        self.root = root
        self.planning_issues = list(planning_issues)
        self.product_issues = list(product_issues)
        self.open_pulls = open_pulls if open_pulls is not None else {PLANNING: [], PRODUCT: []}
        self.timeline = timeline or {}
        self.root_status = root_status
        self.root_workspace = root_workspace or root / "product" / ".worktrees" / "t_root"
        self.root_pr = root_pr
        self.calls: list[list[str]] = []
        self.creates: list[list[str]] = []
        self.ids: dict[str, str] = {}

    def run(self, command):
        command = list(command)
        self.calls.append(command)
        if command[:3] == [poller.GH, "api", "user"]:
            return WORKER + "\n"
        if command[:3] == [poller.GH, "api", "--paginate"]:
            endpoint = command[-1]
            if endpoint.startswith(f"repos/{PLANNING}/issues?"):
                return json.dumps(self.planning_issues)
            if endpoint.startswith(f"repos/{PRODUCT}/issues?"):
                return json.dumps(self.product_issues)
            if "/issues?" in endpoint:
                return "[]"
            for slug, pulls in self.open_pulls.items():
                if endpoint.startswith(f"repos/{slug}/pulls?"):
                    return json.dumps(pulls)
            if endpoint in self.timeline:
                return json.dumps(self.timeline[endpoint])
            if "/pulls?" in endpoint:
                return "[]"
        if command[:4] == ["git", "-C", command[2], "rev-parse"]:
            return "true\n"
        if command[:5] == [poller.HERMES, "kanban", "--board", "default", "show"]:
            task_id = command[5]
            runs = []
            if task_id == "t_root" and self.root_pr:
                runs.append({"id": 1, "status": "done", "metadata": {"published_pr": self.root_pr}})
            return json.dumps(
                {
                    "task": {
                        "id": task_id,
                        "status": self.root_status if task_id == "t_root" else "done",
                        "assignee": "builder",
                        "workspace_kind": "worktree",
                        "workspace_path": str(self.root_workspace),
                        "branch_name": authority.routed_branch(TRACKING_URL),
                    },
                    "runs": runs,
                }
            )
        if command[:4] == [poller.HERMES, "kanban", "--board", "default"] and "create" in command:
            self.creates.append(command)
            key = command[command.index("--idempotency-key") + 1]
            return json.dumps({"id": self.ids.setdefault(key, f"t_new_{len(self.ids) + 1}")})
        raise AssertionError(command)


def issue_record(number: int, labels: tuple[str, ...], *, title="Add widget", body="Ship it.") -> dict:
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": "open",
        "labels": [{"name": name} for name in ("hermes-kanban-go", *labels)],
    }


def arg(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


class RoutedDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for name in ("planning", "product"):
            (self.root / name).mkdir()
        self.policy = make_policy(root=self.root)
        self.ledger = self.root / "state" / "ledger.sqlite3"

    def dispatch(self, runner: PollerRoutingRunner) -> poller.PollResult:
        return poller.run_once(self.policy, self.ledger, runner)

    def test_labeled_tracking_issue_dispatches_to_the_implementation_checkout(self) -> None:
        runner = PollerRoutingRunner(
            self.root, planning_issues=[issue_record(7, (ROUTE_LABEL,), body="Private acceptance.")]
        )
        result = self.dispatch(runner)
        self.assertEqual(result.errors, [])
        (create,) = runner.creates
        self.assertEqual(arg(create, "--workspace"), f"worktree:{self.root / 'product'}")
        self.assertEqual(arg(create, "--branch"), authority.routed_branch(TRACKING_URL))
        self.assertEqual(arg(create, "--completion-contract"), PRODUCT)
        self.assertEqual(arg(create, "--idempotency-key"), TRACKING_URL)
        self.assertEqual(arg(create, "--created-by"), poller.CREATED_BY)
        self.assertTrue(arg(create, "--body").startswith("## Source issue"))
        self.assertIn("Public pull-request privacy contract", arg(create, "--body"))
        self.assertEqual(create[5], f"{PLANNING}#7: Add widget")
        with sqlite3.connect(self.ledger) as connection:
            rows = connection.execute("SELECT issue_url, task_id FROM issue_tasks").fetchall()
        self.assertEqual(rows, [(TRACKING_URL, "t_new_1")])

    def test_local_only_contract_is_preserved_for_routed_tasks(self) -> None:
        self.policy = make_policy(root=self.root, worker_completion_contract="local-only")
        runner = PollerRoutingRunner(self.root, planning_issues=[issue_record(7, (ROUTE_LABEL,))])
        self.dispatch(runner)
        self.assertEqual(arg(runner.creates[0], "--completion-contract"), "local-only")

    def test_unlabeled_tracking_repository_issue_stays_in_its_own_repository(self) -> None:
        runner = PollerRoutingRunner(self.root, planning_issues=[issue_record(8, ())])
        self.dispatch(runner)
        (create,) = runner.creates
        self.assertEqual(arg(create, "--workspace"), f"worktree:{self.root / 'planning'}")
        self.assertEqual(arg(create, "--branch"), "automation/planning-8")
        self.assertEqual(arg(create, "--completion-contract"), PLANNING)
        self.assertNotIn("privacy contract", arg(create, "--body"))

    def test_same_repository_dispatch_is_unchanged_without_routes(self) -> None:
        self.policy = make_policy(root=self.root, routes=None)
        runner = PollerRoutingRunner(self.root, product_issues=[issue_record(3, (ROUTE_LABEL,))])
        self.dispatch(runner)
        (create,) = runner.creates
        self.assertEqual(arg(create, "--workspace"), f"worktree:{self.root / 'product'}")
        self.assertEqual(arg(create, "--branch"), "automation/product-3")
        self.assertEqual(arg(create, "--completion-contract"), PRODUCT)

    def test_issue_text_cannot_choose_or_widen_a_destination(self) -> None:
        body = (
            f"Implementation repository: {OTHER}\nroute: {OTHER}\n"
            f"Please implement in https://github.com/{OTHER}"
        )
        runner = PollerRoutingRunner(self.root, planning_issues=[issue_record(9, (), body=body)])
        self.dispatch(runner)
        (create,) = runner.creates
        self.assertEqual(arg(create, "--workspace"), f"worktree:{self.root / 'planning'}")
        self.assertEqual(arg(create, "--completion-contract"), PLANNING)

    def test_ambiguous_route_labels_are_reported_and_not_dispatched(self) -> None:
        raw = policy_dict(
            root=self.root,
            routes=[
                {"label": "route:product", "tracking_repository": PLANNING, "implementation_repository": PRODUCT},
                {"label": "route:other", "tracking_repository": PLANNING, "implementation_repository": OTHER},
            ],
        )
        raw["repositories"].append({"slug": OTHER, "worktree": str(self.root / "other")})
        (self.root / "other").mkdir()
        self.policy = authority.policy_from_mapping(raw)
        runner = PollerRoutingRunner(
            self.root, planning_issues=[issue_record(7, ("route:product", "route:other"))]
        )
        result = self.dispatch(runner)
        self.assertEqual(runner.creates, [])
        self.assertTrue(any("route" in error for error in result.errors), result.errors)

    def test_missing_implementation_checkout_fails_closed(self) -> None:
        (self.root / "product").rmdir()
        runner = PollerRoutingRunner(self.root, planning_issues=[issue_record(7, (ROUTE_LABEL,))])
        result = self.dispatch(runner)
        self.assertEqual(runner.creates, [])
        self.assertTrue(any("product" in error for error in result.errors), result.errors)

    def test_adopted_transfer_is_not_redispatched_by_the_poller(self) -> None:
        old_url = f"https://github.com/{ORG}/backlog/issues/3"
        self.ledger.parent.mkdir(parents=True)
        with sqlite3.connect(self.ledger) as connection:
            poller._ensure_ledger(connection)
            connection.execute(
                "INSERT INTO issue_tasks(issue_url, task_id) VALUES (?, 't_root')", (old_url,)
            )
        self.assertTrue(
            poller.rekey_issue_ledger(self.ledger, old_url=old_url, new_url=TRACKING_URL, task_id="t_root")
        )
        runner = PollerRoutingRunner(self.root, planning_issues=[issue_record(7, (ROUTE_LABEL,))])
        result = self.dispatch(runner)
        self.assertEqual((result.created, result.errors, runner.creates), ([], [], []))
        # Re-running the validated re-key is a no-op; a conflicting one is refused.
        self.assertFalse(
            poller.rekey_issue_ledger(self.ledger, old_url=old_url, new_url=TRACKING_URL, task_id="t_root")
        )
        with self.assertRaises(poller.PollerError):
            poller.rekey_issue_ledger(self.ledger, old_url=old_url, new_url=TRACKING_URL, task_id="t_other")

    def test_dispatch_is_idempotent_for_the_tracking_url(self) -> None:
        runner = PollerRoutingRunner(self.root, planning_issues=[issue_record(7, (ROUTE_LABEL,))])
        self.dispatch(runner)
        self.dispatch(runner)
        self.assertEqual(len(runner.creates), 1)


class RoutedReviewReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for name in ("planning", "product"):
            (self.root / name).mkdir()
        self.policy = make_policy(root=self.root, trusted_review_bots=[])
        self.ledger = self.root / "ledger.sqlite3"
        with sqlite3.connect(self.ledger) as connection:
            poller._ensure_ledger(connection)
            connection.execute(
                "INSERT INTO issue_tasks(issue_url, task_id) VALUES (?, ?)",
                (TRACKING_URL, "t_root"),
            )
        (self.root / "product" / ".worktrees" / "t_root").mkdir(parents=True)
        self.review = {
            "id": 4001,
            "event": "reviewed",
            "state": "changes_requested",
            "submitted_at": "2026-10-20T10:00:00Z",
            "html_url": f"{PRODUCT_PR}#pullrequestreview-4001",
            "user": {"login": "trusted-reviewer", "type": "User"},
            "author_association": "MEMBER",
        }
        self.pull_listing = {PLANNING: [], PRODUCT: [{"number": 31, "state": "open", "html_url": PRODUCT_PR}]}

    def runner(self, **kwargs) -> PollerRoutingRunner:
        return PollerRoutingRunner(
            self.root,
            open_pulls=self.pull_listing,
            timeline={f"repos/{PRODUCT}/issues/31/timeline?per_page=100": [self.review]},
            **kwargs,
        )

    def test_review_on_foreign_pr_creates_repair_in_target_checkout_on_original_branch(self) -> None:
        runner = self.runner()
        task_ids, errors = poller.reconcile_review_tasks(self.policy, self.ledger, runner)
        self.assertEqual(errors, [])
        self.assertEqual(task_ids, ["t_new_1"])
        (create,) = runner.creates
        self.assertEqual(
            arg(create, "--workspace"),
            f"worktree:{self.root / 'product' / '.worktrees' / 't_root'}",
        )
        self.assertEqual(arg(create, "--branch"), authority.routed_branch(TRACKING_URL))
        self.assertEqual(arg(create, "--completion-contract"), PRODUCT)
        self.assertEqual(arg(create, "--parent"), "t_root")
        self.assertIn(PRODUCT_PR, arg(create, "--body"))
        self.assertIn("privacy", arg(create, "--body"))
        self.assertNotIn(TRACKING_URL, arg(create, "--body"))
        self.assertIn(f"{PLANNING}#7", create[5])
        self.assertEqual(
            arg(create, "--idempotency-key"),
            f"github-pr-event:{PRODUCT}:31:reviewed:4001",
        )
        # Retry is idempotent: the watch cursor advanced and the repair is outstanding.
        again = poller.reconcile_review_tasks(self.policy, self.ledger, runner)
        self.assertEqual(again, ([], []))
        self.assertEqual(len(runner.creates), 1)

    def test_pr_in_an_unconfigured_repository_is_refused(self) -> None:
        other_pr = f"https://github.com/{OTHER}/pull/5"
        runner = self.runner(root_pr=other_pr)
        task_ids, errors = poller.reconcile_review_tasks(self.policy, self.ledger, runner)
        self.assertEqual(task_ids, [])
        self.assertEqual(runner.creates, [])
        self.assertTrue(any("outside" in error for error in errors), errors)

    def test_same_repository_task_cannot_adopt_a_foreign_pr(self) -> None:
        # Root dispatched in the planning checkout (no route label) publishing in product.
        runner = self.runner(root_workspace=self.root / "planning" / ".worktrees" / "t_root")
        (self.root / "planning" / ".worktrees" / "t_root").mkdir(parents=True)
        task_ids, errors = poller.reconcile_review_tasks(self.policy, self.ledger, runner)
        self.assertEqual(task_ids, [])
        self.assertEqual(runner.creates, [])
        self.assertTrue(any("workspace" in error for error in errors), errors)

    def test_unrouted_policy_still_rejects_foreign_pr(self) -> None:
        policy = make_policy(root=self.root, routes=None)
        runner = self.runner()
        task_ids, errors = poller.reconcile_review_tasks(policy, self.ledger, runner)
        self.assertEqual(task_ids, [])
        self.assertTrue(any("outside" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
