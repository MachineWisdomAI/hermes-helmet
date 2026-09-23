#!/usr/bin/env python3
"""Prove setup, doctor, status, skills, company pack, and min-runtime from an installed wheel."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Mapping


TOKEN = "github_pat_" + ("0" * 40)


class FakeGitHub:
    def __init__(self) -> None:
        self.login = "example-agent"
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
        self.labels = {"example-org/demo-repo": set()}
        self.mutated = False

    def current_user(self, token: str) -> str:
        if not token:
            raise RuntimeError("github token is missing")
        return self.login

    def repository_access(self, token: str, slug: str) -> dict[str, object]:
        if slug not in self.repos:
            raise RuntimeError("repository is not accessible")
        return dict(self.repos[slug])

    def list_accessible_repository_slugs(self, token: str) -> tuple[str, ...]:
        return tuple(self.repos.keys())

    def list_visible_repositories(self, token: str) -> tuple[dict[str, object], ...]:
        return tuple({"slug": slug, "private": True} for slug in self.repos)

    def list_labels(self, token: str, slug: str) -> tuple[str, ...]:
        return tuple(sorted(self.labels.get(slug, set())))

    def create_label(self, token: str, slug: str, name: str) -> None:
        self.mutated = True
        self.labels.setdefault(slug, set()).add(name)


class FakeTransport:
    def request(self, method, url, headers, body, timeout):  # noqa: ANN001
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


def _answers(work: Path) -> dict[str, object]:
    checkout = work / "repos" / "demo-repo"
    return {
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
        "repositories": [{"slug": "example-org/demo-repo", "worktree": str(checkout)}],
        "openviking": {"selected": False, "confirmed": False},
        "fava_trails": {"selected": False, "confirmed": False},
        "matt_pocock_skills": {"accepted": False},
        "gstack": {"accepted": False},
        "company_skill_pack": {"declined": True},
    }


def _init_checkout(path: Path, slug: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", f"https://github.com/{slug}.git"],
        check=True,
    )


def _write_fake_gh(path: Path, issue_url: str) -> None:
    issue_json = json.dumps(
        {
            "number": 1,
            "title": "packaged status",
            "body": "read-only",
            "state": "open",
            "html_url": issue_url,
            "labels": [{"name": "ready-for-agent"}],
        }
    )
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"ISSUE = {issue_json!r}\n"
        "args = sys.argv[1:]\n"
        "if not args or args[0] != 'api':\n"
        "    sys.exit(2)\n"
        "endpoint = args[-1]\n"
        "if endpoint.endswith('/issues/1'):\n"
        "    print(ISSUE); sys.exit(0)\n"
        "if '/timeline' in endpoint or '/pulls' in endpoint:\n"
        "    print('[]'); sys.exit(0)\n"
        "if endpoint == 'user':\n"
        "    print(json.dumps({'login': 'example-captain'})); sys.exit(0)\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _require_site_packages() -> None:
    import hermes_helmet

    pkg = Path(hermes_helmet.__file__).resolve()
    if "site-packages" not in pkg.parts:
        raise SystemExit(f"hermes_helmet is not the installed wheel: {pkg}")


def _site_packages(console: Path) -> Path:
    venv = console.resolve().parent.parent
    matches = sorted((venv / "lib").glob("python*/site-packages"))
    if not matches:
        raise SystemExit(f"venv site-packages missing under {venv}")
    return matches[-1]


def _install_cli_fakes(console: Path) -> None:
    """Patch the proof venv only. The installed wheel itself is unchanged."""

    site = _site_packages(console)
    (site / "hermes_helmet_proof_fakes.py").write_text(
        "import hermes_helmet.doctor as helmet_doctor\n"
        "import hermes_helmet.model_lanes as model_lanes\n"
        "import hermes_helmet.setup as helmet_setup\n"
        "\n"
        "TOKEN = 'github_pat_' + ('0' * 40)\n"
        "\n"
        "class FakeGitHub:\n"
        "    def __init__(self):\n"
        "        self.login = 'example-agent'\n"
        "        self.repos = {\n"
        "            'example-org/demo-repo': {\n"
        "                'permissions': {\n"
        "                    'metadata': 'read',\n"
        "                    'contents': 'write',\n"
        "                    'pull_requests': 'write',\n"
        "                    'issues': 'write',\n"
        "                }\n"
        "            }\n"
        "        }\n"
        "        self.labels = {'example-org/demo-repo': {'ready-for-agent', 'hermes-kanban-go'}}\n"
        "        self.mutated = False\n"
        "\n"
        "    def current_user(self, token):\n"
        "        if not token:\n"
        "            raise RuntimeError('github token is missing')\n"
        "        return self.login\n"
        "\n"
        "    def repository_access(self, token, slug):\n"
        "        if slug not in self.repos:\n"
        "            raise RuntimeError('repository is not accessible')\n"
        "        return dict(self.repos[slug])\n"
        "\n"
        "    def list_accessible_repository_slugs(self, token):\n"
        "        return tuple(self.repos.keys())\n"
        "\n"
        "    def list_labels(self, token, slug):\n"
        "        return tuple(sorted(self.labels.get(slug, set())))\n"
        "\n"
        "    def create_label(self, token, slug, name):\n"
        "        self.mutated = True\n"
        "        self.labels.setdefault(slug, set()).add(name)\n"
        "\n"
        "class FakeTransport:\n"
        "    def request(self, method, url, headers=None, body=None, timeout=None):\n"
        "        return {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}\n"
        "\n"
        "def _github():\n"
        "    return FakeGitHub()\n"
        "\n"
        "def _transport():\n"
        "    return FakeTransport()\n"
        "\n"
        "def _secret(prompt='GitHub worker PAT: '):\n"
        "    if 'provider' in prompt.casefold():\n"
        "        return 'provider-test-key'\n"
        "    return TOKEN\n"
        "\n"
        "helmet_setup.default_github_client = _github\n"
        "helmet_setup.default_transport = _transport\n"
        "helmet_setup.prompt_secret = _secret\n"
        "helmet_doctor.default_github_client = _github\n"
        "model_lanes.HttpOpenAITransport = FakeTransport\n",
        encoding="utf-8",
    )
    (site / "hermes_helmet_proof_fakes.pth").write_text(
        "import hermes_helmet_proof_fakes\n",
        encoding="utf-8",
    )
    (site / "sitecustomize.py").write_text(
        "import hermes_helmet_proof_fakes  # noqa: F401\n",
        encoding="utf-8",
    )


def _console_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["PYTHONPATH"] = ""
    return env


def _run_console(console: Path, argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(console), *argv],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd),
        env=_console_env(),
    )


def _prove_setup_and_doctor(console: Path, work: Path) -> Path:
    _install_cli_fakes(console)
    answers = _answers(work)
    checkout = Path(str(answers["repositories"][0]["worktree"]))  # type: ignore[index]
    _init_checkout(checkout, "example-org/demo-repo")
    home = work / "home"
    home.mkdir()
    answers_path = work / "answers.json"
    answers_path.write_text(json.dumps(answers), encoding="utf-8")
    setup = _run_console(
        console,
        ["setup", "--answers", str(answers_path), "--home", str(home), "--json"],
        cwd=work,
    )
    if setup.returncode != 0:
        raise SystemExit(f"setup failed: {setup.stdout}\n{setup.stderr}")
    payload = json.loads(setup.stdout)
    if not payload.get("ok"):
        raise SystemExit(f"setup failed: {payload}")
    print("setup: ok")
    doctor = _run_console(
        console,
        [
            "doctor",
            "--config",
            str(home / ".hermes-helmet" / "policy.json"),
            "--home",
            str(home),
            "--json",
        ],
        cwd=work,
    )
    if doctor.returncode != 0:
        raise SystemExit(f"doctor failed: {doctor.stdout}\n{doctor.stderr}")
    result = json.loads(doctor.stdout)
    if not result.get("ok"):
        raise SystemExit(f"doctor failed: {result}")
    if not result.get("company_skills", {}).get("skipped"):
        raise SystemExit(f"minimum path must skip company skills: {result}")
    if not result.get("openviking", {}).get("skipped"):
        raise SystemExit(f"minimum path must skip OpenViking: {result}")
    if not result.get("fava_trails", {}).get("skipped"):
        raise SystemExit(f"minimum path must skip FAVA Trails: {result}")
    print("doctor: ok")
    return home


def _prove_status(console: Path, home: Path, work: Path) -> None:
    issue_url = "https://github.com/example-org/demo-repo/issues/1"
    gh_bin = work / "fake-gh"
    hermes_bin = work / "fake-hermes"
    _write_fake_gh(gh_bin, issue_url)
    hermes_bin.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    hermes_bin.chmod(hermes_bin.stat().st_mode | stat.S_IXUSR)
    ledger = work / "missing-ledger.sqlite3"
    checkpoints = work / "checkpoints"
    completed = _run_console(
        console,
        [
            "status",
            issue_url,
            "--config",
            str(home / ".hermes-helmet" / "policy.json"),
            "--ledger",
            str(ledger),
            "--checkpoint-dir",
            str(checkpoints),
            "--gh",
            str(gh_bin),
            "--hermes",
            str(hermes_bin),
            "--json",
        ],
        cwd=work,
    )
    if completed.returncode != 0:
        raise SystemExit(f"status failed: {completed.stdout}\n{completed.stderr}")
    report = json.loads(completed.stdout)
    if report.get("issue_url") != issue_url or "terminal" not in report:
        raise SystemExit(f"status payload unexpected: {report}")
    if ledger.exists() or checkpoints.exists():
        raise SystemExit("status created ledger or checkpoint state")
    print("status: ok")
    print("status-readonly: ok")


def _prove_skills(console: Path, work: Path) -> None:
    from hermes_helmet.install_skills import default_skills_source, install_skills

    source = default_skills_source()
    for name in ("setup-helmet", "helmet-issue", "helmet-epic"):
        if not (source / name / "SKILL.md").is_file():
            raise SystemExit(f"bundled skill missing from package: {name}")
    prefix = work / "skill-prefix"
    prefix.mkdir()
    installed = install_skills(targets=["codex", "claude", "hermes"], prefix=prefix)
    if len(installed) != 9:
        raise SystemExit(f"expected 9 skill installs, got {installed}")
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["PYTHONPATH"] = ""
    help_run = subprocess.run(
        [str(console), "install-skills", "--help"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if help_run.returncode != 0 or "install-skills" not in help_run.stdout + help_run.stderr:
        raise SystemExit(help_run.stdout + help_run.stderr)
    print("bundled-skills: ok")


def _prove_company_skills(fixture_root: Path, work: Path) -> None:
    from hermes_helmet import company_skills as cs

    pack = fixture_root / "config/fixtures/exampleco/company-skills"
    state = work / "company-skills"
    hermes_home = work / "hermes-home"
    hermes_home.mkdir()
    imported = cs.import_company_skills(
        source=pack,
        allowlist=["repo-bootstrap"],
        state_root=state,
        hermes_home=hermes_home,
    )
    if imported.status != cs.STATUS_IMPORTED:
        raise SystemExit(f"company skill import failed: {imported}")
    print("company-skills: ok")


def _prove_quickstart(fixture_root: Path, work: Path) -> None:
    from hermes_helmet import authority
    from hermes_helmet import github_issue_poller
    from hermes_helmet import install_poller

    fixture = fixture_root / "config/fixtures/exampleco/policy.json"
    policy = authority.load_authority(fixture)
    if policy.integrations.openviking or policy.integrations.fava_trails:
        raise SystemExit("default integration profile must decline optional services")
    checkout = work / "quickstart-repo"
    _init_checkout(checkout, "example-org/demo-repo")
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    raw["repositories"] = [{"slug": "example-org/demo-repo", "worktree": str(checkout)}]
    profile = work / "quickstart-policy.json"
    profile.write_text(json.dumps(raw), encoding="utf-8")
    ledger = work / "quickstart-ledger.sqlite3"
    launcher = work / "github_issue_poller.py"
    install_poller.install(
        policy_path=profile,
        ledger_path=ledger,
        hermes_bin=Path("/missing/hermes"),
        script_path=launcher,
        configure_model=False,
        reconcile_cron=False,
        import_company_skills=False,
    )

    class SyntheticGitHub:
        issue_url = "https://github.com/example-org/demo-repo/issues/7"

        def __init__(self) -> None:
            self.create_commands: list[list[str]] = []

        def run(self, command: list[str]) -> str:
            if command[:3] == [github_issue_poller.GH, "api", "user"]:
                return "example-agent\n"
            if command[:3] == [github_issue_poller.GH, "api", "--paginate"]:
                if "/issues?" in command[-1]:
                    return json.dumps(
                        [
                            {
                                "number": 7,
                                "title": "Exercise the minimum quickstart",
                                "body": "Synthetic packaged proof.",
                                "state": "open",
                                "labels": [{"name": "hermes-kanban-go"}],
                            }
                        ]
                    )
                return "[]"
            if command[:2] == ["git", "-C"] and command[3:] == [
                "rev-parse",
                "--is-inside-work-tree",
            ]:
                return "true\n"
            if command[:5] == [
                github_issue_poller.HERMES,
                "kanban",
                "--board",
                "default",
                "create",
            ] or (
                command[:4]
                == [github_issue_poller.HERMES, "kanban", "--board", "default"]
                and "create" in command
            ):
                self.create_commands.append(command)
                return json.dumps({"id": "t_quickstart"})
            if command[:5] == [
                github_issue_poller.HERMES,
                "kanban",
                "--board",
                "default",
                "show",
            ]:
                return json.dumps(
                    {
                        "task": {
                            "id": "t_quickstart",
                            "status": "running",
                            "workspace_kind": "worktree",
                            "workspace_path": "/unused",
                            "branch_name": "automation/demo-repo-7",
                        },
                        "comments": [],
                        "runs": [],
                    }
                )
            raise AssertionError(command)

    synthetic = SyntheticGitHub()
    result = github_issue_poller.run_once(
        github_issue_poller.load_policy(profile), ledger, synthetic
    )
    if result.errors or not result.created:
        raise SystemExit(f"quickstart poller failed: {result}")
    replay = github_issue_poller.run_once(
        github_issue_poller.load_policy(profile), ledger, synthetic
    )
    if replay.created or replay.errors:
        raise SystemExit("quickstart replay was not idempotent")
    print("quickstart: ok")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--console", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    _require_site_packages()
    home = _prove_setup_and_doctor(args.console, args.work)
    _prove_status(args.console, home, args.work)
    _prove_skills(args.console, args.work)
    _prove_company_skills(args.fixture_root, args.work)
    _prove_quickstart(args.fixture_root, args.work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
