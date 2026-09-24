#!/usr/bin/env python3
"""Hermes Helmet CLI: status, issue/epic helpers, doctor, FAVA Trails, OpenViking, skills."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from hermes_helmet import doctor as helmet_doctor
from hermes_helmet import setup as helmet_setup

from hermes_helmet.authority import AuthorityError, load_authority
from hermes_helmet.fava_trails import (
    FavaTrailsError,
    default_principal_paths,
    doctor as fava_doctor,
    load_company_template,
    prepare_data_repo_scaffold,
    render_company_template,
    render_mcp_registration_example,
    render_principal_template,
    resolve_company_template,
    run_governed_lifecycle_example,
    setup_messages,
    validate_data_repo_path,
    write_principal_config,
)
from hermes_helmet.openviking import (
    OpenVikingError,
    cross_client_marker_roundtrip,
    default_client_config_paths,
    intentional_shared_marker_write,
    load_client_config,
    proof_verification_fields,
    read_owner_only_secret_file,
    redact_secrets,
    render_client_config_template,
    require_chatgpt_operator_gates,
    require_shared_client_provenance,
    write_client_config,
)
from hermes_helmet.openviking import (
    render_company_template as render_openviking_company_template,
    resolve_company_template as resolve_openviking_company_template,
    setup_messages as openviking_setup_messages,
)
from hermes_helmet.helmet_epic import (
    HelmetEpicError,
    load_policy_for_epic,
    run_epic_pass,
    status_epic,
)
from hermes_helmet.helmet_issue import (
    DEFAULT_CHECKPOINT_DIR,
    DEFAULT_CONFIG,
    DEFAULT_GH,
    DEFAULT_HERMES,
    DEFAULT_LEDGER,
    HelmetIssueError,
    SubprocessRunner,
    apply_review_outcome,
    load_policy_for_issue,
    run_preflight_and_adopt,
    status_issue,
    wait_for_issue_change,
)
from hermes_helmet.install_skills import (
    DEFAULT_TARGETS,
    install_skills,
    last_install_report,
)
from hermes_helmet.company_skills import (
    CompanySkillError,
    build_skill_catalog,
    captain_install_boundary_message,
    import_company_skills_from_policy,
)
from hermes_helmet.authority import load_authority, load_policy
from hermes_helmet.dogfood import (
    DogfoodError,
    fetch_live_github,
    github_evidence_matches,
    load_bound_candidate,
    load_observed_receipts,
    validate_runtime_evidence,
)
from hermes_helmet.model_lanes import (
    HttpOpenAITransport,
    ModelLaneError,
    lanes_from_policy,
    resolve_runtime_credentials,
    rollback_embedding_lane,
    secret_free_contract_examples,
    setup_messages as model_lane_setup_messages,
    setup_model_lanes,
)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Authority policy JSON (version 2)",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER,
        help="H1 SQLite ledger path (read/adopt only for status)",
    )
    parser.add_argument(
        "--checkpoint-dir",
        type=Path,
        default=DEFAULT_CHECKPOINT_DIR,
        help="Non-secret helmet checkpoint directory",
    )
    parser.add_argument(
        "--gh",
        default=DEFAULT_GH,
        help="gh binary (env HERMES_HELMET_GH, else PATH)",
    )
    parser.add_argument(
        "--hermes",
        default=DEFAULT_HERMES,
        help="hermes binary (env HERMES_HELMET_HERMES, else PATH)",
    )
    parser.add_argument(
        "--worker-runtime",
        default="",
        help=(
            "Optional Captain→worker transport command prefix "
            "(env HERMES_HELMET_WORKER_RUNTIME). Verbs: ledger-root, "
            "ledger-watch, dispatch-root, wait. Does not invent a second ledger."
        ),
    )


def cmd_status(args: argparse.Namespace) -> int:
    try:
        policy = load_policy_for_issue(args.config)
        runner = SubprocessRunner(gh=args.gh, hermes=args.hermes)
        report = status_issue(
            policy,
            args.issue_url,
            ledger=args.ledger,
            checkpoint_dir=args.checkpoint_dir,
            runner=runner,
            gh=args.gh,
            hermes=args.hermes,
            worker_runtime=args.worker_runtime,
        )
    except HelmetIssueError as exc:
        print(f"helmet status failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report.to_public_dict(), indent=2, sort_keys=True))
    else:
        print(f"issue:          {report.issue_url}")
        print(f"state:          {report.state}")
        print(f"root_task:      {report.root_task_id or '-'}")
        print(f"pr:             {report.pr_url or '-'}")
        print(f"reviewed_head:  {report.reviewed_head or '-'}")
        print(f"clean_head:     {report.clean_head or '-'}")
        print(f"repair_state:   {report.repair_state}")
        print(f"merge_gate:     {report.merge_gate}")
        print(f"blocker:        {report.blocker or '-'}")
        print(f"terminal:       {report.terminal}")
    return 0


def cmd_dogfood_evidence(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise DogfoodError("evidence must be a JSON object")
        bound = load_bound_candidate(Path(args.bound), policy=load_authority(Path(args.policy)))
        expected_head = str(args.head or "").strip()
        if not expected_head:
            raise DogfoodError("current pull-request head is required")
        published_pr = str(payload.get("published_pr") or "")
        receipts_dir = Path(args.receipts)
        _, _, trusted_github = load_observed_receipts(receipts_dir, bound)
        trusted_priors = trusted_github.get("prior_repairs") or []
        if not isinstance(trusted_priors, list):
            raise DogfoodError("a clean review must cite a real prior repair")
        live_github = fetch_live_github(
            published_pr,
            bound,
            gh=args.gh,
            prior_repairs=trusted_priors,
        )
        github_evidence_matches(live_github, trusted_github)
        validated = validate_runtime_evidence(
            payload,
            bound=bound,
            receipts_dir=receipts_dir,
            expected_head=expected_head,
            live_github=live_github,
        )
    except (DogfoodError, AuthorityError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"helmet dogfood-evidence failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(validated, indent=2, sort_keys=True))
        return 0
    image = validated.get("image")
    worker = validated.get("worker")
    runtime = validated.get("runtime")
    if not isinstance(image, dict) or not isinstance(worker, dict) or not isinstance(runtime, dict):
        print("helmet dogfood-evidence failed: evidence is incomplete", file=sys.stderr)
        return 1
    print(f"digest:         {image.get('digest')}")
    print(f"source:         {validated.get('source_revision')}")
    print(f"task:           {validated.get('task_id')}")
    print(f"pr:             {validated.get('published_pr')}")
    print(f"worker_merged:  {worker.get('merged')}")
    print(f"dogfood_host:   {runtime.get('dogfood_host')}")
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    try:
        runner = SubprocessRunner(gh=args.gh, hermes=args.hermes)
        report = wait_for_issue_change(
            args.issue_url,
            runner,
            worker_runtime=args.worker_runtime,
            timeout_seconds=args.timeout_seconds,
            cursor=args.cursor,
        )
    except HelmetIssueError as exc:
        print(f"helmet wait failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report.to_public_dict(), indent=2, sort_keys=True))
    return 2 if report.outcome == "timeout" else 0


def cmd_issue(args: argparse.Namespace) -> int:
    try:
        policy = load_policy_for_issue(args.config)
        runner = SubprocessRunner(gh=args.gh, hermes=args.hermes)
        if args.record_review:
            if not args.head_sha:
                print(
                    "helmet issue failed: --head-sha is required with --record-review",
                    file=sys.stderr,
                )
                return 1
            checkpoint = apply_review_outcome(
                args.issue_url,
                head_sha=args.head_sha,
                outcome=args.record_review,
                summary=args.summary or "",
                checkpoint_dir=args.checkpoint_dir,
                policy=policy,
                ledger=args.ledger,
                runner=runner,
                gh=args.gh,
                hermes=args.hermes,
                worker_runtime=args.worker_runtime,
            )
            print(json.dumps(checkpoint.to_public_dict(), indent=2, sort_keys=True))
            return 0
        checkpoint, report = run_preflight_and_adopt(
            policy,
            args.issue_url,
            ledger=args.ledger,
            checkpoint_dir=args.checkpoint_dir,
            runner=runner,
            gh=args.gh,
            hermes=args.hermes,
            host_continuation=args.host_continuation,
            one_pass_only=args.one_pass_only,
            apply_dispatch=not args.no_dispatch,
            worker_runtime=args.worker_runtime,
            parent_epic_url=args.parent_epic_url or None,
        )
    except HelmetIssueError as exc:
        print(f"helmet issue failed: {exc}", file=sys.stderr)
        return 1
    payload = {
        "checkpoint": checkpoint.to_public_dict(),
        "status": report.to_public_dict(),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    if checkpoint.state in {"FAILED", "BLOCKED"} and checkpoint.last_blocker:
        return 2
    return 0


def cmd_epic(args: argparse.Namespace) -> int:
    try:
        policy = load_policy_for_epic(args.config)
        runner = SubprocessRunner(gh=args.gh, hermes=args.hermes)
        checkpoint, report, graph = run_epic_pass(
            policy,
            args.epic_url,
            ledger=args.ledger,
            checkpoint_dir=args.checkpoint_dir,
            runner=runner,
            gh=args.gh,
            hermes=args.hermes,
            host_continuation=args.host_continuation,
            one_pass_only=args.one_pass_only,
            apply_dispatch=not args.no_dispatch,
            accept_graph=args.accept_graph,
            continue_independent_branches=not args.stop_on_blocked_branch,
            max_parallelism=args.max_parallelism,
            extra_child_urls=args.child or (),
            worker_runtime=args.worker_runtime,
        )
    except (HelmetEpicError, HelmetIssueError) as exc:
        print(f"helmet epic failed: {exc}", file=sys.stderr)
        return 1
    payload = {
        "checkpoint": checkpoint.to_public_dict(),
        "status": report.to_public_dict(),
    }
    if graph is not None:
        payload["graph"] = {
            "fingerprint": graph.fingerprint,
            "source": graph.source,
            "root": graph.root_url,
            "children": [node.url for node in graph.children],
            "blocked_by": {
                child: sorted(blockers)
                for child, blockers in sorted(graph.blocked_by.items())
            },
        }
    print(json.dumps(payload, indent=2, sort_keys=True))
    # Live graph-read failures surface as nonterminal reports with details.error
    # even when a historical DONE checkpoint is preserved on disk.
    if report.details.get("error") or (
        checkpoint.state in {"FAILED", "BLOCKED", "GRAPH_CHANGED"}
        and (checkpoint.last_blocker or report.blocker)
    ):
        return 2
    if not report.terminal and report.blocker and str(report.blocker).startswith("err:"):
        return 2
    return 0


def cmd_epic_status(args: argparse.Namespace) -> int:
    try:
        policy = load_policy_for_epic(args.config)
        runner = SubprocessRunner(gh=args.gh, hermes=args.hermes)
        report = status_epic(
            policy,
            args.epic_url,
            ledger=args.ledger,
            checkpoint_dir=args.checkpoint_dir,
            runner=runner,
            gh=args.gh,
            hermes=args.hermes,
            extra_child_urls=args.child or (),
            worker_runtime=args.worker_runtime,
        )
    except (HelmetEpicError, HelmetIssueError) as exc:
        print(f"helmet epic-status failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report.to_public_dict(), indent=2, sort_keys=True))
    else:
        print(f"epic:           {report.epic_url}")
        print(f"state:          {report.state}")
        print(f"fingerprint:    {report.fingerprint or '-'}")
        print(f"max_parallel:   {report.max_parallelism}")
        print(f"root_open:      {report.root_open}")
        print(f"completed:      {len(report.completed)}")
        print(f"active:         {len(report.active)}")
        print(f"ready:          {len(report.ready)}")
        print(f"blocked:        {len(report.blocked)}")
        print(f"failed:         {len(report.failed)}")
        print(f"awaiting_human: {len(report.awaiting_human)}")
        print(f"blocker:        {report.blocker or '-'}")
        print(f"terminal:       {report.terminal}")
        for label, urls in (
            ("completed", report.completed),
            ("active", report.active),
            ("ready", report.ready),
            ("blocked", report.blocked),
            ("failed", report.failed),
            ("awaiting_human", report.awaiting_human),
        ):
            for url in urls:
                print(f"  {label}: {url}")
    return 0


def cmd_install_skills(args: argparse.Namespace) -> int:
    try:
        results = install_skills(
            targets=args.target,
            prefix=args.prefix,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"helmet install-skills failed: {exc}", file=sys.stderr)
        return 1
    print(captain_install_boundary_message())
    for item in results:
        action = "would install" if args.dry_run else "installed"
        print(f"{action}: {item}")
    report = last_install_report()
    for item in report.conflicts:
        print(f"conflict: {item}", file=sys.stderr)
    for target in report.skipped_hosts:
        print(f"skipped host: {target}", file=sys.stderr)
    return 1 if report.conflicts or report.skipped_hosts else 0


def cmd_setup(args: argparse.Namespace) -> int:
    try:
        report = helmet_setup.run_setup(
            answers_path=args.answers,
            home=args.home,
            probe=True,
        )
    except (helmet_setup.SetupError, OSError) as exc:
        print(f"helmet setup failed: {exc}", file=sys.stderr)
        return 1
    payload = report.to_public_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"setup:       {'ok' if report.ok else 'FAILED'}")
        print(f"policy:      {report.policy_path}")
        print(f"contract:    {report.contract_path}")
        print(f"fingerprint: {report.fingerprint}")
        for conflict in payload["skills"].get("conflicts", []):
            print(f"conflict:    {conflict}")
    return 0 if report.ok else 1



def _client_paths_from_args(args: argparse.Namespace) -> dict[str, Path] | None:
    if getattr(args, "openviking_root", None) is None and not any(
        getattr(args, name, None)
        for name in ("codex_config", "chatgpt_config", "hermes_config")
    ):
        return None
    paths = default_client_config_paths(
        Path(args.openviking_root)
        if getattr(args, "openviking_root", None) is not None
        else Path.home() / ".hermes-helmet" / "openviking"
    )
    if getattr(args, "codex_config", None):
        paths["codex"] = Path(args.codex_config)
    if getattr(args, "chatgpt_config", None):
        paths["chatgpt"] = Path(args.chatgpt_config)
    if getattr(args, "hermes_config", None):
        paths["hermes"] = Path(args.hermes_config)
    return paths


def _fava_principal_paths_from_args(args: argparse.Namespace) -> dict[str, Path] | None:
    root = getattr(args, "fava_root", None)
    captain = getattr(args, "captain_config", None)
    executor = getattr(args, "executor_config", None)
    if root is None and captain is None and executor is None:
        return None
    paths = default_principal_paths(
        Path(root) if root is not None else Path.home() / ".hermes-helmet" / "fava-trails"
    )
    if captain is not None:
        paths["captain"] = Path(captain)
    if executor is not None:
        paths["executor"] = Path(executor)
    return paths


def cmd_doctor(args: argparse.Namespace) -> int:
    principal_paths = _fava_principal_paths_from_args(args)
    data_repo = Path(args.data_repo) if getattr(args, "data_repo", None) else None
    client_paths = _client_paths_from_args(args)
    live = bool(getattr(args, "live", False))
    legacy = getattr(args, "home", None) is None
    if legacy:
        # Preserve the established policy/integration doctor for callers that
        # have not run the new adopter setup workflow yet.
        report = fava_doctor(
            args.config,
            principal_paths=principal_paths,
            data_repo=data_repo,
            require_live=live,
            openviking_client_paths=client_paths,
            openviking_require_live=live,
            model_lane_runtime_dir=getattr(args, "runtime_dir", None),
        )
    else:
        report = helmet_doctor.doctor(
            args.config,
            home=args.home,
            skills_prefix=args.home,
            principal_paths=principal_paths,
            data_repo=data_repo,
            # Setup-state acceptance is a pre-dispatch gate: the worker,
            # allowlist, labels, and provider/model must always be checked live.
            require_live=True,
            openviking_client_paths=client_paths,
            model_lane_runtime_dir=getattr(args, "runtime_dir", None),
        )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif legacy:
        policy_raw = report.get("policy")
        policy = policy_raw if isinstance(policy_raw, dict) else {}
        print(f"policy:      {'ok' if policy.get('ok') else 'FAILED'}")
        fava_raw = report.get("fava_trails")
        fava_section = fava_raw if isinstance(fava_raw, dict) else {}
        if fava_section.get("skipped"):
            print("fava_trails: skipped (declined or unavailable)")
        else:
            mode = fava_section.get("verification_mode") or "offline"
            print(f"fava_trails: {'ok' if fava_section.get('ok') else 'FAILED'} ({mode})")
            findings = fava_section.get("findings") or []
            if isinstance(findings, list):
                for finding in findings:
                    if isinstance(finding, dict):
                        mark = "ok" if finding.get("ok") else "FAIL"
                        print(f"  [{mark}] {finding.get('code')}: {finding.get('message')}")
        ov_raw = report.get("openviking")
        if isinstance(ov_raw, dict):
            if ov_raw.get("skipped"):
                print("openviking:  skipped (declined or unavailable)")
            else:
                mode = ov_raw.get("verification_mode") or "offline"
                print(f"openviking:  {'ok' if ov_raw.get('ok') else 'FAILED'} ({mode})")
                findings = ov_raw.get("findings") or []
                if isinstance(findings, list):
                    for finding in findings:
                        if isinstance(finding, dict):
                            mark = "ok" if finding.get("ok") else "FAIL"
                            print(f"  [{mark}] {finding.get('code')}: {finding.get('message')}")
        lanes_raw = report.get("model_lanes")
        if isinstance(lanes_raw, dict):
            print(f"model_lanes: {'ok' if lanes_raw.get('ok') else 'FAILED'}")
            if lanes_raw.get("skipped_optional"):
                print("  skipped optional (minimum runtime operational)")
            findings = lanes_raw.get("findings") or []
            if isinstance(findings, list):
                for finding in findings:
                    print(f"  [info] {finding}")
    else:
        print(f"doctor:      {'ok' if report.get('ok') else 'FAILED'}")
        for name in (
            "identity",
            "file_modes",
            "repository_allowlist",
            "labels",
            "checkouts",
            "model_lanes",
            "bundled_skills",
            "company_skills",
            "signal",
            "openviking",
            "fava_trails",
            "policy_drift",
        ):
            section = report.get(name)
            if isinstance(section, dict):
                status = "skipped" if section.get("skipped") else ("ok" if section.get("ok") else "FAILED")
                print(f"{name}: {status}")
    return 0 if report.get("ok") else 1


def _fava_template_overrides(args: argparse.Namespace) -> dict[str, object]:
    company_template = getattr(args, "company_template", None)
    company_scope = getattr(args, "company_scope", None)
    company_slug = getattr(args, "company_slug", None)
    remote_url = getattr(args, "remote_url", None)
    return {
        "company_template_path": Path(company_template) if company_template else None,
        "company_scope": company_scope if isinstance(company_scope, str) and company_scope.strip() else None,
        "company_slug": company_slug if isinstance(company_slug, str) and company_slug.strip() else None,
        "remote_url": remote_url if isinstance(remote_url, str) and remote_url.strip() else None,
    }


def _add_fava_template_overrides(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--company-template",
        type=Path,
        default=None,
        help="Load a previously emitted company.template.json",
    )
    parser.add_argument(
        "--company-scope",
        default=None,
        help="Adopter company scope override (e.g. exampleco/engineering)",
    )
    parser.add_argument(
        "--company-slug",
        default=None,
        help="Adopter company slug override for template defaults",
    )
    parser.add_argument(
        "--remote-url",
        default=None,
        help="Credential-free data remote URL for templates/scaffolds",
    )


def cmd_fava_template(args: argparse.Namespace) -> int:
    try:
        policy = load_authority(args.config)
        overrides = _fava_template_overrides(args)
        template = resolve_company_template(policy, **overrides)  # type: ignore[arg-type]
        data_repo = getattr(args, "data_repo", None)
        if data_repo:
            # Admit path before any stdout render.
            data_repo = validate_data_repo_path(str(data_repo), field="data_repo")
        if args.principal:
            print(
                render_principal_template(
                    template,
                    args.principal,
                    data_repo=data_repo,
                ),
                end="",
            )
        elif args.mcp:
            principal = args.mcp
            print(
                render_mcp_registration_example(
                    template,
                    principal,
                    data_repo=data_repo,
                ),
                end="",
            )
        else:
            print(render_company_template(template), end="")
    except (AuthorityError, FavaTrailsError, OSError) as exc:
        print(f"helmet fava template failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_fava_setup(args: argparse.Namespace) -> int:
    try:
        policy = load_authority(args.config)
        overrides = _fava_template_overrides(args)
        root = Path(args.fava_root)
        company_path = root / "company.template.json"
        if overrides["company_template_path"] is None and company_path.is_file():
            overrides["company_template_path"] = company_path
        template = resolve_company_template(policy, **overrides)  # type: ignore[arg-type]
        data_repo = getattr(args, "data_repo", None)
        if data_repo:
            data_repo = validate_data_repo_path(str(data_repo), field="data_repo")
        for line in setup_messages(template):
            print(f"- {line}")
        if args.write_templates:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            company_path.write_text(render_company_template(template), encoding="utf-8")
            print(f"wrote {company_path}")
            for principal in ("captain", "executor"):
                path = root / "templates" / f"{principal}.principal.json.example"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    render_principal_template(template, principal, data_repo=data_repo),
                    encoding="utf-8",
                )
                print(f"wrote {path}")
                mcp_path = root / "templates" / f"{principal}.mcp.json.example"
                mcp_path.write_text(
                    render_mcp_registration_example(
                        template, principal, data_repo=data_repo
                    ),
                    encoding="utf-8",
                )
                print(f"wrote {mcp_path}")
        if args.scaffold_data_repo:
            result = prepare_data_repo_scaffold(
                Path(args.scaffold_data_repo),
                template,
                allow_existing=bool(args.allow_existing_data_repo),
            )
            print(json.dumps(result, indent=2, sort_keys=True))
    except (AuthorityError, FavaTrailsError, OSError) as exc:
        print(f"helmet fava setup failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_fava_write_principal(args: argparse.Namespace) -> int:
    try:
        policy = load_authority(args.config)
        overrides = _fava_template_overrides(args)
        if overrides["company_template_path"] is None:
            root_arg = getattr(args, "fava_root", None)
            if root_arg:
                candidate = Path(root_arg) / "company.template.json"
                if candidate.is_file():
                    overrides["company_template_path"] = candidate
        template = resolve_company_template(policy, **overrides)  # type: ignore[arg-type]
        config = write_principal_config(
            args.output,
            principal=args.principal,
            template=template,
            data_repo=args.data_repo,
            trust_gate_key_file=str(args.trust_gate_key_file)
            if args.trust_gate_key_file
            else None,
        )
        print(
            f"wrote owner-only {config.principal} principal for agent {config.agent_id}; "
            "credentials not embedded"
        )
    except (AuthorityError, FavaTrailsError, OSError) as exc:
        print(
            f"helmet fava write-principal failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_fava_lifecycle_demo(args: argparse.Namespace) -> int:
    try:
        if args.company_template:
            template = load_company_template(args.company_template)
            scope = template.company_scope
            captain = template.principals["captain"]
            executor = template.principals["executor"]
        else:
            policy = load_authority(args.config)
            template = resolve_company_template(
                policy,
                company_scope=args.company_scope,
                company_slug=args.company_slug,
            )
            scope = template.company_scope
            captain = template.principals["captain"]
            executor = template.principals["executor"]
        result = run_governed_lifecycle_example(
            company_scope=scope,
            captain_id=captain,
            executor_id=executor,
        )
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(f"lifecycle-demo: {'ok' if result.get('ok') else 'FAILED'}")
            print(f"company_scope: {result.get('company_scope')}")
            print(f"principals: {result.get('principals')}")
            for step in result.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                name = step.get("step")
                ok = step.get("ok", True)
                print(f"  - {name}: {'ok' if ok else 'FAIL'}")
        return 0 if result.get("ok") else 1
    except (AuthorityError, FavaTrailsError, OSError, KeyError) as exc:
        print(f"helmet fava lifecycle-demo failed: {exc}", file=sys.stderr)
        return 1
def cmd_import_company_skills(args: argparse.Namespace) -> int:
    try:
        policy = load_policy(args.config)
        result = import_company_skills_from_policy(
            policy,
            state_root=args.state_root,
            dry_run=args.dry_run,
            hermes_home=args.hermes_home,
            hermes_config_path=args.hermes_config,
            register_discovery=not args.skip_discovery_registration,
        )
    except (CompanySkillError, OSError) as exc:
        print(f"helmet import-company-skills failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - authority errors etc.
        print(f"helmet import-company-skills failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result.to_public_dict(), indent=2, sort_keys=True))
    else:
        print(f"status:   {result.status}")
        print(f"message:  {result.message}")
        print(f"mutated:  {result.mutated}")
        if result.installed_names:
            print(f"skills:   {', '.join(result.installed_names)}")
        if result.release_id:
            print(f"release:  {result.release_id}")
        if result.catalog.state_root:
            print(f"state:    {result.catalog.state_root}")
        if result.discovery_path:
            print(f"discovery:{result.discovery_path}")
        if result.validation.errors:
            for err in result.validation.errors:
                print(f"error:    {err}", file=sys.stderr)
    # Core startup continues for skipped/preserved/rejected; only hard failures
    # return non-zero above. Rejected still exits 0 so optional packs do not
    # block the control loop, but status is visible.
    return 0


def cmd_skill_catalog(args: argparse.Namespace) -> int:
    try:
        policy = None
        if args.config is not None and Path(args.config).is_file():
            try:
                policy = load_authority(args.config)
            except Exception:  # noqa: BLE001
                policy = load_policy(args.config)
        catalog = build_skill_catalog(policy=policy, state_root=args.state_root)
    except CompanySkillError as exc:
        print(f"helmet skill-catalog failed: {exc}", file=sys.stderr)
        return 1
    payload = catalog.to_public_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"status:         {catalog.status}")
        print(f"role_boundary:  {catalog.role_boundary}")
        print(f"message:        {catalog.message}")
        print(f"state_root:     {catalog.state_root or '-'}")
        print(f"source:         {catalog.source or '-'}")
        print(f"active_release: {catalog.active_release or '-'}")
        for entry in catalog.skills:
            print(f"skill:          {entry.role}:{entry.name}")
    return 0



def _openviking_template_overrides_from_args(args: argparse.Namespace) -> dict[str, object]:
    account = getattr(args, "account", None)
    user = getattr(args, "user", None)
    service_url = getattr(args, "service_url", None)
    company_template = getattr(args, "company_template", None)
    return {
        "account": account if isinstance(account, str) and account.strip() else None,
        "user": user if isinstance(user, str) and user.strip() else None,
        "service_url": (
            service_url if isinstance(service_url, str) and service_url.strip() else None
        ),
        "company_template_path": Path(company_template) if company_template else None,
    }


def _add_openviking_template_overrides(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--account",
        default=None,
        help="Adopter OpenViking account override (non-secret)",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="Adopter shared company-context user override (non-secret)",
    )
    parser.add_argument(
        "--service-url",
        default=None,
        help="Adopter OpenViking service URL override (no credentials in URL)",
    )
    parser.add_argument(
        "--company-template",
        type=Path,
        default=None,
        help=(
            "Load a previously emitted company.template.json "
            "(consumes customized account/user/url/peers)"
        ),
    )


def cmd_openviking_template(args: argparse.Namespace) -> int:
    try:
        policy = load_authority(args.config)
        overrides = _openviking_template_overrides_from_args(args)
        template = resolve_openviking_company_template(policy, **overrides)  # type: ignore[arg-type]
        if args.client:
            print(render_client_config_template(template, args.client), end="")
        else:
            print(render_openviking_company_template(template), end="")
    except (AuthorityError, OpenVikingError, OSError) as exc:
        print(f"helmet openviking template failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_openviking_setup(args: argparse.Namespace) -> int:
    try:
        policy = load_authority(args.config)
        overrides = _openviking_template_overrides_from_args(args)
        root = Path(args.openviking_root)
        company_path = root / "company.template.json"
        if overrides["company_template_path"] is None and company_path.is_file():
            overrides["company_template_path"] = company_path
        template = resolve_openviking_company_template(policy, **overrides)  # type: ignore[arg-type]
        for line in openviking_setup_messages(template):
            print(f"- {line}")
        if args.write_templates:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            company_path.write_text(render_openviking_company_template(template), encoding="utf-8")
            print(f"wrote {company_path}")
            for client in ("codex", "chatgpt", "hermes"):
                path = root / "templates" / f"{client}.ovcli.conf.example"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    render_client_config_template(template, client), encoding="utf-8"
                )
                print(f"wrote {path}")
    except (AuthorityError, OpenVikingError, OSError) as exc:
        print(f"helmet openviking setup failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_openviking_write_client(args: argparse.Namespace) -> int:
    try:
        policy = load_authority(args.config)
        overrides = _openviking_template_overrides_from_args(args)
        if overrides["company_template_path"] is None:
            candidate = Path(args.output).expanduser().resolve().parents[2] / (
                "company.template.json"
            )
            root_arg = getattr(args, "openviking_root", None)
            if root_arg:
                root_candidate = Path(root_arg) / "company.template.json"
                if root_candidate.is_file():
                    overrides["company_template_path"] = root_candidate
            elif candidate.is_file():
                overrides["company_template_path"] = candidate
        template = resolve_openviking_company_template(policy, **overrides)  # type: ignore[arg-type]
        api_key = read_owner_only_secret_file(args.api_key_file)
        config = write_client_config(
            args.output,
            client=args.client,
            template=template,
            api_key=api_key,
        )
        api_key = ""
        print(
            f"wrote owner-only {config.client} config for peer {config.peer_id}; "
            "credential not displayed"
        )
    except (AuthorityError, OpenVikingError, OSError) as exc:
        print(
            f"helmet openviking write-client failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_openviking_shared_proof(args: argparse.Namespace) -> int:
    """Run intentional shared marker write/search/recall/read across three clients."""

    try:
        paths = _client_paths_from_args(args) or {}
        missing = [name for name in ("codex", "chatgpt", "hermes") if name not in paths]
        if missing:
            print(
                "helmet openviking shared-proof requires codex, chatgpt, and hermes configs",
                file=sys.stderr,
            )
            return 1
        configs = {
            name: load_client_config(path, expected_client=name)
            for name, path in paths.items()
        }
        require_shared_client_provenance(configs)
        known_secrets = tuple(cfg.api_key for cfg in configs.values())
        if args.transport == "http":
            from hermes_helmet.openviking import http_transport

            def transport(method, url, headers, payload=None):  # type: ignore[no-untyped-def]
                return http_transport(
                    method, url, headers, payload, known_secrets=known_secrets
                )

        else:
            from hermes_helmet.openviking import FakeOpenVikingService

            service = FakeOpenVikingService()
            for cfg in configs.values():
                service.register_user_key(
                    cfg.api_key, account=cfg.account, user=cfg.user, role="user"
                )
            transport = service.transport
        result = cross_client_marker_roundtrip(
            configs, transport=transport, transport_kind=args.transport
        )
        public = json.loads(
            redact_secrets(json.dumps(result, sort_keys=True), *known_secrets)
        )
        if args.json:
            print(json.dumps(public, indent=2, sort_keys=True))
        else:
            print(f"shared-proof: {'ok' if public.get('ok') else 'FAILED'}")
            print(f"transport: {public.get('transport')}")
            print(f"verification_scope: {public.get('verification_scope')}")
            print(f"acceptance: {public.get('acceptance')}")
            print(f"shared_namespace: {public.get('shared_namespace')}")
            print(f"origin_evidence: {public.get('origin_evidence')}")
            markers = public.get("markers") or {}
            if isinstance(markers, dict):
                for client, meta in markers.items():
                    if not isinstance(meta, dict):
                        continue
                    print(f"  {client}: uri={meta.get('uri')}")
        return 0 if result.get("ok") else 1
    except (OpenVikingError, OSError, ValueError) as exc:
        print(
            f"helmet openviking shared-proof failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1


def cmd_openviking_write_marker(args: argparse.Namespace) -> int:
    try:
        config = load_client_config(args.client_config, expected_client=args.client)
        if config.client == "chatgpt":
            require_chatgpt_operator_gates(config)
        from hermes_helmet.openviking import FakeOpenVikingService, http_transport

        if args.transport == "http":

            def transport(method, url, headers, payload=None):  # type: ignore[no-untyped-def]
                return http_transport(
                    method, url, headers, payload, known_secrets=(config.api_key,)
                )

        else:
            service = FakeOpenVikingService()
            service.register_user_key(
                config.api_key, account=config.account, user=config.user, role="user"
            )
            transport = service.transport
        uri, view, _headers = intentional_shared_marker_write(
            config, args.text, transport=transport
        )
        public = {"uri": uri, "origin_view": view}
        public.update(proof_verification_fields(transport_kind=args.transport))
        print(
            json.dumps(
                json.loads(redact_secrets(json.dumps(public), config.api_key)),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (OpenVikingError, OSError, ValueError) as exc:
        print(
            f"helmet openviking write-marker failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1


def cmd_models_setup(args: argparse.Namespace) -> int:
    try:
        policy = load_authority(args.config)
        probe = bool(getattr(args, "probe", False))
        lanes = lanes_from_policy(policy)
        runtime_dir = getattr(args, "runtime_dir", None)
        embedding_index_state = getattr(args, "embedding_index_state", None)
        if lanes.embeddings is not None and embedding_index_state is None:
            raise ModelLaneError(
                "selecting an embedding lane requires --embedding-index-state"
            )
        credentials = resolve_runtime_credentials(lanes, runtime_dir)
        report = setup_model_lanes(
            policy,
            transport=HttpOpenAITransport() if probe else None,
            probe=probe,
            credentials=credentials,
            embedding_index_state=embedding_index_state,
            reindex_confirmed=bool(getattr(args, "reindex_confirmed", False)),
        )
    except (AuthorityError, ModelLaneError, OSError) as exc:
        print(f"helmet models setup failed: {exc}", file=sys.stderr)
        return 1
    payload = report.to_public_dict()
    payload["examples"] = secret_free_contract_examples()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"ok: {report.ok}")
        print(f"setup_success: {report.setup_success}")
        print(f"skipped_optional: {report.skipped_optional}")
        for line in model_lane_setup_messages():
            print(line)
    return 0 if report.ok else 1


def cmd_models_rollback_embedding(args: argparse.Namespace) -> int:
    try:
        state = getattr(args, "embedding_index_state", None)
        if state is None:
            raise ModelLaneError("embedding rollback requires --embedding-index-state")
        restored = rollback_embedding_lane(
            Path(state),
            index_restore_confirmed=bool(getattr(args, "index_restore_confirmed", False)),
        )
    except (ModelLaneError, OSError, ValueError) as exc:
        print(f"helmet models rollback-embedding failed: {exc}", file=sys.stderr)
        return 1
    payload = restored.public_dict()
    payload["index_restore_confirmed"] = True
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"restored fingerprint model: {restored.model}")
        print(f"restored fingerprint dimensions: {restored.dimensions}")
        print("index restore or re-index was confirmed; fingerprint only")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helmet",
        description=(
            "Hermes Helmet: Open-source software factory for coding agents: "
            "delegation, review, and repair"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status_p = sub.add_parser("status", help="Read-only orchestration status for one issue")
    _add_common(status_p)
    status_p.add_argument("issue_url")
    status_p.add_argument("--json", action="store_true")
    status_p.set_defaults(func=cmd_status)

    dogfood_p = sub.add_parser(
        "dogfood-evidence",
        help="Validate bound-candidate runtime dogfood evidence (read-only)",
    )
    dogfood_p.add_argument("evidence", type=Path, help="Secret-free dogfood evidence JSON")
    dogfood_p.add_argument(
        "--bound",
        type=Path,
        default=Path("config/dogfood-bound.json"),
        help="Owner-selected bound candidate identity JSON",
    )
    dogfood_p.add_argument(
        "--policy",
        type=Path,
        default=Path("config/policy.example.json"),
        help="Version-2 authority policy that binds Captain and repositories",
    )
    dogfood_p.add_argument(
        "--receipts",
        type=Path,
        required=True,
        help="Directory of immutable Codex receipts and observations",
    )
    dogfood_p.add_argument(
        "--head",
        required=True,
        help="Live pull-request head the evidence must match",
    )
    dogfood_p.add_argument(
        "--gh",
        default="gh",
        help="gh binary used to read live pull, reviews, and commits",
    )
    dogfood_p.add_argument("--json", action="store_true")
    dogfood_p.set_defaults(func=cmd_dogfood_evidence)

    wait_p = sub.add_parser(
        "wait",
        help="Block once in the worker runtime until an issue needs another pass",
    )
    _add_common(wait_p)
    wait_p.add_argument("issue_url")
    wait_p.add_argument(
        "--timeout-seconds",
        type=int,
        default=1800,
        help="Bounded worker-side wait (1..86400; default: 1800)",
    )
    wait_p.add_argument(
        "--cursor",
        default="",
        help="Opaque secret-free cursor returned by the previous wait",
    )
    wait_p.set_defaults(func=cmd_wait)

    issue_p = sub.add_parser(
        "issue",
        help="One truthful helmet-issue pass (preflight/adopt/discover) or record review",
    )
    _add_common(issue_p)
    issue_p.add_argument("issue_url")
    issue_p.add_argument(
        "--host-continuation",
        default="unknown",
        help="Host recurrence capability: cron|session|unknown|none",
    )
    issue_p.add_argument(
        "--one-pass-only",
        action="store_true",
        help="Record that this host cannot continue out of session",
    )
    issue_p.add_argument(
        "--no-dispatch",
        action="store_true",
        help="Mutation-free discovery/adopt only (no label or root-task create)",
    )
    issue_p.add_argument(
        "--record-review",
        choices=("clean", "changes_requested", "blocked"),
        help="Record a Captain review outcome against the checkpoint",
    )
    issue_p.add_argument("--head-sha", default="", help="Exact PR head reviewed")
    issue_p.add_argument(
        "--summary",
        default="",
        help="Ignored for persistence (review prose stays on GitHub)",
    )
    issue_p.add_argument(
        "--parent-epic-url",
        default="",
        help="Validated epic root URL for merge-authority inheritance on resume",
    )
    issue_p.set_defaults(func=cmd_issue)

    epic_p = sub.add_parser(
        "epic",
        help="One truthful helmet-epic pass (graph load/validate/frontier/child invoke)",
    )
    _add_common(epic_p)
    epic_p.add_argument("epic_url")
    epic_p.add_argument(
        "--host-continuation",
        default="unknown",
        help="Host recurrence capability: cron|session|unknown|none",
    )
    epic_p.add_argument(
        "--one-pass-only",
        action="store_true",
        help="Record that this host cannot continue out of session",
    )
    epic_p.add_argument(
        "--no-dispatch",
        action="store_true",
        help="Graph/status only (no child helmet-issue dispatch)",
    )
    epic_p.add_argument(
        "--accept-graph",
        action="store_true",
        help="Accept the current live graph fingerprint (required after graph changes)",
    )
    epic_p.add_argument(
        "--stop-on-blocked-branch",
        action="store_true",
        help="Do not continue independent branches when a child fails (default: continue)",
    )
    epic_p.add_argument(
        "--max-parallelism",
        type=int,
        default=None,
        help="Override policy max_epic_parallelism for this pass",
    )
    epic_p.add_argument(
        "--child",
        action="append",
        default=[],
        help="Optional explicit child issue URL (repeatable; still requires Parent link)",
    )
    epic_p.set_defaults(func=cmd_epic)

    epic_status_p = sub.add_parser(
        "epic-status",
        help="Read-oriented epic status (rebuilds graph; no child dispatch)",
    )
    _add_common(epic_status_p)
    epic_status_p.add_argument("epic_url")
    epic_status_p.add_argument("--json", action="store_true")
    epic_status_p.add_argument(
        "--child",
        action="append",
        default=[],
        help="Optional explicit child issue URL (repeatable)",
    )
    epic_status_p.set_defaults(func=cmd_epic_status)

    setup_p = sub.add_parser(
        "setup",
        help="Create or resume a validated adopter-owned Hermes Helmet installation",
    )
    setup_p.add_argument(
        "--answers",
        type=Path,
        required=True,
        help="Secret-free setup answers JSON",
    )
    setup_p.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Installation home (default: current user's home)",
    )
    setup_p.add_argument("--json", action="store_true")
    setup_p.set_defaults(func=cmd_setup)

    install_p = sub.add_parser(
        "install-skills",
        help=(
            "Explicit setup: install bundled Captain/orchestrator skills into "
            "Codex/Claude/Hermes skill directories (never used by executor runtime import)"
        ),
    )
    install_p.add_argument(
        "--target",
        action="append",
        choices=sorted(DEFAULT_TARGETS),
        help="Install target (repeatable). Default: all supported targets",
    )
    install_p.add_argument(
        "--prefix",
        type=Path,
        default=None,
        help="Optional path prefix for tests / non-home installs",
    )
    install_p.add_argument("--dry-run", action="store_true")
    install_p.set_defaults(func=cmd_install_skills)

    doctor_p = sub.add_parser(
        "doctor",
        help="Verify authority + optional FAVA Trails, OpenViking, and model-lane config",
    )
    doctor_p.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Authority policy JSON (version 2)",
    )
    doctor_p.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Installation home containing setup state and bundled skills",
    )
    doctor_p.add_argument(
        "--fava-root",
        type=Path,
        default=None,
        help="Root containing principals/captain and principals/executor configs",
    )
    doctor_p.add_argument("--captain-config", type=Path, default=None)
    doctor_p.add_argument("--executor-config", type=Path, default=None)
    doctor_p.add_argument(
        "--data-repo",
        type=Path,
        default=None,
        help="Optional company-owned FAVA data repository path override",
    )
    doctor_p.add_argument(
        "--openviking-root",
        type=Path,
        default=None,
        help="Private root containing clients/{codex,chatgpt,hermes}/ovcli.conf",
    )
    doctor_p.add_argument("--codex-config", type=Path, default=None)
    doctor_p.add_argument("--chatgpt-config", type=Path, default=None)
    doctor_p.add_argument("--hermes-config", type=Path, default=None)
    doctor_p.add_argument(
        "--live",
        action="store_true",
        help=(
            "Authenticate optional FAVA principals and OpenViking client keys "
            "against their live services on the legacy doctor path. Setup-state "
            "doctor (--home) always verifies the worker, repository boundary, "
            "labels, and selected provider/model live and fails closed."
        ),
    )
    doctor_p.add_argument(
        "--runtime-dir",
        type=Path,
        default=None,
        help="Owner-only directory of per-lane runtime configs (credential_env names only)",
    )
    doctor_p.add_argument("--json", action="store_true")
    doctor_p.set_defaults(func=cmd_doctor)

    fava_p = sub.add_parser(
        "fava",
        help="Optional FAVA Trails company-brain setup and lifecycle demo",
    )
    fava_sub = fava_p.add_subparsers(dest="fava_command", required=True)

    fava_setup = fava_sub.add_parser(
        "setup",
        help="Print guided setup steps and optional non-secret templates",
    )
    fava_setup.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    fava_setup.add_argument(
        "--fava-root",
        type=Path,
        default=Path("config/fixtures/exampleco/fava-trails"),
    )
    fava_setup.add_argument("--write-templates", action="store_true")
    fava_setup.add_argument(
        "--scaffold-data-repo",
        type=Path,
        default=None,
        help="Create non-destructive empty data-repo scaffold (no JJ bootstrap)",
    )
    fava_setup.add_argument(
        "--allow-existing-data-repo",
        action="store_true",
        help="Allow scaffolding files into an existing empty-ish directory",
    )
    fava_setup.add_argument(
        "--data-repo",
        default=None,
        help="Placeholder/real data repo path for emitted principal templates",
    )
    _add_fava_template_overrides(fava_setup)
    fava_setup.set_defaults(func=cmd_fava_setup)

    fava_template = fava_sub.add_parser(
        "template",
        help="Render company, principal, or MCP registration templates (no secrets)",
    )
    fava_template.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    fava_template.add_argument(
        "--principal",
        choices=("captain", "executor"),
        default=None,
        help="Render one ordinary principal example instead of the company template",
    )
    fava_template.add_argument(
        "--mcp",
        choices=("captain", "executor"),
        default=None,
        help="Render one MCP registration example for an ordinary principal",
    )
    fava_template.add_argument("--data-repo", default=None)
    _add_fava_template_overrides(fava_template)
    fava_template.set_defaults(func=cmd_fava_template)

    fava_write = fava_sub.add_parser(
        "write-principal",
        help="Write one owner-only ordinary principal config (no secret values)",
    )
    fava_write.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    fava_write.add_argument("--principal", choices=("captain", "executor"), required=True)
    fava_write.add_argument("--data-repo", required=True)
    fava_write.add_argument("--output", type=Path, required=True)
    fava_write.add_argument("--fava-root", type=Path, default=None)
    fava_write.add_argument(
        "--trust-gate-key-file",
        type=Path,
        default=None,
        help="Optional path pointer to an owner-only key file (value not read/embedded)",
    )
    _add_fava_template_overrides(fava_write)
    fava_write.set_defaults(func=cmd_fava_write_principal)

    fava_demo = fava_sub.add_parser(
        "lifecycle-demo",
        help="Hermetic governed lifecycle example (draft/propose/approve/reject/supersede/recall)",
    )
    fava_demo.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    fava_demo.add_argument("--json", action="store_true")
    _add_fava_template_overrides(fava_demo)
    fava_demo.set_defaults(func=cmd_fava_lifecycle_demo)


    ov_p = sub.add_parser(
        "openviking",
        help="Optional OpenViking shared company working-context helpers (AGPL-3.0)",
    )
    ov_sub = ov_p.add_subparsers(dest="openviking_command", required=True)

    ov_setup = ov_sub.add_parser(
        "setup",
        help="Print guided setup steps and optional non-secret templates",
    )
    ov_setup.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ov_setup.add_argument(
        "--openviking-root",
        type=Path,
        default=Path("config/fixtures/exampleco/openviking"),
    )
    ov_setup.add_argument(
        "--write-templates",
        action="store_true",
        help="Write non-secret company/client templates under --openviking-root",
    )
    _add_openviking_template_overrides(ov_setup)
    ov_setup.set_defaults(func=cmd_openviking_setup)

    ov_template = ov_sub.add_parser(
        "template",
        help="Render company or per-client non-secret template JSON",
    )
    ov_template.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ov_template.add_argument(
        "--client",
        choices=("codex", "chatgpt", "hermes"),
        default=None,
        help="When set, render one client example instead of the company template",
    )
    _add_openviking_template_overrides(ov_template)
    ov_template.set_defaults(func=cmd_openviking_template)

    ov_write = ov_sub.add_parser(
        "write-client",
        help="Write one owner-only client config from a key file (key never printed)",
    )
    ov_write.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ov_write.add_argument(
        "--client",
        required=True,
        choices=("codex", "chatgpt", "hermes"),
    )
    ov_write.add_argument(
        "--api-key-file",
        type=Path,
        required=True,
        help="Owner-only file containing the USER key (not a root/admin key)",
    )
    ov_write.add_argument("--output", type=Path, required=True)
    ov_write.add_argument(
        "--openviking-root",
        type=Path,
        default=None,
        help="Optional root containing company.template.json to consume",
    )
    _add_openviking_template_overrides(ov_write)
    ov_write.set_defaults(func=cmd_openviking_write_client)

    ov_proof = ov_sub.add_parser(
        "shared-proof",
        help=(
            "Intentional shared marker write/search/recall/read across "
            "codex/chatgpt/hermes with origin header evidence"
        ),
    )
    ov_proof.add_argument(
        "--openviking-root",
        type=Path,
        default=None,
        help="Private root containing clients/{codex,chatgpt,hermes}/ovcli.conf",
    )
    ov_proof.add_argument("--codex-config", type=Path, default=None)
    ov_proof.add_argument("--chatgpt-config", type=Path, default=None)
    ov_proof.add_argument("--hermes-config", type=Path, default=None)
    ov_proof.add_argument(
        "--transport",
        choices=("fake", "http"),
        default="fake",
        help=(
            "fake=hermetic synthetic/non-acceptance demo; "
            "http=live OpenViking service (not official-client acceptance)"
        ),
    )
    ov_proof.add_argument("--json", action="store_true")
    ov_proof.set_defaults(func=cmd_openviking_shared_proof)

    ov_marker = ov_sub.add_parser(
        "write-marker",
        help="Intentional shared marker write for one client config",
    )
    ov_marker.add_argument(
        "--client",
        required=True,
        choices=("codex", "chatgpt", "hermes"),
    )
    ov_marker.add_argument(
        "--client-config",
        type=Path,
        required=True,
        help="Owner-only client ovcli.conf",
    )
    ov_marker.add_argument("--text", required=True, help="Marker text to write")
    ov_marker.add_argument(
        "--transport",
        choices=("fake", "http"),
        default="fake",
        help=(
            "fake=hermetic synthetic/non-acceptance demo; "
            "http=live OpenViking service (not official-client acceptance)"
        ),
    )
    ov_marker.set_defaults(func=cmd_openviking_write_marker)

    import_p = sub.add_parser(
        "import-company-skills",
        help=(
            "Import allowlisted executor skills from the configured company pack "
            "into Hermes-owned persistent state"
        ),
    )
    import_p.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Authority policy JSON (optional skills.company_pack)",
    )
    import_p.add_argument(
        "--state-root",
        type=Path,
        default=None,
        help="Hermes-owned company-skills state root (default under HERMES_HOME)",
    )
    import_p.add_argument(
        "--hermes-home",
        type=Path,
        default=None,
        help="Hermes home for skills.external_dirs registration (default: $HERMES_HOME)",
    )
    import_p.add_argument(
        "--hermes-config",
        type=Path,
        default=None,
        help="Hermes config.yaml path for discovery registration",
    )
    import_p.add_argument(
        "--skip-discovery-registration",
        action="store_true",
        help="Do not register active skills in Hermes skills.external_dirs",
    )
    import_p.add_argument("--dry-run", action="store_true")
    import_p.add_argument("--json", action="store_true")
    import_p.set_defaults(func=cmd_import_company_skills)

    catalog_p = sub.add_parser(
        "skill-catalog",
        help="Emit deterministic Captain/executor skill catalog for setup-helmet",
    )
    catalog_p.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Authority policy JSON (optional)",
    )
    catalog_p.add_argument(
        "--state-root",
        type=Path,
        default=None,
        help="Hermes-owned company-skills state root",
    )
    catalog_p.add_argument("--json", action="store_true")
    catalog_p.set_defaults(func=cmd_skill_catalog)

    models_p = sub.add_parser(
        "models",
        help="Provider-neutral Hermes/FAVA/OpenViking model-lane setup and probes",
    )
    models_sub = models_p.add_subparsers(dest="models_command", required=True)
    models_setup = models_sub.add_parser(
        "setup",
        help="Validate selected model lanes; optional lanes stay declined by default",
    )
    models_setup.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    models_setup.add_argument(
        "--probe",
        action="store_true",
        help="Run live OpenAI-compatible probes for every selected lane before reporting success",
    )
    models_setup.add_argument(
        "--runtime-dir",
        type=Path,
        default=None,
        help="Owner-only directory of per-lane runtime configs (credential_env names only)",
    )
    models_setup.add_argument(
        "--embedding-index-state",
        type=Path,
        default=None,
        help="Owner-only embedding index fingerprint used for re-index protection",
    )
    models_setup.add_argument(
        "--reindex-confirmed",
        action="store_true",
        help="Confirm embedding model, dimension, or quantization change and keep rollback",
    )
    models_setup.add_argument("--json", action="store_true")
    models_setup.set_defaults(func=cmd_models_setup)
    models_rollback = models_sub.add_parser(
        "rollback-embedding",
        help="Restore the previous embedding fingerprint after a confirmed index restore or re-index",
    )
    models_rollback.add_argument(
        "--embedding-index-state",
        type=Path,
        required=True,
        help="Owner-only embedding index fingerprint used for rollback",
    )
    models_rollback.add_argument(
        "--index-restore-confirmed",
        action="store_true",
        help="Confirm a compatible preserved-index restore or re-index completed before swapping fingerprints",
    )
    models_rollback.add_argument("--json", action="store_true")
    models_rollback.set_defaults(func=cmd_models_rollback_embedding)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
