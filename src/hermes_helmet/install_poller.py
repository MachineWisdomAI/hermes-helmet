#!/usr/bin/env python3
"""Install or reconcile the in-container GitHub issue poller cron job.

Runs inside the Hermes Helmet container (or any host that already has Hermes
and the poller package on PYTHONPATH). Does not require Docker Compose, Signal,
OpenViking, WisdomLoop, FAVA Trails, or any skill pack.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Sequence

from hermes_helmet import github_issue_poller as poller
from hermes_helmet.company_skills import (
    CompanySkillError,
    import_company_skills_from_policy,
)

JOB_NAME = "github-issue-poller"
DEFAULT_POLICY = Path("/opt/data/github-issue-poller/policy.json")
DEFAULT_LEDGER = Path("/opt/data/github-issue-poller/ledger.sqlite3")
DEFAULT_SCRIPT = Path("/opt/data/scripts/github_issue_poller.py")
DEFAULT_HERMES = poller.DEFAULT_HERMES

LAUNCHER_TEMPLATE = """#!/usr/bin/env python3
\"\"\"Installed Hermes Helmet GitHub issue poller launcher.\"\"\"
from __future__ import annotations

import sys
from pathlib import Path

# Prefer the image-installed package.
for candidate in ("/opt/hermes-helmet/src",):
    if candidate not in sys.path and Path(candidate).is_dir():
        sys.path.insert(0, candidate)

from hermes_helmet.github_issue_poller import main

raise SystemExit(main([
    "--config", {policy!r},
    "--ledger", {ledger!r},
]))
"""


class InstallError(RuntimeError):
    """Installation cannot safely complete."""


def _run(command: Sequence[str], *, input_text: str | None = None) -> str:
    completed = subprocess.run(
        list(command),
        input=input_text,
        text=True,
        capture_output=True,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip() or "command failed"
        raise InstallError(detail)
    return completed.stdout


def _hermes_python(hermes_bin: Path) -> Path:
    candidate = hermes_bin.resolve().parent / "python"
    if candidate.is_file():
        return candidate
    which = shutil.which("python3") or shutil.which("python")
    if which:
        return Path(which)
    return Path(sys.executable)


def _install_launcher(script_path: Path, policy_path: Path, ledger_path: Path) -> None:
    script_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = LAUNCHER_TEMPLATE.format(
        policy=str(policy_path),
        ledger=str(ledger_path),
    )
    tmp = script_path.with_suffix(script_path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.chmod(0o700)
    tmp.replace(script_path)


def _cron_reconciliation_program(
    *,
    schedule: str,
    deliver: str,
    script_path: Path,
) -> str:
    return f"""
from cron.jobs import create_job, list_jobs, update_job
jobs = [job for job in list_jobs(include_disabled=True) if job.get('name') == {JOB_NAME!r}]
if len(jobs) > 1:
    raise SystemExit('multiple {JOB_NAME} cron jobs found')
updates = {{
    'schedule': {schedule!r},
    'script': {str(script_path)!r},
    'no_agent': True,
    'deliver': {deliver!r},
    'enabled': True,
    'state': 'scheduled',
}}
if jobs:
    update_job(jobs[0]['id'], updates)
    print('updated ' + jobs[0]['id'])
else:
    job = create_job(
        prompt='',
        schedule={schedule!r},
        name={JOB_NAME!r},
        deliver={deliver!r},
        script={str(script_path)!r},
        no_agent=True,
    )
    print('created ' + job['id'])
"""


def _cron_validation_program(
    *,
    schedule: str,
    deliver: str,
    script_path: Path,
) -> str:
    expected = {
        "schedule_display": schedule,
        "script": str(script_path),
        "no_agent": True,
        "deliver": deliver,
        "enabled": True,
        "state": "scheduled",
    }
    return f"""
from cron.jobs import list_jobs
jobs = [job for job in list_jobs(include_disabled=True) if job.get('name') == {JOB_NAME!r}]
if len(jobs) != 1:
    raise SystemExit('expected exactly one {JOB_NAME} cron job')
for key, value in {expected!r}.items():
    if jobs[0].get(key) != value:
        raise SystemExit(f'{{key}} is not configured as expected')
