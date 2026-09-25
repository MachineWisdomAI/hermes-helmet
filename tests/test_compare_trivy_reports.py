#!/usr/bin/env python3
"""Tests for the dynamic base-versus-wrapper Trivy gate."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compare_trivy_reports", ROOT / "scripts" / "compare_trivy_reports.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

BASE_REF = (
    "nousresearch/hermes-agent:v2026.9.14@"
    "sha256:99641e57ec762c59e54cb44aa6746b7fc68c18b3c5ddb088af54234c613d9294"
)
DATABASE_SHA256 = "a" * 64


def report(*, derived: bool = False) -> dict:
    layers = ["sha256:base"]
    labels = {}
    if derived:
        layers.append("sha256:wrapper")
        labels[MODULE.BASE_LABEL] = BASE_REF
    return {
        "SchemaVersion": 2,
        "Trivy": {"Version": "0.74.0"},
        "ArtifactID": "sha256:derived" if derived else "sha256:base-image",
        "Metadata": {
            "ImageID": "sha256:derived" if derived else "sha256:base-image",
            "DiffIDs": layers,
            "ImageConfig": {
                "architecture": "amd64",
                "os": "linux",
                "config": {"Labels": labels},
            },
        },
        "Results": [
            {
                "Target": "Debian GNU/Linux 13",
                "Class": "os-pkgs",
                "Type": "debian",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-INHERITED",
                        "PkgName": "base-package",
                        "InstalledVersion": "1.0",
                        "FixedVersion": "1.1",
                        "Severity": "HIGH",
                    }
                ],
            }
        ],
    }


class CompareTrivyReportsTests(unittest.TestCase):
    def test_inherited_findings_are_reported_but_do_not_fail_wrapper_gate(self) -> None:
        summary, failures = MODULE.compare(
            report(), report(derived=True),
            expected_platform="linux/amd64", expected_base_ref=BASE_REF,
            database_sha256=DATABASE_SHA256,
            java_database_sha256="absent",
        )
        self.assertEqual(failures, [])
        self.assertEqual(summary["base"]["high_or_critical"], 1)
        self.assertEqual(summary["delta"]["added_high_or_critical"], [])
        inherited = summary["delta"]["inherited_high_or_critical"]
        self.assertEqual(len(inherited), 1)
        self.assertEqual(inherited[0]["id"], "CVE-INHERITED")
        self.assertEqual(inherited[0]["package"], "base-package")
        self.assertEqual(inherited[0]["installed_version"], "1.0")
        self.assertEqual(inherited[0]["fixed_version"], "1.1")
        self.assertEqual(inherited[0]["severity"], "HIGH")
        self.assertEqual(summary["trivy_java_database_sha256"], "absent")

    def test_wrapper_added_high_or_critical_finding_fails(self) -> None:
        derived = report(derived=True)
        derived["Results"][0]["Vulnerabilities"].append(
            {
                "VulnerabilityID": "CVE-WRAPPER",
                "PkgName": "wrapper-package",
                "InstalledVersion": "2.0",
                "Severity": "CRITICAL",
            }
        )
        summary, failures = MODULE.compare(
            report(), derived,
            expected_platform="linux/amd64", expected_base_ref=BASE_REF,
            database_sha256=DATABASE_SHA256,
            java_database_sha256="absent",
        )
        self.assertEqual(len(summary["delta"]["added_high_or_critical"]), 1)
        self.assertTrue(any("wrapper adds 1" in failure for failure in failures))

    def test_severity_worsening_fails_as_an_added_identity(self) -> None:
        derived = report(derived=True)
        derived["Results"][0]["Vulnerabilities"][0]["Severity"] = "CRITICAL"
        summary, failures = MODULE.compare(
            report(), derived,
            expected_platform="linux/amd64", expected_base_ref=BASE_REF,
            database_sha256=DATABASE_SHA256,
            java_database_sha256="absent",
        )
        self.assertEqual(len(summary["delta"]["added_high_or_critical"]), 1)
        self.assertEqual(len(summary["delta"]["removed_high_or_critical"]), 1)
        self.assertTrue(any("wrapper adds 1" in failure for failure in failures))

    def test_any_derived_secret_fails(self) -> None:
        derived = report(derived=True)
        derived["Results"][0]["Secrets"] = [
            {"RuleID": "synthetic", "Severity": "HIGH", "StartLine": 1, "EndLine": 1}
        ]
        summary, failures = MODULE.compare(
            report(), derived,
            expected_platform="linux/amd64", expected_base_ref=BASE_REF,
            database_sha256=DATABASE_SHA256,
            java_database_sha256="absent",
        )
        self.assertEqual(summary["derived"]["secret_findings"], 1)
        self.assertTrue(any("secret findings" in failure for failure in failures))

    def test_lower_severity_derived_secret_fails(self) -> None:
        derived = report(derived=True)
        derived["Results"][0]["Secrets"] = [
            {
                "RuleID": "generic-secret",
                "Severity": "MEDIUM",
                "Match": "AKIAEXAMPLESECRET",
                "Code": {"Lines": [{"Content": "token=AKIAEXAMPLESECRET"}]},
                "StartLine": 2,
                "EndLine": 2,
            }
        ]
        summary, failures = MODULE.compare(
            report(), derived,
            expected_platform="linux/amd64", expected_base_ref=BASE_REF,
            database_sha256=DATABASE_SHA256,
            java_database_sha256="absent",
        )
        self.assertEqual(summary["derived"]["secret_findings"], 1)
        self.assertTrue(any("secret findings" in failure for failure in failures))
        dumped = json.dumps(summary)
        self.assertNotIn("AKIAEXAMPLESECRET", dumped)
        self.assertNotIn("token=", dumped)

    def test_platform_layer_base_label_and_scanner_mismatches_fail(self) -> None:
        derived = report(derived=True)
        derived["Metadata"]["ImageConfig"]["architecture"] = "arm64"
        derived["Metadata"]["DiffIDs"][0] = "sha256:not-the-base"
        derived["Metadata"]["ImageConfig"]["config"]["Labels"][MODULE.BASE_LABEL] = "drift"
        derived["Trivy"]["Version"] = "different"
        _summary, failures = MODULE.compare(
            report(), derived,
            expected_platform="linux/amd64", expected_base_ref=BASE_REF,
            database_sha256=DATABASE_SHA256,
            java_database_sha256="absent",
        )
        self.assertEqual(len(failures), 4)

    def test_incomplete_evidence_fails_closed(self) -> None:
        base = report()
        del base["Metadata"]["DiffIDs"]
        with self.assertRaisesRegex(MODULE.ReportError, "layer identities"):
            MODULE.compare(
                base, report(derived=True),
                expected_platform="linux/amd64", expected_base_ref=BASE_REF,
                database_sha256=DATABASE_SHA256,
                java_database_sha256="absent",
            )

    def test_removed_finding_is_recorded(self) -> None:
        derived = report(derived=True)
        derived["Results"][0]["Vulnerabilities"] = []
        summary, failures = MODULE.compare(
            report(), derived,
            expected_platform="linux/amd64", expected_base_ref=BASE_REF,
            database_sha256=DATABASE_SHA256,
            java_database_sha256="absent",
        )
        self.assertEqual(failures, [])
        self.assertEqual(len(summary["delta"]["removed_high_or_critical"]), 1)
        self.assertEqual(summary["delta"]["inherited_high_or_critical"], [])

    def test_retained_inherited_report_excludes_removed_findings(self) -> None:
        base = report()
        base["Results"][0]["Vulnerabilities"].append(
            {
                "VulnerabilityID": "CVE-REMOVED",
                "PkgName": "gone-package",
                "InstalledVersion": "3.0",
                "FixedVersion": "3.1",
                "Severity": "HIGH",
            }
        )
        derived = report(derived=True)
        summary, failures = MODULE.compare(
            base, derived,
            expected_platform="linux/amd64", expected_base_ref=BASE_REF,
            database_sha256=DATABASE_SHA256,
            java_database_sha256="absent",
        )
        self.assertEqual(failures, [])
        inherited_ids = [
            item["id"] for item in summary["delta"]["inherited_high_or_critical"]
        ]
        removed_ids = [
            item["id"] for item in summary["delta"]["removed_high_or_critical"]
        ]
        self.assertEqual(inherited_ids, ["CVE-INHERITED"])
        self.assertEqual(removed_ids, ["CVE-REMOVED"])
        inherited = summary["delta"]["inherited_high_or_critical"][0]
        self.assertEqual(inherited["package"], "base-package")
        self.assertEqual(inherited["installed_version"], "1.0")
        self.assertEqual(inherited["fixed_version"], "1.1")
        self.assertEqual(inherited["severity"], "HIGH")

    def test_operating_system_mismatch_fails(self) -> None:
        derived = report(derived=True)
        derived["Metadata"]["ImageConfig"]["os"] = "windows"
        _summary, failures = MODULE.compare(
            report(), derived,
            expected_platform="linux/amd64", expected_base_ref=BASE_REF,
            database_sha256=DATABASE_SHA256,
            java_database_sha256="absent",
        )
        self.assertTrue(any("derived platform windows/amd64" in item for item in failures))

    def test_database_digest_evidence_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "trivy-db.sha256"
            evidence.write_text(f"{DATABASE_SHA256}  .cache/trivy/db/trivy.db\n")
            self.assertEqual(MODULE._database_sha256(evidence), DATABASE_SHA256)
            evidence.write_text("not-a-digest\n")
            with self.assertRaisesRegex(MODULE.ReportError, "invalid Trivy database"):
                MODULE._database_sha256(evidence)

            java_evidence = Path(directory) / "trivy-java-db.sha256"
            java_evidence.write_text("absent\n")
            self.assertEqual(MODULE._java_database_sha256(java_evidence), "absent")
            java_evidence.write_text(
                f"{DATABASE_SHA256}  .cache/trivy/java-db/trivy-java.db\n"
            )
            self.assertEqual(
                MODULE._java_database_sha256(java_evidence), DATABASE_SHA256
            )
            java_evidence.write_text("not-a-digest\n")
            with self.assertRaisesRegex(
                MODULE.ReportError, "invalid Trivy Java database"
            ):
                MODULE._java_database_sha256(java_evidence)


if __name__ == "__main__":
    unittest.main()
