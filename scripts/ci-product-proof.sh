#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -n "${PYTHON_BIN:-}" ]; then
    :
elif [ -x "${ROOT}/.venv-verify/bin/python" ]; then
    PYTHON_BIN="${ROOT}/.venv-verify/bin/python"
else
    PYTHON_BIN="python3"
fi

PYTHONPATH=src "$PYTHON_BIN" - <<'PY'
import json
import subprocess
import tempfile
from pathlib import Path

from hermes_helmet import authority
from hermes_helmet import company_skills
from hermes_helmet import fava_trails
from hermes_helmet import github_issue_poller
from hermes_helmet import install_poller
from hermes_helmet import model_lanes
from hermes_helmet import openviking

root = Path.cwd()
fixture = root / "config/fixtures/exampleco/policy.json"
policy = authority.load_authority(fixture)
assert not policy.integrations.openviking
assert not policy.integrations.fava_trails
contract = authority.render_crew_contract(policy)
assert "example-captain" in contract and "example-agent" in contract


class SyntheticGitHub:
    issue_url = "https://github.com/example-org/demo-repo/issues/7"

    def __init__(self):
        self.create_commands = []

    def run(self, command):
        if command[:3] == [github_issue_poller.GH, "api", "user"]:
            return "example-agent\n"
        if command[:3] == [github_issue_poller.GH, "api", "--paginate"]:
            if "/issues?" in command[-1]:
                return json.dumps(
                    [
                        {
                            "number": 7,
                            "title": "Exercise the minimum quickstart",
                            "body": "Synthetic CI proof.",
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
        if command[:4] == [
            github_issue_poller.HERMES,
            "kanban",
            "--board",
            "default",
        ] and "create" in command:
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
        raise AssertionError(f"unexpected synthetic command: {command!r}")


with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    checkout = temp / "demo-repo"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "remote",
            "add",
            "origin",
            "https://github.com/example-org/demo-repo.git",
        ],
        check=True,
    )
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    raw["repositories"] = [
        {"slug": "example-org/demo-repo", "worktree": str(checkout)}
    ]
    profile = temp / "policy.json"
    profile.write_text(json.dumps(raw), encoding="utf-8")
    ledger = temp / "ledger.sqlite3"
    launcher = temp / "github_issue_poller.py"
    install_poller.install(
        policy_path=profile,
        ledger_path=ledger,
        hermes_bin=Path("/missing/hermes"),
        script_path=launcher,
        configure_model=False,
        reconcile_cron=False,
        import_company_skills=False,
    )
    synthetic = SyntheticGitHub()
    result = github_issue_poller.run_once(
        github_issue_poller.load_policy(profile), ledger, synthetic
    )
    assert launcher.is_file() and result.errors == []
    assert [(issue.url, task_id) for issue, task_id in result.created] == [
        (synthetic.issue_url, "t_quickstart")
    ]
    assert len(synthetic.create_commands) == 1
    create = synthetic.create_commands[0]
    assert create[create.index("--idempotency-key") + 1] == synthetic.issue_url
    assert create[create.index("--created-by") + 1] == "github-issue-poller"
    assert create[create.index("--workspace") + 1] == f"worktree:{checkout.resolve()}"

    replay = github_issue_poller.run_once(
        github_issue_poller.load_policy(profile), ledger, synthetic
    )
    assert replay.created == [] and replay.errors == []
    assert len(synthetic.create_commands) == 1

    state = temp / "company-skills"
    hermes_home = temp / "hermes-home"
    hermes_home.mkdir()
    imported = company_skills.import_company_skills(
        source=root / "config/fixtures/exampleco/company-skills",
        allowlist=["repo-bootstrap"],
        state_root=state,
        hermes_home=hermes_home,
    )
    assert imported.status == company_skills.STATUS_IMPORTED

    template = openviking.default_company_template()
    key = "synthetic-user-key-with-enough-length"
    configs = {
        client: openviking.write_client_config(
            path, client=client, template=template, api_key=key
        )
        for client, path in openviking.default_client_config_paths(
            temp / "openviking"
        ).items()
    }
    service = openviking.FakeOpenVikingService()
    service.register_user_key(
        key, account=template.account, user=template.user, role="user"
    )
    proof = openviking.cross_client_marker_roundtrip(
        configs, transport=service.transport
    )
    assert proof["ok"] and proof["verification_scope"] == "synthetic_non_acceptance"

    fava = fava_trails.run_governed_lifecycle_example(
        engine=fava_trails.FakeFavaEngine()
    )
    assert fava["ok"] and fava["engine"] == "fake-fava-lifecycle"

    lane = model_lanes.lane_from_mapping(
        "fava_generation",
        {
            "kind": "chat_completions",
            "provider": "openai-compatible",
            "model": "synthetic-chat",
            "base_url": "http://127.0.0.1:11434/v1",
            "timeout_seconds": 5,
            "structured_output": True,
        },
    )
    transport = model_lanes.FakeOpenAITransport(chat_content='{"ok": true}')
    probe = model_lanes.probe_chat_completions(
        lane, transport=transport, credential="synthetic-provider-credential"
    )
    assert probe.ok and transport.chat_called

print("minimum quickstart: policy, contract, poller install, and synthetic trigger passed")
print("optional profiles: company skills, OpenViking, FAVA, and provider fixtures passed")
PY
