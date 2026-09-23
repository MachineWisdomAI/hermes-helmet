from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hermes_helmet.authority import merge_authority_for, policy_from_mapping  # noqa: E402
from hermes_helmet import helmet_epic as he  # noqa: E402
from hermes_helmet import helmet_issue as hi  # noqa: E402
from hermes_helmet import cli as helmet_cli  # noqa: E402
from hermes_helmet import install_skills as skills  # noqa: E402


FIXTURE_ORG = "example-org"
FIXTURE_REPO = "demo-repo"
FIXTURE_SLUG = f"{FIXTURE_ORG}/{FIXTURE_REPO}"
EPIC_URL = f"https://github.com/{FIXTURE_SLUG}/issues/42"
CHILD_A = f"https://github.com/{FIXTURE_SLUG}/issues/100"
CHILD_B = f"https://github.com/{FIXTURE_SLUG}/issues/101"
CHILD_C = f"https://github.com/{FIXTURE_SLUG}/issues/102"
CHILD_H = f"https://github.com/{FIXTURE_SLUG}/issues/116"
BLOCKER_EXT = f"https://github.com/{FIXTURE_SLUG}/issues/26"
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
        "max_epic_parallelism": 2,
        "repositories": [
            {"slug": FIXTURE_SLUG, "worktree": "/opt/data/repos/demo-repo"}
        ],
    }
    base.update(overrides)
    return base


def _policy(**overrides: object):
    return policy_from_mapping(_policy_dict(**overrides))


def _issue_payload(
    number: int,
    *,
    title: str,
    body: str,
    state: str = "open",
    state_reason: str | None = None,
    labels: list[str] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "html_url": f"https://github.com/{FIXTURE_SLUG}/issues/{number}",
        "labels": [{"name": name} for name in (labels or [])],
    }
    if state_reason is not None:
        payload["state_reason"] = state_reason
    return payload


class FakeEpicRunner:
    def __init__(self) -> None:
        self.identity = CAPTAIN
        self.issues: dict[int, dict[str, object]] = {}
        self.sub_issues: dict[int, object] = {}
        self.blocked_by: dict[int, object] = {}
        self.sub_issues_http: dict[int, int] = {}
        self.blocked_by_http: dict[int, int] = {}
        # Child number → parent issue payload, or omit / parent_http for 404.
        self.parent_of: dict[int, dict[str, object]] = {}
        self.parent_http: dict[int, int] = {}
        self.search_items: list[dict[str, object]] = []
        self.search_incomplete: bool = False
        self.search_total_count: int | None = None
        self.search_fail: bool = False
        self.search_fail_once: int = 0
        self.calls: list[list[str]] = []
        self.labels_posted: list[str] = []

    def add_issue(self, payload: dict[str, object]) -> None:
        self.issues[int(payload["number"])] = payload

    def run(self, command: list[str]) -> str:
        self.calls.append(list(command))
        if command[0].endswith("gh") and command[1] == "api":
            if "--method" in command and "labels" in "".join(command):
                self.labels_posted.append(" ".join(command))
                return "[]"
            endpoint = command[-1]
            path = endpoint.split("?", 1)[0]
            if endpoint == "user" or path == "user":
                return json.dumps({"login": self.identity})
            if path.startswith("search/issues") or endpoint.startswith("search/issues?"):
                if self.search_fail or self.search_fail_once > 0:
                    if self.search_fail_once > 0:
                        self.search_fail_once -= 1
                    raise hi.HelmetIssueError(
                        hi.checkpoint_note(
                            "err", "command_failed", "gh", "exit1", "http500"
                        )
                    )
                payload: dict[str, object] = {
                    "items": self.search_items,
                    "incomplete_results": self.search_incomplete,
                }
                if self.search_total_count is not None:
                    payload["total_count"] = self.search_total_count
                else:
                    payload["total_count"] = len(self.search_items)
                return json.dumps(payload)
            if path.endswith("/parent"):
                number = int(path.rstrip("/").split("/")[-2])
                if number in self.parent_http:
                    code = self.parent_http[number]
                    raise hi.HelmetIssueError(
                        hi.checkpoint_note(
                            "err", "command_failed", "gh", "exit1", f"http{code}"
                        )
                    )
                if number not in self.parent_of:
                    raise hi.HelmetIssueError(
                        hi.checkpoint_note(
                            "err", "command_failed", "gh", "exit1", "http404"
                        )
                    )
                return json.dumps(self.parent_of[number])
            if "/sub_issues" in path:
                number = int(path.rstrip("/").split("/")[-2])
                if number in self.sub_issues_http:
                    code = self.sub_issues_http[number]
                    raise hi.HelmetIssueError(
                        hi.checkpoint_note(
                            "err", "command_failed", "gh", "exit1", f"http{code}"
                        )
                    )
                return json.dumps(self.sub_issues.get(number, []))
            if "/dependencies/" in path:
                number = int(path.rstrip("/").split("/")[-3])
                if number in self.blocked_by_http:
                    code = self.blocked_by_http[number]
                    raise hi.HelmetIssueError(
                        hi.checkpoint_note(
                            "err", "command_failed", "gh", "exit1", f"http{code}"
                        )
                    )
                return json.dumps(self.blocked_by.get(number, []))
            if path.startswith(f"repos/{FIXTURE_SLUG}/issues/"):
                number = int(path.rsplit("/", 1)[-1])
                if number not in self.issues:
                    raise hi.HelmetIssueError(
                        hi.checkpoint_note(
                            "err", "command_failed", "gh", "exit1", "http404"
                        )
                    )
                return json.dumps(self.issues[number])
        raise hi.HelmetIssueError(
            hi.checkpoint_note("err", "command_failed", "unknown", "exit1")
        )


class ParseLinkTests(unittest.TestCase):
    def test_parent_and_blocked_by_sections(self) -> None:
        body = f"""## Parent

- [Epic]({EPIC_URL})

## Blocked by

- [W3b]({BLOCKER_EXT})
- {CHILD_A}

## Other

Ignore #99 prose ordering.
"""
        self.assertEqual(
            he.parse_parent_links(body, repository_slug=FIXTURE_SLUG),
            (EPIC_URL,),
        )
        self.assertEqual(
            he.parse_blocked_by_links(body, repository_slug=FIXTURE_SLUG),
            (BLOCKER_EXT, CHILD_A),
        )

    def test_line_forms(self) -> None:
        body = f"Parent: {EPIC_URL}\nBlocked by: {CHILD_A}\n"
        self.assertEqual(
            he.parse_parent_links(body, repository_slug=FIXTURE_SLUG),
            (EPIC_URL,),
        )
        self.assertEqual(
            he.parse_blocked_by_links(body, repository_slug=FIXTURE_SLUG),
            (CHILD_A,),
        )

    def test_same_repo_hash_refs_resolve(self) -> None:
        body = "## Parent\n\n- #42\n\n## Blocked by\n\n- #100\n"
        self.assertEqual(
            he.parse_parent_links(body, repository_slug=FIXTURE_SLUG),
            (EPIC_URL,),
        )
        self.assertEqual(
            he.parse_blocked_by_links(body, repository_slug=FIXTURE_SLUG),
            (CHILD_A,),
        )

    def test_malformed_blocked_by_rejected(self) -> None:
        body = "## Blocked by\n\n- not-a-valid-ref\n"
        with self.assertRaises(he.HelmetEpicError) as ctx:
            he.parse_blocked_by_links(body, repository_slug=FIXTURE_SLUG)
        self.assertIn("malformed_issue_link", str(ctx.exception))

    def test_relative_hash_without_repo_context_fails_closed(self) -> None:
        with self.assertRaises(TypeError):
            # repository_slug is required
            he.parse_parent_links("## Parent\n\n- #42\n")  # type: ignore[call-arg]


class GraphValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = _policy()
        self.runner = FakeEpicRunner()
        self.runner.add_issue(
            _issue_payload(
                42,
                title="Epic root",
                body="Train root\n",
                labels=["ready-for-agent"],
            )
        )
        self.runner.add_issue(
            _issue_payload(
                100,
                title="Linear A",
                body=f"## Parent\n\n- {EPIC_URL}\n",
                labels=["ready-for-agent"],
            )
        )
        self.runner.add_issue(
            _issue_payload(
                101,
                title="Linear B",
                body=(
                    f"## Parent\n\n- {EPIC_URL}\n\n"
                    f"## Blocked by\n\n- {CHILD_A}\n"
                ),
                labels=["ready-for-agent"],
            )
        )
        self.runner.add_issue(
            _issue_payload(
                102,
                title="Parallel C",
                body=f"## Parent\n\n- {EPIC_URL}\n",
                labels=["ready-for-agent"],
            )
        )
        self.runner.add_issue(
            _issue_payload(
                26,
                title="External blocker",
                body="done",
                state="closed",
                state_reason="completed",
            )
        )
        self.runner.add_issue(
            _issue_payload(
                116,
                title="Human-only public launch",
                body=(
                    f"## Parent\n\n- {EPIC_URL}\n\n"
                    "Human-only; never dispatch.\n"
                ),
                labels=["ready-for-agent"],
            )
        )

    def test_body_fallback_linear_and_parallel_graph(self) -> None:
        graph = he.build_epic_graph(
            self.policy,
            EPIC_URL,
            self.runner,
            extra_child_urls=[CHILD_A, CHILD_B, CHILD_C],
        )
        self.assertEqual(graph.source, "body")
        self.assertEqual(len(graph.children), 3)
        self.assertEqual(graph.blocked_by[CHILD_B], frozenset({CHILD_A}))
        self.assertEqual(graph.blocked_by[CHILD_A], frozenset())
        self.assertEqual(graph.blocked_by[CHILD_C], frozenset())

    def test_rejects_cycle(self) -> None:
        self.runner.issues[100]["body"] = (
            f"## Parent\n\n- {EPIC_URL}\n\n## Blocked by\n\n- {CHILD_B}\n"
        )
        self.runner.issues[101]["body"] = (
            f"## Parent\n\n- {EPIC_URL}\n\n## Blocked by\n\n- {CHILD_A}\n"
        )
        with self.assertRaises(he.HelmetEpicError) as ctx:
            he.build_epic_graph(
                self.policy,
                EPIC_URL,
                self.runner,
                extra_child_urls=[CHILD_A, CHILD_B],
            )
        self.assertIn("dependency_cycle", str(ctx.exception))

    def test_rejects_self_edge(self) -> None:
        self.runner.issues[100]["body"] = (
            f"## Parent\n\n- {EPIC_URL}\n\n## Blocked by\n\n- {CHILD_A}\n"
        )
        with self.assertRaises(he.HelmetEpicError) as ctx:
            he.build_epic_graph(
                self.policy,
                EPIC_URL,
                self.runner,
                extra_child_urls=[CHILD_A],
            )
        self.assertIn("self_edge", str(ctx.exception))

    def test_rejects_incomplete_closed_blocker(self) -> None:
        self.runner.issues[26]["state"] = "closed"
        self.runner.issues[26]["state_reason"] = "not_planned"
        self.runner.issues[100]["body"] = (
            f"## Parent\n\n- {EPIC_URL}\n\n## Blocked by\n\n- {BLOCKER_EXT}\n"
        )
        with self.assertRaises(he.HelmetEpicError) as ctx:
            he.build_epic_graph(
                self.policy,
                EPIC_URL,
                self.runner,
                extra_child_urls=[CHILD_A],
            )
        self.assertIn("blocker_closed_incomplete", str(ctx.exception))

    def test_rejects_ambiguous_parents(self) -> None:
        other = f"https://github.com/{FIXTURE_SLUG}/issues/99"
        self.runner.add_issue(
            _issue_payload(99, title="Other epic", body="x", labels=["ready-for-agent"])
        )
        self.runner.issues[100]["body"] = (
            f"## Parent\n\n- {EPIC_URL}\n- {other}\n"
        )
        with self.assertRaises(he.HelmetEpicError) as ctx:
            he.build_epic_graph(
                self.policy,
                EPIC_URL,
                self.runner,
                extra_child_urls=[CHILD_A],
            )
        self.assertIn("child_ambiguous_parents", str(ctx.exception))

    def test_rejects_duplicate_children_seed(self) -> None:
        with self.assertRaises(he.HelmetEpicError) as ctx:
            he.build_epic_graph(
                self.policy,
                EPIC_URL,
                self.runner,
                extra_child_urls=[CHILD_A, CHILD_A],
            )
        self.assertIn("duplicate_explicit_child", str(ctx.exception))

    def test_same_repo_parent_hash_validates(self) -> None:
        self.runner.issues[100]["body"] = "## Parent\n\n- #42\n"
        graph = he.build_epic_graph(
            self.policy,
            EPIC_URL,
            self.runner,
            extra_child_urls=[CHILD_A],
        )
        self.assertEqual(len(graph.children), 1)
        self.assertEqual(graph.children[0].parent_urls, (EPIC_URL,))

    def test_open_external_blocker_not_satisfied(self) -> None:
        self.runner.issues[26]["state"] = "open"
        self.runner.issues[26].pop("state_reason", None)
        self.runner.issues[100]["body"] = (
            f"## Parent\n\n- {EPIC_URL}\n\n## Blocked by\n\n- {BLOCKER_EXT}\n"
        )
        graph = he.build_epic_graph(
            self.policy,
            EPIC_URL,
            self.runner,
            extra_child_urls=[CHILD_A],
        )
        self.assertIn(BLOCKER_EXT, graph.external_blockers)
        self.assertFalse(
            he.blockers_satisfied(CHILD_A, graph, completed_urls=set())
        )

    def test_native_dependency_not_used_as_membership(self) -> None:
        # Root blocked_by must not become the child list.
        self.runner.blocked_by[42] = [{"html_url": CHILD_A}]
        with self.assertRaises(he.HelmetEpicError) as ctx:
            he.build_epic_graph(self.policy, EPIC_URL, self.runner)
        self.assertIn("epic_has_no_children", str(ctx.exception))

    def test_native_child_blocked_by_edges(self) -> None:
        self.runner.sub_issues[42] = [{"html_url": CHILD_A}, {"html_url": CHILD_B}]
        self.runner.blocked_by[101] = [{"html_url": CHILD_A}]
        self.runner.issues[100]["body"] = "native child"
        self.runner.issues[101]["body"] = "native child"
        graph = he.build_epic_graph(self.policy, EPIC_URL, self.runner)
        self.assertEqual(graph.blocked_by[CHILD_B], frozenset({CHILD_A}))

    def test_human_only_operative_not_prose_mention(self) -> None:
        ordinary = he.EpicNode(
            url=CHILD_A,
            number=100,
            repository_slug=FIXTURE_SLUG,
            title="Ship helmet-epic",
            body=(
                "Preserve human-only gates elsewhere and never dispatch the "
                "public-launch issue from this worker path.\n"
            ),
            state="open",
            state_reason=None,
            labels=("ready-for-agent",),
        )
        self.assertFalse(he.is_human_only_issue(ordinary))
        gate = he.EpicNode(
            url=CHILD_H,
            number=116,
            repository_slug=FIXTURE_SLUG,
            title="Approve and publish",
            body=f"## Parent\n\n- {EPIC_URL}\n\nHuman-only; never dispatch.\n",
            state="open",
            state_reason=None,
            labels=("ready-for-agent",),
        )
        self.assertTrue(he.is_human_only_issue(gate))
        title_gate = he.EpicNode(
            url=CHILD_H,
            number=116,
            repository_slug=FIXTURE_SLUG,
            title="Public launch (human only)",
            body=f"## Parent\n\n- {EPIC_URL}\n\n## Human-only authority gate\n\nOwner action.\n",
            state="open",
            state_reason=None,
            labels=("ready-for-agent",),
        )
        self.assertTrue(he.is_human_only_issue(title_gate))
        heading_only = he.EpicNode(
            url=CHILD_H,
            number=116,
            repository_slug=FIXTURE_SLUG,
            title="Public launch closeout",
            body=f"## Parent\n\n- {EPIC_URL}\n\n## Human-only authority gate\n\nDo not automate.\n",
            state="open",
            state_reason=None,
            labels=("ready-for-agent",),
        )
        self.assertTrue(he.is_human_only_issue(heading_only))

    def test_non_allowlisted_child_rejected(self) -> None:
        foreign = "https://github.com/other-org/other/issues/1"
        with self.assertRaises((he.HelmetEpicError, hi.HelmetIssueError)):
            he.build_epic_graph(
                self.policy,
                EPIC_URL,
                self.runner,
                extra_child_urls=[foreign],
            )

    def test_native_sub_issues_preferred_when_present(self) -> None:
        self.runner.sub_issues[42] = [
            {"html_url": CHILD_A},
            {"html_url": CHILD_C},
        ]
        graph = he.build_epic_graph(self.policy, EPIC_URL, self.runner)
        self.assertEqual(graph.source, "native")
        self.assertEqual(
            sorted(node.url for node in graph.children),
            sorted([CHILD_A, CHILD_C]),
        )


class FrontierAndPassTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = _policy()
        self.runner = FakeEpicRunner()
        self.runner.add_issue(
            _issue_payload(
                42,
                title="Epic root",
                body="Epic\n\nMerge when clean: yes\n",
                labels=["ready-for-agent"],
            )
        )
        self.runner.add_issue(
            _issue_payload(
                100,
                title="A",
                body=f"## Parent\n\n- {EPIC_URL}\n",
                labels=["ready-for-agent"],
            )
        )
        self.runner.add_issue(
            _issue_payload(
                101,
                title="B depends A",
                body=(
                    f"## Parent\n\n- {EPIC_URL}\n\n"
                    f"## Blocked by\n\n- {CHILD_A}\n"
                ),
                labels=["ready-for-agent"],
            )
        )
        self.runner.add_issue(
            _issue_payload(
                102,
                title="C parallel",
                body=f"## Parent\n\n- {EPIC_URL}\n",
                labels=["ready-for-agent"],
            )
        )
        self.runner.add_issue(
            _issue_payload(
                116,
                title="Approve and publish",
                body=(
                    f"## Parent\n\n- {EPIC_URL}\n\n"
                    "Human-only; never dispatch this issue.\n"
                ),
                labels=["ready-for-agent"],
            )
        )

    def test_preflight_rejects_root_dispatch_label(self) -> None:
        self.runner.issues[42]["labels"] = [
            {"name": "ready-for-agent"},
            {"name": "hermes-kanban-go"},
        ]
        with self.assertRaises(he.HelmetEpicError) as ctx:
            he.preflight_epic(self.policy, EPIC_URL, self.runner)
        self.assertIn("epic_root_has_dispatch_label", str(ctx.exception))

    def test_preflight_rejects_worker_identity(self) -> None:
        self.runner.identity = WORKER
        with self.assertRaises(he.HelmetEpicError):
            he.preflight_epic(self.policy, EPIC_URL, self.runner)

    def test_ready_frontier_caps_at_two_and_skips_blocked(self) -> None:
        buckets = {
            "completed": [],
            "active": [],
            "ready": [CHILD_A, CHILD_C, "https://github.com/x/y/issues/3"],
            "blocked": [CHILD_B],
            "failed": [],
            "awaiting_human": [],
        }
        frontier = he.compute_ready_frontier(buckets, max_parallelism=2)
        self.assertEqual(frontier, [CHILD_A, CHILD_C])
        buckets["active"] = [CHILD_A, CHILD_C]
        self.assertEqual(
            he.compute_ready_frontier(buckets, max_parallelism=2),
            [],
        )

    def test_linear_then_parallel_invocation_order(self) -> None:
        invoked: list[str] = []

        def invoker(policy, child_url, **kwargs):
            invoked.append(child_url)
            # Simulate child A completing immediately via closed issue.
            if child_url == CHILD_A:
                self.runner.issues[100]["state"] = "closed"
                self.runner.issues[100]["state_reason"] = "completed"
            return {
                "issue_url": child_url,
                "checkpoint": {"state": "DISPATCH"},
                "status": {"state": "DISPATCH"},
            }

        with tempfile.TemporaryDirectory() as tmp:
            ck = Path(tmp)
            checkpoint, report, graph = he.run_epic_pass(
                self.policy,
                EPIC_URL,
                checkpoint_dir=ck,
                runner=self.runner,
                extra_child_urls=[CHILD_A, CHILD_B, CHILD_C, CHILD_H],
                child_invoker=invoker,
                accept_graph=True,
            )
        self.assertIsNotNone(graph)
        # First pass: A and C ready (B blocked by A; H human-only). Cap 2.
        self.assertEqual(invoked, [CHILD_A, CHILD_C])
        self.assertNotIn(EPIC_URL, invoked)
        self.assertNotIn(CHILD_H, invoked)
        self.assertEqual(report.details.get("epic_merge_mode"), "unattended_when_clean")
        # Root never labeled.
        self.assertEqual(self.runner.labels_posted, [])
        self.assertTrue(report.root_open)

        # Second pass after A completed: B becomes ready.
        invoked.clear()
        with tempfile.TemporaryDirectory() as tmp:
            # Fresh dir would lose checkpoint; reuse fingerprint acceptance via accept.
            ck = Path(tmp)
            checkpoint2, report2, _ = he.run_epic_pass(
                self.policy,
                EPIC_URL,
                checkpoint_dir=ck,
                runner=self.runner,
                extra_child_urls=[CHILD_A, CHILD_B, CHILD_C, CHILD_H],
                child_invoker=invoker,
                accept_graph=True,
            )
        # A completed, C may still be ready/active depending classification;
        # B should be eligible once A completed.
        self.assertIn(CHILD_B, invoked)
        self.assertNotIn(CHILD_H, invoked)

    def test_graph_change_pauses_dispatch(self) -> None:
        invoked: list[str] = []

        def invoker(policy, child_url, **kwargs):
            invoked.append(child_url)
            return {
                "issue_url": child_url,
                "checkpoint": {"state": "DISPATCH"},
                "status": {"state": "DISPATCH"},
            }

        with tempfile.TemporaryDirectory() as tmp:
            ck = Path(tmp)
            he.run_epic_pass(
                self.policy,
                EPIC_URL,
                checkpoint_dir=ck,
                runner=self.runner,
                extra_child_urls=[CHILD_A, CHILD_C],
                child_invoker=invoker,
                accept_graph=True,
                apply_dispatch=False,
            )
            first_fp = he.load_epic_checkpoint(
                he.epic_checkpoint_path(ck, EPIC_URL)
            ).accepted_fingerprint
            invoked.clear()
            # Add a new child → fingerprint changes.
            checkpoint, report, _ = he.run_epic_pass(
                self.policy,
                EPIC_URL,
                checkpoint_dir=ck,
                runner=self.runner,
                extra_child_urls=[CHILD_A, CHILD_C, CHILD_B],
                child_invoker=invoker,
                accept_graph=False,
                apply_dispatch=True,
            )
            self.assertEqual(checkpoint.state, "GRAPH_CHANGED")
            self.assertEqual(invoked, [])
            self.assertIn("graph_changed", report.blocker or "")
            # Accept refresh allows dispatch again.
            checkpoint2, _, _ = he.run_epic_pass(
                self.policy,
                EPIC_URL,
                checkpoint_dir=ck,
                runner=self.runner,
                extra_child_urls=[CHILD_A, CHILD_C, CHILD_B],
                child_invoker=invoker,
                accept_graph=True,
                apply_dispatch=True,
            )
            self.assertNotEqual(checkpoint2.accepted_fingerprint, first_fp)
            self.assertTrue(invoked)

    def test_resume_does_not_duplicate_without_reinvoke_when_active(self) -> None:
        # Mark child A as already having an active issue checkpoint.
        with tempfile.TemporaryDirectory() as tmp:
            ck = Path(tmp)
            issue_path = hi.checkpoint_path(ck, CHILD_A)
            hi.save_checkpoint(
                issue_path,
                hi.Checkpoint(
                    version=hi.CHECKPOINT_VERSION,
                    issue_url=CHILD_A,
                    state="WAIT_PR",
                    root_task_id="t_existing",
                ),
            )
            invoked: list[str] = []

            def invoker(policy, child_url, **kwargs):
                invoked.append(child_url)
                return {
                    "issue_url": child_url,
                    "checkpoint": {"state": "DISPATCH"},
                    "status": {"state": "DISPATCH"},
                }

            # status_issue will try live github; stub via patch classify path by
            # making status_issue return active for CHILD_A.
            with mock.patch.object(he, "status_issue") as status_mock:

                def _status(policy, url, **kwargs):
                    if url == CHILD_A:
                        return hi.StatusReport(
                            issue_url=url,
                            state="WAIT_PR",
                            root_task_id="t_existing",
                            pr_url=None,
                            reviewed_head=None,
                            clean_head=None,
                            repair_state="none",
                            merge_gate="not_ready",
                            blocker=None,
                            terminal=False,
                        )
                    return hi.StatusReport(
                        issue_url=url,
                        state="UNKNOWN",
                        root_task_id=None,
                        pr_url=None,
                        reviewed_head=None,
                        clean_head=None,
                        repair_state="none",
                        merge_gate="not_ready",
                        blocker=None,
                        terminal=False,
                    )

                status_mock.side_effect = _status
                _, report, _ = he.run_epic_pass(
                    self.policy,
                    EPIC_URL,
                    checkpoint_dir=ck,
                    runner=self.runner,
                    extra_child_urls=[CHILD_A, CHILD_C],
                    child_invoker=invoker,
                    accept_graph=True,
                )
            # A is active and must be resumed; C fills the remaining free slot.
            self.assertEqual(invoked, [CHILD_A, CHILD_C])
            self.assertIn(CHILD_A, report.active)

    def test_resume_wait_repair_before_new_ready(self) -> None:
        invoked: list[str] = []

        def invoker(policy, child_url, **kwargs):
            invoked.append(child_url)
            return {
                "issue_url": child_url,
                "checkpoint": {"state": "REVIEW_HEAD"},
                "status": {"state": "REVIEW_HEAD"},
            }

        with tempfile.TemporaryDirectory() as tmp:
            ck = Path(tmp)
            with mock.patch.object(he, "status_issue") as status_mock:

                def _status(policy, url, **kwargs):
                    if url == CHILD_A:
                        return hi.StatusReport(
                            issue_url=url,
                            state="WAIT_REPAIR",
                            root_task_id="t_a",
                            pr_url=f"https://github.com/{FIXTURE_SLUG}/pull/1",
                            reviewed_head="abc",
                            clean_head=None,
                            repair_state="none",
                            merge_gate="not_ready",
                            blocker=None,
                            terminal=False,
                        )
                    return hi.StatusReport(
                        issue_url=url,
                        state="UNKNOWN",
                        root_task_id=None,
                        pr_url=None,
                        reviewed_head=None,
                        clean_head=None,
                        repair_state="none",
                        merge_gate="not_ready",
                        blocker=None,
                        terminal=False,
                    )

                status_mock.side_effect = _status
                he.run_epic_pass(
                    self.policy,
                    EPIC_URL,
                    checkpoint_dir=ck,
                    runner=self.runner,
                    extra_child_urls=[CHILD_A, CHILD_C],
                    child_invoker=invoker,
                    accept_graph=True,
                )
        self.assertIn(CHILD_A, invoked)
        self.assertIn(CHILD_C, invoked)
        self.assertEqual(invoked[0], CHILD_A)

    def test_recovered_root_counts_toward_parallelism(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ck = Path(tmp)
            with mock.patch.object(he, "status_issue") as status_mock:

                def _status(policy, url, **kwargs):
                    if url == CHILD_C:
                        return hi.StatusReport(
                            issue_url=url,
                            state="UNKNOWN",
                            root_task_id="t_live_c",
                            pr_url=None,
                            reviewed_head=None,
                            clean_head=None,
                            repair_state="none",
                            merge_gate="not_ready",
                            blocker=None,
                            terminal=False,
                        )
                    return hi.StatusReport(
                        issue_url=url,
                        state="UNKNOWN",
                        root_task_id=None,
                        pr_url=None,
                        reviewed_head=None,
                        clean_head=None,
                        repair_state="none",
                        merge_gate="not_ready",
                        blocker=None,
                        terminal=False,
                    )

                status_mock.side_effect = _status
                invoked: list[str] = []

                def invoker(policy, child_url, **kwargs):
                    invoked.append(child_url)
                    return {
                        "issue_url": child_url,
                        "checkpoint": {"state": "DISPATCH"},
                        "status": {"state": "DISPATCH"},
                    }

                _, report, _ = he.run_epic_pass(
                    self.policy,
                    EPIC_URL,
                    checkpoint_dir=ck,
                    runner=self.runner,
                    extra_child_urls=[CHILD_A, CHILD_B, CHILD_C],
                    child_invoker=invoker,
                    accept_graph=True,
                )
        # C recovered active; B blocked by A; free slots = 1 → only A (+ resume C).
        self.assertIn(CHILD_C, report.active)
        self.assertEqual(sorted(invoked), sorted([CHILD_A, CHILD_C]))
        self.assertNotIn(CHILD_B, invoked)

    def test_merge_gate_awaiting_string_is_human(self) -> None:
        self.assertTrue(
            he._merge_gate_awaits_human("explicit_captain_approval:awaiting")
        )
        self.assertFalse(he._merge_gate_awaits_human("unattended_when_clean:ready"))
        self.assertFalse(he._merge_gate_awaits_human("unattended_when_clean:awaiting"))
        self.assertTrue(he._merge_gate_awaits_human("explicit_captain_approval"))

    def test_blocked_branch_continues_independent_by_default(self) -> None:
        buckets = {
            "completed": [],
            "active": [],
            "ready": [CHILD_C],
            "blocked": [CHILD_B],
            "failed": [CHILD_A],
            "awaiting_human": [],
        }
        self.assertEqual(
            he.compute_ready_frontier(
                buckets, max_parallelism=2, continue_independent_branches=True
            ),
            [CHILD_C],
        )
        self.assertEqual(
            he.compute_ready_frontier(
                buckets, max_parallelism=2, continue_independent_branches=False
            ),
            [],
        )

    def test_merge_mode_propagates_from_epic(self) -> None:
        root, _, mode = he.preflight_epic(self.policy, EPIC_URL, self.runner)
        self.assertEqual(mode, "unattended_when_clean")
        child_mode = merge_authority_for(
            self.policy, issue_body="child without marker", epic_body=root.body
        )
        self.assertEqual(child_mode, "unattended_when_clean")
        narrowed = merge_authority_for(
            self.policy,
            issue_body="child\n\nMerge when clean: no\n",
            epic_body=root.body,
        )
        self.assertEqual(narrowed, "explicit_captain_approval")

    def test_parent_epic_url_persisted_on_child_invoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ck = Path(tmp)
            seen: dict[str, object] = {}

            def invoker(policy, child_url, **kwargs):
                seen["epic_url"] = kwargs.get("epic_url")
                seen["epic_body"] = kwargs.get("epic_body")
                path = hi.checkpoint_path(ck, child_url)
                hi.save_checkpoint(
                    path,
                    hi.Checkpoint(
                        version=hi.CHECKPOINT_VERSION,
                        issue_url=child_url,
                        state="WAIT_PR",
                        parent_epic_url=kwargs.get("epic_url"),
                        merge_mode=merge_authority_for(
                            policy,
                            issue_body="child",
                            epic_body=kwargs.get("epic_body"),
                        ),
                    ),
                )
                return {
                    "issue_url": child_url,
                    "checkpoint": {"state": "WAIT_PR"},
                    "status": {"state": "WAIT_PR"},
                }

            he.run_epic_pass(
                self.policy,
                EPIC_URL,
                checkpoint_dir=ck,
                runner=self.runner,
                extra_child_urls=[CHILD_A],
                child_invoker=invoker,
                accept_graph=True,
            )
            self.assertEqual(seen.get("epic_url"), EPIC_URL)
            loaded = hi.load_checkpoint(hi.checkpoint_path(ck, CHILD_A))
            assert loaded is not None
            self.assertEqual(loaded.parent_epic_url, EPIC_URL)
            self.assertEqual(loaded.merge_mode, "unattended_when_clean")

            # Resume merge gate without epic_body uses parent_epic_url + live Parent.
            body, parent = hi.resolve_parent_epic_body(
                self.policy,
                epic_body=None,
                parent_epic_url=loaded.parent_epic_url,
                runner=self.runner,
                child_url=CHILD_A,
            )
            self.assertEqual(parent, EPIC_URL)
            self.assertIn("Merge when clean: yes", body or "")
            mode = merge_authority_for(
                self.policy, issue_body="child without marker", epic_body=body
            )
            self.assertEqual(mode, "unattended_when_clean")

    def test_epic_remains_open_on_done_children(self) -> None:
        for number in (100, 101, 102):
            self.runner.issues[number]["state"] = "closed"
            self.runner.issues[number]["state_reason"] = "completed"
        with tempfile.TemporaryDirectory() as tmp:
            ck = Path(tmp)
            checkpoint, report, _ = he.run_epic_pass(
                self.policy,
                EPIC_URL,
                checkpoint_dir=ck,
                runner=self.runner,
                extra_child_urls=[CHILD_A, CHILD_B, CHILD_C],
                accept_graph=True,
                apply_dispatch=False,
            )
        self.assertEqual(checkpoint.state, "DONE")
        self.assertTrue(report.root_open)
        self.assertEqual(self.runner.issues[42]["state"], "open")

    def test_status_refreshes_stale_done_when_ready_appears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ck = Path(tmp)
            # First pass: mark complete.
            for number in (100, 102):
                self.runner.issues[number]["state"] = "closed"
                self.runner.issues[number]["state_reason"] = "completed"
            he.run_epic_pass(
                self.policy,
                EPIC_URL,
                checkpoint_dir=ck,
                runner=self.runner,
                extra_child_urls=[CHILD_A, CHILD_C],
                accept_graph=True,
                apply_dispatch=False,
            )
            # Re-open a child under the same accepted fingerprint membership set
            # by replacing C with a new ready-only reclassification via status.
            self.runner.issues[102]["state"] = "open"
            self.runner.issues[102].pop("state_reason", None)
            report = he.status_epic(
                self.policy,
                EPIC_URL,
                checkpoint_dir=ck,
                runner=self.runner,
                extra_child_urls=[CHILD_A, CHILD_C],
            )
            self.assertIn(CHILD_C, report.ready)
            self.assertNotEqual(report.state, "DONE")
            self.assertFalse(report.terminal)


class CaptainReview5135755754RegressionTests(unittest.TestCase):
    """Regressions for Captain re-review 5135755754 on PR #22."""

    def setUp(self) -> None:
        self.policy = _policy()
        self.runner = FakeEpicRunner()
        self.runner.add_issue(
            _issue_payload(
                42,
                title="Epic root",
                body="Epic\n\nMerge when clean: yes\n",
                labels=["ready-for-agent"],
            )
        )
        self.runner.add_issue(
            _issue_payload(
                100,
                title="A",
                body=f"## Parent\n\n- {EPIC_URL}\n",
                labels=["ready-for-agent"],
            )
        )
        self.runner.add_issue(
            _issue_payload(
                101,
                title="B",
                body=f"## Parent\n\n- {EPIC_URL}\n",
                labels=["ready-for-agent"],
            )
        )
        self.runner.add_issue(
            _issue_payload(
                116,
                title="Public launch (human only)",
                body=(
                    f"## Parent\n\n- {EPIC_URL}\n\n"
                    "## Human-only authority gate\n\nOwner only.\n"
                ),
                labels=["ready-for-agent"],
            )
        )

    def test_search_mention_without_parent_is_not_membership(self) -> None:
        mention = f"https://github.com/{FIXTURE_SLUG}/issues/51"
        self.runner.add_issue(
            _issue_payload(
                51,
                title="Unrelated mention",
                body=f"See also {EPIC_URL} in docs; no Parent section.\n",
                labels=["ready-for-agent"],
            )
        )
        self.runner.search_items = [
            {"html_url": CHILD_A},
            {"html_url": mention},
        ]
        graph = he.build_epic_graph(self.policy, EPIC_URL, self.runner)
        self.assertEqual([n.url for n in graph.children], [CHILD_A])

    def test_search_wrong_parent_is_skipped(self) -> None:
        other_epic = f"https://github.com/{FIXTURE_SLUG}/issues/14"
        wrong = f"https://github.com/{FIXTURE_SLUG}/issues/77"
        self.runner.add_issue(
            _issue_payload(14, title="Other epic", body="root", labels=["ready-for-agent"])
        )
        self.runner.add_issue(
            _issue_payload(
                77,
                title="Child of other",
                body=(
                    f"## Parent\n\n- {other_epic}\n\n"
                    f"## Notes\n\nAlso mentions {EPIC_URL} in narrative only.\n"
                ),
                labels=["ready-for-agent"],
            )
        )
        self.runner.search_items = [{"html_url": CHILD_A}, {"html_url": wrong}]
        graph = he.build_epic_graph(self.policy, EPIC_URL, self.runner)
        self.assertEqual([n.url for n in graph.children], [CHILD_A])

    def test_explicit_child_still_requires_parent(self) -> None:
        self.runner.issues[100]["body"] = "no parent link here\n"
        with self.assertRaises(he.HelmetEpicError) as ctx:
            he.build_epic_graph(
                self.policy,
                EPIC_URL,
                self.runner,
                extra_child_urls=[CHILD_A],
            )
        self.assertIn("child_missing_parent_link", str(ctx.exception))

    def test_mixed_malformed_blocked_by_rejected(self) -> None:
        body = (
            f"## Blocked by\n\n- {CHILD_A} /issues/not-a-number\n"
        )
        with self.assertRaises(he.HelmetEpicError) as ctx:
            he.parse_blocked_by_links(body, repository_slug=FIXTURE_SLUG)
        self.assertIn("malformed_issue_link", str(ctx.exception))

    def test_native_blocked_by_http_403_fails_closed(self) -> None:
        self.runner.sub_issues[42] = [{"html_url": CHILD_A}, {"html_url": CHILD_B}]
        self.runner.blocked_by_http[100] = 403
        with self.assertRaises(he.HelmetEpicError) as ctx:
            he.build_epic_graph(self.policy, EPIC_URL, self.runner)
        self.assertIn("native_blocked_by_read_failed", str(ctx.exception))

    def test_native_sub_issues_http_404_falls_back(self) -> None:
        self.runner.sub_issues_http[42] = 404
        graph = he.build_epic_graph(
            self.policy,
            EPIC_URL,
            self.runner,
            extra_child_urls=[CHILD_A],
        )
        self.assertEqual(graph.source, "body")
        self.assertEqual(len(graph.children), 1)

    def test_partial_search_failure_fails_closed(self) -> None:
        self.runner.search_fail_once = 1
        with self.assertRaises(he.HelmetEpicError) as ctx:
            he.build_epic_graph(
                self.policy,
                EPIC_URL,
                self.runner,
                extra_child_urls=[CHILD_A],
            )
        self.assertIn("search_unavailable", str(ctx.exception))

    def test_parent_move_drops_stale_inheritance(self) -> None:
        other = f"https://github.com/{FIXTURE_SLUG}/issues/99"
        self.runner.add_issue(
            _issue_payload(
                99,
                title="Other parent",
                body="No merge marker here\n",
                labels=["ready-for-agent"],
            )
        )
        # Child Parent moved from EPIC_URL to other.
        self.runner.issues[100]["body"] = f"## Parent\n\n- {other}\n"
        body, parent = hi.resolve_parent_epic_body(
            self.policy,
            epic_body=None,
            parent_epic_url=EPIC_URL,
            runner=self.runner,
            child_url=CHILD_A,
        )
        self.assertEqual(parent, other)
        self.assertNotIn("Merge when clean: yes", body or "")
        mode = merge_authority_for(
            self.policy, issue_body="child without marker", epic_body=body
        )
        self.assertEqual(mode, "explicit_captain_approval")

    def test_parent_removed_drops_inheritance(self) -> None:
        self.runner.issues[100]["body"] = "no parent anymore\n"
        body, parent = hi.resolve_parent_epic_body(
            self.policy,
            epic_body="Epic\n\nMerge when clean: yes\n",
            parent_epic_url=EPIC_URL,
            runner=self.runner,
            child_url=CHILD_A,
        )
        self.assertIsNone(body)
        self.assertIsNone(parent)

    def test_status_graph_failure_not_terminal_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ck = Path(tmp)
            path = he.epic_checkpoint_path(ck, EPIC_URL)
            he.save_epic_checkpoint(
                path,
                he.EpicCheckpoint(
                    version=he.EPIC_CHECKPOINT_VERSION,
                    epic_url=EPIC_URL,
                    state="DONE",
                    graph_fingerprint="deadbeef",
                    accepted_fingerprint="deadbeef",
                    completed_children=[CHILD_A],
                ),
            )
            self.runner.search_fail = True
            report = he.status_epic(
                self.policy,
                EPIC_URL,
                checkpoint_dir=ck,
                runner=self.runner,
            )
            self.assertFalse(report.terminal)
            self.assertNotEqual(report.state, "DONE")
            self.assertIn("search_unavailable", report.blocker or "")
            # Historical checkpoint bytes unchanged.
            loaded = he.load_epic_checkpoint(path, expected_epic_url=EPIC_URL)
            assert loaded is not None
            self.assertEqual(loaded.state, "DONE")

    def test_human_only_gate_not_ready_frontier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ck = Path(tmp)
            buckets = he.classify_children(
                self.policy,
                he.build_epic_graph(
                    self.policy,
                    EPIC_URL,
                    self.runner,
                    extra_child_urls=[CHILD_A, CHILD_H],
                ),
                checkpoint_dir=ck,
                runner=self.runner,
                gh="gh",
                hermes="hermes",
                ledger=Path(tmp) / "ledger.sqlite3",
                worker_runtime=(),
                epic_body="Epic\n",
            )
        self.assertIn(CHILD_H, buckets["awaiting_human"])
        self.assertNotIn(CHILD_H, buckets["ready"])

    def test_malformed_full_url_numeric_prefix_rejected(self) -> None:
        """…/issues/2oops must not silently become issue #2 (Captain 5135896358)."""

        for suffix in ("2oops", "2-not-a-number", "2invalid/path"):
            bad = f"https://github.com/{FIXTURE_SLUG}/issues/{suffix}"
            body = f"## Blocked by\n\n- {bad}\n"
            with self.assertRaises(he.HelmetEpicError) as ctx:
                he.parse_blocked_by_links(body, repository_slug=FIXTURE_SLUG)
            self.assertIn("malformed_issue_link", str(ctx.exception))
            # Prefix must not resolve as a clean dependency either.
            with self.assertRaises(he.HelmetEpicError):
                he.resolve_issue_ref(bad, repository_slug=FIXTURE_SLUG)

        # A completed real #2 must not satisfy a malformed declared dependency.
        self.runner.add_issue(
            _issue_payload(
                2,
                title="Real two",
                body="done",
                state="closed",
                state_reason="completed",
                labels=["ready-for-agent"],
            )
        )
        malformed = f"https://github.com/{FIXTURE_SLUG}/issues/2oops"
        self.runner.issues[100]["body"] = (
            f"## Parent\n\n- {EPIC_URL}\n\n## Blocked by\n\n- {malformed}\n"
        )
        with self.assertRaises(he.HelmetEpicError) as ctx:
            he.build_epic_graph(
                self.policy,
                EPIC_URL,
                self.runner,
                extra_child_urls=[CHILD_A],
            )
        self.assertIn("malformed_issue_link", str(ctx.exception))

    def test_epic_pass_graph_failure_not_terminal_done(self) -> None:
        """Ordinary epic continuation must not exit-success DONE on live read fail."""

        with tempfile.TemporaryDirectory() as tmp:
            ck = Path(tmp)
            path = he.epic_checkpoint_path(ck, EPIC_URL)
            he.save_epic_checkpoint(
                path,
                he.EpicCheckpoint(
                    version=he.EPIC_CHECKPOINT_VERSION,
                    epic_url=EPIC_URL,
                    state="DONE",
                    graph_fingerprint="deadbeef",
                    accepted_fingerprint="deadbeef",
                    completed_children=[CHILD_A],
                ),
            )
            self.runner.search_fail = True
            checkpoint, report, graph = he.run_epic_pass(
                self.policy,
                EPIC_URL,
                checkpoint_dir=ck,
                runner=self.runner,
                apply_dispatch=False,
            )
            self.assertIsNone(graph)
            self.assertFalse(report.terminal)
            self.assertNotEqual(report.state, "DONE")
            self.assertIn("search_unavailable", report.blocker or "")
            self.assertIn("error", report.details)
            loaded = he.load_epic_checkpoint(path, expected_epic_url=EPIC_URL)
            assert loaded is not None
            self.assertEqual(loaded.state, "DONE")

            # CLI must not return success for this live uncertainty.
            parser = helmet_cli.build_parser()
            args = parser.parse_args(
                [
                    "epic",
                    EPIC_URL,
                    "--no-dispatch",
                    "--config",
                    str(Path(tmp) / "missing-config.yaml"),
                    "--checkpoint-dir",
                    str(ck),
                ]
            )
            with mock.patch.object(helmet_cli, "load_policy_for_epic", return_value=self.policy):
                with mock.patch.object(
                    helmet_cli, "SubprocessRunner", return_value=self.runner
                ):
                    # cmd_epic constructs its own runner; inject via run_epic_pass path.
                    with mock.patch.object(
                        helmet_cli,
                        "run_epic_pass",
                        return_value=(checkpoint, report, None),
                    ):
                        code = helmet_cli.cmd_epic(args)
            self.assertEqual(code, 2)

    def test_native_only_child_inherits_parent_merge(self) -> None:
        """Native sub-issue membership validates parent merge inheritance."""

        self.runner.sub_issues[42] = [{"html_url": CHILD_A}]
        self.runner.issues[100]["body"] = "native-only child; no Parent line\n"
        # Child-side native parent endpoint matches parent-side membership.
        self.runner.parent_of[100] = dict(self.runner.issues[42])
        # Discover via native membership.
        graph = he.build_epic_graph(self.policy, EPIC_URL, self.runner)
        self.assertEqual([n.url for n in graph.children], [CHILD_A])
        self.assertEqual(graph.source, "native")

        body, parent = hi.resolve_parent_epic_body(
            self.policy,
            epic_body=None,
            parent_epic_url=EPIC_URL,
            runner=self.runner,
            child_url=CHILD_A,
        )
        self.assertEqual(parent, EPIC_URL)
        self.assertIn("Merge when clean: yes", body or "")
        mode = merge_authority_for(
            self.policy, issue_body="child without marker", epic_body=body
        )
        self.assertEqual(mode, "unattended_when_clean")

        # Narrow child opt-out still wins; caller body alone without native/body
        # membership must not inherit when membership is gone.
        narrowed = merge_authority_for(
            self.policy,
            issue_body="child\n\nMerge when clean: no\n",
            epic_body=body,
        )
        self.assertEqual(narrowed, "explicit_captain_approval")

        self.runner.sub_issues[42] = []  # native membership revoked
        del self.runner.parent_of[100]
        body2, parent2 = hi.resolve_parent_epic_body(
            self.policy,
            epic_body="Epic\n\nMerge when clean: yes\n",
            parent_epic_url=EPIC_URL,
            runner=self.runner,
            child_url=CHILD_A,
        )
        self.assertIsNone(body2)
        self.assertIsNone(parent2)

    def test_native_move_stale_body_blocks_merge_inheritance(self) -> None:
        """Stale body/saved Parent must not bypass a different live native parent.

        Captain review 5135988896: child moved natively to non-opt-in parent while
        checkpoint + body still name the opted-in epic.
        """

        other = f"https://github.com/{FIXTURE_SLUG}/issues/43"
        self.runner.add_issue(
            _issue_payload(
                43,
                title="New parent without opt-in",
                body="Ordinary parent body\n",
                labels=["ready-for-agent"],
            )
        )
        # Stale body still declares epic #42; native parent is #43.
        self.runner.issues[100]["body"] = f"## Parent\n\n- {EPIC_URL}\n"
        self.runner.sub_issues[42] = []
        self.runner.sub_issues[43] = [{"html_url": CHILD_A}]
        self.runner.parent_of[100] = dict(self.runner.issues[43])

        # Graph: old epic must not admit via stale body; new epic admits native.
        with self.assertRaises(he.HelmetEpicError) as ctx:
            he.build_epic_graph(
                self.policy,
                EPIC_URL,
                self.runner,
                extra_child_urls=[CHILD_A],
            )
        self.assertIn("child_native_parent_not_epic", str(ctx.exception))

        graph43 = he.build_epic_graph(self.policy, other, self.runner)
        self.assertEqual([n.url for n in graph43.children], [CHILD_A])
        self.assertEqual(graph43.source, "native")

        body, parent = hi.resolve_parent_epic_body(
            self.policy,
            epic_body="Epic\n\nMerge when clean: yes\n",
            parent_epic_url=EPIC_URL,
            runner=self.runner,
            child_url=CHILD_A,
        )
        self.assertEqual(parent, other)
        self.assertNotIn("Merge when clean: yes", body or "")
        mode = merge_authority_for(
            self.policy, issue_body=self.runner.issues[100]["body"], epic_body=body
        )
        self.assertEqual(mode, "explicit_captain_approval")

        # Actual merge gate with runner must not return merge_allowed on stale #42.
        issue = hi.IssueRef(
            repository=self.policy.repositories[0],
            number=100,
            title="Child A",
            body=str(self.runner.issues[100]["body"]),
            state="open",
            labels=("ready-for-agent",),
            html_url=CHILD_A,
        )
        pull = hi.PullRequestRef(
            FIXTURE_SLUG,
            10,
            f"https://github.com/{FIXTURE_SLUG}/pull/10",
            "abc123deadbeef",
            "automation/demo-repo-100",
            "main",
            WORKER,
            "open",
            False,
            "clean",
            False,
        )
        cp = hi.Checkpoint(
            version=1,
            issue_url=CHILD_A,
            state="READY",
            clean_head="abc123deadbeef",
            parent_epic_url=EPIC_URL,
        )

        # Extend runner for live PR + Captain approval path used by the gate.
        pr_payload = {
            "number": 10,
            "html_url": pull.url,
            "state": "open",
            "merged": False,
            "draft": False,
            "mergeable_state": "clean",
            "body": f"Closes #100\n{CHILD_A}\n",
            "user": {"login": WORKER},
            "head": {"sha": "abc123deadbeef", "ref": "automation/demo-repo-100"},
            "base": {"ref": "main"},
        }
        reviews = [
            {
                "id": 1,
                "user": {"login": CAPTAIN},
                "state": "APPROVED",
                "commit_id": "abc123deadbeef",
                "submitted_at": "2026-01-01T00:00:00Z",
                "body": "clean",
            }
        ]
        original_run = self.runner.run

        def run_with_pr(command: list[str]) -> str:
            endpoint = command[-1] if command else ""
            path = endpoint.split("?", 1)[0]
            if path == f"repos/{FIXTURE_SLUG}/pulls/10":
                return json.dumps(pr_payload)
            if path.startswith(f"repos/{FIXTURE_SLUG}/pulls/10/reviews"):
                return json.dumps(reviews)
            return original_run(command)

        self.runner.run = run_with_pr  # type: ignore[method-assign]
        decision, blocker = hi.evaluate_merge_gate(
            self.policy,
            issue,
            pull,
            cp,
            required_checks_green=True,
            mergeable=True,
            runner=self.runner,
            gh="gh",
            epic_body="Epic\n\nMerge when clean: yes\n",
        )
        self.assertEqual(decision, "stop_for_approval")
        self.assertIn("explicit_captain_approval_required", blocker or "")
        self.assertEqual(cp.parent_epic_url, other)
        self.assertEqual(cp.merge_mode, "explicit_captain_approval")
        # Native parent endpoint was consulted during the gate.
        parent_calls = [
            c
            for c in self.runner.calls
            if any(str(part).endswith("/parent") for part in c)
        ]
        self.assertTrue(parent_calls)

    def test_native_parent_resolves_body_parent_ambiguity(self) -> None:
        """Native parent wins consistently when body lists unrelated multi-Parents."""

        other = f"https://github.com/{FIXTURE_SLUG}/issues/43"
        third = f"https://github.com/{FIXTURE_SLUG}/issues/44"
        self.runner.add_issue(
            _issue_payload(43, title="Native parent", body="no opt-in\n")
        )
        self.runner.add_issue(
            _issue_payload(44, title="Third", body="also no opt-in\n")
        )
        # Body names two non-native parents; saved claims opted-in epic.
        self.runner.issues[100]["body"] = f"## Parent\n\n- {other}\n- {third}\n"
        self.runner.parent_of[100] = dict(self.runner.issues[42])
        self.runner.sub_issues[42] = [{"html_url": CHILD_A}]

        body, parent = hi.resolve_parent_epic_body(
            self.policy,
            epic_body=None,
            parent_epic_url=EPIC_URL,
            runner=self.runner,
            child_url=CHILD_A,
        )
        self.assertEqual(parent, EPIC_URL)
        self.assertIn("Merge when clean: yes", body or "")

        # Without native parent, multi-Parent body that still names the saved
        # epic fails closed as ambiguous (must not pick by incidental order).
        del self.runner.parent_of[100]
        self.runner.sub_issues[42] = []
        self.runner.issues[100]["body"] = f"## Parent\n\n- {EPIC_URL}\n- {other}\n"
        with self.assertRaises(hi.HelmetIssueError) as ctx:
            hi.resolve_parent_epic_body(
                self.policy,
                epic_body=None,
                parent_epic_url=EPIC_URL,
                runner=self.runner,
                child_url=CHILD_A,
            )
        self.assertIn("child_ambiguous_parents", str(ctx.exception))

        # Multi-Parent naming only unrelated issues drops stale saved inheritance.
        self.runner.issues[100]["body"] = f"## Parent\n\n- {other}\n- {third}\n"
        body2, parent2 = hi.resolve_parent_epic_body(
            self.policy,
            epic_body="Epic\n\nMerge when clean: yes\n",
            parent_epic_url=EPIC_URL,
            runner=self.runner,
            child_url=CHILD_A,
        )
        self.assertIsNone(body2)
        self.assertIsNone(parent2)


class CliAndInstallTests(unittest.TestCase):
    def test_cli_epic_help(self) -> None:
        parser = helmet_cli.build_parser()
        args = parser.parse_args(
            ["epic", EPIC_URL, "--no-dispatch", "--accept-graph", "--config", "x"]
        )
        self.assertEqual(args.command, "epic")
        self.assertTrue(args.no_dispatch)
        self.assertTrue(args.accept_graph)

    def test_skill_install_includes_helmet_epic(self) -> None:
        messages = skills.static_validate_all_targets()
        self.assertTrue(any("helmet-epic" in item for item in messages))
        source = skills.default_skills_source() / "helmet-epic"
        skills.validate_skill_tree(source)
        text = (source / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: helmet-epic", text)
        self.assertIn("hermes-helmet", text)

    def test_no_force_push_in_epic_module(self) -> None:
        source = (ROOT / "src/hermes_helmet/helmet_epic.py").read_text(encoding="utf-8")
        for needle in (
            "git push --force",
            "git push -f",
            "git push --force-with-lease",
            "gh pr merge",
        ):
            self.assertNotIn(needle, source)


class IncidentalCompletionEpicTests(unittest.TestCase):
    """Epic classification must not treat incidental merged PRs as child DONE."""

    def test_dependent_stays_blocked_when_prerequisite_has_unrelated_merged_pr(self) -> None:
        policy = _policy()
        prereq = f"https://github.com/{FIXTURE_SLUG}/issues/14"
        dependent = f"https://github.com/{FIXTURE_SLUG}/issues/15"
        root = he.EpicNode(
            url=EPIC_URL,
            number=42,
            repository_slug=FIXTURE_SLUG,
            title="Epic",
            body="Epic root\n",
            state="open",
            state_reason=None,
            labels=("ready-for-agent",),
            is_root=True,
        )
        node_prereq = he.EpicNode(
            url=prereq,
            number=14,
            repository_slug=FIXTURE_SLUG,
            title="Open prerequisite",
            body=f"## Parent\n\n- {EPIC_URL}\n",
            state="open",
            state_reason=None,
            labels=("ready-for-agent", "hermes-kanban-go"),
            parent_urls=(EPIC_URL,),
        )
        node_dep = he.EpicNode(
            url=dependent,
            number=15,
            repository_slug=FIXTURE_SLUG,
            title="Dependent",
            body=f"## Parent\n\n- {EPIC_URL}\n\n## Blocked by\n\n- {prereq}\n",
            state="open",
            state_reason=None,
            labels=("ready-for-agent",),
            parent_urls=(EPIC_URL,),
            blocked_by_urls=(prereq,),
        )
        graph = he.EpicGraph(
            root_url=EPIC_URL,
            root=root,
            children=(node_prereq, node_dep),
            blocked_by={prereq: frozenset(), dependent: frozenset({prereq})},
            fingerprint="synthetic-incidental-pr",
            source="body",
        )

        unrelated_pr = {
            "number": 22,
            "html_url": f"https://github.com/{FIXTURE_SLUG}/pull/22",
            "state": "closed",
            "merged": True,
            "draft": False,
            "mergeable_state": "unknown",
            "body": "Closes #13\n",
            "user": {"login": WORKER},
            "head": {"sha": "mergedsha0013", "ref": "automation/demo-repo-13"},
            "base": {"ref": "main"},
        }

        class StatusCapableRunner:
            def __init__(self) -> None:
                self.identity = CAPTAIN
                self.issues = {
                    14: {
                        "number": 14,
                        "title": "Open prerequisite",
                        "body": node_prereq.body,
                        "state": "open",
                        "html_url": prereq,
                        "labels": [
                            {"name": "ready-for-agent"},
                            {"name": "hermes-kanban-go"},
                        ],
                    },
                    15: {
                        "number": 15,
                        "title": "Dependent",
                        "body": node_dep.body,
                        "state": "open",
                        "html_url": dependent,
                        "labels": [{"name": "ready-for-agent"}],
                    },
                }
                self.timeline: dict[int, list[object]] = {
                    14: [
                        {
                            "event": "cross-referenced",
                            "source": {
                                "issue": {
                                    "number": 22,
                                    "pull_request": {
                                        "url": (
                                            f"https://api.github.com/repos/"
                                            f"{FIXTURE_SLUG}/pulls/22"
                                        )
                                    },
                                }
                            },
                        }
                    ],
                    15: [],
                }
                self.pulls = {22: unrelated_pr}

            def run(self, command: list[str]) -> str:
                if command[0].endswith("gh") and command[1] == "api":
                    endpoint = command[-1]
                    path = endpoint.split("?", 1)[0]
                    if endpoint == "user" or path == "user":
                        return json.dumps({"login": self.identity})
                    if path.startswith(f"repos/{FIXTURE_SLUG}/issues/"):
                        parts = path.rsplit("/", 2)
                        if parts[-1] == "timeline":
                            number = int(parts[-2])
                            return json.dumps(self.timeline.get(number, []))
                        number = int(parts[-1])
                        return json.dumps(self.issues[number])
                    if path.startswith(f"repos/{FIXTURE_SLUG}/pulls"):
                        if "?" in endpoint:
                            return json.dumps([])
                        number = int(path.rsplit("/", 1)[-1])
                        return json.dumps(self.pulls[number])
                raise hi.HelmetIssueError(
                    hi.checkpoint_note("err", "command_failed", "unknown", "exit1")
                )

        runner = StatusCapableRunner()
        with tempfile.TemporaryDirectory() as tmp:
            buckets = he.classify_children(
                policy,
                graph,
                checkpoint_dir=Path(tmp),
                runner=runner,  # type: ignore[arg-type]
                gh="gh",
                hermes="hermes",
                ledger=Path(tmp) / "ledger.sqlite3",
                worker_runtime=(),
                epic_body="Epic\n",
            )
        self.assertNotIn(prereq, buckets["completed"])
        self.assertIn(dependent, buckets["blocked"])
        self.assertNotIn(dependent, buckets["ready"])
        self.assertNotIn(dependent, buckets["completed"])

    def test_dependent_stays_blocked_for_incidental_full_url_body(self) -> None:
        """Ordinary full-URL body reference must not complete a prerequisite."""
        policy = _policy()
        prereq = f"https://github.com/{FIXTURE_SLUG}/issues/14"
        dependent = f"https://github.com/{FIXTURE_SLUG}/issues/15"
        root = he.EpicNode(
            url=EPIC_URL,
            number=42,
            repository_slug=FIXTURE_SLUG,
            title="Epic",
            body="Epic root\n",
            state="open",
            state_reason=None,
            labels=("ready-for-agent",),
            is_root=True,
        )
        node_prereq = he.EpicNode(
            url=prereq,
            number=14,
            repository_slug=FIXTURE_SLUG,
            title="Open prerequisite",
            body=f"## Parent\n\n- {EPIC_URL}\n",
            state="open",
            state_reason=None,
            labels=("ready-for-agent", "hermes-kanban-go"),
            parent_urls=(EPIC_URL,),
        )
        node_dep = he.EpicNode(
            url=dependent,
            number=15,
            repository_slug=FIXTURE_SLUG,
            title="Dependent",
            body=f"## Parent\n\n- {EPIC_URL}\n\n## Blocked by\n\n- {prereq}\n",
            state="open",
            state_reason=None,
            labels=("ready-for-agent",),
            parent_urls=(EPIC_URL,),
            blocked_by_urls=(prereq,),
        )
        graph = he.EpicGraph(
            root_url=EPIC_URL,
            root=root,
            children=(node_prereq, node_dep),
            blocked_by={prereq: frozenset(), dependent: frozenset({prereq})},
            fingerprint="synthetic-incidental-url",
            source="body",
        )
        unrelated_pr = {
            "number": 22,
            "html_url": f"https://github.com/{FIXTURE_SLUG}/pull/22",
            "state": "closed",
            "merged": True,
            "draft": False,
            "mergeable_state": "unknown",
            "body": (
                "Closes #13\n\n"
                f"Deferred: see also [issue 14]({prereq}).\n"
            ),
            "user": {"login": WORKER},
            "head": {"sha": "mergedsha0013", "ref": "automation/demo-repo-13"},
            "base": {"ref": "main"},
        }

        class StatusCapableRunner:
            def __init__(self) -> None:
                self.identity = CAPTAIN
                self.issues = {
                    14: {
                        "number": 14,
                        "title": "Open prerequisite",
                        "body": node_prereq.body,
                        "state": "open",
                        "html_url": prereq,
                        "labels": [
                            {"name": "ready-for-agent"},
                            {"name": "hermes-kanban-go"},
                        ],
                    },
                    15: {
                        "number": 15,
                        "title": "Dependent",
                        "body": node_dep.body,
                        "state": "open",
                        "html_url": dependent,
                        "labels": [{"name": "ready-for-agent"}],
                    },
                }
                self.timeline: dict[int, list[object]] = {
                    14: [
                        {
                            "event": "cross-referenced",
                            "source": {
                                "issue": {
                                    "number": 22,
                                    "pull_request": {
                                        "url": (
                                            f"https://api.github.com/repos/"
                                            f"{FIXTURE_SLUG}/pulls/22"
                                        )
                                    },
                                }
                            },
                        }
                    ],
                    15: [],
                }
                self.pulls = {22: unrelated_pr}

            def run(self, command: list[str]) -> str:
                if command[0].endswith("gh") and command[1] == "api":
                    endpoint = command[-1]
                    path = endpoint.split("?", 1)[0]
                    if endpoint == "user" or path == "user":
                        return json.dumps({"login": self.identity})
                    if path.startswith(f"repos/{FIXTURE_SLUG}/issues/"):
                        parts = path.rsplit("/", 2)
                        if parts[-1] == "timeline":
                            number = int(parts[-2])
                            return json.dumps(self.timeline.get(number, []))
                        number = int(parts[-1])
                        return json.dumps(self.issues[number])
                    if path.startswith(f"repos/{FIXTURE_SLUG}/pulls"):
                        if "?" in endpoint:
                            return json.dumps([])
                        number = int(path.rsplit("/", 1)[-1])
                        return json.dumps(self.pulls[number])
                raise hi.HelmetIssueError(
                    hi.checkpoint_note("err", "command_failed", "unknown", "exit1")
                )

        runner = StatusCapableRunner()
        with tempfile.TemporaryDirectory() as tmp:
            buckets = he.classify_children(
                policy,
                graph,
                checkpoint_dir=Path(tmp),
                runner=runner,  # type: ignore[arg-type]
                gh="gh",
                hermes="hermes",
                ledger=Path(tmp) / "ledger.sqlite3",
                worker_runtime=(),
                epic_body="Epic\n",
            )
        self.assertNotIn(prereq, buckets["completed"])
        self.assertIn(dependent, buckets["blocked"])
        self.assertNotIn(dependent, buckets["ready"])
        self.assertNotIn(dependent, buckets["completed"])


if __name__ == "__main__":
    unittest.main()
