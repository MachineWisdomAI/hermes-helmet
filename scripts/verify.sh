#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

for required in \
    README.md \
    LICENSE \
    AGENTS.md \
    CLAUDE.md \
    .gitignore \
    pyproject.toml \
    config/policy.example.json \
    config/setup.answers.example.json \
    config/fixtures/exampleco/policy.json \
    config/fixtures/exampleco/company-skills/repo-bootstrap/SKILL.md \
    config/fixtures/exampleco/openviking/company.template.json \
    config/fixtures/exampleco/openviking/templates/codex.ovcli.conf.example \
    config/fixtures/exampleco/openviking/templates/chatgpt.ovcli.conf.example \
    config/fixtures/exampleco/openviking/templates/hermes.ovcli.conf.example \
    config/fixtures/exampleco/model-lanes/company.template.json \
    CHANGELOG.md \
    CONTRIBUTING.md \
    SECURITY.md \
    CODE_OF_CONDUCT.md \
    NOTICE \
    docs/captain-and-crew.md \
    docs/authority-schema.md \
    docs/private-overlay.md \
    docs/runtime-image.md \
    docs/quickstart.md \
    docs/public-preview.md \
    docs/helmet-issue.md \
    docs/helmet-epic.md \
    docs/fava-trails.md \
    docs/company-skills.md \
    docs/openviking.md \
    docs/model-lanes.md \
    skills/setup-helmet/SKILL.md \
    skills/helmet-issue/SKILL.md \
    skills/helmet-epic/SKILL.md \
    src/hermes_helmet/helmet_issue.py \
    src/hermes_helmet/helmet_epic.py \
    src/hermes_helmet/fava_trails.py \
    src/hermes_helmet/cli.py \
    src/hermes_helmet/install_skills.py \
    src/hermes_helmet/company_skills.py \
    src/hermes_helmet/openviking.py \
    src/hermes_helmet/model_lanes.py \
    src/hermes_helmet/setup.py \
    src/hermes_helmet/doctor.py \
    src/hermes_helmet/public_surface.py \
    src/hermes_helmet/jj_toolchain.py \
    tests/test_public_boundary.py \
    tests/test_ci_supply_chain.py \
    tests/test_preview_runtime_image.py \
    tests/test_compare_trivy_reports.py \
    scripts/compare_trivy_reports.py \
    scripts/ci-product-proof.sh \
    .github/workflows/verify.yml \
    .github/workflows/preview-runtime-image.yml \
    .github/dependabot.yml \
    .github/PULL_REQUEST_TEMPLATE.md \
    .github/ISSUE_TEMPLATE/config.yml \
    .github/ISSUE_TEMPLATE/bug.md \
    .github/ISSUE_TEMPLATE/feature.md
do
    test -s "$required"
done

# Reject accidental private identifiers in the public-facing example surface.
# Tests may mention the forbidden markers only as negative examples.
# Canonical public FAVA pin links are allowed as tokens only: strip those
# references, then re-check remaining content so a legitimate citation cannot
# conceal a private default on the same line.
PYTHONPATH=src python3 - <<'PY'
from pathlib import Path
import sys
from hermes_helmet.public_surface import FAVA_PIN, has_forbidden_public_marker, scan_public_surface

assert has_forbidden_public_marker("owner=" + "yia-" + "mw-agent")
assert has_forbidden_public_marker('<svg aria-label="' + "grok-" + "4.5" + '">')
_repo = "Machine" + "WisdomAI/fava-" + "trails"
_readme = f"https://github.com/{_repo}/blob/{FAVA_PIN}/README.md"
assert not has_forbidden_public_marker(_readme)
assert has_forbidden_public_marker(_readme + "?owner=" + "yia-" + "mw-agent")
assert has_forbidden_public_marker(_readme + "#" + "wisdom" + "helm-builder")
assert has_forbidden_public_marker(
    f"https://github.com/{_repo}/tree/{FAVA_PIN}/" + "time" + "left--"
)

violations = scan_public_surface(Path("."))
if violations:
    sys.stderr.write("verify: private Machine Wisdom identifiers found in public surface\n")
    for item in violations[:50]:
        sys.stderr.write(item + "\n")
    sys.exit(1)
print("public surface: private identifiers absent after pin-token strip")
PY