"""


def _verify_worktrees(policy: poller.Policy) -> None:
    for repository in policy.repositories:
        if not repository.worktree.is_dir():
            raise InstallError(
                f"allowlisted worktree is unavailable: {repository.worktree}"
            )
        try:
            state = _run(
                [
                    "git",
                    "-C",
                    str(repository.worktree),
                    "rev-parse",
                    "--is-inside-work-tree",
                ]
            ).strip()
        except InstallError as exc:
            raise InstallError(
                f"allowlisted worktree is not a Git repository: {repository.worktree}"
            ) from exc
        if state != "true":
            raise InstallError(
                f"allowlisted worktree is not a Git repository: {repository.worktree}"
            )


def _configure_worker_model(policy: poller.Policy, hermes_bin: Path) -> None:
    common = [str(hermes_bin), "-p", policy.assignee]
    _run([*common, "config", "set", "model.provider", policy.inference_provider])
    _run([*common, "config", "set", "model.default", policy.inference_model])
    _run([*common, "config", "set", "agent.max_turns", str(policy.worker_max_turns)])


def install(
    *,
    policy_path: Path,
    ledger_path: Path,
    hermes_bin: Path,
    script_path: Path = DEFAULT_SCRIPT,
    configure_model: bool = True,
    reconcile_cron: bool = True,
    import_company_skills: bool = True,
    company_skills_state_root: Path | None = None,
) -> None:
    if not policy_path.is_file():
        raise InstallError(f"policy is missing: {policy_path}")
    try:
        policy = poller.load_policy(policy_path)
    except poller.PollerError as exc:
        raise InstallError(f"invalid policy: {exc}") from exc

    ledger_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _verify_worktrees(policy)
    _install_launcher(script_path, policy_path, ledger_path)

    if configure_model and (hermes_bin.is_file() or shutil.which(str(hermes_bin))):
        resolved = hermes_bin if hermes_bin.is_file() else Path(shutil.which(str(hermes_bin)) or hermes_bin)
        _configure_worker_model(policy, resolved)

    if reconcile_cron:
        if not hermes_bin.is_file() and not shutil.which(str(hermes_bin)):
            raise InstallError(f"hermes binary is unavailable: {hermes_bin}")
        resolved = hermes_bin if hermes_bin.is_file() else Path(shutil.which(str(hermes_bin)) or hermes_bin)
        python = _hermes_python(resolved)
        _run(
            [
                str(python),
                "-c",
                _cron_reconciliation_program(
                    schedule=policy.schedule,
                    deliver=policy.cron_deliver,
                    script_path=script_path,
                ),
            ]
        )
        _run(
            [
                str(python),
                "-c",
                _cron_validation_program(
                    schedule=policy.schedule,
                    deliver=policy.cron_deliver,
                    script_path=script_path,
                ),
            ]
        )

    if import_company_skills:
        # Optional executor pack: skipped/preserved/rejected never block core install.
        try:
            from hermes_helmet.authority import load_policy as load_authority_policy

            authority_policy = load_authority_policy(policy_path)
            result = import_company_skills_from_policy(
                authority_policy,
                state_root=company_skills_state_root,
            )
            print(
                f"company skill import: status={result.status} message={result.message}"
            )
        except CompanySkillError as exc:
            print(f"company skill import: status=rejected message={exc}")
        except Exception as exc:  # noqa: BLE001 - optional path must not block startup
            print(f"company skill import: status=skipped message={exc}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT)
    parser.add_argument("--hermes", type=Path, default=Path(DEFAULT_HERMES))
    parser.add_argument(
        "--skip-model-config",
        action="store_true",
        help="Do not write provider/model/max_turns into the worker profile.",
    )
    parser.add_argument(
        "--skip-cron",
        action="store_true",
        help="Validate policy and worktrees only; do not touch Hermes cron.",
    )
    parser.add_argument(
        "--skip-company-skills",
        action="store_true",
        help="Do not attempt optional company skill-pack import.",
    )
    parser.add_argument(
        "--company-skills-state-root",
        type=Path,
        default=None,
        help="Hermes-owned company-skills state root (default under HERMES_HOME).",
    )
    args = parser.parse_args(argv)
    try:
        install(
            policy_path=args.config,
            ledger_path=args.ledger,
            hermes_bin=args.hermes,
            script_path=args.script,
            configure_model=not args.skip_model_config,
            reconcile_cron=not args.skip_cron,
            import_company_skills=not args.skip_company_skills,
            company_skills_state_root=args.company_skills_state_root,
        )
    except (InstallError, OSError) as exc:
        print(f"Hermes Helmet poller install failed: {exc}", file=sys.stderr)
        return 1
    print("Hermes Helmet poller installed; Hermes cron owns the persisted schedule.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
