from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hermes_helmet import github_issue_poller as poller
from hermes_helmet import public_surface

SLUG = "example-org/demo-repo"
SHA = "a" * 40


def pull(number: int, *, reviewers=("agent-bot",), state="open", sha=SHA, fork=False):
    return {
        "number": number,
        "state": state,
        "head": {"sha": sha, "repo": {"full_name": "forker/demo-repo" if fork else SLUG}},
        "requested_reviewers": [{"login": login} for login in reviewers],
    }


class ReviewRequestRunner:
    def __init__(self, pulls, events=None):
        self.pulls = pulls
        self.events = events or {}
        self.creates: list[list[str]] = []
        self.tasks: dict[str, str] = {}

    def run(self, command):
        if command[:3] == [poller.GH, "api", "user"]:
            return "agent-bot\n"
        if command[:3] == [poller.GH, "api", "--paginate"]:
            endpoint = command[-1]
            if endpoint.startswith(f"repos/{SLUG}/pulls?"):
                return json.dumps(self.pulls)
            if endpoint.startswith(f"repos/{SLUG}/issues?"):
                return "[]"
            for number, events in self.events.items():
                if endpoint == f"repos/{SLUG}/issues/{number}/events?per_page=100":
                    return json.dumps(events)
        if command[0] == "git":
            return "true\n"
        if command[:4] == [poller.HERMES, "kanban", "--board", "default"] and "create" in command:
            self.creates.append(command)
            key = command[command.index("--idempotency-key") + 1]
            task_id = self.tasks.setdefault(key, f"t_review_{len(self.tasks) + 1}")
            return json.dumps({"id": task_id})
        raise AssertionError(command)


def requested(event_id, login="agent-bot"):
    return {"id": event_id, "event": "review_requested", "requested_reviewer": {"login": login}}


class ReviewRequestIntakeTests(unittest.TestCase):
    def policy(self, root: Path) -> poller.Policy:
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
            repositories=(poller.Repository(SLUG, checkout),),
        )

    def test_pending_request_creates_one_ordinary_task_reused_by_later_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            runner = ReviewRequestRunner(
                [pull(7), pull(8, reviewers=("someone-else",)), pull(9, state="closed"), pull(10, fork=True)],
                {7: [requested(5, "other"), requested(11), requested(12)], 10: [requested(30)]},
            )
            ledger = root / "ledger.sqlite3"
            result = poller.run_once(policy, ledger, runner)
            self.assertEqual(result.errors, [])
            self.assertEqual(len(runner.creates), 2)
            first = runner.creates[0]
            self.assertEqual(first[first.index("--assignee") + 1], "builder")
            self.assertEqual(first[first.index("--created-by") + 1], poller.CREATED_BY)
            self.assertEqual(
                first[first.index("--idempotency-key") + 1],
                f"github-pr-review-request:{SLUG}:7:12",
            )
            self.assertEqual(first[first.index("--workspace") + 1], f"worktree:{root / 'demo-repo'}")
            self.assertIn("--max-runtime", first)
            self.assertNotIn("--completion-contract", first)
            self.assertIn("review/demo-repo-pr-7-12", first)
            second = runner.creates[1]
            self.assertIn(f"{SLUG}:10:30", second[second.index("--idempotency-key") + 1])
            poller.run_once(policy, ledger, runner)
            self.assertEqual(len(runner.tasks), 2)  # same keys reuse the same tasks

    def test_new_request_at_same_sha_is_a_distinct_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            runner = ReviewRequestRunner([pull(7)], {7: [requested(11)]})
            poller.run_once(policy, root / "l.sqlite3", runner)
            runner.events[7].append(requested(20))
            poller.run_once(policy, root / "l.sqlite3", runner)
            self.assertEqual(len(runner.tasks), 2)

    def test_no_request_event_is_reported_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = ReviewRequestRunner([pull(7)], {7: []})
            result = poller.run_once(self.policy(root), root / "l.sqlite3", runner)
            self.assertEqual(runner.creates, [])
            self.assertEqual(len(result.errors), 1)

    def test_repository_outside_allowlist_is_never_queried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self.policy(root)
            seen: list[str] = []

            class Spy(ReviewRequestRunner):
                def run(self, command):
                    seen.append(" ".join(command))
                    return super().run(command)

            Spy([]).run([poller.GH, "api", "user"])
            runner = Spy([])
            poller.run_once(policy, root / "l.sqlite3", runner)
            self.assertFalse([c for c in seen if "other-org" in c])

    def test_review_contract_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = self.policy(Path(directory))
            body = poller.render_review_task_body(
                pull_url=f"https://github.com/{SLUG}/pull/7",
                head_sha=SHA,
                request_url=f"https://github.com/{SLUG}/pull/7#event-12",
                policy=policy,
            )
            for needle in (
                f"https://github.com/{SLUG}/pull/7",
                SHA,
                "agent-bot",
                "git rev-parse HEAD",
                "full review",
                "configured with",
                "do not select or change",
                "re-read the live pull request head",
                "REQUEST_CHANGES",
                "APPROVE",
                "blocked/error",
                "review URL",
                "six-question audit",
                "Do not commit, push, force-push, merge",
                "replacement pull request",
                "change the model provider",
            ):
                self.assertIn(needle, body)
            self.assertNotIn("published_pr", body)
            for line in body.splitlines():
                self.assertFalse(public_surface.has_forbidden_public_marker(line), line)


if __name__ == "__main__":
    unittest.main()