# Syntax-check shell scripts.
for script in scripts/verify.sh scripts/ci-product-proof.sh scripts/build-dev-image.sh \
    scripts/smoke-dev-image.sh deploy/hermes/*.sh; do
    sh -n "$script"
done

# Ensure accepted FAVA Trails pin is importable for protocol-backed lifecycle checks.
FAVA_PIN="10f689f7455c0c5c5898f2a2e6bc8cf4fe84a6d7"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! "$PYTHON_BIN" -c 'import fava_trails, yaml' 2>/dev/null; then
    if command -v uv >/dev/null 2>&1; then
        VERIFY_VENV="${ROOT}/.venv-verify"
        if [ ! -x "${VERIFY_VENV}/bin/python" ]; then
            uv venv "${VERIFY_VENV}"
        fi
        # Prefer a local pin checkout when present; otherwise install the public pin.
        if [ -d "${FAVA_TRAILS_SRC:-}/src/fava_trails" ]; then
            uv pip install --python "${VERIFY_VENV}/bin/python" -e "${FAVA_TRAILS_SRC}"
        elif [ -d /opt/data/repos/fava-trails/src/fava_trails ]; then
            uv pip install --python "${VERIFY_VENV}/bin/python" -e /opt/data/repos/fava-trails
        else
            uv pip install --python "${VERIFY_VENV}/bin/python" \
                "git+https://github.com/MachineWisdomAI/fava-trails.git@${FAVA_PIN}"
        fi
        PYTHON_BIN="${VERIFY_VENV}/bin/python"
    else
        echo "verify: accepted FAVA Trails package is required (install fava-trails@${FAVA_PIN})" >&2
        exit 1
    fi
fi
export PYTHON_BIN
# jj is required for the accepted FAVA lifecycle demo.
if command -v jj >/dev/null 2>&1; then
    JJ_BIN="$(command -v jj)"
elif [ -x "${HOME}/.local/bin/jj" ]; then
    JJ_BIN="${HOME}/.local/bin/jj"
else
    echo "verify: jj is required for FAVA lifecycle protocol checks" >&2
    exit 1
fi
if ! PYTHONPATH=src "$PYTHON_BIN" -m hermes_helmet.jj_toolchain --check-binary "$JJ_BIN" >/dev/null; then
    echo "verify: exact jj 0.45.1 is required for accepted FAVA lifecycle protocol checks" >&2
    exit 1
fi

# Behavioral parity suite.
PYTHONPATH=src "$PYTHON_BIN" -m unittest discover -s tests -v

# Compose file must parse as YAML-ish structure (no docker required).
"$PYTHON_BIN" - <<'PY'
from pathlib import Path
text = Path("deploy/compose.yaml").read_text(encoding="utf-8")
assert "openviking" not in text.lower()
assert "signal" not in text.lower()
assert "fava" not in text.lower()
assert "wisdomloop" not in text.lower()
assert "wise-agents-toolkit" not in text.lower()
assert "services:" in text and "hermes:" in text
assert "../config/policy.json" in text
assert "./config/policy.example.json" not in text
assert 'user: "${HERMES_UID:-10000}:${HERMES_GID:-10000}"' in text
assert "os.getuid()!=0" in text.replace(" ", "")
dockerfile = Path("deploy/Dockerfile").read_text(encoding="utf-8")
assert "USER ${HERMES_UID}:${HERMES_GID}" in dockerfile or "USER 10000:10000" in dockerfile
assert 'chown -R "${HERMES_UID}:${HERMES_GID}" /opt/data' in dockerfile
assert "chown -R root:root /opt/hermes-helmet" in dockerfile
assert 'chown -R "${HERMES_UID}:${HERMES_GID}" /opt/hermes-helmet' not in dockerfile
assert "chmod -R go-w /opt/hermes-helmet" in dockerfile
entrypoint = Path("deploy/hermes/runtime-entrypoint.sh").read_text(encoding="utf-8")
assert "unset GH_TOKEN" in entrypoint and "unset GITHUB_TOKEN" in entrypoint
upstream = 'exec /init /opt/hermes/docker/main-wrapper.sh "$@"'
assert upstream in entrypoint
assert entrypoint.index("unset GH_TOKEN") < entrypoint.index(upstream)
assert entrypoint.index("unset GITHUB_TOKEN") < entrypoint.index(upstream)
# Optional company skill import runs on the supported startup path after PAT scrub
# and before upstream exec; skip/reject must remain non-blocking.
assert "run_company_skills_import" in entrypoint
assert "import-company-skills" in entrypoint
assert entrypoint.index("unset GH_TOKEN") < entrypoint.index("run_company_skills_import")
assert entrypoint.index("run_company_skills_import") < entrypoint.index(upstream)
# Canonical Hermes base pin: immutable tag+digest, no latest/main.
expected_base = (
    "nousresearch/hermes-agent:v2026.9.14@"
    "sha256:99641e57ec762c59e54cb44aa6746b7fc68c18b3c5ddb088af54234c613d9294"
)
assert f"ARG HERMES_BASE_IMAGE={expected_base}" in dockerfile
assert f"HERMES_BASE_IMAGE: ${{HERMES_BASE_IMAGE:-{expected_base}}}" in text
build_script = Path("scripts/build-dev-image.sh").read_text(encoding="utf-8")
assert f'BASE_IMAGE="${{HERMES_BASE_IMAGE:-{expected_base}}}"' in build_script
env_example = Path("deploy/.env.example").read_text(encoding="utf-8")
assert f"# HERMES_BASE_IMAGE={expected_base}" in env_example
for blob in (dockerfile, text, build_script, env_example):
    assert "hermes-agent:latest" not in blob
    assert "hermes-agent:main" not in blob
    assert "v2026.7.20" not in blob
print("compose surface excludes optional private services")
print("runtime preserves upstream init after root-owned policy and token scrub")
print("runtime entrypoint attempts optional company skill import non-blocking")
print(f"hermes base image defaults agree on {expected_base}")
PY

# Confirm control-loop source forbids merge / force-push command paths.
PYTHONPATH=src "$PYTHON_BIN" - <<'PY'
from pathlib import Path
source = Path("src/hermes_helmet/github_issue_poller.py").read_text(encoding="utf-8")
for needle in ("gh pr merge", "git push --force", "git push -f", "git push --force-with-lease"):
    assert needle not in source, needle
print("control loop has no merge/force-push code paths")
# helmet-issue may call the pulls merge API for Captain unattended mode only.
helmet = Path("src/hermes_helmet/helmet_issue.py").read_text(encoding="utf-8")
for needle in ("git push --force", "git push -f", "git push --force-with-lease"):
    assert needle not in helmet, needle
assert "create_task_once" in helmet
assert "should_create_repair_from_activity" in helmet
print("helmet-issue has no force-push paths and reuses H1 task creation")
epic = Path("src/hermes_helmet/helmet_epic.py").read_text(encoding="utf-8")
for needle in (
    "git push --force",
    "git push -f",
    "git push --force-with-lease",
    "gh pr merge",
):
    assert needle not in epic, needle
assert "run_preflight_and_adopt" in epic
assert "max_epic_parallelism" in epic
print("helmet-epic has no merge/force-push paths and invokes helmet-issue")
# Optional FAVA integration stays out of the public compose runtime and skips when declined.
from hermes_helmet.fava_trails import doctor as fava_doctor, run_governed_lifecycle_example
from pathlib import Path as _Path
fava_report = fava_doctor(_Path("config/fixtures/exampleco/policy.json"))
assert fava_report["ok"] is True
assert fava_report["fava_trails"]["skipped"] is True
lifecycle = run_governed_lifecycle_example()
assert lifecycle["ok"] is True, lifecycle
assert lifecycle.get("engine") == "accepted-fava-trail-manager", lifecycle.get("engine")
print("fava trails doctor skips when declined; accepted-engine lifecycle demo passes")
PY

# Portable skill static validation for Codex, Claude, and Hermes targets.
PYTHONPATH=src "$PYTHON_BIN" - <<'PY'
from pathlib import Path
from hermes_helmet.install_skills import default_skills_source, install_skills, static_validate_all_targets

bundled = default_skills_source()
assert bundled.is_dir(), bundled
assert (bundled / "helmet-issue" / "SKILL.md").is_file()
assert (bundled / "helmet-epic" / "SKILL.md").is_file()
assert (bundled / "setup-helmet" / "SKILL.md").is_file()
messages = static_validate_all_targets()
assert any("helmet-issue" in item for item in messages), messages
assert any("helmet-epic" in item for item in messages), messages
assert any("setup-helmet" in item for item in messages), messages
# Package default source must resolve without an explicit repo path.
assert default_skills_source().is_dir()
import tempfile
with tempfile.TemporaryDirectory() as tmp:
    installed = install_skills(
        targets=["codex", "claude", "hermes"],
        prefix=Path(tmp),
    )
    # Three skills × three targets.
    assert len(installed) == 9, installed
print("setup-helmet, helmet-issue, and helmet-epic validate for all targets")
print("canonical skills validate; packaged sdist/wheel asset coverage runs in tests.test_packaged_release")
PY

# Company skill pack import: skip without pack, import allowlisted, preserve, reject.
PYTHONPATH=src "$PYTHON_BIN" - <<'PY'
from pathlib import Path
import tempfile
from hermes_helmet import company_skills as cs
from hermes_helmet.authority import load_authority

policy = load_authority(Path("config/fixtures/exampleco/policy.json"))
assert not policy.skills.configured
with tempfile.TemporaryDirectory() as tmp:
    skipped = cs.import_company_skills_from_policy(policy, state_root=Path(tmp) / "state")
    assert skipped.status == cs.STATUS_SKIPPED, skipped
pack = Path("config/fixtures/exampleco/company-skills").resolve()
assert (pack / "repo-bootstrap" / "SKILL.md").is_file()
with tempfile.TemporaryDirectory() as tmp:
    state = Path(tmp) / "state"
    hermes_home = Path(tmp) / "hermes-home"
    hermes_home.mkdir()
    first = cs.import_company_skills(
        source=pack,
        allowlist=["repo-bootstrap"],
        state_root=state,
        hermes_home=hermes_home,
    )
    assert first.status == cs.STATUS_IMPORTED and first.mutated, first
    assert (state / "active").resolve().joinpath("skills/repo-bootstrap/SKILL.md").is_file()
    discovery = cs.executor_discovery_dir(state)
    assert discovery.is_dir()
    cfg = (hermes_home / "config.yaml").read_text(encoding="utf-8")
    assert "external_dirs" in cfg and str(discovery) in cfg
    preserved = cs.import_company_skills(
        source=Path(tmp) / "missing",
        allowlist=["repo-bootstrap"],
        state_root=state,
        hermes_home=hermes_home,
    )
    assert preserved.status == cs.STATUS_PRESERVED and not preserved.mutated, preserved
    rejected = cs.import_company_skills(
        source=pack,
        allowlist=["helmet-issue"],
        state_root=state,
        hermes_home=hermes_home,
    )
    assert rejected.status == cs.STATUS_REJECTED and not rejected.mutated, rejected
    catalog = cs.build_skill_catalog(state_root=state)
    public = catalog.to_public_dict()
    assert public["role_boundary"] == "captain_setup_vs_executor_import"
    assert public["generated_at"] == catalog.generated_at
    again = cs.build_skill_catalog(state_root=state).to_public_dict()
    assert again == public
    names = {item["name"] for item in public["skills"] if item.get("source") == "bundled"}
    assert {"setup-helmet", "helmet-issue", "helmet-epic"} <= names
print("company skill import skip/import/preserve/reject, discovery, and catalog ok")
PY

# Optional OpenViking surfaces stay out of minimum Compose and keep AGPL notice.
PYTHONPATH=src "$PYTHON_BIN" - <<'PY'
from pathlib import Path
import json
from hermes_helmet.authority import load_authority
from hermes_helmet.openviking import doctor

docs = Path("docs/openviking.md").read_text(encoding="utf-8")
assert "AGPL-3.0" in docs
assert "FAVA" in docs
assert "transcript" in docs.casefold()
assert "loopback" in docs.casefold()
assert "official" in docs.casefold()
assert "shared-all" in docs.casefold()
assert "captain/operator" in docs.casefold()
assert "retire the old key" in docs.casefold()
assert "non-acceptance" in docs.casefold()
assert "mcp-stdio" not in docs
company = json.loads(Path("config/fixtures/exampleco/openviking/company.template.json").read_text(encoding="utf-8"))
assert company["account"] == "exampleco"
assert company["license"] == "AGPL-3.0"
assert set(company["peers"]) >= {"codex", "chatgpt", "hermes"}
policy = load_authority(Path("config/fixtures/exampleco/policy.json"))
assert policy.integrations.openviking is False
report = doctor(Path("config/fixtures/exampleco/policy.json"))
assert report["ok"] is True
assert report["openviking"]["skipped"] is True
print("openviking optional integration templates and doctor skip path ok")
PY

# Optional model lanes stay out of minimum Compose and keep independent contracts.
PYTHONPATH=src "$PYTHON_BIN" - <<'PY'
from pathlib import Path
import json
from hermes_helmet.authority import load_authority
from hermes_helmet.model_lanes import doctor_model_lanes, secret_free_contract_examples

docs = Path("docs/model-lanes.md").read_text(encoding="utf-8")
assert "Ollama" in docs and "Unsloth Studio" in docs and "OrbStack" in docs
assert "loopback" in docs.casefold()
assert "host.docker.internal" in docs
assert "/v1/chat/completions" in docs
assert "/v1/embeddings" in docs
assert "re-index" in docs
assert "rollback" in docs
examples = secret_free_contract_examples()
assert examples["fava_generation"]["path"].endswith("/chat/completions")
assert "semantic" in examples["openviking_semantic_generation"]["note"].casefold()
assert examples["embeddings"]["path"].endswith("/embeddings")
policy = load_authority(Path("config/fixtures/exampleco/policy.json"))
assert policy.model_lanes.optional_lanes_selected is False
report = doctor_model_lanes(policy)
assert report.ok is True
assert report.skipped_optional is True
template = json.loads(Path("config/fixtures/exampleco/model-lanes/company.template.json").read_text(encoding="utf-8"))
assert "/v1/embeddings" in json.dumps(template)
print("model-lanes optional skip path and secret-free contracts ok")
PY

empty_tree=$(git hash-object -t tree /dev/null)
git diff --check "$empty_tree" HEAD
git diff --cached --check
git diff --check

echo "Hermes Helmet verification passed."
