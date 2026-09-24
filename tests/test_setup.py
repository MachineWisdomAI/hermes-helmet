#!/usr/bin/env python3
"""H9 setup-helmet / hermes-helmet setup and doctor tests."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import BytesIO, StringIO
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Mapping
from unittest.mock import patch
from urllib.error import HTTPError

from hermes_helmet import authority, doctor as helmet_doctor, setup as helmet_setup
from hermes_helmet import install_skills as skills
from hermes_helmet.cli import main as cli_main


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "github_pat_" + ("0" * 40)

# Live GitHub 422 bodies observed from empty write probes (no `errors` array).
LIVE_INVALID_REQUEST_REFS = {
    "message": 'Invalid request.\n\n"ref", "sha" weren\'t supplied.',
}
LIVE_INVALID_REQUEST_PULLS = {
    "message": 'Invalid request.\n\n"base", "head" weren\'t supplied.',
}
LIVE_INVALID_REQUEST_ISSUES = {
    "message": 'Invalid request.\n\n"title" wasn\'t supplied.',
}

_REFS_MISSING = (("ref", "missing_field"), ("sha", "missing_field"))
_PULLS_MISSING = (("head", "missing_field"), ("base", "missing_field"))
_ISSUES_MISSING = (("title", "missing_field"),)


def _http_error(status: int, payload: object | None = None) -> HTTPError:
    body = None if payload is None else BytesIO(json.dumps(payload).encode("utf-8"))
    return HTTPError("https://api.github.test", status, "error", {}, body)


def _answers(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "company": {"display_name": "ExampleCo", "slug": "exampleco"},
        "captain_github_login": "example-captain",
        "worker_github_login": "example-agent",
        "schedule": "every 15m",
        "board": "default",
        "assignee": "builder",
        "inference_provider": "openai",
        "inference_model": "gpt-4.1",
        "worker_max_turns": 100,
        "ready_label": "ready-for-agent",
        "dispatch_label": "hermes-kanban-go",
        "github_owners": ["example-org"],
        "repositories": [
            {
                "slug": "example-org/demo-repo",
                "worktree": "/opt/data/repos/demo-repo",
            }
        ],
        "openviking": {"selected": False, "confirmed": False},
        "fava_trails": {"selected": False, "confirmed": False},
        "matt_pocock_skills": {"accepted": False},
        "gstack": {"accepted": False},
        "company_skill_pack": {"declined": True},
    }
    payload.update(overrides)
    return payload


class FakeGitHub:
    def __init__(
        self,
        *,
        login: str = "example-agent",
        repos: Mapping[str, Mapping[str, object]] | None = None,
        extra_slugs: tuple[str, ...] = (),
        extra_private: Mapping[str, bool] | None = None,
        labels: Mapping[str, set[str]] | None = None,
    ) -> None:
        self.login = login
        self.repos = {
            "example-org/demo-repo": {
                "permissions": {
                    "metadata": "read",
                    "contents": "write",
                    "pull_requests": "write",
                    "issues": "write",
                }
            }
        }
        if repos is not None:
            self.repos = dict(repos)
        self.extra_slugs = extra_slugs
        self.extra_private = dict(extra_private or {})
        self.labels = {
            slug: set(names) for slug, names in (labels or {}).items()
        }
        self.created_labels: list[tuple[str, str]] = []
        self.applied_labels: list[tuple[str, str]] = []
        self.mutated = False

    def current_user(self, token: str) -> str:
        self._require_token(token)
        return self.login

    def repository_access(self, token: str, slug: str) -> dict[str, object]:
        self._require_token(token)
        if slug not in self.repos:
            raise helmet_setup.SetupError("repository is not accessible")
        return dict(self.repos[slug])

    def list_accessible_repository_slugs(self, token: str) -> tuple[str, ...]:
        self._require_token(token)
        return tuple(item["slug"] for item in self.list_visible_repositories(token))  # type: ignore[misc]

    def list_visible_repositories(self, token: str) -> tuple[dict[str, object], ...]:
        self._require_token(token)
        rows: list[dict[str, object]] = [
            {"slug": slug, "private": True} for slug in self.repos
        ]
        for slug in self.extra_slugs:
            rows.append(
                {
                    "slug": slug,
                    "private": False if self.extra_private.get(slug) is False else True,
                }
            )
        return tuple(rows)

    def list_labels(self, token: str, slug: str) -> tuple[str, ...]:
        self._require_token(token)
        return tuple(sorted(self.labels.get(slug, set())))

    def create_label(self, token: str, slug: str, name: str) -> None:
        self._require_token(token)
        self.mutated = True
        self.created_labels.append((slug, name))
        self.labels.setdefault(slug, set()).add(name)

    def apply_issue_label(self, token: str, slug: str, name: str) -> None:
        self._require_token(token)
        self.mutated = True
        self.applied_labels.append((slug, name))
        raise AssertionError("dispatch label must not be applied to issues")

    def _require_token(self, token: str) -> None:
        if not token:
            raise helmet_setup.SetupError("github token is missing")


class FakeTransport:
    def request(self, method, url, headers, body, timeout):  # noqa: ANN001
        assert "github_pat_" not in str(url)
        auth = ""
        if isinstance(headers, dict):
            auth = str(headers.get("Authorization") or "")
        assert TOKEN not in auth
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


def _run_setup(
    tmp: Path,
    answers: dict[str, object],
    *,
    github: FakeGitHub | None = None,
    token: str = TOKEN,
    worktree_exists: bool = True,
) -> helmet_setup.SetupReport:
    answers = json.loads(json.dumps(answers))
    for index, repo in enumerate(answers.get("repositories") or []):
        if isinstance(repo, dict):
            repo["worktree"] = str(tmp / "repos" / f"repo-{index}")
    home = tmp / "home"
    home.mkdir()
    answers_path = tmp / "answers.json"
    answers_path.write_text(json.dumps(answers), encoding="utf-8")
    if worktree_exists:
        for repo in answers.get("repositories") or []:
            if isinstance(repo, dict) and isinstance(repo.get("worktree"), str):
                checkout = Path(str(repo["worktree"]))
                checkout.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    ["git", "init", "-q", str(checkout)],
                    check=True,
                )
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(checkout),
                        "remote",
                        "add",
                        "origin",
                        f"https://github.com/{repo['slug']}.git",
                    ],
                    check=True,
                )
    return helmet_setup.run_setup(
        answers_path=answers_path,
        home=home,
        pat_provider=lambda: token,
        github=github or FakeGitHub(),
        transport=FakeTransport(),
        probe=True,
    )


class QuestionnaireTests(unittest.TestCase):
    def test_defaults_select_openviking_and_fava_without_confirmation(self) -> None:
        questionnaire = helmet_setup.default_questionnaire()
        self.assertIs(questionnaire["openviking"]["selected"], True)
        self.assertIs(questionnaire["openviking"]["confirmed"], False)
        self.assertIs(questionnaire["fava_trails"]["selected"], True)
        self.assertIs(questionnaire["fava_trails"]["confirmed"], False)

    def test_answers_reject_secret_shaped_keys_and_values(self) -> None:
        raw = _answers()
        raw["github_token"] = TOKEN
        with self.assertRaisesRegex(helmet_setup.SetupError, "secret"):
            helmet_setup.load_answers_mapping(raw)
        raw = _answers()
        raw["notes"] = TOKEN
        with self.assertRaisesRegex(helmet_setup.SetupError, "secret"):
            helmet_setup.load_answers_mapping(raw)

    def test_answers_render_h2_policy_without_adopter_secrets(self) -> None:
        mapping = helmet_setup.answers_to_policy_mapping(_answers())
        policy = authority.policy_from_mapping(mapping)
        self.assertEqual(policy.version, 2)
        public = json.dumps(authority.authority_public_dict(policy))
        self.assertNotIn(TOKEN, public)
        self.assertFalse(policy.integrations.openviking)
        self.assertFalse(policy.integrations.fava_trails)
        self.assertFalse(policy.integrations.signal)

    def test_answers_reject_unknown_top_level_and_nested_keys(self) -> None:
        top_level = _answers(worker_github_logni="example-agent")
        with self.assertRaisesRegex(helmet_setup.SetupError, "worker_github_logni"):
            helmet_setup.load_answers_mapping(top_level)
        nested = _answers(openviking={"selected": True, "confirmd": True})
        with self.assertRaisesRegex(helmet_setup.SetupError, "confirmd"):
            helmet_setup.load_answers_mapping(nested)
        repository = _answers()
        repository["repositories"][0]["slgu"] = "example-org/demo-repo"  # type: ignore[index]
        with self.assertRaisesRegex(helmet_setup.SetupError, "slgu"):
            helmet_setup.load_answers_mapping(repository)


class SecretAndGithubTests(unittest.TestCase):
    def test_github_write_probe_distinguishes_validation_from_read_only(self) -> None:
        client = helmet_setup.GitHubClient()
        validation = HTTPError(
            "https://api.github.test",
            422,
            "invalid",
            {},
            BytesIO(b'{"message":"Invalid request","errors":[{"field":"title","code":"missing_field"}]}'),
        )
        with patch("hermes_helmet.setup.request.urlopen", side_effect=validation):
            self.assertTrue(
                client._write_probe(
                    TOKEN,
                    "POST",
                    "/repos/o/r/issues",
                    {},
                    expected_errors=(("title", "missing_field"),),
                )
            )
        wrong_validation = HTTPError(
            "https://api.github.test",
            422,
            "invalid",
            {},
            BytesIO(b'{"errors":[{"field":"title","code":"custom"}]}'),
        )
        with patch("hermes_helmet.setup.request.urlopen", side_effect=wrong_validation):
            with self.assertRaisesRegex(helmet_setup.SetupError, "unexpected validation"):
                client._write_probe(
                    TOKEN,
                    "POST",
                    "/repos/o/r/issues",
                    {},
                    expected_errors=(("title", "missing_field"),),
                )
        spam_validation = HTTPError(
            "https://api.github.test",
            422,
            "invalid",
            {},
            BytesIO(
                b'{"errors":['
                b'{"field":"ref","code":"missing_field"},'
                b'{"field":"sha","code":"missing_field"},'
                b'{"field":"spam","code":"custom"}]}'
            ),
        )
        with patch("hermes_helmet.setup.request.urlopen", side_effect=spam_validation):
            with self.assertRaisesRegex(helmet_setup.SetupError, "unexpected validation"):
                client._write_probe(
                    TOKEN,
                    "POST",
                    "/repos/o/r/git/refs",
                    {},
                    expected_errors=(("ref", "missing_field"), ("sha", "missing_field")),
                    exact_errors=True,
                )
        forbidden = HTTPError("https://api.github.test", 403, "forbidden", {}, None)
        with patch("hermes_helmet.setup.request.urlopen", side_effect=forbidden):
            self.assertFalse(
                client._write_probe(
                    TOKEN,
                    "POST",
                    "/repos/o/r/issues",
                    {},
                    expected_errors=(("title", "missing_field"),),
                )
            )

    def test_github_write_probe_accepts_live_invalid_request_shapes(self) -> None:
        client = helmet_setup.GitHubClient()
        cases = (
            (
                LIVE_INVALID_REQUEST_REFS,
                "/repos/o/r/git/refs",
                _REFS_MISSING,
                True,
            ),
            (
                LIVE_INVALID_REQUEST_PULLS,
                "/repos/o/r/pulls",
                _PULLS_MISSING,
                False,
            ),
            (
                LIVE_INVALID_REQUEST_ISSUES,
                "/repos/o/r/issues",
                _ISSUES_MISSING,
                False,
            ),
            (
                {"message": 'Invalid request.\n\n"sha", "ref" weren\'t supplied.'},
                "/repos/o/r/git/refs",
                _REFS_MISSING,
                True,
            ),
            (
                {"message": 'Invalid request.\n\n"head", "base" weren\'t supplied.'},
                "/repos/o/r/pulls",
                _PULLS_MISSING,
                False,
            ),
        )
        for payload, path, expected, exact in cases:
            with self.subTest(path=path, message=payload["message"]):
                with patch(
                    "hermes_helmet.setup.request.urlopen",
                    side_effect=_http_error(422, payload),
                ):
                    self.assertTrue(
                        client._write_probe(
                            TOKEN,
                            "POST",
                            path,
                            {},
                            expected_errors=expected,
                            exact_errors=exact,
                        )
                    )

    def test_github_write_probe_rejects_ambiguous_invalid_request_shapes(self) -> None:
        client = helmet_setup.GitHubClient()
        cases = (
            {"message": 'Invalid request.\n\n"ref", "sha", "spam" weren\'t supplied.'},
            {"message": 'Invalid request.\n\n"ref" weren\'t supplied.'},
            {"message": 'Invalid request.\n\n"title" weren\'t supplied.'},
            {"message": 'Invalid request.\n\n"ref", "sha" wasn\'t supplied.'},
            {"message": 'Invalid request.\n\n"ref" wasn\'t supplied.'},
            {"message": "Invalid request."},
            {"message": "Validation Failed"},
            {"message": 'Invalid request.\n\n"ref", "sha" weren\'t supplied.', "errors": []},
            {"message": 'Invalid request.\n\n"base", "head", "title" weren\'t supplied.'},
            {"message": 'Please supply "ref" and "sha".'},
            b"{",
        )
        for payload in cases:
            with self.subTest(payload=payload):
                if isinstance(payload, bytes):
                    error = HTTPError("https://api.github.test", 422, "invalid", {}, BytesIO(payload))
                else:
                    error = _http_error(422, payload)
                with patch("hermes_helmet.setup.request.urlopen", side_effect=error):
                    with self.assertRaisesRegex(helmet_setup.SetupError, "unexpected validation"):
                        client._write_probe(
                            TOKEN,
                            "POST",
                            "/repos/o/r/git/refs",
                            {},
                            expected_errors=_REFS_MISSING,
                            exact_errors=True,
                        )

    def test_github_write_probe_auth_failures_are_no_write_and_success_fails_closed(self) -> None:
        client = helmet_setup.GitHubClient()
        for status in (401, 403, 404):
            with self.subTest(status=status):
                with patch(
                    "hermes_helmet.setup.request.urlopen",
                    side_effect=_http_error(status),
                ):
                    self.assertFalse(
                        client._write_probe(
                            TOKEN,
                            "POST",
                            "/repos/o/r/issues",
                            {},
                            expected_errors=_ISSUES_MISSING,
                        )
                    )

        class _Ok:
            def __enter__(self) -> "_Ok":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

        with patch("hermes_helmet.setup.request.urlopen", return_value=_Ok()):
            with self.assertRaisesRegex(helmet_setup.SetupError, "unexpectedly succeeded"):
                client._write_probe(
                    TOKEN,
                    "POST",
                    "/repos/o/r/issues",
                    {},
                    expected_errors=_ISSUES_MISSING,
                )

    def test_contents_probe_uses_structured_git_refs_validation(self) -> None:
        client = helmet_setup.GitHubClient()
        with patch.object(client, "_request", return_value={}), patch.object(
            client, "_write_probe", return_value=True
        ) as probe:
            result = client.repository_access(TOKEN, "example-org/demo-repo")
        self.assertEqual(result["permissions"]["contents"], "write")
        contents_call = probe.call_args_list[0]
        self.assertEqual(contents_call.args[1:4], ("POST", "/repos/example-org/demo-repo/git/refs", {}))
        self.assertEqual(
            contents_call.kwargs["expected_errors"],
            (("ref", "missing_field"), ("sha", "missing_field")),
        )
        self.assertIs(contents_call.kwargs["exact_errors"], True)

    def test_label_discovery_paginates(self) -> None:
        client = helmet_setup.GitHubClient()
        pages: list[str] = []

        def fake_request(token: str, method: str, path: str, body: object = None) -> object:
            pages.append(path)
            if path.endswith("page=1"):
                return [{"name": f"label-{index}"} for index in range(100)]
            return [{"name": "ready-for-agent"}, {"name": "hermes-kanban-go"}]

        with patch.object(client, "_request", side_effect=fake_request):
            labels = client.list_labels(TOKEN, "example-org/demo-repo")
        self.assertIn("ready-for-agent", labels)
        self.assertEqual(len(pages), 2)

    def test_pat_uses_owner_only_file_and_never_enters_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = _run_setup(Path(tmp), _answers())
            self.assertTrue(report.ok)
            secret = Path(tmp) / "home" / ".hermes-helmet" / "secrets" / "github_worker_pat"
            self.assertTrue(secret.is_file())
            mode = stat.S_IMODE(secret.stat().st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(secret.read_text(encoding="utf-8"), TOKEN)
            policy_text = (Path(tmp) / "home" / ".hermes-helmet" / "policy.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(TOKEN, policy_text)
            self.assertNotIn(TOKEN, json.dumps(report.to_public_dict()))
            contract = (
                Path(tmp) / "home" / ".hermes-helmet" / "generated" / "crew-contract.md"
            ).read_text(encoding="utf-8")
            self.assertNotIn(TOKEN, contract)
            self.assertIn("ExampleCo", contract)
            self.assertIn("`example-captain`", contract)

    def test_github_rejects_captain_identity_and_extra_private_repos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(helmet_setup.SetupError, "Captain"):
                _run_setup(
                    Path(tmp),
                    _answers(),
                    github=FakeGitHub(login="example-captain"),
                )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(helmet_setup.SetupError, "allowlist"):
                _run_setup(
                    Path(tmp),
                    _answers(),
                    github=FakeGitHub(extra_slugs=("example-org/other-repo",)),
                )

    def test_github_accepts_public_read_visibility_outside_allowlist(self) -> None:
        github = FakeGitHub(
            extra_slugs=("octocat/Hello-World",),
            extra_private={"octocat/Hello-World": False},
        )
        with tempfile.TemporaryDirectory() as tmp:
            report = _run_setup(Path(tmp), _answers(), github=github)
            self.assertTrue(report.ok)
            self.assertEqual(report.github["worker_access_scope"], "selected")
            self.assertEqual(report.github["extra_public_repositories"], ["octocat/Hello-World"])
            self.assertEqual(report.github["extra_private_repositories"], [])

    def test_github_accepts_extra_private_visibility_with_broader_scope(self) -> None:
        github = FakeGitHub(extra_slugs=("example-org/other-repo",))
        with tempfile.TemporaryDirectory() as tmp:
            report = _run_setup(
                Path(tmp),
                _answers(worker_access_scope="broader"),
                github=github,
            )
            self.assertTrue(report.ok)
            self.assertEqual(report.github["worker_access_scope"], "broader")
            self.assertEqual(report.github["extra_private_repositories"], ["example-org/other-repo"])

    def test_github_rejects_missing_configured_repository(self) -> None:
        github = FakeGitHub(repos={})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(helmet_setup.SetupError, "allowlist"):
                _run_setup(Path(tmp), _answers(), github=github)

    def test_github_requires_metadata_contents_pr_and_issue_permissions(self) -> None:
        weak = {
            "example-org/demo-repo": {
                "permissions": {
                    "metadata": "read",
                    "contents": "write",
                    "pull_requests": "read",
                    "issues": "write",
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(helmet_setup.SetupError, "pull_requests:write"):
                _run_setup(Path(tmp), _answers(), github=FakeGitHub(repos=weak))

    def test_github_rejects_read_only_token_even_when_reads_succeed(self) -> None:
        read_only = {
            "example-org/demo-repo": {
                "permissions": {
                    "metadata": "read",
                    "contents": "read",
                    "pull_requests": "read",
                    "issues": "read",
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(helmet_setup.SetupError, "contents:write"):
                _run_setup(Path(tmp), _answers(), github=FakeGitHub(repos=read_only))

    def test_labels_are_created_without_applying_dispatch(self) -> None:
        github = FakeGitHub()
        with tempfile.TemporaryDirectory() as tmp:
            report = _run_setup(Path(tmp), _answers(), github=github)
            self.assertTrue(report.ok)
            created = {(slug, name) for slug, name in github.created_labels}
            self.assertIn(("example-org/demo-repo", "ready-for-agent"), created)
            self.assertIn(("example-org/demo-repo", "hermes-kanban-go"), created)
            self.assertEqual(github.applied_labels, [])


class ProbeSkillsOptionalTests(unittest.TestCase):
    def test_provider_probe_and_gates_redact_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = _run_setup(Path(tmp), _answers())
            public = report.to_public_dict()
            self.assertTrue(public["model_probe"]["ok"])
            gates = public["provider_gates"]
            self.assertTrue(gates)
            blob = json.dumps(public)
            self.assertNotIn(TOKEN, blob)
            self.assertNotIn("Bearer ", blob)

    def test_unsupported_provider_fails_before_any_mutation(self) -> None:
        github = FakeGitHub()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            answers = root / "answers.json"
            answers.write_text(
                json.dumps(_answers(inference_provider="anthropic", inference_model="claude-test")),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(helmet_setup.SetupError, "unsupported by the default probe"):
                helmet_setup.run_setup(
                    answers_path=answers,
                    home=home,
                    pat_provider=lambda: TOKEN,
                    github=github,
                    transport=FakeTransport(),
                )
            self.assertFalse(github.mutated)
            self.assertFalse((home / ".hermes-helmet").exists())

    def test_provider_probe_error_is_secret_free_and_precedes_github_mutation(self) -> None:
        class BrokenTransport:
            def request(self, *args, **kwargs):  # noqa: ANN002, ANN003
                raise helmet_setup.ModelLaneError("sensitive internal provider detail")

        github = FakeGitHub()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            answers = root / "answers.json"
            answers.write_text(json.dumps(_answers()), encoding="utf-8")
            with self.assertRaisesRegex(helmet_setup.SetupError, "provider/model probe failed") as raised:
                helmet_setup.run_setup(
                    answers_path=answers,
                    home=home,
                    pat_provider=lambda: TOKEN,
                    github=github,
                    transport=BrokenTransport(),
                )
            self.assertNotIn("sensitive", str(raised.exception))
            self.assertFalse(github.mutated)

    def test_conflict_prevents_writes_to_every_selected_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "prefix"
            dest = prefix / ".codex" / "skills" / "helmet-issue"
            dest.mkdir(parents=True)
            (dest / "SKILL.md").write_text(
                "---\nname: helmet-issue\ndescription: foreign\n---\n\nforeign\n",
                encoding="utf-8",
            )
            result = skills.install_skills(targets=["codex", "claude"], prefix=prefix)
            self.assertFalse(any("claude:" in item for item in result))
            report = skills.last_install_report()
            self.assertTrue(report.conflicts)
            self.assertTrue(any("codex" in item for item in report.conflicts))
            self.assertEqual(report.skipped_hosts, ("codex", "claude"))
            self.assertFalse((prefix / ".claude").exists())
            self.assertIn("foreign", (dest / "SKILL.md").read_text(encoding="utf-8"))
            self.assertFalse((prefix / ".codex" / "skills" / "setup-helmet").exists())

    def test_mid_apply_failure_rolls_back_every_new_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "prefix"
            prefix.mkdir()
            real_replace = os.replace
            calls = {"count": 0}

            def fail_second(source: object, destination: object) -> None:
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("injected apply failure")
                real_replace(source, destination)

            with patch.object(skills.os, "replace", side_effect=fail_second):
                with self.assertRaisesRegex(skills.InstallError, "rolled back"):
                    skills.install_skills(targets=["codex", "claude"], prefix=prefix)
            self.assertFalse((prefix / ".codex").exists())
            self.assertFalse((prefix / ".claude").exists())

    def test_setup_skill_conflict_causes_zero_setup_or_github_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            foreign = home / ".hermes" / "skills" / "hermes-helmet" / "setup-helmet"
            foreign.mkdir(parents=True)
            (foreign / "SKILL.md").write_text("foreign\n", encoding="utf-8")
            answers = root / "answers.json"
            answers.write_text(json.dumps(_answers()), encoding="utf-8")
            before = {
                str(path.relative_to(home)): path.read_bytes()
                for path in home.rglob("*")
                if path.is_file()
            }
            github = FakeGitHub()
            with patch.object(github, "current_user", side_effect=AssertionError("GitHub must not be called")):
                with self.assertRaisesRegex(helmet_setup.SetupError, "skill conflict"):
                    helmet_setup.run_setup(
                        answers_path=answers,
                        home=home,
                        pat_provider=lambda: TOKEN,
                        github=github,
                        transport=FakeTransport(),
                    )
            after = {
                str(path.relative_to(home)): path.read_bytes()
                for path in home.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)
            self.assertFalse((home / ".hermes-helmet").exists())
            self.assertFalse(github.mutated)

    def test_setup_rejects_redirected_generated_and_skill_paths_before_github(self) -> None:
        for redirected in ("generated", "codex-skills"):
            with self.subTest(redirected=redirected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                home = root / "home"
                home.mkdir()
                outside = root / "outside"
                outside.mkdir()
                if redirected == "generated":
                    state = home / ".hermes-helmet"
                    state.mkdir(mode=0o700)
                    (state / "generated").symlink_to(outside, target_is_directory=True)
                else:
                    skill_root = home / ".codex"
                    skill_root.mkdir()
                    (skill_root / "skills").symlink_to(outside, target_is_directory=True)
                answers = root / "answers.json"
                answers.write_text(json.dumps(_answers()), encoding="utf-8")
                github = FakeGitHub()
                with patch.object(github, "current_user", side_effect=AssertionError("GitHub must not be called")):
                    with self.assertRaisesRegex(helmet_setup.SetupError, "made no changes|symlink"):
                        helmet_setup.run_setup(
                            answers_path=answers,
                            home=home,
                            pat_provider=lambda: TOKEN,
                            github=github,
                            transport=FakeTransport(),
                        )
                self.assertFalse(github.mutated)

    def test_post_preflight_install_failure_is_explicitly_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            answers = _answers()
            checkout = root / "repos" / "repo-0"
            answers["repositories"] = [{"slug": "example-org/demo-repo", "worktree": str(checkout)}]
            checkout.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(
                ["git", "-C", str(checkout), "remote", "add", "origin", "https://github.com/example-org/demo-repo.git"],
                check=True,
            )
            home = root / "home"
            home.mkdir()
            answers_path = root / "answers.json"
            answers_path.write_text(json.dumps(answers), encoding="utf-8")
            github = FakeGitHub()
            prompts = {"count": 0}

            def provide_token() -> str:
                prompts["count"] += 1
                return TOKEN

            with patch.object(
                helmet_setup,
                "install_skills",
                side_effect=skills.InstallError("injected sensitive installer detail"),
            ):
                with self.assertRaisesRegex(helmet_setup.SetupError, "incomplete.*resumed") as raised:
                    helmet_setup.run_setup(
                        answers_path=answers_path,
                        home=home,
                        pat_provider=provide_token,
                        github=github,
                        transport=FakeTransport(),
                    )
            self.assertNotIn("sensitive", str(raised.exception))
            state_path = home / ".hermes-helmet" / "setup-state.json"
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["status"], "incomplete")
            first_labels = list(github.created_labels)
            self.assertEqual(len(first_labels), 2)
            secret_path = home / ".hermes-helmet" / "secrets" / "github_worker_pat"
            first_secret = (secret_path.stat().st_mtime_ns, secret_path.read_bytes())

            report = helmet_setup.run_setup(
                answers_path=answers_path,
                home=home,
                pat_provider=provide_token,
                github=github,
                transport=FakeTransport(),
            )
            self.assertTrue(report.ok)
            self.assertEqual(prompts["count"], 1)
            self.assertEqual(github.created_labels, first_labels)
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["status"], "complete")
            self.assertEqual((secret_path.stat().st_mtime_ns, secret_path.read_bytes()), first_secret)
            self.assertEqual(secret_path.read_text(encoding="utf-8"), TOKEN)

    def test_post_preflight_conflict_race_stays_incomplete_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            answers = _answers()
            checkout = root / "repos" / "repo-0"
            answers["repositories"] = [{"slug": "example-org/demo-repo", "worktree": str(checkout)}]
            checkout.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(
                ["git", "-C", str(checkout), "remote", "add", "origin", "https://github.com/example-org/demo-repo.git"],
                check=True,
            )
            home = root / "home"
            home.mkdir()
            answers_path = root / "answers.json"
            answers_path.write_text(json.dumps(answers), encoding="utf-8")
            github = FakeGitHub()
            prompts = {"count": 0}

            def provide_token() -> str:
                prompts["count"] += 1
                return TOKEN

            foreign = home / ".claude" / "skills" / "setup-helmet"
            real_install = skills.install_skills

            def inject_conflict(**kwargs: object) -> list[str]:
                foreign.mkdir(parents=True)
                (foreign / "SKILL.md").write_text("sensitive-marker\n", encoding="utf-8")
                return real_install(**kwargs)  # type: ignore[arg-type]

            with patch.object(helmet_setup, "install_skills", side_effect=inject_conflict):
                with self.assertRaisesRegex(helmet_setup.SetupError, "changed after preflight.*incomplete") as raised:
                    helmet_setup.run_setup(
                        answers_path=answers_path,
                        home=home,
                        pat_provider=provide_token,
                        github=github,
                        transport=FakeTransport(),
                    )
            self.assertNotIn("sensitive-marker", str(raised.exception))
            self.assertNotIn(TOKEN, str(raised.exception))
            state_path = home / ".hermes-helmet" / "setup-state.json"
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["status"], "incomplete")
            first_labels = list(github.created_labels)
            secret_path = home / ".hermes-helmet" / "secrets" / "github_worker_pat"
            first_secret = (secret_path.stat().st_mtime_ns, secret_path.read_bytes())

            (foreign / "SKILL.md").unlink()
            foreign.rmdir()
            report = helmet_setup.run_setup(
                answers_path=answers_path,
                home=home,
                pat_provider=provide_token,
                github=github,
                transport=FakeTransport(),
            )
            self.assertTrue(report.ok)
            self.assertEqual(prompts["count"], 1)
            self.assertEqual(github.created_labels, first_labels)
            self.assertEqual((secret_path.stat().st_mtime_ns, secret_path.read_bytes()), first_secret)
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["status"], "complete")

    def test_openviking_and_fava_need_confirmation_before_external_action(self) -> None:
        selected = _answers(
            openviking={"selected": True, "confirmed": False},
            fava_trails={"selected": True, "confirmed": False},
        )
        with tempfile.TemporaryDirectory() as tmp:
            report = _run_setup(Path(tmp), selected)
            self.assertTrue(report.ok)
            self.assertFalse(report.external_openviking)
            self.assertFalse(report.external_fava)
            policy = authority.load_authority(
                Path(tmp) / "home" / ".hermes-helmet" / "policy.json"
            )
            self.assertFalse(policy.integrations.openviking)
            self.assertFalse(policy.integrations.fava_trails)

        confirmed = _answers(
            openviking={"selected": True, "confirmed": True},
            fava_trails={"selected": True, "confirmed": True},
        )
        with tempfile.TemporaryDirectory() as tmp:
            report = _run_setup(Path(tmp), confirmed)
            self.assertTrue(report.ok)
            self.assertTrue(report.external_openviking)
            self.assertTrue(report.external_fava)
            policy = authority.load_authority(
                Path(tmp) / "home" / ".hermes-helmet" / "policy.json"
            )
            self.assertTrue(policy.integrations.openviking)
            self.assertTrue(policy.integrations.fava_trails)

    def test_recommendations_and_company_pack_are_guidance_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = _run_setup(
                Path(tmp),
                _answers(
                    matt_pocock_skills={"accepted": True},
                    gstack={"accepted": True},
                    company_skill_pack={"declined": True},
                ),
            )
            public = report.to_public_dict()
            recs = {item["name"]: item for item in public["recommendations"]}
            self.assertIn("mattpocock-skills", recs)
            self.assertIn("gstack", recs)
            self.assertTrue(recs["mattpocock-skills"]["pin"])
            self.assertTrue(recs["gstack"]["source_url"].startswith("https://"))
            self.assertFalse(recs["mattpocock-skills"]["installed"])
            self.assertFalse(recs["gstack"]["installed"])
            self.assertFalse(recs["mattpocock-skills"]["runtime_dependency"])
            plan = public["company_skill_pack_plan"]
            self.assertEqual(plan["status"], "declined")
            self.assertIn("repo-bootstrap", plan["recommended_skill"])
            self.assertTrue(plan["never_copy_into_hermes_helmet"])
            self.assertFalse((Path(tmp) / "home" / ".hermes-helmet" / "private-skills").exists())

    def test_resume_after_interruption_reuses_secret_and_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = _run_setup(Path(tmp), _answers())
            secret = Path(tmp) / "home" / ".hermes-helmet" / "secrets" / "github_worker_pat"
            policy_path = Path(tmp) / "home" / ".hermes-helmet" / "policy.json"
            first_policy = policy_path.read_text(encoding="utf-8")
            called = {"n": 0}

            def _boom() -> str:
                called["n"] += 1
                raise AssertionError("PAT prompt must not rerun when secret exists")

            answers_path = Path(tmp) / "answers.json"
            second = helmet_setup.run_setup(
                answers_path=answers_path,
                home=Path(tmp) / "home",
                pat_provider=_boom,
                github=FakeGitHub(),
                transport=FakeTransport(),
                probe=True,
            )
            self.assertTrue(second.ok)
            self.assertEqual(called["n"], 0)
            self.assertEqual(secret.read_text(encoding="utf-8"), TOKEN)
            self.assertEqual(policy_path.read_text(encoding="utf-8"), first_policy)
            self.assertEqual(first.to_public_dict()["fingerprint"], second.to_public_dict()["fingerprint"])


class DoctorAndSkillTests(unittest.TestCase):
    def test_declined_optionals_pass_setup_and_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = _run_setup(Path(tmp), _answers())
            self.assertTrue(report.ok)
            home = Path(tmp) / "home"
            github = FakeGitHub(
                labels={"example-org/demo-repo": {"ready-for-agent", "hermes-kanban-go"}}
            )
            result = helmet_doctor.doctor(
                home / ".hermes-helmet" / "policy.json",
                home=home,
                skills_prefix=home,
                github=github,
                secret_file=home / ".hermes-helmet" / "secrets" / "github_worker_pat",
                model_transport=FakeTransport(),
            )
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["identity"]["ok"])
            self.assertTrue(result["file_modes"]["ok"])
            self.assertTrue(result["repository_allowlist"]["ok"])
            self.assertTrue(result["labels"]["ok"])
            self.assertTrue(result["checkouts"]["ok"])
            self.assertTrue(result["model_lanes"]["ok"])
            self.assertTrue(result["bundled_skills"]["ok"])
            self.assertTrue(result["company_skills"]["ok"])
            self.assertTrue(result["company_skills"]["skipped"])
            self.assertTrue(result["signal"]["ok"])
            self.assertTrue(result["signal"]["skipped"])
            self.assertFalse(result["signal"]["enabled"])
            self.assertTrue(result["openviking"]["skipped"])
            self.assertTrue(result["fava_trails"]["skipped"])
            self.assertTrue(result["policy_drift"]["ok"])
            self.assertFalse(github.mutated)
            self.assertEqual(github.created_labels, [])

    def test_doctor_does_not_mutate_labels_or_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _run_setup(Path(tmp), _answers())
            home = Path(tmp) / "home"
            before = {
                path: (path.stat().st_mtime_ns, path.read_bytes())
                for path in home.rglob("*")
                if path.is_file()
            }
            github = FakeGitHub(
                labels={"example-org/demo-repo": {"ready-for-agent", "hermes-kanban-go"}}
            )
            helmet_doctor.doctor(
                home / ".hermes-helmet" / "policy.json",
                home=home,
                skills_prefix=home,
                github=github,
                secret_file=home / ".hermes-helmet" / "secrets" / "github_worker_pat",
                model_transport=FakeTransport(),
            )
            self.assertFalse(github.mutated)
            after = {
                path: (path.stat().st_mtime_ns, path.read_bytes())
                for path in home.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)

    def test_setup_state_doctor_defaults_to_live_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _run_setup(Path(tmp), _answers())
            home = Path(tmp) / "home"
            read_only = FakeGitHub(
                repos={
                    "example-org/demo-repo": {
                        "permissions": {
                            "metadata": "read",
                            "contents": "read",
                            "pull_requests": "read",
                            "issues": "read",
                        }
                    }
                },
                labels={"example-org/demo-repo": {"ready-for-agent", "hermes-kanban-go"}},
            )
            with patch("hermes_helmet.doctor.default_github_client", return_value=read_only):
                result = helmet_doctor.doctor(
                    home / ".hermes-helmet" / "policy.json",
                    home=home,
                    model_transport=FakeTransport(),
                )
            self.assertFalse(result["ok"])
            self.assertFalse(result["repository_allowlist"]["ok"])
            self.assertNotIn("skipped", result["repository_allowlist"])

    def test_doctor_rejects_non_git_and_wrong_origin_checkouts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _run_setup(Path(tmp), _answers())
            home = Path(tmp) / "home"
            policy = authority.load_authority(home / ".hermes-helmet" / "policy.json")
            checkout = policy.repositories[0].worktree
            subprocess.run(
                ["git", "-C", str(checkout), "remote", "set-url", "--add", "--push", "origin", "ssh://git@github.com:2222/example-org/demo-repo.git"],
                check=True,
            )
            github = FakeGitHub(labels={"example-org/demo-repo": {"ready-for-agent", "hermes-kanban-go"}})
            result = helmet_doctor.doctor(
                home / ".hermes-helmet" / "policy.json",
                home=home,
                github=github,
                model_transport=FakeTransport(),
            )
            self.assertFalse(result["ok"])
            self.assertIn("origin-url-mismatch:example-org/demo-repo", result["checkouts"]["findings"])
            subprocess.run(
                ["git", "-C", str(checkout), "config", "--unset-all", "remote.origin.pushurl"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(checkout), "remote", "set-url", "origin", "https://user@github.com/example-org/demo-repo.git"],
                check=True,
            )
            result = helmet_doctor.doctor(
                home / ".hermes-helmet" / "policy.json",
                home=home,
                github=github,
                model_transport=FakeTransport(),
            )
            self.assertIn("origin-url-mismatch:example-org/demo-repo", result["checkouts"]["findings"])
            subprocess.run(["git", "-C", str(checkout), "remote", "remove", "origin"], check=True)
            git_dir = checkout / ".git"
            os.rename(git_dir, checkout / ".not-git")
            result = helmet_doctor.doctor(
                home / ".hermes-helmet" / "policy.json",
                home=home,
                github=github,
                model_transport=FakeTransport(),
            )
            self.assertIn("not-git-worktree:example-org/demo-repo", result["checkouts"]["findings"])

    def test_remote_parser_accepts_only_canonical_credential_free_github_urls(self) -> None:
        accepted = {
            "https://github.com/example-org/demo-repo.git",
            "https://github.com/example-org/demo-repo",
            "git@github.com:example-org/demo-repo.git",
            "ssh://git@github.com/example-org/demo-repo.git",
        }
        for remote in accepted:
            with self.subTest(remote=remote):
                self.assertEqual(helmet_doctor._remote_slug(remote), "example-org/demo-repo")
        rejected = {
            "https://user@github.com/example-org/demo-repo.git",
            "https://user:pass@github.com/example-org/demo-repo.git",
            "https://github.com:8443/example-org/demo-repo.git",
            "https://github.com/example-org/demo-repo.git?token=x",
            "https://github.com/example-org/demo-repo.git#fragment",
            "git://github.com/example-org/demo-repo.git",
            "ssh://root@github.com/example-org/demo-repo.git",
            "ssh://git@github.com:2222/example-org/demo-repo.git",
            "git@github.com:/example-org/demo-repo.git",
            "git@github.com:example-org/demo-repo.git?x=1",
        }
        for remote in rejected:
            with self.subTest(remote=remote):
                self.assertIsNone(helmet_doctor._remote_slug(remote))

    def test_setup_and_doctor_reject_unsafe_home_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            os.chmod(home, 0o777)
            answers = root / "answers.json"
            answers.write_text(json.dumps(_answers()), encoding="utf-8")
            with self.assertRaisesRegex(helmet_setup.SetupError, "group- or other-writable"):
                helmet_setup.run_setup(
                    answers_path=answers,
                    home=home,
                    pat_provider=lambda: TOKEN,
                    github=FakeGitHub(),
                    transport=FakeTransport(),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _run_setup(root, _answers())
            real_home = root / "home"
            linked_home = root / "linked-home"
            linked_home.symlink_to(real_home, target_is_directory=True)
            github = FakeGitHub()
            with self.assertRaisesRegex(helmet_setup.SetupError, "home must not be a symlink"):
                helmet_setup.run_setup(
                    answers_path=root / "answers.json",
                    home=linked_home,
                    github=github,
                    transport=FakeTransport(),
                )
            with patch.object(github, "current_user", side_effect=AssertionError("live probe must not run")):
                result = helmet_doctor.doctor(
                    real_home / ".hermes-helmet" / "policy.json",
                    home=linked_home,
                    github=github,
                    model_transport=FakeTransport(),
                )
            self.assertFalse(result["ok"])
            self.assertFalse(result["file_modes"]["ok"])

    def test_setup_and_doctor_reject_symlinked_credentials_and_unsafe_secret_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _run_setup(root, _answers())
            home = root / "home"
            secret = home / ".hermes-helmet" / "secrets" / "github_worker_pat"
            target = root / "outside-token"
            target.write_text(TOKEN, encoding="utf-8")
            os.chmod(target, 0o600)
            secret.unlink()
            secret.symlink_to(target)
            github = FakeGitHub(labels={"example-org/demo-repo": {"ready-for-agent", "hermes-kanban-go"}})
            with self.assertRaisesRegex(helmet_setup.SetupError, "must not be a symlink"):
                helmet_setup.run_setup(
                    answers_path=root / "answers.json",
                    home=home,
                    github=github,
                    transport=FakeTransport(),
                )
            result = helmet_doctor.doctor(
                home / ".hermes-helmet" / "policy.json",
                home=home,
                github=github,
                model_transport=FakeTransport(),
            )
            self.assertFalse(result["file_modes"]["ok"])
            self.assertFalse(github.mutated)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            external = root / "external-state"
            (external / "secrets").mkdir(parents=True, mode=0o700)
            os.chmod(external, 0o700)
            (home / ".hermes-helmet").symlink_to(external, target_is_directory=True)
            answers = root / "answers.json"
            answers.write_text(json.dumps(_answers()), encoding="utf-8")
            with self.assertRaisesRegex(helmet_setup.SetupError, "must not be a symlink"):
                helmet_setup.run_setup(
                    answers_path=answers,
                    home=home,
                    pat_provider=lambda: TOKEN,
                    github=FakeGitHub(),
                    transport=FakeTransport(),
                )

    def test_doctor_rejects_redirected_generated_and_skill_paths_before_live_probe(self) -> None:
        for redirected in ("generated", "claude-skills"):
            with self.subTest(redirected=redirected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _run_setup(root, _answers())
                home = root / "home"
                if redirected == "generated":
                    managed = home / ".hermes-helmet" / "generated"
                else:
                    managed = home / ".claude" / "skills"
                outside = root / f"outside-{redirected}"
                managed.rename(outside)
                managed.symlink_to(outside, target_is_directory=True)
                github = FakeGitHub()
                with patch.object(github, "current_user", side_effect=AssertionError("live probe must not run")):
                    result = helmet_doctor.doctor(
                        home / ".hermes-helmet" / "policy.json",
                        home=home,
                        github=github,
                        model_transport=FakeTransport(),
                    )
                self.assertFalse(result["ok"])
                self.assertFalse(result["file_modes"]["ok"])
                self.assertIn("symlink", " ".join(result["file_modes"]["findings"]))

    def test_legacy_doctor_human_output_is_preserved(self) -> None:
        report = {
            "ok": True,
            "policy": {"ok": True},
            "fava_trails": {"ok": True, "skipped": True},
            "openviking": {"ok": True, "skipped": True},
            "model_lanes": {
                "ok": True,
                "skipped_optional": True,
                "findings": ["legacy detail"],
            },
        }
        output = StringIO()
        with patch("hermes_helmet.cli.fava_doctor", return_value=report), redirect_stdout(output):
            code = cli_main(["doctor", "--config", "unused-policy.json"])
        self.assertEqual(code, 0)
        rendered = output.getvalue()
        self.assertIn("policy:      ok", rendered)
        self.assertIn("fava_trails: skipped", rendered)
        self.assertIn("openviking:  skipped", rendered)
        self.assertIn("model_lanes: ok", rendered)
        self.assertIn("[info] legacy detail", rendered)

    def test_setup_helmet_skill_matches_cli_and_avoids_helm_terms(self) -> None:
        skill = (
            ROOT / "skills" / "setup-helmet" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---"))
        self.assertIn("name: setup-helmet", skill)
        self.assertIn("helmet setup", skill)
        self.assertIn("helmet doctor", skill)
        self.assertIn("non-echoing", skill.casefold())
        self.assertIn("getpass", skill.casefold() + skill)
        self.assertNotIn("helm install", skill.casefold())
        self.assertNotIn("helm chart", skill.casefold())
        self.assertNotIn("kubernetes", skill.casefold())

    def test_cli_setup_and_doctor_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            answers = Path(tmp) / "answers.json"
            answer_payload = _answers()
            for index, repo in enumerate(answer_payload["repositories"]):
                repo["worktree"] = str(Path(tmp) / "repos" / f"repo-{index}")
            answers.write_text(json.dumps(answer_payload), encoding="utf-8")
            for repo in answer_payload["repositories"]:
                checkout = Path(str(repo["worktree"]))
                checkout.mkdir(parents=True, exist_ok=True)
                subprocess.run(["git", "init", "-q", str(checkout)], check=True)
                subprocess.run(
                    ["git", "-C", str(checkout), "remote", "add", "origin", f"https://github.com/{repo['slug']}.git"],
                    check=True,
                )
            github = FakeGitHub()
            with patch("hermes_helmet.setup.default_github_client", return_value=github), patch(
                "hermes_helmet.setup.default_transport", return_value=FakeTransport()
            ), patch(
                "hermes_helmet.setup.prompt_secret",
                side_effect=[TOKEN, "provider-test-key"],
            ):
                code = cli_main(
                    [
                        "setup",
                        "--answers",
                        str(answers),
                        "--home",
                        str(home),
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            github.labels["example-org/demo-repo"] = {"ready-for-agent", "hermes-kanban-go"}
            with patch("hermes_helmet.doctor.default_github_client", return_value=github), patch(
                "hermes_helmet.model_lanes.HttpOpenAITransport", return_value=FakeTransport()
            ):
                code = cli_main(
                    [
                        "doctor",
                        "--config",
                        str(home / ".hermes-helmet" / "policy.json"),
                        "--home",
                        str(home),
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
