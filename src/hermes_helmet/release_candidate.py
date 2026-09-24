#!/usr/bin/env python3
"""Private v0.1.0 release-candidate evidence, packaging helpers, and promotion gate.

CI may package and prove a private ``0.1.0rc1`` candidate. The scanned image,
final manifest, and owner-gated GHCR/GitHub promotion share that version.
Minting numbered ``0.1.0`` is out of scope for this candidate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


PACKAGE_VERSION = "0.1.0rc1"
IMAGE_VERSION = PACKAGE_VERSION
MINTED_VERSION = "0.1.0"
CHANNEL = "private-rc"
REQUIRED_REVIEW_TRIGGERS = frozenset(
    {"next-stable-hermes", "base-digest-change", "emergency-security"}
)
TRIVY_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[.-][0-9A-Za-z]+)*$")
GITHUB_RELEASE_EXTRA_ASSETS = ("SHA256SUMS", "release-evidence.json")
BUNDLED_SKILLS = ("setup-helmet", "helmet-issue", "helmet-epic")
IMAGE_REPOSITORY = "ghcr.io/machinewisdomai/hermes-helmet-oss"
PLATFORMS = ("linux/amd64", "linux/arm64")
PREVIOUS_SOURCE_REVISION = "2c687a453fffe06385e2ab858288e23a153cfb10"
PREVIOUS_IMAGE_DIGEST = (
    "sha256:316b6849cf6d663dc7fe40d7b3a543ae1f0d16d20b9dd58e0f0b2564c8424691"
)
PREVIOUS_IMAGE_REF = f"{IMAGE_REPOSITORY}:dev-{PREVIOUS_SOURCE_REVISION}"
PREVIOUS_IMAGE_PIN = f"{IMAGE_REPOSITORY}@{PREVIOUS_IMAGE_DIGEST}"
SOURCE_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")
CHECKSUM_LINE_RE = re.compile(r"^([0-9a-f]{64})  ([^/\s]+)$")
DEFAULT_GITHUB_REPOSITORY = "MachineWisdomAI/hermes-helmet-oss"
PINNED_HERMES_BASE = (
    "sha256:99641e57ec762c59e54cb44aa6746b7fc68c18b3c5ddb088af54234c613d9294"
)
MAX_OWNER_DECISION_AGE = timedelta(days=30)
INTENDED_DIST = Path("dist") / "rc"
DOCUMENT_FILES = {
    "changelog": "CHANGELOG.md",
    "license": "LICENSE",
    "notice": "NOTICE",
    "sbom": "source-sbom.spdx.json",
    "provenance": "provenance.json",
}
PLATFORM_REPORT_FILES = {
    "linux/amd64": "delta-amd64.json",
    "linux/arm64": "delta-arm64.json",
}
REQUIRED_DOCUMENT_FILES = tuple(DOCUMENT_FILES.values())
REQUIRED_REPORT_FILES = tuple(PLATFORM_REPORT_FILES.values())
SKIP_CHECKSUM_NAMES = frozenset({"SHA256SUMS", "release-evidence.json"})
FORBIDDEN_WORKDIR_PATHS = (
    Path("/"),
    Path("/tmp"),
    Path("/var"),
    Path("/usr"),
    Path("/etc"),
    Path("/home"),
    Path("/opt"),
    Path("/root"),
    Path("/dev"),
    Path("/proc"),
    Path("/sys"),
    Path("/boot"),
)

ROLLBACK_PROCEDURE = (
    "Keep GHCR, the GitHub repository, any release, and the package private. "
    "Redeploy the last tested H11 image by pulling and running the immutable "
    f"digest `{PREVIOUS_IMAGE_PIN}` (source revision {PREVIOUS_SOURCE_REVISION}, "
    f"mutable tag `{PREVIOUS_IMAGE_REF}` is not the rollback target). "
    "Do not rebuild. Do not retag `0.1.0`. Do not change visibility. Confirm "
    "`hermes-helmet doctor` and read-only `hermes-helmet status` against that "
    "previous revision."
)


class ReleaseCandidateError(RuntimeError):
    """Release-candidate evidence or promotion is not acceptable."""


@dataclass(frozen=True)
class Artifact:
    name: str
    sha256: str


@dataclass(frozen=True)
class PromotionPlan:
    source_digest: str
    tags: tuple[str, ...]
    reuse_existing_manifest: bool
    rebuild: bool
    repository: str = IMAGE_REPOSITORY
    rollback_digest: str = PREVIOUS_IMAGE_DIGEST


@dataclass(frozen=True)
class GitHubReleaseAsset:
    name: str
    digest: str = ""


@dataclass(frozen=True)
class GitHubReleasePlan:
    tag: str
    target: str
    title: str
    notes: str
    assets: tuple[str, ...]
    prerelease: bool = True
    latest: bool = False
    dry_run: bool = True
    reuse_existing: bool = False
    repository: str = DEFAULT_GITHUB_REPOSITORY


@dataclass(frozen=True)
class GitHubCandidateRef:
    tag: str
    commit: str | None
    tag_present: bool
    release_present: bool
    prerelease: bool = False
    latest: bool = False
    reuse_existing: bool = False
    assets: tuple[GitHubReleaseAsset, ...] = ()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_digest(value: object, *, label: str) -> str:
    text = str(value or "")
    if not DIGEST_RE.match(text):
        raise ReleaseCandidateError(f"{label} must be one immutable sha256 digest")
    if "," in text:
        raise ReleaseCandidateError(f"{label} must be one immutable sha256 digest")
    return text


def _require_private(mapping: Mapping[str, object], key: str) -> None:
    if mapping.get(key) != "private":
        raise ReleaseCandidateError(f"{key} visibility must remain private")


def _parse_expires_at(value: object) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ReleaseCandidateError("owner decision must name expires/review conditions")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReleaseCandidateError("owner decision expires_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ReleaseCandidateError("owner decision expires_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validate_review_conditions(value: object) -> None:
    if not isinstance(value, Mapping):
        raise ReleaseCandidateError("owner decision must name canonical review conditions")
    extra = set(value) - {"triggers", "notes"}
    if extra:
        raise ReleaseCandidateError("owner decision must name canonical review conditions")
    triggers = value.get("triggers")
    if not isinstance(triggers, list) or not triggers:
        raise ReleaseCandidateError("owner decision must name canonical review conditions")
    if any(not isinstance(item, str) or not item.strip() for item in triggers):
        raise ReleaseCandidateError("owner decision must name canonical review conditions")
    if set(triggers) != REQUIRED_REVIEW_TRIGGERS:
        raise ReleaseCandidateError("owner decision must name canonical review conditions")
    notes = value.get("notes", "")
    if notes is not None and not isinstance(notes, str):
        raise ReleaseCandidateError("owner decision must name canonical review conditions")


def validate_sanitized_report(path: Path, *, platform: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseCandidateError(f"platform scan report is unreadable: {path.name}") from exc
    if not isinstance(payload, Mapping):
        raise ReleaseCandidateError(f"platform scan report is invalid: {path.name}")
    if payload.get("platform") != platform:
        raise ReleaseCandidateError(f"sanitized report platform mismatch: {path.name}")
    expected_base = str(payload.get("expected_base_ref") or "")
    if not expected_base.endswith("@" + PINNED_HERMES_BASE):
        raise ReleaseCandidateError("sanitized report base reference is not the pinned Hermes base")
    failures = payload.get("failures")
    if not isinstance(failures, list) or any(failures):
        raise ReleaseCandidateError("sanitized report must have zero failures")
    delta = payload.get("delta")
    if not isinstance(delta, Mapping):
        raise ReleaseCandidateError(f"platform scan report is missing delta: {path.name}")
    added = delta.get("added_high_or_critical")
    if not isinstance(added, list) or added:
        raise ReleaseCandidateError("sanitized report must have zero added HIGH/CRITICAL identities")
    derived = payload.get("derived")
    if not isinstance(derived, Mapping):
        raise ReleaseCandidateError(f"platform scan report is missing derived counts: {path.name}")
    secrets = derived.get("secret_findings")
    if not isinstance(secrets, int) or secrets != 0:
        raise ReleaseCandidateError("sanitized report must have zero secrets")
    database = str(payload.get("trivy_database_sha256") or "")
    if not CHECKSUM_RE.match(database):
        raise ReleaseCandidateError("sanitized report database identity is required")
    java_database = str(payload.get("trivy_java_database_sha256") or "")
    if java_database != "absent" and not CHECKSUM_RE.match(java_database):
        raise ReleaseCandidateError("sanitized report Java database identity is required")
    trivy_version = str(payload.get("trivy_version") or "").strip()
    if not TRIVY_VERSION_RE.match(trivy_version):
        raise ReleaseCandidateError("sanitized report Trivy version is required")
    base = payload.get("base")
    if not isinstance(base, Mapping):
        raise ReleaseCandidateError(f"platform scan report is missing inherited counts: {path.name}")
    count = base.get("high_or_critical")
    if not isinstance(count, int) or count < 0:
        raise ReleaseCandidateError(f"platform scan report inherited count is invalid: {path.name}")
    return {
        "inherited_high_critical": count,
        "trivy_database_sha256": database,
        "trivy_java_database_sha256": java_database,
        "trivy_version": trivy_version,
    }


def inherited_high_critical_count(path: Path) -> int:
    platform = next(
        (name for name, filename in PLATFORM_REPORT_FILES.items() if filename == path.name),
        "",
    )
    if not platform:
        raise ReleaseCandidateError(f"platform scan report is invalid: {path.name}")
    report = validate_sanitized_report(path, platform=platform)
    count = report["inherited_high_critical"]
    if not isinstance(count, int):
        raise ReleaseCandidateError(f"platform scan report inherited count is invalid: {path.name}")
    return count


def write_provenance_file(
    dist_dir: Path, *, source_revision: str, image_digest: str
) -> Path:
    digest = _require_digest(image_digest, label="image digest")
    if not SOURCE_REVISION_RE.match(source_revision):
        raise ReleaseCandidateError("exact source revision is required")
    payload = {
        "builder": "docker/build-push-action",
        "image": f"{IMAGE_REPOSITORY}@{digest}",
        "source_revision": source_revision,
        "attestations": [
            "BuildKit SBOM (sbom=true)",
            "SLSA provenance (provenance: mode=max)",
        ],
        "bound_to": "image digest, not a mutable tag",
    }
    path = dist_dir / DOCUMENT_FILES["provenance"]
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def validate_evidence(payload: Mapping[str, object]) -> dict[str, object]:
    """Fail closed unless the private RC evidence contract is complete."""

    if payload.get("version") != PACKAGE_VERSION:
        raise ReleaseCandidateError("evidence version must be the private RC package")
    if payload.get("channel") != CHANNEL:
        raise ReleaseCandidateError("evidence channel must be private-rc")
    if payload.get("minted_version") != MINTED_VERSION:
        raise ReleaseCandidateError("minted version remains 0.1.0 until owner promotion")
    revision = str(payload.get("source_revision") or "")
    if not SOURCE_REVISION_RE.match(revision):
        raise ReleaseCandidateError("exact source revision is required")
    if payload.get("base_digest") != PINNED_HERMES_BASE:
        raise ReleaseCandidateError("candidate evidence must record the pinned Hermes base digest")

    image = payload.get("image")
    if not isinstance(image, Mapping):
        raise ReleaseCandidateError("image evidence is required")
    if image.get("repository") != IMAGE_REPOSITORY:
        raise ReleaseCandidateError("image repository is not the private Hermes Helmet image")
    _require_digest(image.get("digest"), label="image digest")
    platforms = tuple(image.get("platforms") or ())
    if platforms != PLATFORMS:
        raise ReleaseCandidateError("image must record linux/amd64 and linux/arm64")
    tags = tuple(image.get("tags") or ())
    if not tags:
        raise ReleaseCandidateError("image tags are required")
    if any(str(tag) in {MINTED_VERSION, f"{MINTED_VERSION}-rc"} for tag in tags):
        raise ReleaseCandidateError("numbered 0.1.0 tags are not minted by this candidate")
    if image.get("version") != IMAGE_VERSION:
        raise ReleaseCandidateError("image version must be the private RC package")

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ReleaseCandidateError("checksums for non-image artifacts are required")
    names: set[str] = set()
    for item in artifacts:
        if not isinstance(item, Mapping):
            raise ReleaseCandidateError("artifact checksum entries must be objects")
        name = str(item.get("name") or "")
        checksum = str(item.get("sha256") or "")
        if not name or not CHECKSUM_RE.match(checksum):
            raise ReleaseCandidateError("artifact checksums must be sha256 hex digests")
        names.add(name)
    if not any(name.endswith(".whl") for name in names):
        raise ReleaseCandidateError("checksums for non-image artifacts are required")
    if not any(name.endswith(".tar.gz") for name in names):
        raise ReleaseCandidateError("checksums for non-image artifacts are required")
    missing = [name for name in (*REQUIRED_DOCUMENT_FILES, *REQUIRED_REPORT_FILES) if name not in names]
    if missing:
        raise ReleaseCandidateError("checksums for non-image artifacts are required")

    skills = tuple(payload.get("bundled_skills") or ())
    if skills != BUNDLED_SKILLS:
        raise ReleaseCandidateError("release must package the three portable skills")

    documents = payload.get("documents")
    if not isinstance(documents, Mapping):
        raise ReleaseCandidateError("changelog, licenses/notices, SBOM, and provenance are required")
    for key, filename in DOCUMENT_FILES.items():
        if documents.get(key) != filename:
            raise ReleaseCandidateError("changelog, licenses/notices, SBOM, and provenance are required")
        if filename not in names:
            raise ReleaseCandidateError("changelog, licenses/notices, SBOM, and provenance are required")

    rollback = payload.get("rollback")
    if not isinstance(rollback, Mapping):
        raise ReleaseCandidateError("previous rollback target is required")
    if rollback.get("previous_source_revision") != PREVIOUS_SOURCE_REVISION:
        raise ReleaseCandidateError("previous tested rollback target is required")
    if rollback.get("previous_image_ref") != PREVIOUS_IMAGE_REF:
        raise ReleaseCandidateError("previous tested rollback target is required")
    if rollback.get("previous_image_digest") != PREVIOUS_IMAGE_DIGEST:
        raise ReleaseCandidateError("previous tested rollback digest is required")
    if tuple(rollback.get("previous_platforms") or ()) != PLATFORMS:
        raise ReleaseCandidateError("previous tested rollback platforms are required")
    if PREVIOUS_IMAGE_DIGEST not in str(rollback.get("procedure") or ""):
        raise ReleaseCandidateError("rollback procedure is required")

    visibility = payload.get("visibility")
    if not isinstance(visibility, Mapping):
        raise ReleaseCandidateError("visibility evidence is required")
    for key in ("repository", "release", "package", "image"):
        _require_private(visibility, key)
    if visibility.get("publication_change") is not False:
        raise ReleaseCandidateError("publication or visibility change is forbidden")

    promotion = payload.get("promotion")
    if not isinstance(promotion, Mapping):
        raise ReleaseCandidateError("promotion gate is required")
    if promotion.get("automatic") is not False:
        raise ReleaseCandidateError("automatic numbered promotion is forbidden")
    if promotion.get("requires_owner_decision") is not True:
        raise ReleaseCandidateError("owner decision is required before minting 0.1.0")
    if promotion.get("rebuild_forbidden") is not True:
        raise ReleaseCandidateError("promotion must reuse the scanned manifest without rebuild")

    return json.loads(json.dumps(payload))


def verify_evidence_files(dist_dir: Path, payload: Mapping[str, object]) -> None:
    validated = validate_evidence(payload)
    artifacts = validated["artifacts"]
    if not isinstance(artifacts, list):
        raise ReleaseCandidateError("checksums for non-image artifacts are required")
    for item in artifacts:
        if not isinstance(item, Mapping):
            raise ReleaseCandidateError("artifact checksum entries must be objects")
        name = str(item["name"])
        expected = str(item["sha256"])
        path = dist_dir / name
        if not path.is_file():
            raise ReleaseCandidateError(f"release artifact is missing: {name}")
        if sha256_file(path) != expected:
            raise ReleaseCandidateError(f"release artifact checksum mismatch: {name}")
        if path.resolve().parent != dist_dir.resolve():
            raise ReleaseCandidateError(f"release artifact escaped dist dir: {name}")


def build_evidence(
    *,
    source_revision: str,
    image_digest: str,
    artifacts: Sequence[Artifact],
    image_tags: Sequence[str] | None = None,
) -> dict[str, object]:
    digest = _require_digest(image_digest, label="image digest")
    if not SOURCE_REVISION_RE.match(source_revision):
        raise ReleaseCandidateError("exact source revision is required")
    tags = list(image_tags or (f"dev-{source_revision}",))
    payload = {
        "version": PACKAGE_VERSION,
        "channel": CHANNEL,
        "minted_version": MINTED_VERSION,
        "source_revision": source_revision,
        "base_digest": PINNED_HERMES_BASE,
        "image": {
            "repository": IMAGE_REPOSITORY,
            "digest": digest,
            "platforms": list(PLATFORMS),
            "tags": tags,
            "version": IMAGE_VERSION,
        },
        "artifacts": [{"name": item.name, "sha256": item.sha256} for item in artifacts],
        "bundled_skills": list(BUNDLED_SKILLS),
        "documents": dict(DOCUMENT_FILES),
        "rollback": {
            "previous_source_revision": PREVIOUS_SOURCE_REVISION,
            "previous_image_ref": PREVIOUS_IMAGE_REF,
            "previous_image_digest": PREVIOUS_IMAGE_DIGEST,
            "previous_platforms": list(PLATFORMS),
            "procedure": ROLLBACK_PROCEDURE,
        },
        "visibility": {
            "repository": "private",
            "release": "private",
            "package": "private",
            "image": "private",
            "publication_change": False,
        },
        "promotion": {
            "automatic": False,
            "requires_owner_decision": True,
            "rebuild_forbidden": True,
        },
    }
    return validate_evidence(payload)


def collect_artifacts(dist_dir: Path, *, complete: bool = False) -> tuple[Artifact, ...]:
    if not dist_dir.is_dir():
        raise ReleaseCandidateError("release dist directory is missing")
    items: list[Artifact] = []
    for path in sorted(dist_dir.iterdir()):
        if not path.is_file() or path.name in SKIP_CHECKSUM_NAMES:
            continue
        if path.suffix in {".whl", ".gz"} or path.name in {
            *REQUIRED_DOCUMENT_FILES,
            *REQUIRED_REPORT_FILES,
        }:
            items.append(Artifact(name=path.name, sha256=sha256_file(path)))
    if not items:
        raise ReleaseCandidateError("no wheel or sdist artifacts found")
    if complete:
        names = {item.name for item in items}
        if not any(name.endswith(".whl") for name in names):
            raise ReleaseCandidateError("no wheel or sdist artifacts found")
        if not any(name.endswith(".tar.gz") for name in names):
            raise ReleaseCandidateError("no wheel or sdist artifacts found")
        missing = [name for name in (*REQUIRED_DOCUMENT_FILES, *REQUIRED_REPORT_FILES) if name not in names]
        if missing:
            raise ReleaseCandidateError("checksums for non-image artifacts are required")
    return tuple(items)


def write_checksums(dist_dir: Path, artifacts: Iterable[Artifact]) -> Path:
    path = dist_dir / "SHA256SUMS"
    lines = [f"{item.sha256}  {item.name}\n" for item in artifacts]
    path.write_text("".join(lines), encoding="utf-8")
    return path


def _artifact_checksums(artifacts: Iterable[Artifact] | Sequence[object]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for item in artifacts:
        if isinstance(item, Artifact):
            name, checksum = item.name, item.sha256
        elif isinstance(item, Mapping):
            name = str(item.get("name") or "")
            checksum = str(item.get("sha256") or "")
        else:
            raise ReleaseCandidateError("artifact checksum entries must be objects")
        if not name or not CHECKSUM_RE.match(checksum):
            raise ReleaseCandidateError("artifact checksums must be sha256 hex digests")
        if name in expected or name in SKIP_CHECKSUM_NAMES or "/" in name:
            raise ReleaseCandidateError("checksum manifest is invalid")
        expected[name] = checksum
    if not expected:
        raise ReleaseCandidateError("checksums for non-image artifacts are required")
    return expected


def validate_checksum_manifest(
    dist_dir: Path, artifacts: Iterable[Artifact] | Sequence[object]
) -> None:
    expected = _artifact_checksums(artifacts)
    path = dist_dir / "SHA256SUMS"
    if not path.is_file():
        raise ReleaseCandidateError("checksum manifest is missing")
    if path.resolve().parent != dist_dir.resolve():
        raise ReleaseCandidateError("release artifact escaped dist dir: SHA256SUMS")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseCandidateError("checksum manifest is unreadable") from exc
    parsed: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            raise ReleaseCandidateError("checksum manifest is invalid")
        match = CHECKSUM_LINE_RE.match(line)
        if match is None:
            raise ReleaseCandidateError("checksum manifest is invalid")
        checksum, name = match.group(1), match.group(2)
        if name in parsed or name in SKIP_CHECKSUM_NAMES:
            raise ReleaseCandidateError("checksum manifest is invalid")
        parsed[name] = checksum
    if parsed != expected:
        raise ReleaseCandidateError("checksum manifest does not match bound evidence")
    for name, checksum in expected.items():
        artifact = dist_dir / name
        if not artifact.is_file():
            raise ReleaseCandidateError(f"release artifact is missing: {name}")
        if sha256_file(artifact) != checksum:
            raise ReleaseCandidateError(f"release artifact checksum mismatch: {name}")


def validate_owner_decision(
    payload: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    if payload.get("version") != PACKAGE_VERSION:
        raise ReleaseCandidateError("owner decision must name candidate version 0.1.0rc1")
    _require_digest(payload.get("base_digest"), label="exact base digest")
    _require_digest(payload.get("image_digest"), label="image digest")
    if payload.get("base_digest") != PINNED_HERMES_BASE:
        raise ReleaseCandidateError("owner decision must name the exact pinned Hermes base digest")
    revision = str(payload.get("source_revision") or "")
    if not SOURCE_REVISION_RE.match(revision):
        raise ReleaseCandidateError("owner decision must name the exact source revision")
    _require_digest(payload.get("rollback_digest"), label="previous tested image digest")
    if payload.get("rollback_digest") != PREVIOUS_IMAGE_DIGEST:
        raise ReleaseCandidateError("owner decision must name the previous tested rollback digest")
    expires = _parse_expires_at(payload.get("expires_at"))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if expires <= current.astimezone(timezone.utc):
        raise ReleaseCandidateError("owner decision has expired")
    if expires > current.astimezone(timezone.utc) + MAX_OWNER_DECISION_AGE:
        raise ReleaseCandidateError("owner decision expiry exceeds 30 days")
    checksum = str(payload.get("evidence_sha256") or "")
    if not CHECKSUM_RE.match(checksum):
        raise ReleaseCandidateError("owner decision must name the candidate evidence digest")
    _validate_review_conditions(payload.get("review_conditions"))
    if payload.get("accepted_inherited_risk") is not True:
        raise ReleaseCandidateError("inherited HIGH/CRITICAL risk must be explicitly accepted")
    if payload.get("publication") is not False:
        raise ReleaseCandidateError("publication remains forbidden")
    if payload.get("visibility") != "private":
        raise ReleaseCandidateError("visibility must remain private")
    platforms = payload.get("platforms")
    if not isinstance(platforms, Mapping):
        raise ReleaseCandidateError("both platform scan reports are required")
    for name in PLATFORMS:
        entry = platforms.get(name)
        if not isinstance(entry, Mapping):
            raise ReleaseCandidateError("both platform scan reports are required")
        report = str(entry.get("report") or "").strip()
        if not report or Path(report).name != PLATFORM_REPORT_FILES[name]:
            raise ReleaseCandidateError("both platform scan reports are required")
        checksum = str(entry.get("report_sha256") or "")
        if not CHECKSUM_RE.match(checksum):
            raise ReleaseCandidateError("platform scan report checksums are required")
        count = entry.get("inherited_high_critical")
        if not isinstance(count, int) or count < 0:
            raise ReleaseCandidateError("inherited high/critical counts are required")
    return json.loads(json.dumps(payload))


def bind_owner_decision(
    payload: Mapping[str, object],
    *,
    evidence: Mapping[str, object],
    dist_dir: Path,
    now: datetime | None = None,
) -> dict[str, object]:
    decision = validate_owner_decision(payload, now=now)
    recorded = validate_evidence(evidence)
    verify_evidence_files(dist_dir, recorded)
    image = recorded.get("image")
    rollback = recorded.get("rollback")
    if not isinstance(image, Mapping) or not isinstance(rollback, Mapping):
        raise ReleaseCandidateError("candidate evidence is incomplete")
    if decision["image_digest"] != image.get("digest"):
        raise ReleaseCandidateError("owner decision image digest must match the scanned manifest")
    if decision["source_revision"] != recorded["source_revision"]:
        raise ReleaseCandidateError("owner decision source revision must match candidate evidence")
    if decision["rollback_digest"] != rollback.get("previous_image_digest"):
        raise ReleaseCandidateError("owner decision rollback digest must match candidate evidence")
    if decision["base_digest"] != recorded.get("base_digest"):
        raise ReleaseCandidateError("owner decision base digest must match candidate evidence")
    evidence_path = dist_dir / "release-evidence.json"
    if not evidence_path.is_file():
        raise ReleaseCandidateError("candidate evidence is missing")
    try:
        on_disk = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseCandidateError("candidate evidence is missing") from exc
    if json.dumps(validate_evidence(on_disk), sort_keys=True) != json.dumps(
        recorded, sort_keys=True
    ):
        raise ReleaseCandidateError("owner decision evidence must match the candidate file")
    if sha256_file(evidence_path) != decision.get("evidence_sha256"):
        raise ReleaseCandidateError("owner decision evidence digest mismatch")
    platforms = decision["platforms"]
    if not isinstance(platforms, Mapping):
        raise ReleaseCandidateError("both platform scan reports are required")
    database_identities: set[tuple[object, object, object]] = set()
    for name in PLATFORMS:
        entry = platforms[name]
        if not isinstance(entry, Mapping):
            raise ReleaseCandidateError("both platform scan reports are required")
        filename = PLATFORM_REPORT_FILES[name]
        path = dist_dir / filename
        if not path.is_file():
            raise ReleaseCandidateError("both platform scan reports are required")
        digest = sha256_file(path)
        if digest != entry.get("report_sha256"):
            raise ReleaseCandidateError("platform scan report checksum mismatch")
        report = validate_sanitized_report(path, platform=name)
        if report["inherited_high_critical"] != entry.get("inherited_high_critical"):
            raise ReleaseCandidateError("inherited high/critical counts do not match sanitized reports")
        database_identities.add(
            (
                report["trivy_version"],
                report["trivy_database_sha256"],
                report["trivy_java_database_sha256"],
            )
        )
    if len(database_identities) != 1:
        raise ReleaseCandidateError("sanitized reports must share one Trivy version and database identity")
    return decision


def inspect_rollback_image(digest: str = PREVIOUS_IMAGE_DIGEST) -> None:
    pin = f"{IMAGE_REPOSITORY}@{_require_digest(digest, label='previous tested image digest')}"
    commands = (
        ["docker", "buildx", "imagetools", "inspect", pin],
        ["docker", "manifest", "inspect", pin],
    )
    errors: list[str] = []
    for command in commands:
        try:
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as exc:
            errors.append(str(exc))
            continue
        if completed.returncode == 0 and completed.stdout.strip():
            return
        errors.append(completed.stderr.strip() or completed.stdout.strip() or "inspect failed")
    raise ReleaseCandidateError("previous tested rollback image is not pullable")


def promotion_plan(
    *,
    decision: Mapping[str, object] | None,
    image_digest: str,
    evidence: Mapping[str, object] | None = None,
    dist_dir: Path | None = None,
    now: datetime | None = None,
    inspect_rollback: Callable[[str], None] | None = inspect_rollback_image,
) -> PromotionPlan:
    if decision is None:
        raise ReleaseCandidateError("owner decision is required before minting 0.1.0")
    if evidence is None or dist_dir is None:
        raise ReleaseCandidateError("owner decision must be bound to candidate evidence")
    validated = bind_owner_decision(decision, evidence=evidence, dist_dir=dist_dir, now=now)
    digest = _require_digest(image_digest, label="image digest")
    if validated["image_digest"] != digest:
        raise ReleaseCandidateError("owner decision image digest must match the scanned manifest")
    if inspect_rollback is not None:
        inspect_rollback(str(validated["rollback_digest"]))
    return PromotionPlan(
        source_digest=digest,
        tags=(PACKAGE_VERSION,),
        reuse_existing_manifest=True,
        rebuild=False,
        rollback_digest=str(validated["rollback_digest"]),
    )


def promotion_command(plan: PromotionPlan) -> list[str]:
    if plan.rebuild or not plan.reuse_existing_manifest:
        raise ReleaseCandidateError("promotion must reuse the scanned manifest without rebuild")
    command = ["docker", "buildx", "imagetools", "create"]
    for tag in plan.tags:
        command.extend(["--tag", f"{plan.repository}:{tag}"])
    command.append(f"{plan.repository}@{plan.source_digest}")
    return command


def _canonical_github_repository(value: str) -> str:
    text = value.strip()
    text = re.sub(r"^https?://github\.com/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^git@github\.com:", "", text, flags=re.IGNORECASE)
    text = text.removesuffix(".git").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text):
        raise ReleaseCandidateError("GitHub repository identity is invalid")
    return text


def resolve_github_repository(repository: str | None = None) -> str:
    bound = DEFAULT_GITHUB_REPOSITORY
    if repository is not None:
        if _canonical_github_repository(repository).lower() != bound.lower():
            raise ReleaseCandidateError("GitHub repository is not the bound project repository")
    for key in ("GH_REPO", "GITHUB_REPOSITORY"):
        value = os.environ.get(key)
        if not value:
            continue
        if _canonical_github_repository(value).lower() != bound.lower():
            raise ReleaseCandidateError(f"{key} does not match the bound GitHub repository")
    return bound


def _github_api(
    path: str,
    *,
    method: str = "GET",
    fields: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, object] | None]:
    command = [
        "gh",
        "api",
        "--header",
        "Accept: application/vnd.github+json",
        "--header",
        "X-GitHub-Api-Version: 2022-11-28",
        path,
    ]
    if method != "GET":
        command.extend(["--method", method])
    if fields:
        for key, value in fields.items():
            command.extend(["-f", f"{key}={value}"])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ReleaseCandidateError("cannot verify candidate GitHub tag") from exc
    payload: dict[str, object] | None
    try:
        parsed = json.loads(completed.stdout or "null")
    except json.JSONDecodeError:
        parsed = None
    payload = parsed if isinstance(parsed, dict) else None
    if completed.returncode == 0:
        return 200, payload
    message = str((payload or {}).get("message") or completed.stderr or "")
    lowered = message.lower()
    if "not found" in lowered:
        return 404, payload
    if "already exists" in lowered or "unprocessable" in lowered:
        return 422, payload
    raise ReleaseCandidateError("cannot verify candidate GitHub tag")


def _peel_tag_commit(
    payload: Mapping[str, object],
    *,
    repository: str,
    api: Callable[[str], tuple[int, dict[str, object] | None]],
) -> str:
    obj = payload.get("object")
    if not isinstance(obj, Mapping):
        raise ReleaseCandidateError("cannot verify candidate GitHub tag")
    kind = str(obj.get("type") or "")
    sha = str(obj.get("sha") or "").lower()
    if kind == "commit":
        if not SOURCE_REVISION_RE.match(sha):
            raise ReleaseCandidateError("cannot verify candidate GitHub tag")
        return sha
    if kind != "tag":
        raise ReleaseCandidateError("cannot verify candidate GitHub tag")
    status, annotated = api(f"/repos/{repository}/git/tags/{sha}")
    if status != 200 or not isinstance(annotated, Mapping):
        raise ReleaseCandidateError("cannot verify candidate GitHub tag")
    inner = annotated.get("object")
    if not isinstance(inner, Mapping) or str(inner.get("type") or "") != "commit":
        raise ReleaseCandidateError("cannot verify candidate GitHub tag")
    commit = str(inner.get("sha") or "").lower()
    if not SOURCE_REVISION_RE.match(commit):
        raise ReleaseCandidateError("cannot verify candidate GitHub tag")
    return commit


def _release_assets(payload: Mapping[str, object]) -> tuple[GitHubReleaseAsset, ...]:
    raw = payload.get("assets")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ReleaseCandidateError("cannot verify candidate GitHub tag")
    assets: list[GitHubReleaseAsset] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ReleaseCandidateError("cannot verify candidate GitHub tag")
        name = str(item.get("name") or "")
        if not name or "/" in name or name in seen:
            raise ReleaseCandidateError("cannot verify candidate GitHub tag")
        seen.add(name)
        assets.append(GitHubReleaseAsset(name=name, digest=str(item.get("digest") or "")))
    return tuple(assets)


def _normalize_release_asset_digest(value: object) -> str:
    text = str(value or "").strip().lower()
    if CHECKSUM_RE.match(text):
        return f"sha256:{text}"
    if DIGEST_RE.match(text):
        return text
    raise ReleaseCandidateError(
        "existing GitHub prerelease asset digest is missing; repair the release or delete it and retry"
    )


def _validate_existing_release_assets(
    state: GitHubCandidateRef,
    *,
    dist_dir: Path,
    names: Sequence[str],
) -> None:
    if not state.assets:
        raise ReleaseCandidateError(
            "existing GitHub prerelease is missing required assets; repair the release or delete it and retry"
        )
    remote: dict[str, str] = {}
    for asset in state.assets:
        if asset.name in remote:
            raise ReleaseCandidateError("existing GitHub prerelease assets are invalid")
        remote[asset.name] = _normalize_release_asset_digest(asset.digest)
    expected = {name: f"sha256:{sha256_file(dist_dir / name)}" for name in names}
    missing = [name for name in expected if name not in remote]
    if missing:
        raise ReleaseCandidateError(
            "existing GitHub prerelease is missing required assets; repair the release or delete it and retry"
        )
    for name, digest in expected.items():
        if remote[name] != digest:
            raise ReleaseCandidateError(
                "existing GitHub prerelease asset digest does not match bound evidence"
            )


def inspect_github_candidate_ref(
    tag: str,
    *,
    expected_revision: str,
    repository: str | None = None,
    api: Callable[[str], tuple[int, dict[str, object] | None]] | None = None,
) -> GitHubCandidateRef:
    expected = expected_revision.lower()
    if not SOURCE_REVISION_RE.match(expected):
        raise ReleaseCandidateError("exact source revision is required")
    repo = resolve_github_repository(repository)
    query = api or _github_api
    tag_status, tag_payload = query(f"/repos/{repo}/git/ref/tags/{tag}")
    release_status, release_payload = query(f"/repos/{repo}/releases/tags/{tag}")
    if tag_status not in {200, 404} or release_status not in {200, 404}:
        raise ReleaseCandidateError("cannot verify candidate GitHub tag")
    if tag_status == 404:
        if release_status == 200:
            raise ReleaseCandidateError("existing GitHub release is missing its candidate tag")
        return GitHubCandidateRef(
            tag=tag,
            commit=None,
            tag_present=False,
            release_present=False,
        )
    if not isinstance(tag_payload, Mapping):
        raise ReleaseCandidateError("cannot verify candidate GitHub tag")
    commit = _peel_tag_commit(tag_payload, repository=repo, api=query)
    if commit != expected:
        raise ReleaseCandidateError(
            "existing candidate tag does not match the approved source revision"
        )
    if release_status == 404:
        return GitHubCandidateRef(
            tag=tag,
            commit=commit,
            tag_present=True,
            release_present=False,
        )
    if not isinstance(release_payload, Mapping):
        raise ReleaseCandidateError("cannot verify candidate GitHub tag")
    if release_payload.get("prerelease") is not True:
        raise ReleaseCandidateError("GitHub release must remain a private prerelease")
    latest_status, latest_payload = query(f"/repos/{repo}/releases/latest")
    if latest_status == 200 and isinstance(latest_payload, Mapping):
        if str(latest_payload.get("tag_name") or "") == tag:
            raise ReleaseCandidateError("GitHub release must not be marked latest")
    elif latest_status not in {200, 404}:
        raise ReleaseCandidateError("cannot verify candidate GitHub tag")
    targetish = str(release_payload.get("target_commitish") or "").lower()
    if SOURCE_REVISION_RE.match(targetish) and targetish != expected:
        raise ReleaseCandidateError(
            "existing candidate tag does not match the approved source revision"
        )
    return GitHubCandidateRef(
        tag=tag,
        commit=commit,
        tag_present=True,
        release_present=True,
        prerelease=True,
        latest=False,
        reuse_existing=True,
        assets=_release_assets(release_payload),
    )


def github_release_plan(
    *,
    decision: Mapping[str, object],
    evidence: Mapping[str, object],
    dist_dir: Path,
    now: datetime | None = None,
    dry_run: bool = True,
    inspect_ref: Callable[..., GitHubCandidateRef] | None = inspect_github_candidate_ref,
) -> GitHubReleasePlan:
    bind_owner_decision(decision, evidence=evidence, dist_dir=dist_dir, now=now)
    recorded = validate_evidence(evidence)
    artifacts = recorded["artifacts"]
    if not isinstance(artifacts, list):
        raise ReleaseCandidateError("checksums for non-image artifacts are required")
    validate_checksum_manifest(dist_dir, artifacts)
    names: list[str] = []
    for item in artifacts:
        if not isinstance(item, Mapping):
            raise ReleaseCandidateError("artifact checksum entries must be objects")
        names.append(str(item["name"]))
    for extra in GITHUB_RELEASE_EXTRA_ASSETS:
        if extra not in names:
            names.append(extra)
    for name in names:
        path = dist_dir / name
        if not path.is_file():
            raise ReleaseCandidateError(f"GitHub release asset is missing: {name}")
        if path.resolve().parent != dist_dir.resolve():
            raise ReleaseCandidateError(f"release artifact escaped dist dir: {name}")
    tag = f"v{PACKAGE_VERSION}"
    target = str(recorded["source_revision"])
    repository = resolve_github_repository()
    state = GitHubCandidateRef(tag=tag, commit=None, tag_present=False, release_present=False)
    if inspect_ref is not None:
        state = inspect_ref(tag, expected_revision=target, repository=repository)
    if state.reuse_existing:
        _validate_existing_release_assets(state, dist_dir=dist_dir, names=names)
    return GitHubReleasePlan(
        tag=tag,
        target=target,
        title=f"Hermes Helmet {PACKAGE_VERSION} (private)",
        notes=(
            "Private release candidate. Keep the repository, release, package, "
            "and image private. Do not mark this as latest or change visibility."
        ),
        assets=tuple(names),
        prerelease=True,
        latest=False,
        dry_run=dry_run,
        reuse_existing=state.reuse_existing,
        repository=repository,
    )


def github_release_command(plan: GitHubReleasePlan, *, dist_dir: Path) -> list[str]:
    if plan.latest:
        raise ReleaseCandidateError("GitHub release must not be marked latest")
    if not plan.prerelease:
        raise ReleaseCandidateError("GitHub release must remain a private prerelease")
    repository = resolve_github_repository(plan.repository)
    command = [
        "gh",
        "release",
        "create",
        plan.tag,
        "--repo",
        repository,
        "--verify-tag",
        "--title",
        plan.title,
        "--notes",
        plan.notes,
        "--prerelease",
        "--latest=false",
    ]
    for name in plan.assets:
        command.append(str(dist_dir / name))
    return command


def github_tag_ref_command(plan: GitHubReleasePlan) -> list[str]:
    repository = resolve_github_repository(plan.repository)
    return [
        "gh",
        "api",
        "--method",
        "POST",
        f"/repos/{repository}/git/refs",
        "-f",
        f"ref=refs/tags/{plan.tag}",
        "-f",
        f"sha={plan.target}",
    ]


def format_github_release_command(argv: Sequence[str]) -> str:
    return shlex.join(list(argv))


def _create_github_tag_ref(*, repository: str, tag: str, sha: str) -> int:
    status, _payload = _github_api(
        f"/repos/{repository}/git/refs",
        method="POST",
        fields={"ref": f"refs/tags/{tag}", "sha": sha},
    )
    if status not in {200, 201, 422}:
        raise ReleaseCandidateError("cannot create candidate GitHub tag")
    return status


def ensure_github_candidate_tag(
    plan: GitHubReleasePlan,
    *,
    create_ref: Callable[..., int] | None = None,
    inspect_ref: Callable[..., GitHubCandidateRef] | None = None,
) -> GitHubCandidateRef:
    repository = resolve_github_repository(plan.repository)
    creator = create_ref or _create_github_tag_ref
    status = creator(repository=repository, tag=plan.tag, sha=plan.target)
    if status not in {200, 201, 422}:
        raise ReleaseCandidateError("cannot create candidate GitHub tag")
    inspector = inspect_ref or inspect_github_candidate_ref
    state = inspector(plan.tag, expected_revision=plan.target, repository=repository)
    if not state.tag_present or state.commit != plan.target.lower():
        raise ReleaseCandidateError(
            "existing candidate tag does not match the approved source revision"
        )
    return state


def execute_github_release(
    plan: GitHubReleasePlan,
    *,
    dist_dir: Path,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    create_ref: Callable[..., int] | None = None,
    inspect_ref: Callable[..., GitHubCandidateRef] | None = None,
) -> int:
    if not plan.reuse_existing:
        print(format_github_release_command(github_tag_ref_command(plan)))
    argv = github_release_command(plan, dist_dir=dist_dir)
    print(format_github_release_command(argv))
    if plan.reuse_existing:
        print(
            "github-release: existing private prerelease already matches "
            "the approved source revision and bound assets"
        )
        return 0
    if plan.dry_run:
        return 0
    ensure_github_candidate_tag(plan, create_ref=create_ref, inspect_ref=inspect_ref)
    runner = run or subprocess.run
    completed = runner(list(argv), check=False, shell=False)
    if completed.returncode != 0:
        raise ReleaseCandidateError("GitHub prerelease command failed")
    return 0


def rollback_command(digest: str = PREVIOUS_IMAGE_DIGEST) -> list[str]:
    pin = f"{IMAGE_REPOSITORY}@{_require_digest(digest, label='previous tested image digest')}"
    return ["docker", "pull", pin]


def write_evidence_file(path: Path, payload: Mapping[str, object]) -> Path:
    validated = validate_evidence(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(validated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _is_forbidden_workdir(resolved: Path) -> bool:
    forbidden = {path.resolve() for path in FORBIDDEN_WORKDIR_PATHS}
    try:
        forbidden.add(Path.home().resolve())
    except OSError:
        pass
    return resolved in forbidden


def prepare_dir(path: Path, *, root: Path | None = None) -> Path:
    """Create a new work directory. Refuse unsafe or pre-existing roots."""

    raw = Path(os.path.expanduser(str(path)))
    resolved = (raw if raw.is_absolute() else Path.cwd() / raw).resolve()
    if _is_forbidden_workdir(resolved):
        raise ReleaseCandidateError("workdir is an unsafe root")
    if root is not None:
        project = root.resolve()
        if resolved == project:
            raise ReleaseCandidateError("workdir is an unsafe root")
    if resolved.exists():
        raise ReleaseCandidateError("workdir already exists")
    parent = resolved.parent
    if not parent.is_dir():
        project = None if root is None else root.resolve()
        intended = None if project is None else (project / INTENDED_DIST).resolve()
        if intended is not None and resolved == intended:
            if _is_forbidden_workdir(parent):
                raise ReleaseCandidateError("workdir is an unsafe root")
            parent.mkdir(mode=0o755)
        else:
            raise ReleaseCandidateError("workdir parent must already exist")
    if _is_forbidden_workdir(parent) and parent in {Path("/").resolve(), Path("/etc").resolve()}:
        raise ReleaseCandidateError("workdir is an unsafe root")
    resolved.mkdir(mode=0o700)
    return resolved


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes Helmet private RC evidence")
    sub = parser.add_subparsers(dest="command", required=True)
    write = sub.add_parser("write", help="Write validated private RC evidence")
    write.add_argument("--source-revision", required=True)
    write.add_argument("--image-digest", required=True)
    write.add_argument("--dist", type=Path, required=True)
    write.add_argument("--out", type=Path, required=True)
    promote = sub.add_parser("promote-command", help="Print owner-gated retag command")
    promote.add_argument("--decision", type=Path, required=True)
    promote.add_argument("--image-digest", required=True)
    promote.add_argument("--evidence", type=Path, required=True)
    promote.add_argument("--dist", type=Path, required=True)
    promote.add_argument("--dry-run", action="store_true")
    github_release = sub.add_parser(
        "github-release-command", help="Print owner-gated private GitHub prerelease command"
    )
    github_release.add_argument("--decision", type=Path, required=True)
    github_release.add_argument("--evidence", type=Path, required=True)
    github_release.add_argument("--dist", type=Path, required=True)
    github_release.add_argument("--dry-run", action="store_true")
    prepare = sub.add_parser("prepare-dir", help="Create a new packaging or proof directory")
    prepare.add_argument("--path", type=Path, required=True)
    prepare.add_argument("--root", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.command == "prepare-dir":
        created = prepare_dir(args.path, root=args.root)
        print(created)
        return 0

    if args.command == "write":
        write_provenance_file(
            args.dist,
            source_revision=args.source_revision,
            image_digest=args.image_digest,
        )
        artifacts = collect_artifacts(args.dist, complete=True)
        write_checksums(args.dist, artifacts)
        payload = build_evidence(
            source_revision=args.source_revision,
            image_digest=args.image_digest,
            artifacts=artifacts,
        )
        verify_evidence_files(args.dist, payload)
        write_evidence_file(args.out, payload)
        print(args.out)
        return 0

    decision = json.loads(Path(args.decision).read_text(encoding="utf-8"))
    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    if args.command == "github-release-command":
        plan = github_release_plan(
            decision=decision,
            evidence=evidence,
            dist_dir=args.dist,
            dry_run=args.dry_run,
        )
        return execute_github_release(plan, dist_dir=args.dist)
    plan = promotion_plan(
        decision=decision,
        image_digest=args.image_digest,
        evidence=evidence,
        dist_dir=args.dist,
    )
    command = promotion_command(plan)
    print(" ".join(command))
    if args.dry_run:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
