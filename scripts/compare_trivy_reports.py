#!/usr/bin/env python3
"""Fail closed when a Hermes Helmet image adds serious base-image risk."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any


BLOCKING_SEVERITIES = frozenset({"HIGH", "CRITICAL"})
BASE_LABEL = "org.opencontainers.image.base.name"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReportError(RuntimeError):
    """Raised when scan evidence is missing, inconsistent, or unsafe."""


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReportError(f"cannot read Trivy report {path}: {error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("Results"), list):
        raise ReportError(f"invalid Trivy report structure: {path}")
    if not payload.get("Trivy", {}).get("Version"):
        raise ReportError(f"Trivy version is missing: {path}")
    return payload


def _image_config(report: dict[str, Any]) -> dict[str, Any]:
    config = report.get("Metadata", {}).get("ImageConfig")
    if not isinstance(config, dict):
        raise ReportError("image configuration metadata is missing")
    return config


def _diff_ids(report: dict[str, Any]) -> list[str]:
    diff_ids = report.get("Metadata", {}).get("DiffIDs")
    if not isinstance(diff_ids, list) or not diff_ids or not all(
        isinstance(item, str) and item.startswith("sha256:") for item in diff_ids
    ):
        raise ReportError("image layer identities are missing or invalid")
    return diff_ids


def _platform(report: dict[str, Any]) -> tuple[str, str]:
    config = _image_config(report)
    operating_system = config.get("os")
    architecture = config.get("architecture")
    if not isinstance(operating_system, str) or not operating_system:
        raise ReportError("image operating-system metadata is missing")
    if not isinstance(architecture, str) or not architecture:
        raise ReportError("image architecture metadata is missing")
    return operating_system, architecture


def _database_sha256(path: Path) -> str:
    try:
        fields = path.read_text(encoding="utf-8").split()
    except OSError as error:
        raise ReportError(f"cannot read Trivy database digest {path}: {error}") from error
    if not fields or not SHA256_RE.fullmatch(fields[0]):
        raise ReportError(f"invalid Trivy database SHA-256 evidence: {path}")
    return fields[0]


def _java_database_sha256(path: Path) -> str:
    try:
        fields = path.read_text(encoding="utf-8").split()
    except OSError as error:
        raise ReportError(f"cannot read Trivy Java database evidence {path}: {error}") from error
    if fields == ["absent"]:
        return "absent"
    if not fields or not SHA256_RE.fullmatch(fields[0]):
        raise ReportError(f"invalid Trivy Java database SHA-256 evidence: {path}")
    return fields[0]


def _labels(report: dict[str, Any]) -> dict[str, Any]:
    runtime_config = _image_config(report).get("config")
    if not isinstance(runtime_config, dict):
        raise ReportError("image runtime configuration metadata is missing")
    labels = runtime_config.get("Labels") or {}
    if not isinstance(labels, dict):
        raise ReportError("image label metadata is invalid")
    return labels


def _vulnerabilities(report: dict[str, Any]) -> dict[tuple[str, ...], dict[str, Any]]:
    findings: dict[tuple[str, ...], dict[str, Any]] = {}
    for result in report["Results"]:
        if not isinstance(result, dict):
            raise ReportError("invalid Trivy result entry")
        target = str(result.get("Target") or "")
        finding_class = str(result.get("Class") or "")
        finding_type = str(result.get("Type") or "")
        # Trivy names the OS target after the scanned image or archive. That
        # label necessarily differs between base and derived reports even when
        # the installed OS package is identical. Language and binary targets
        # remain meaningful because a wrapper can add a new package-bearing
        # file at a distinct path.
        identity_target = "" if finding_class == "os-pkgs" else target
        for vulnerability in result.get("Vulnerabilities") or []:
            if not isinstance(vulnerability, dict):
                raise ReportError("invalid vulnerability entry")
            severity = str(vulnerability.get("Severity") or "").upper()
            if severity not in BLOCKING_SEVERITIES:
                continue
            identifier = str(vulnerability.get("VulnerabilityID") or "")
            package = str(vulnerability.get("PkgName") or "")
            installed = str(vulnerability.get("InstalledVersion") or "")
            package_identifier = vulnerability.get("PkgIdentifier") or {}
            if not isinstance(package_identifier, dict):
                raise ReportError("vulnerability package identity is invalid")
            purl = str(package_identifier.get("PURL") or "")
            if not identifier or not package or not installed:
                raise ReportError("vulnerability identity is incomplete")
            key = (
                identity_target,
                finding_class,
                finding_type,
                identifier,
                package,
                installed,
                severity,
                purl,
            )
            findings[key] = {
                "target": target,
                "class": finding_class,
                "type": finding_type,
                "id": identifier,
                "package": package,
                "installed_version": installed,
                "fixed_version": vulnerability.get("FixedVersion") or None,
                "severity": severity,
                "purl": purl or None,
            }
    return findings


def _secrets(report: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for result in report["Results"]:
        target = str(result.get("Target") or "")
        for secret in result.get("Secrets") or []:
            if not isinstance(secret, dict):
                raise ReportError("invalid secret entry")
            findings.append(
                {
                    "target": target,
                    "rule_id": secret.get("RuleID") or None,
                    "category": secret.get("Category") or None,
                    "severity": secret.get("Severity") or None,
                    "start_line": secret.get("StartLine") or None,
                    "end_line": secret.get("EndLine") or None,
                }
            )
    return findings


def _ordered(findings: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        findings,
        key=lambda item: (
            str(item.get("severity") or ""),
            str(item.get("id") or ""),
            str(item.get("package") or ""),
            str(item.get("target") or ""),
        ),
    )


def compare(
    base: dict[str, Any],
    derived: dict[str, Any],
    *,
    expected_platform: str,
    expected_base_ref: str,
    database_sha256: str,
    java_database_sha256: str,
) -> tuple[dict[str, Any], list[str]]:
    if not SHA256_RE.fullmatch(database_sha256):
        raise ReportError("invalid Trivy database SHA-256")
    if java_database_sha256 != "absent" and not SHA256_RE.fullmatch(
        java_database_sha256
    ):
        raise ReportError("invalid Trivy Java database SHA-256")
    failures: list[str] = []
    expected_operating_system, expected_architecture = expected_platform.split("/", 1)
    base_operating_system, base_architecture = _platform(base)
    derived_operating_system, derived_architecture = _platform(derived)
    if (base_operating_system, base_architecture) != (
        expected_operating_system,
        expected_architecture,
    ):
        failures.append(
            "base platform "
            f"{base_operating_system}/{base_architecture!s} does not match "
            f"{expected_platform!r}"
        )
    if (derived_operating_system, derived_architecture) != (
        expected_operating_system,
        expected_architecture,
    ):
        failures.append(
            "derived platform "
            f"{derived_operating_system}/{derived_architecture!s} does not match "
            f"{expected_platform!r}"
        )

    base_layers = _diff_ids(base)
    derived_layers = _diff_ids(derived)
    if derived_layers[: len(base_layers)] != base_layers:
        failures.append("derived image does not extend the scanned base-image layers")

    base_label = _labels(derived).get(BASE_LABEL)
    if base_label != expected_base_ref:
        failures.append(
            f"derived base label {base_label!r} does not match the expected immutable ref"
        )

    base_trivy = base["Trivy"]["Version"]
    derived_trivy = derived["Trivy"]["Version"]
    if base_trivy != derived_trivy:
        failures.append(
            f"Trivy version mismatch: base={base_trivy!r}, derived={derived_trivy!r}"
        )

    base_vulnerabilities = _vulnerabilities(base)
    derived_vulnerabilities = _vulnerabilities(derived)
    added_keys = set(derived_vulnerabilities) - set(base_vulnerabilities)
    removed_keys = set(base_vulnerabilities) - set(derived_vulnerabilities)
    added = _ordered(derived_vulnerabilities[key] for key in added_keys)
    removed = _ordered(base_vulnerabilities[key] for key in removed_keys)
    secrets = _secrets(derived)
    if added:
        failures.append(f"wrapper adds {len(added)} HIGH or CRITICAL vulnerability identities")
    if secrets:
        failures.append(f"derived image contains {len(secrets)} secret findings")

    summary = {
        "policy": {
            "blocking_severities": sorted(BLOCKING_SEVERITIES),
            "wrapper_added_findings_allowed": 0,
            "secret_findings_allowed": 0,
        },
        "platform": expected_platform,
        "expected_base_ref": expected_base_ref,
        "trivy_version": base_trivy,
        "trivy_database_sha256": database_sha256,
        "trivy_java_database_sha256": java_database_sha256,
        "base": {
            "artifact_id": base.get("ArtifactID"),
            "image_id": base.get("Metadata", {}).get("ImageID"),
            "high_or_critical": len(base_vulnerabilities),
            "layers": len(base_layers),
        },
        "derived": {
            "artifact_id": derived.get("ArtifactID"),
            "image_id": derived.get("Metadata", {}).get("ImageID"),
            "high_or_critical": len(derived_vulnerabilities),
            "layers": len(derived_layers),
            "secret_findings": len(secrets),
        },
        "delta": {
            "added_high_or_critical": added,
            "removed_high_or_critical": removed,
        },
        "failures": failures,
    }
    return summary, failures


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--derived", type=Path, required=True)
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"), required=True)
    parser.add_argument("--expected-base-ref", required=True)
    parser.add_argument("--database-sha256-file", type=Path, required=True)
    parser.add_argument("--java-database-sha256-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        summary, failures = compare(
            _load(arguments.base),
            _load(arguments.derived),
            expected_platform=arguments.platform,
            expected_base_ref=arguments.expected_base_ref,
            database_sha256=_database_sha256(arguments.database_sha256_file),
            java_database_sha256=_java_database_sha256(
                arguments.java_database_sha256_file
            ),
        )
    except ReportError as error:
        summary = {"failures": [str(error)]}
        failures = summary["failures"]
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if failures:
        for failure in failures:
            print(f"supply-chain gate: {failure}", file=sys.stderr)
        return 1
    print(
        "supply-chain gate: clean wrapper delta; "
        f"{summary['base']['high_or_critical']} inherited HIGH/CRITICAL findings reported"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
