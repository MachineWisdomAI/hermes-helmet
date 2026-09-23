#!/usr/bin/env python3
"""Authority schema, renderer, trust, and merge-policy tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hermes_helmet import authority, github_issue_poller as poller, preflight


ROOT = Path(__file__).resolve().parents[1]
EXAMPLECO = ROOT / "config" / "fixtures" / "exampleco" / "policy.json"
EXAMPLE_POLICY = ROOT / "config" / "policy.example.json"

FORBIDDEN_PUBLIC_MARKERS = (
    "time" + "left--",
    "yia-" + "mw-agent",
    "wisdom" + "helm-builder",
    "MachineWisdomAI/",
    "xai-" + "oauth",
    "grok-4" + ".5",
)


class AuthorityTests(unittest.TestCase):
    def test_exampleco_fixture_renders_deterministically(self) -> None:
        policy = authority.load_authority(EXAMPLECO)
        self.assertEqual(policy.version, 2)
        self.assertEqual(policy.company_display_name, "ExampleCo")
        self.assertEqual(policy.company_slug, "exampleco")
        self.assertEqual(policy.captain_github_login, "example-captain")
        self.assertEqual(policy.worker_github_login, "example-agent")
        self.assertEqual(policy.github_identity, "example-agent")
        self.assertEqual(policy.assignee, "builder")
        self.assertEqual(policy.ready_label, "ready-for-agent")
        self.assertEqual(policy.dispatch_label, "hermes-kanban-go")
        self.assertEqual(policy.required_label, "hermes-kanban-go")
        self.assertEqual(policy.github_owners, ("example-org",))
        self.assertEqual(
            [repository.slug for repository in policy.repositories],
            ["example-org/demo-repo", "example-org/docs-repo"],
        )
        self.assertEqual(policy.inference_provider, "openai")
        self.assertEqual(policy.inference_model, "gpt-4.1")
        self.assertEqual(policy.trusted_review_bots, ("github-code-quality[bot]",))
        self.assertEqual(
            policy.trusted_human_associations,
            ("OWNER", "MEMBER", "COLLABORATOR"),
        )
        self.assertEqual(policy.merge_default_mode, "explicit_captain_approval")
        self.assertFalse(policy.integrations.openviking)
        self.assertEqual(policy.budgets.max_issue_runtime_minutes, 240)
        self.assertEqual(policy.max_epic_parallelism, 2)
        self.assertEqual(
            [peer.peer_id for peer in policy.openviking_peers],
            ["codex", "chatgpt", "hermes"],
        )

        contract = authority.render_crew_contract(policy)
        self.assertEqual(contract, authority.render_crew_contract(policy))
        self.assertIn("ExampleCo", contract)
        self.assertIn("`example-captain`", contract)
        self.assertIn("`example-agent`", contract)
        self.assertIn("Never impersonate", contract)
        self.assertIn("Default deny", contract)
        self.assertIn("Silence is not approval", contract)
        self.assertIn("What did it read?", contract)
        self.assertIn("Where did the human approve, reject, or correct it?", contract)
        for marker in FORBIDDEN_PUBLIC_MARKERS:
            self.assertNotIn(marker, contract)

        public = json.dumps(authority.authority_public_dict(policy), sort_keys=True)
        for marker in FORBIDDEN_PUBLIC_MARKERS:
            self.assertNotIn(marker, public)

    def test_example_policy_matches_authority_surface(self) -> None:
        example = authority.load_authority(EXAMPLE_POLICY)
        self.assertEqual(example.company_display_name, "ExampleCo")
        self.assertEqual(example.worker_github_login, "example-agent")
        self.assertEqual(example.captain_github_login, "example-captain")
        self.assertEqual(example.worker_access_scope, "selected")
        self.assertEqual(example.worker_completion_contract, "github-pr")
        text = EXAMPLE_POLICY.read_text(encoding="utf-8")
        for marker in FORBIDDEN_PUBLIC_MARKERS:
            self.assertNotIn(marker, text)

    def test_worker_access_scope_defaults_and_rejects_unknown(self) -> None:
        base = json.loads(EXAMPLECO.read_text(encoding="utf-8"))
        self.assertEqual(
            authority.policy_from_mapping(base).worker_access_scope, "selected"
        )
        broader = dict(base, worker_access_scope="broader")
        self.assertEqual(
            authority.policy_from_mapping(broader).worker_access_scope, "broader"
        )
        with self.assertRaises(authority.AuthorityError):
            authority.policy_from_mapping(dict(base, worker_access_scope="all-repos"))

    def test_worker_completion_contract_defaults_and_rejects_unknown(self) -> None:
        base = json.loads(EXAMPLECO.read_text(encoding="utf-8"))
        policy = authority.policy_from_mapping(base)
        self.assertEqual(policy.worker_completion_contract, "github-pr")
        self.assertEqual(
            policy.kanban_completion_contract("example-org/demo-repo"),
            "example-org/demo-repo",
        )
        local = authority.policy_from_mapping(
            dict(base, worker_completion_contract="local-only")
        )
        self.assertEqual(local.worker_completion_contract, "local-only")
        self.assertEqual(
            local.kanban_completion_contract("example-org/demo-repo"),
            "local-only",
        )
        with self.assertRaises(authority.AuthorityError):
            authority.policy_from_mapping(
                dict(base, worker_completion_contract="branch-rules")
            )

    def test_v1_policy_still_loads_for_poller_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(
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
                                "slug": "example-org/demo-repo",
                                "worktree": "/opt/data/repos/demo-repo",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            policy = poller.load_policy(path)
            self.assertEqual(policy.version, 1)
            self.assertEqual(policy.github_identity, "agent-bot")
            self.assertEqual(policy.captain_github_login, "")
            self.assertEqual(policy.github_owners, ("example-org",))

    def test_validation_rejects_identity_and_allowlist_problems(self) -> None:
        base = json.loads(EXAMPLECO.read_text(encoding="utf-8"))
        cases = [
            ({"captain_github_login": "example-agent"}, "captain_github_login"),
            ({"captain_github_login": ""}, "captain_github_login"),
            ({"worker_github_login": ""}, "worker_github_login"),
            ({"repositories": []}, "repositories"),
            (
                {
                    "repositories": [
                        {
                            "slug": "example-org/demo-repo",
                            "worktree": "/opt/data/repos/demo-repo",
                        },
                        {
                            "slug": "example-org/demo-repo",
                            "worktree": "/opt/data/repos/other",
                        },
                    ]
                },
                "repositories",
            ),
            (
                {
                    "repositories": [
                        {
                            "slug": "example-org/demo-repo",
                            "worktree": "/opt/data/repos/../etc/passwd",
                        }
                    ]
                },
                "worktree",
            ),
            (
                {
                    "repositories": [
                        {
                            "slug": "example-org/demo-repo",
                            "worktree": "relative/path",
                        }
                    ]
                },
                "worktree",
            ),
            (
                {
                    "openviking_peers": [
                        {"id": "codex"},
                        {"id": "CODEX"},
                    ]
                },
                "openviking_peers",
            ),
            ({"budgets": {"max_issue_runtime_minutes": 0, "max_repair_rounds": 1}}, "budgets"),
            ({"budgets": {"max_issue_runtime_minutes": 1, "max_repair_rounds": -3}}, "budgets"),
            ({"max_epic_parallelism": 0}, "max_epic_parallelism"),
            (
                {
                    "github_owners": ["example-org"],
                    "repositories": [
                        {
                            "slug": "other-org/demo-repo",
                            "worktree": "/opt/data/repos/demo-repo",
                        }
                    ],
                },
                "repositories",
            ),
            ({"token": "should-not-be-here"}, "token"),
        ]
        for override, field in cases:
            with self.subTest(field=field, override=override):
                payload = dict(base)
                payload.update(override)
                with self.assertRaises(authority.AuthorityError) as ctx:
                    authority.policy_from_mapping(payload)
                message = str(ctx.exception)
                self.assertIn(field, message)
                self.assertNotIn("should-not-be-here", message)
                self.assertNotIn("ghp_", message)

    def test_worker_identity_fails_closed_for_captain_unknown_and_mismatch(self) -> None:
        policy = authority.load_authority(EXAMPLECO)
        authority.verify_worker_identity(policy, "example-agent")
        with self.assertRaisesRegex(authority.AuthorityError, "Captain"):
            authority.verify_worker_identity(policy, "example-captain")
        with self.assertRaisesRegex(authority.AuthorityError, "missing"):
            authority.verify_worker_identity(policy, "")
        with self.assertRaisesRegex(authority.AuthorityError, "does not match"):
            authority.verify_worker_identity(policy, "someone-else")

        checks = preflight.assert_worker_ready(
            policy,
            preflight.StaticIdentityProbe("example-agent"),
        )
        self.assertEqual(checks, ("github-worker-identity",))
        with self.assertRaises(preflight.PreflightError):
            preflight.assert_worker_ready(
                policy,
                preflight.StaticIdentityProbe("example-captain"),
            )
        with self.assertRaises(preflight.PreflightError):
            preflight.assert_worker_ready(
                policy,
                preflight.StaticIdentityProbe("unknown-bot"),
            )

    def test_poller_verify_identity_uses_configured_worker(self) -> None:
        policy = authority.load_authority(EXAMPLECO)

        class Runner:
            def __init__(self, login: str) -> None:
                self.login = login

            def run(self, command):
                if command[:3] == [poller.GH, "api", "user"]:
                    return self.login + "\n"
                raise AssertionError(command)

        poller.verify_identity(policy, Runner("example-agent"))
        with self.assertRaises(poller.PollerError):
            poller.verify_identity(policy, Runner("example-captain"))
        with self.assertRaises(poller.PollerError):
            poller.verify_identity(policy, Runner("other-agent"))

    def test_generated_task_text_names_only_configured_worker_and_allowlist(self) -> None:
        policy = authority.load_authority(EXAMPLECO)
        body = authority.render_issue_task_body(
            issue_url="https://github.com/example-org/demo-repo/issues/11",
            issue_body="Ship authority contract",
            policy=policy,
        )
        self.assertIn("example-agent", body)
        self.assertIn("example-org/demo-repo", body)
        self.assertIn("example-org/docs-repo", body)
        self.assertIn("never merge", body)
        self.assertIn("never force-push", body)
        self.assertIn("example-captain", body)
        self.assertIn("metadata.published_pr", body)
        for marker in FORBIDDEN_PUBLIC_MARKERS:
            self.assertNotIn(marker, body)

        issue = poller.Issue(
            repository=policy.repositories[0],
            number=11,
            title="authority",
            body="Ship authority contract",
        )
        poller_body = poller._task_body(issue, policy)
        self.assertEqual(poller_body, body)

        repair = authority.render_repair_task_body(
            pull_url="https://github.com/example-org/demo-repo/pull/12",
            event_kind="reviewed",
            event_state="changes_requested",
            event_actor="trusted-reviewer",
            event_url=(
                "https://github.com/example-org/demo-repo/"
                "pull/12#pullrequestreview-1"
            ),
        )
        self.assertIn("metadata.published_pr", repair)
        self.assertNotIn("metadata.pr_url", body)
        self.assertNotIn("metadata.pr_url", repair)
        contract = authority.render_crew_contract(policy)
        self.assertIn("metadata.published_pr", contract)
        self.assertIn("Newly contracted completions", contract)
        self.assertIn("require `metadata.published_pr`", contract)
        self.assertIn("historical terminal-task adoption", contract)
        self.assertIn("worker completion contract", contract)
        self.assertIn("`github-pr`", contract)
        self.assertIn("`local-only`", contract)
        self.assertNotIn("still accepted", contract)

    def test_trusted_human_and_bot_handling(self) -> None:
        policy = authority.load_authority(EXAMPLECO)
        self.assertTrue(
            authority.is_trusted_human(
                actor_type="User",
                author_association="MEMBER",
                policy=policy,
            )
        )
        self.assertTrue(
            authority.is_trusted_human(
                actor_type="User",
                author_association="OWNER",
                policy=policy,
            )
        )
        self.assertTrue(
            authority.is_trusted_human(
                actor_type="User",
                author_association="COLLABORATOR",
                policy=policy,
            )
        )
        self.assertFalse(
            authority.is_trusted_human(
                actor_type="User",
                author_association="CONTRIBUTOR",
                policy=policy,
            )
        )
        self.assertFalse(
            authority.is_trusted_human(
                actor_type="Bot",
                author_association="OWNER",
                policy=policy,
            )
        )
        self.assertTrue(
            authority.is_trusted_bot(
                actor_type="Bot",
                login="github-code-quality[bot]",
                policy=policy,
            )
        )
        self.assertFalse(
            authority.is_trusted_bot(
                actor_type="Bot",
                login="random-bot",
                policy=policy,
            )
        )
        self.assertFalse(
            authority.is_trusted_bot(
                actor_type="User",
                login="github-code-quality[bot]",
                policy=policy,
            )
        )

    def test_merge_when_clean_explicit_and_inherited(self) -> None:
        policy = authority.load_authority(EXAMPLECO)
        self.assertEqual(
            authority.merge_authority_for(policy, issue_body="ordinary issue"),
            "explicit_captain_approval",
        )
        self.assertTrue(
            authority.unattended_merge_allowed(
                policy,
                issue_body="Please ship.\n\nMerge when clean: yes\n",
            )
        )
        self.assertTrue(
            authority.unattended_merge_allowed(
                policy,
                issue_body="Please ship.\n\n- Merge when clean: yes\n",
            )
        )
        self.assertTrue(
            authority.unattended_merge_allowed(
                policy,
                issue_body="child without marker",
                epic_body="Epic root\n\nMerge when clean: yes\n",
            )
        )
        self.assertFalse(
            authority.unattended_merge_allowed(
                policy,
                issue_body="child narrows\n\nMerge when clean: no\n",
                epic_body="Epic root\n\nMerge when clean: yes\n",
            )
        )
        self.assertFalse(
            authority.unattended_merge_allowed(
                policy,
                issue_body="",
                epic_body="no marker here",
            )
        )
        ambiguous_bodies = [
            "Do not set Merge when clean: yes",
            "The default requires Merge when clean: yes on the issue",
            "Epic #42 mentions Merge when clean: yes as documentation only",
            '"Merge when clean: yes"',
            "`Merge when clean: yes`",
            "Merge when clean: yeah",
            "Merge when clean yes",
            "Please Merge when clean: yes now",
            "Merge when clean: yes please",
        ]
        for body in ambiguous_bodies:
            with self.subTest(body=body):
                self.assertFalse(
                    authority.unattended_merge_allowed(policy, issue_body=body),
                    body,
                )
                self.assertFalse(
                    authority.unattended_merge_allowed(
                        policy,
                        issue_body="child",
                        epic_body=body,
                    ),
                    body,
                )

    def test_secret_shaped_and_unknown_fields_rejected_nested(self) -> None:
        base = json.loads(EXAMPLECO.read_text(encoding="utf-8"))
        cases = [
            ({"openai_api_key": "sk-test-value"}, "openai_api_key"),
            ({"api-key": "sk-test-value"}, "api-key"),
            ({"GH_TOKEN": "ghp_super_secret_value"}, "GH_TOKEN"),
            (
                {
                    "integrations": {
                        "openviking": False,
                        "fava_trails": False,
                        "signal": False,
                        "github_token": "ghp_super_secret_value",
                    }
                },
                "integrations.github_token",
            ),
            (
                {
                    "integrations": {
                        "openviking": False,
                        "fava_trails": False,
                        "signal": False,
                        "provider_secret": "nested-secret-value",
                    }
                },
                "integrations.provider_secret",
            ),
            (
                {
                    "company": {
                        "display_name": "ExampleCo",
                        "slug": "exampleco",
                        "openviking_key": "ovk-secret",
                    }
                },
                "company.openviking_key",
            ),
            (
                {
                    "budgets": {
                        "max_issue_runtime_minutes": 120,
                        "max_repair_rounds": 10,
                        "unknown_budget_knob": 1,
                    }
                },
                "budgets.unknown_budget_knob",
            ),
            ({"not_a_real_field": True}, "not_a_real_field"),
        ]
        for override, field in cases:
            with self.subTest(field=field):
                payload = dict(base)
                payload.update(override)
                with self.assertRaises(authority.AuthorityError) as ctx:
                    authority.policy_from_mapping(payload)
                message = str(ctx.exception)
                self.assertIn(field, message)
                self.assertNotIn("sk-test-value", message)
                self.assertNotIn("ghp_super_secret_value", message)
                self.assertNotIn("nested-secret-value", message)
                self.assertNotIn("ovk-secret", message)

    def test_alias_conflicts_rejected_matching_aliases_accepted(self) -> None:
        base = json.loads(EXAMPLECO.read_text(encoding="utf-8"))

        matching = dict(base)
        matching["github_identity"] = matching["worker_github_login"]
        matching["required_label"] = matching["dispatch_label"]
        policy = authority.policy_from_mapping(matching)
        self.assertEqual(policy.github_identity, "example-agent")
        self.assertEqual(policy.required_label, "hermes-kanban-go")

        conflict_worker = dict(base)
        conflict_worker["github_identity"] = "other-agent"
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.policy_from_mapping(conflict_worker)
        self.assertIn("worker_github_login", str(ctx.exception))
        self.assertIn("conflicts", str(ctx.exception))

        conflict_label = dict(base)
        conflict_label["required_label"] = "different-label"
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.policy_from_mapping(conflict_label)
        self.assertIn("dispatch_label", str(ctx.exception))
        self.assertIn("conflicts", str(ctx.exception))

    def test_ready_and_dispatch_labels_must_differ_case_insensitively(self) -> None:
        base = json.loads(EXAMPLECO.read_text(encoding="utf-8"))

        exact = dict(base)
        exact["ready_label"] = exact["dispatch_label"]
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.policy_from_mapping(exact)
        message = str(ctx.exception)
        self.assertIn("ready_label", message)
        self.assertIn("dispatch_label", message)

        case_only = dict(base)
        case_only["ready_label"] = "Ready"
        case_only["dispatch_label"] = "ready"
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.policy_from_mapping(case_only)
        message = str(ctx.exception)
        self.assertIn("ready_label", message)
        self.assertIn("dispatch_label", message)
        self.assertIn("case-insensitively", message)

    def test_openviking_peers_require_object_with_id(self) -> None:
        base = json.loads(EXAMPLECO.read_text(encoding="utf-8"))

        valid = dict(base)
        valid["openviking_peers"] = [{"id": "codex"}, {"id": "chatgpt"}]
        policy = authority.policy_from_mapping(valid)
        self.assertEqual(
            [peer.peer_id for peer in policy.openviking_peers],
            ["codex", "chatgpt"],
        )

        bare_string = dict(base)
        bare_string["openviking_peers"] = ["codex"]
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.policy_from_mapping(bare_string)
        self.assertIn("openviking_peers[0]", str(ctx.exception))
        self.assertIn("object with id", str(ctx.exception))

        peer_id_only = dict(base)
        peer_id_only["openviking_peers"] = [{"peer_id": "codex"}]
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.policy_from_mapping(peer_id_only)
        message = str(ctx.exception)
        self.assertIn("openviking_peers[0]", message)
        self.assertTrue("peer_id" in message or ".id" in message)

        both_keys = dict(base)
        both_keys["openviking_peers"] = [{"id": "codex", "peer_id": "other"}]
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.policy_from_mapping(both_keys)
        self.assertIn("openviking_peers[0].peer_id", str(ctx.exception))

        missing_id = dict(base)
        missing_id["openviking_peers"] = [{}]
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.policy_from_mapping(missing_id)
        self.assertIn("openviking_peers[0].id", str(ctx.exception))

        duplicates = dict(base)
        duplicates["openviking_peers"] = [{"id": "codex"}, {"id": "CODEX"}]
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.policy_from_mapping(duplicates)
        self.assertIn("openviking_peers", str(ctx.exception))
        self.assertIn("unique", str(ctx.exception))

    def test_repository_slug_case_and_worktree_collisions_rejected(self) -> None:
        base = json.loads(EXAMPLECO.read_text(encoding="utf-8"))
        case_dup = dict(base)
        case_dup["repositories"] = [
            {
                "slug": "example-org/demo-repo",
                "worktree": "/opt/data/repos/demo-repo",
            },
            {
                "slug": "Example-Org/Demo-Repo",
                "worktree": "/opt/data/repos/demo-repo-other",
            },
        ]
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.policy_from_mapping(case_dup)
        self.assertIn("repositories", str(ctx.exception))

        path_dup = dict(base)
        path_dup["repositories"] = [
            {
                "slug": "example-org/demo-repo",
                "worktree": "/opt/data/repos/shared-checkout",
            },
            {
                "slug": "example-org/docs-repo",
                "worktree": "/opt/data/repos/shared-checkout",
            },
        ]
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.policy_from_mapping(path_dup)
        self.assertIn("repositories", str(ctx.exception))
        self.assertIn("worktree", str(ctx.exception))

    def test_errors_name_fields_without_sensitive_values(self) -> None:
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.policy_from_mapping(
                {
                    "version": 2,
                    "company": {"display_name": "ExampleCo", "slug": "exampleco"},
                    "captain_github_login": "example-captain",
                    "worker_github_login": "example-agent",
                    "schedule": "every 15m",
                    "board": "default",
                    "assignee": "builder",
                    "inference_provider": "openai",
                    "inference_model": "gpt-4.1",
                    "worker_max_turns": 100,
                    "dispatch_label": "hermes-kanban-go",
                    "github_token": "ghp_super_secret_value",
                    "repositories": [
                        {
                            "slug": "example-org/demo-repo",
                            "worktree": "/opt/data/repos/demo-repo",
                        }
                    ],
                }
            )
        message = str(ctx.exception)
        self.assertIn("github_token", message)
        self.assertNotIn("ghp_super_secret_value", message)

    def test_legacy_trailing_hyphen_logins_load(self) -> None:
        """Existing GitHub accounts may end with hyphens; policies must load."""
        base = json.loads(EXAMPLECO.read_text(encoding="utf-8"))

        captain_legacy = dict(base)
        captain_legacy["captain_github_login"] = "legacy-captain--"
        policy = authority.policy_from_mapping(captain_legacy)
        self.assertEqual(policy.captain_github_login, "legacy-captain--")
        self.assertEqual(policy.worker_github_login, "example-agent")

        worker_legacy = dict(base)
        worker_legacy["worker_github_login"] = "legacy-worker--"
        policy = authority.policy_from_mapping(worker_legacy)
        self.assertEqual(policy.worker_github_login, "legacy-worker--")
        self.assertEqual(policy.github_identity, "legacy-worker--")
        self.assertEqual(policy.captain_github_login, "example-captain")

        both = dict(base)
        both["captain_github_login"] = "legacy-captain--"
        both["worker_github_login"] = "legacy-worker-"
        policy = authority.policy_from_mapping(both)
        self.assertEqual(policy.captain_github_login, "legacy-captain--")
        self.assertEqual(policy.worker_github_login, "legacy-worker-")

        # Ordinary modern logins and single-char still load.
        ordinary = dict(base)
        ordinary["captain_github_login"] = "a"
        ordinary["worker_github_login"] = "z" * 39
        policy = authority.policy_from_mapping(ordinary)
        self.assertEqual(policy.captain_github_login, "a")
        self.assertEqual(policy.worker_github_login, "z" * 39)

    def test_login_validation_rejects_unsafe_empty_and_overlong(self) -> None:
        base = json.loads(EXAMPLECO.read_text(encoding="utf-8"))
        invalid_captains = [
            "",
            "   ",
            "-leading",
            "has_underscore",
            "has.name",
            "has space",
            "a" * 40,
            "bad/login",
            "name@host",
        ]
        for login in invalid_captains:
            with self.subTest(captain=login):
                payload = dict(base)
                payload["captain_github_login"] = login
                with self.assertRaises(authority.AuthorityError) as ctx:
                    authority.policy_from_mapping(payload)
                message = str(ctx.exception)
                self.assertIn("captain_github_login", message)
                self.assertNotIn("ghp_", message)

        invalid_workers = ["", "-worker", "worker_name", "x" * 40]
        for login in invalid_workers:
            with self.subTest(worker=login):
                payload = dict(base)
                payload["worker_github_login"] = login
                with self.assertRaises(authority.AuthorityError) as ctx:
                    authority.policy_from_mapping(payload)
                self.assertIn("worker_github_login", str(ctx.exception))

        # Bot-style logins remain out of band for captain/worker fields; bots
        # stay on trusted_review_bots without LOGIN_RE.
        botish = dict(base)
        botish["captain_github_login"] = "reviewer[bot]"
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.policy_from_mapping(botish)
        self.assertIn("captain_github_login", str(ctx.exception))
        self.assertIn("valid GitHub login", str(ctx.exception))

        # Existing bot allowlist entries still load unchanged.
        policy = authority.policy_from_mapping(base)
        self.assertEqual(policy.trusted_review_bots, ("github-code-quality[bot]",))

    def test_legacy_login_identity_enforcement_stays_exact(self) -> None:
        base = json.loads(EXAMPLECO.read_text(encoding="utf-8"))
        base["captain_github_login"] = "legacy-captain--"
        base["worker_github_login"] = "legacy-worker--"
        policy = authority.policy_from_mapping(base)

        # Worker path keeps exact configured-identity comparison (case-sensitive).
        authority.verify_worker_identity(policy, "legacy-worker--")
        with self.assertRaisesRegex(authority.AuthorityError, "Captain"):
            authority.verify_worker_identity(policy, "legacy-captain--")
        with self.assertRaisesRegex(authority.AuthorityError, "does not match"):
            authority.verify_worker_identity(policy, "Legacy-Worker--")
        with self.assertRaisesRegex(authority.AuthorityError, "does not match"):
            authority.verify_worker_identity(policy, "legacy-worker-")
        with self.assertRaisesRegex(authority.AuthorityError, "does not match"):
            authority.verify_worker_identity(policy, "example-agent")

        # Captain path remains case-insensitive exact login match.
        authority.verify_captain_identity(policy, "legacy-captain--")
        authority.verify_captain_identity(policy, "LEGACY-CAPTAIN--")
        with self.assertRaisesRegex(authority.AuthorityError, "does not match"):
            authority.verify_captain_identity(policy, "legacy-captain-")
        with self.assertRaisesRegex(authority.AuthorityError, "worker GitHub login"):
            authority.verify_captain_identity(policy, "legacy-worker--")

        # Captain/worker separation still fail-closed when equal ignoring case.
        same = dict(base)
        same["captain_github_login"] = "legacy-same--"
        same["worker_github_login"] = "Legacy-Same--"
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.policy_from_mapping(same)
        self.assertIn("captain_github_login", str(ctx.exception))
        self.assertIn("must differ", str(ctx.exception))


class InstalledConsoleLegacyLoginTests(unittest.TestCase):
    """Exercise the packaged console entry point with a legacy-login policy.

    Uses the stdlib venv + pip toolchain (what Verify CI provides via setup-python),
    installs into an isolated prefix outside the checkout, clears source PYTHONPATH,
    asserts package import provenance under site-packages, and requires a successful
    parsed ``status --json`` against a synthetic read-only GitHub fixture.
    """

    def test_installed_console_loads_legacy_captain_policy(self) -> None:
        import os
        import stat
        import subprocess
        import sys
        import venv

        root = ROOT
        issue_url = "https://github.com/example-org/demo-repo/issues/1"
        issue_endpoint = "repos/example-org/demo-repo/issues/1"
        timeline_prefix = "repos/example-org/demo-repo/issues/1/timeline"
        pulls_prefix = "repos/example-org/demo-repo/pulls?"

        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            # Keep all install/runtime artifacts outside the source tree.
            work = tmp / "outside-checkout"
            work.mkdir()
            dist = work / "dist"
            build_venv = work / "build-venv"
            install_venv = work / "install-venv"
            run_cwd = work / "run-cwd"
            run_cwd.mkdir()
            policy_path = work / "policy.json"
            ledger_path = work / "missing-ledger.sqlite3"
            checkpoint_dir = work / "checkpoints"
            gh_bin = work / "fake-gh"
            hermes_bin = work / "fake-hermes"

            payload = json.loads(EXAMPLECO.read_text(encoding="utf-8"))
            payload["captain_github_login"] = "legacy-captain--"
            payload["worker_github_login"] = "legacy-worker--"
            policy_path.write_text(json.dumps(payload), encoding="utf-8")

            # Synthetic read-only gh: issue fetch + empty timeline/open-PR discovery.
            issue_json = json.dumps(
                {
                    "number": 1,
                    "title": "legacy login fixture",
                    "body": "synthetic read-only status probe",
                    "state": "open",
                    "html_url": issue_url,
                    "labels": [{"name": "ready-for-agent"}],
                }
            )
            gh_bin.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                f"ISSUE = {issue_json!r}\n"
                "args = sys.argv[1:]\n"
                "if not args or args[0] != 'api':\n"
                "    print('unsupported', file=sys.stderr); sys.exit(2)\n"
                "endpoint = args[-1]\n"
                f"if endpoint == {issue_endpoint!r}:\n"
                "    print(ISSUE); sys.exit(0)\n"
                f"if endpoint.startswith({timeline_prefix!r}):\n"
                "    print('[]'); sys.exit(0)\n"
                f"if endpoint.startswith({pulls_prefix!r}):\n"
                "    print('[]'); sys.exit(0)\n"
                "if endpoint == 'user':\n"
                "    print(json.dumps({'login': 'legacy-captain--'})); sys.exit(0)\n"
                "print('unexpected endpoint: ' + endpoint, file=sys.stderr)\n"
                "sys.exit(3)\n",
                encoding="utf-8",
            )
            gh_bin.chmod(gh_bin.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            hermes_bin.write_text(
                "#!/bin/sh\necho 'hermes fixture unused' >&2\nexit 2\n",
                encoding="utf-8",
            )
            hermes_bin.chmod(
                hermes_bin.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )

            def _venv_python(venv_dir: Path) -> Path:
                if os.name == "nt":
                    return venv_dir / "Scripts" / "python.exe"
                return venv_dir / "bin" / "python"

            def _venv_console(venv_dir: Path) -> Path:
                if os.name == "nt":
                    return venv_dir / "Scripts" / "hermes-helmet.exe"
                return venv_dir / "bin" / "hermes-helmet"

            def _clean_env(**extra: str) -> dict[str, str]:
                env = {
                    key: value
                    for key, value in os.environ.items()
                    if key not in {"PYTHONPATH", "PYTHONHOME", "HERMES_HELMET_GH", "HERMES_HELMET_HERMES"}
                }
                # Force empty PYTHONPATH so checkout src/ cannot shadow the wheel.
                env["PYTHONPATH"] = ""
                env.update(extra)
                return env

            # Build wheel with the same Python family CI provides (no uv required).
            venv.create(build_venv, with_pip=True, clear=True)
            build_python = _venv_python(build_venv)
            self.assertTrue(build_python.is_file(), msg=f"missing build python {build_python}")
            bootstrap = subprocess.run(
                [
                    str(build_python),
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    "pip",
                    "setuptools",
                    "wheel",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=_clean_env(),
            )
            self.assertEqual(
                bootstrap.returncode,
                0,
                msg=f"build toolchain install failed:\n{bootstrap.stdout}\n{bootstrap.stderr}",
            )
            dist.mkdir()
            wheel_build = subprocess.run(
                [
                    str(build_python),
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--no-build-isolation",
                    "-w",
                    str(dist),
                    str(root),
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=str(work),
                env=_clean_env(),
            )
            self.assertEqual(
                wheel_build.returncode,
                0,
                msg=f"wheel build failed:\n{wheel_build.stdout}\n{wheel_build.stderr}",
            )
            wheels = sorted(dist.glob("hermes_helmet-*.whl")) + sorted(
                dist.glob("hermes-helmet-*.whl")
            )
            self.assertTrue(wheels, msg=f"no wheel in {list(dist.iterdir())}")
            wheel = wheels[0]

            venv.create(install_venv, with_pip=True, clear=True)
            python = _venv_python(install_venv)
            console = _venv_console(install_venv)
            install = subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    str(wheel),
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=str(work),
                env=_clean_env(),
            )
            self.assertEqual(
                install.returncode,
                0,
                msg=f"pip install failed:\n{install.stdout}\n{install.stderr}",
            )
            self.assertTrue(console.is_file(), msg=f"missing console script {console}")

            # Import must resolve to the installed wheel, not checkout src/.
            probe = subprocess.run(
                [
                    str(python),
                    "-c",
                    (
                        "import hermes_helmet, hermes_helmet.authority as authority\n"
                        "from pathlib import Path\n"
                        f"root = Path({str(root)!r}).resolve()\n"
                        "pkg = Path(hermes_helmet.__file__).resolve()\n"
                        "auth = Path(authority.__file__).resolve()\n"
                        "print(f'pkg={pkg}')\n"
                        "print(f'auth={auth}')\n"
                        "assert 'site-packages' in pkg.parts, pkg\n"
                        "assert 'site-packages' in auth.parts, auth\n"
                        "assert root not in pkg.parents, pkg\n"
                        "assert (root / 'src') not in auth.parents, auth\n"
                        f"p = authority.load_authority(Path({str(policy_path)!r}))\n"
                        "assert p.captain_github_login == 'legacy-captain--'\n"
                        "assert p.worker_github_login == 'legacy-worker--'\n"
                        "print('legacy-login-ok')\n"
                    ),
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=str(run_cwd),
                env=_clean_env(),
            )
            self.assertEqual(
                probe.returncode,
                0,
                msg=f"installed load/provenance failed:\n{probe.stdout}\n{probe.stderr}",
            )
            self.assertIn("legacy-login-ok", probe.stdout)
            self.assertIn("site-packages", probe.stdout)

            # Console must load the legacy policy and emit successful parsed status.
            status = subprocess.run(
                [
                    str(console),
                    "status",
                    issue_url,
                    "--config",
                    str(policy_path),
                    "--ledger",
                    str(ledger_path),
                    "--checkpoint-dir",
                    str(checkpoint_dir),
                    "--gh",
                    str(gh_bin),
                    "--hermes",
                    str(hermes_bin),
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=str(run_cwd),
                env=_clean_env(),
            )
            combined = status.stdout + status.stderr
            self.assertEqual(
                status.returncode,
                0,
                msg=f"installed console status failed:\n{combined}",
            )
            self.assertNotIn("policy_load_failed", combined)
            self.assertNotIn("must be a valid GitHub login", combined)
            try:
                report = json.loads(status.stdout)
            except json.JSONDecodeError as exc:
                self.fail(f"status did not emit JSON:\n{combined}\n{exc}")
            self.assertEqual(report.get("issue_url"), issue_url)
            self.assertIn("state", report)
            self.assertIsInstance(report.get("state"), str)
            self.assertTrue(report["state"].strip(), msg=f"empty state: {report}")
            self.assertIn("repair_state", report)
            self.assertIn("merge_gate", report)
            self.assertIn("terminal", report)
            # Read-only probe must not create ledger/checkpoint side effects.
            self.assertFalse(ledger_path.exists(), msg="status wrote a ledger")
            wrote_checkpoints = checkpoint_dir.exists() and any(
                checkpoint_dir.rglob("*")
            )
            self.assertFalse(wrote_checkpoints, msg="status wrote checkpoint files")
            self.assertGreaterEqual(sys.version_info[:2], (3, 11))


if __name__ == "__main__":
    unittest.main()
