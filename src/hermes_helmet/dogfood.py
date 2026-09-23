#!/usr/bin/env python3
"""Runtime dogfood evidence against a bound release candidate.

The validator is generic: public fixtures use ExampleCo identities. One-off
private pins and real adopter receipts live in the private overlay, not in this
module. Codex is the required runtime host; Claude Code and Hermes-host runtime
acceptance are not required. Numbered ``0.1.0`` is not minted here.

Command and operational claims are accepted only from an immutable hash-bound
receipt set. GitHub PR, head, branch, Captain reviews, review IDs, and commits
are derived from that trusted GitHub snapshot and, on the CLI, from a live API
read. Cited earlier-train repairs are bound to independently fetched pull,
review, and commit evidence for that prior PR, not a PR-shaped URL. The
live prior must include a bound-Captain ``CHANGES_REQUESTED`` review whose
GitHub submission time precedes the repair-head timestamp. Epic resume
and released-wrapper dispatch are observed receipts, not caller-authored URLs.
Captain login and repository allowlists come from a version-2 authority policy;
the bound manifest is the owner-selected pin.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from hermes_helmet import release_candidate as rc
from hermes_helmet.authority import Policy


CODEX_CHECKS = (
    "setup",
    "doctor",
    "status_readonly",
    "persistence",
    "skills",
    "no_skills",
    "default_integrations",
)
FLOW_CHECKS = ("epic_resume", "issue_dispatch")
CODEX_CHECK_COMMANDS: dict[str, tuple[str, ...]] = {
    "setup": ("hermes-helmet", "setup"),
    "doctor": ("hermes-helmet", "doctor"),
    "status_readonly": ("hermes-helmet", "status"),
    "persistence": ("hermes-helmet", "status"),
    "skills": ("hermes-helmet", "install-skills"),
    "no_skills": ("hermes-helmet", "doctor"),
    "default_integrations": ("hermes-helmet", "doctor"),
}
DISPATCH_SKILL = "helmet-issue"
EPIC_SKILL = "helmet-epic"
CANONICAL_BOUND_SHA256 = (
    "689d6cd7bf1645b690eb35d1b78d097996f8c825df22a6162f87deceb0c6c164"
)
MANUFACTURED_RE = re.compile(r"manufactured", re.IGNORECASE)
HEAD_RE = rc.SOURCE_REVISION_RE
TASK_ID_RE = re.compile(r"^t_[0-9a-f]{8,}$")
GITHUB_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
WRAPPER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
REVIEW_STATE_MAP = {
    "CHANGES_REQUESTED": "changes_requested",
    "APPROVED": "clean",
}
_SECRET_VALUE_RE = re.compile(
    r"(?:github_pat_[A-Za-z0-9_]{20,}|gh[opusr]_[A-Za-z0-9]{20,}|"
    r"sk-[A-Za-z0-9_-]{16,}|xai-[A-Za-z0-9_-]{16,})"
)
_SECRET_KEY_PARTS = frozenset(
    {
        "token",
        "tokens",
        "pat",
        "password",
        "passwd",
        "secret",
        "secrets",
        "apikey",
        "credential",
        "credentials",
        "authorization",
        "authkey",
        "privatekey",
        "accesskey",
        "secretkey",
    }
)
_SECRET_COMPACT_KEYS = frozenset(
    {
        "token",
        "pat",
        "password",
        "secret",
        "apikey",
        "authorization",
        "githubtoken",
        "ghtoken",
    }
)
RECEIPT_SPEC = {
    "command": None,
    "exit_code": None,
    "observed_at": None,
    "output": None,
}
GITHUB_SPEC = {
    "pull": {
        "html_url": None,
        "number": None,
        "head": {"sha": None, "ref": None},
    },
    "reviews": {
        "id": None,
        "user": {"login": None},
        "commit_id": None,
        "state": None,
        "submitted_at": None,
        "html_url": None,
    },
    "commits": {
        "sha": None,
        "committed_at": None,
    },
    "prior_repairs": {
        "html_url": None,
        "head": None,
        "head_before": None,
        "repaired_at": None,
    },
}
BOUND_SPEC = {
    "version": None,
    "channel": None,
    "minted_version": None,
    "source_revision": None,
    "image_repository": None,
    "image_digest": None,
    "previous_image_digest": None,
    "github_repository": None,
    "dogfood_host": None,
    "captain_github_login": None,
    "original_branch": None,
    "epic_url": None,
    "issue_url": None,
    "wrapper": None,
    "trusted_receipts_sha256": None,
}
OBSERVATION_SPEC = {
    "worker": {"merged": None, "force_pushed": None},
    "adopted_existing": None,
    "operator_announcement": None,
    "rollback": {
        "previous_image_digest": None,
        "restored_image_digest": None,
        "rebuilt": None,
        "visibility_changed": None,
        "minted_0_1_0": None,
        "preserved_state": None,
    },
    "visibility": {
        "repository": None,
        "release": None,
        "package": None,
        "image": None,
        "publication_change": None,
    },
    "checkpoint": {
        "persisted": None,
        "resumed": None,
        "skill": None,
        "epic_url": None,
        "dependency_ready": None,
    },
    "dispatch": {"skill": None, "issue_url": None, "wrapper": None},
    "wrapper": {"name": None, "image_digest": None},
}
EVIDENCE_SPEC = {
    "version": None,
    "channel": None,
    "minted_version": None,
    "source_revision": None,
    "image": {
        "repository": None,
        "digest": None,
        "version": None,
        "tags": None,
    },
    "task_id": None,
    "published_pr": None,
    "current_head": None,
    "merge": {"policy": None, "when_clean": None, "directive": None},
    "reviews": {
        "head": None,
        "outcome": None,
        "prior_repair": None,
        "reviewed_at": None,
        "review_id": None,
        "reviewer": None,
    },
    "repairs": {
        "published_pr": None,
        "head_before": None,
        "head_after": None,
        "force_push": None,
        "repaired_at": None,
    },
    "runtime": {
        "dogfood_host": None,
        "claude_required": None,
        "hermes_host_required": None,
    },
}


class DogfoodError(RuntimeError):
    """Runtime dogfood evidence is not acceptable."""


@dataclass(frozen=True)
class BoundCandidate:
    """Immutable identity a runtime receipt must match."""

    version: str
    channel: str
    minted_version: str
    source_revision: str
    image_repository: str
    image_digest: str
    previous_image_digest: str
    github_repository: str
    dogfood_host: str
    captain_github_login: str
    original_branch: str
    epic_url: str
    issue_url: str
    wrapper: str
    trusted_receipts_sha256: str


def _key_is_secret_shaped(key: str) -> bool:
    lowered = str(key).casefold()
    parts = [part for part in re.split(r"[-_]+", lowered) if part]
    compact = "".join(parts)
    if lowered in _SECRET_COMPACT_KEYS or compact in _SECRET_COMPACT_KEYS:
        return True
    if any(part in _SECRET_KEY_PARTS for part in parts):
        return True
    if "api" in parts and "key" in parts:
        return True
    if compact.endswith(("token", "secret", "password", "apikey", "credential")):
        return True
    return False


def _flag_is_secret_shaped(part: str) -> bool:
    if not part.startswith("-"):
        return False
    return _key_is_secret_shaped(part.lstrip("-"))


def _reject_secret_leaves(obj: object) -> None:
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            if _key_is_secret_shaped(str(key)):
                raise DogfoodError("secrets must not appear in dogfood evidence")
            _reject_secret_leaves(value)
        return
    if isinstance(obj, list):
        for item in obj:
            _reject_secret_leaves(item)
        return
    if isinstance(obj, str):
        if _SECRET_VALUE_RE.search(obj) or _flag_is_secret_shaped(obj):
            raise DogfoodError("secrets must not appear in dogfood evidence")


def _reject_keys(raw: Mapping[str, object], allowed: frozenset[str]) -> None:
    for key in raw.keys():
        key_text = str(key)
        if _key_is_secret_shaped(key_text):
            raise DogfoodError("secrets must not appear in dogfood evidence")
        if key_text not in allowed:
            raise DogfoodError("dogfood evidence contains an unrecognized field")


def _walk_schema(obj: object, spec: object) -> None:
    if spec is None:
        if isinstance(obj, Mapping):
            _reject_keys(obj, frozenset())
        elif isinstance(obj, list):
            for item in obj:
                _walk_schema(item, None)
        return
    if isinstance(obj, list):
        for item in obj:
            _walk_schema(item, spec)
        return
    if not isinstance(obj, Mapping):
        return
    assert isinstance(spec, dict)
    _reject_keys(obj, frozenset(spec))
    for key, value in obj.items():
        _walk_schema(value, spec[str(key)])


def _require_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DogfoodError(f"{label} is required")
    return value


def _require_bool(value: object, *, label: str, expected: bool | None = None) -> bool:
    if not isinstance(value, bool):
        raise DogfoodError(f"{label} is required")
    if expected is not None and value is not expected:
        raise DogfoodError(f"{label} is required")
    return value


def _require_digest(value: object, *, label: str) -> str:
    text = str(value or "")
    if not rc.DIGEST_RE.match(text):
        raise DogfoodError(f"{label} must be one immutable sha256 digest")
    return text


def _require_head(value: object, *, label: str) -> str:
    text = str(value or "")
    if not HEAD_RE.match(text):
        raise DogfoodError(f"{label} must be an exact 40-character revision")
    return text


def _require_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DogfoodError(f"{label} is required")
    return value


def _require_timestamp(value: object, *, label: str) -> datetime:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DogfoodError(f"{label} must be a timezone-aware timestamp") from exc
    if parsed.tzinfo is None:
        raise DogfoodError(f"{label} must be a timezone-aware timestamp")
    return parsed


def _canonical_timestamp(value: object, *, label: str) -> str:
    return _require_timestamp(value, label=label).isoformat()


def _policy_slugs(policy: Policy) -> frozenset[str]:
    slugs = frozenset(repo.slug for repo in policy.repositories)
    if not slugs:
        raise DogfoodError("bound GitHub repository is required")
    return slugs


def _policy_allows_repository(policy: Policy, slug: str) -> bool:
    if slug not in _policy_slugs(policy):
        return False
    owners = policy.github_owners
    if owners:
        owner = slug.split("/", 1)[0]
        if owner not in owners:
            return False
    return True


def _issue_url_repository(url: str, slugs: frozenset[str]) -> str | None:
    for slug in slugs:
        if _issue_url_re(slug).match(url):
            return slug
    return None


def _pr_url_re(github_repository: str) -> re.Pattern[str]:
    return re.compile(
        rf"^https://github\.com/{re.escape(github_repository)}/pull/[1-9][0-9]*$"
    )


def _issue_url_re(github_repository: str) -> re.Pattern[str]:
    return re.compile(
        rf"^https://github\.com/{re.escape(github_repository)}/issues/[1-9][0-9]*$"
    )


def trusted_receipts_sha256(receipts_dir: Path) -> str:
    """Hash the trusted receipt set as sorted filename plus content digest."""

    if not receipts_dir.is_dir():
        raise DogfoodError("observed runtime receipts are required")
    files = sorted(path for path in receipts_dir.iterdir() if path.is_file())
    if not files:
        raise DogfoodError("observed runtime receipts are required")
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def load_bound_candidate(path: Path, *, policy: Policy) -> BoundCandidate:
    """Load an owner-selected secret-free bound candidate identity.

    Authority (Captain login and repository allowlist) is derived from a
    version-2 policy. The ExampleCo manifest remains a public fixture; it is
    not the only accepted pin.
    """

    if policy.version < 2:
        raise DogfoodError("bound candidate authority must be a version-2 policy")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DogfoodError("bound candidate manifest is required") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DogfoodError("bound candidate manifest is required") from exc
    if not isinstance(payload, Mapping):
        raise DogfoodError("bound candidate manifest is required")
    _walk_schema(payload, BOUND_SPEC)
    source = str(payload.get("source_revision") or "")
    github_repository = str(payload.get("github_repository") or "")
    if not HEAD_RE.match(source):
        raise DogfoodError("bound source revision must be an exact 40-character revision")
    if not GITHUB_REPOSITORY_RE.match(github_repository):
        raise DogfoodError("bound GitHub repository is required")
    if not _policy_allows_repository(policy, github_repository):
        raise DogfoodError("bound GitHub repository is required")
    host = str(payload.get("dogfood_host") or "")
    if host != "codex":
        raise DogfoodError("bound runtime host must be Codex")
    captain = str(policy.captain_github_login or "")
    if not LOGIN_RE.match(captain):
        raise DogfoodError("bound Captain login is required")
    bound_captain = str(payload.get("captain_github_login") or "")
    if bound_captain != captain:
        raise DogfoodError("bound Captain login must match the authority policy")
    branch = str(payload.get("original_branch") or "")
    if not BRANCH_RE.match(branch):
        raise DogfoodError("bound original branch is required")
    epic_url = str(payload.get("epic_url") or "")
    issue_url = str(payload.get("issue_url") or "")
    allowed = frozenset(
        slug for slug in _policy_slugs(policy) if _policy_allows_repository(policy, slug)
    )
    if _issue_url_repository(epic_url, allowed) is None:
        raise DogfoodError("bound epic URL is required")
    if not _issue_url_re(github_repository).match(issue_url):
        raise DogfoodError("bound issue URL is required")
    if epic_url == issue_url:
        raise DogfoodError("bound epic URL is required")
    wrapper = str(payload.get("wrapper") or "")
    if not WRAPPER_RE.match(wrapper):
        raise DogfoodError("bound wrapper identity is required")
    receipts_hash = str(payload.get("trusted_receipts_sha256") or "")
    if not rc.CHECKSUM_RE.match(receipts_hash):
        raise DogfoodError("bound trusted receipt set is required")
    version = str(payload.get("version") or "")
    channel = str(payload.get("channel") or "")
    minted_version = str(payload.get("minted_version") or "")
    if version != rc.PACKAGE_VERSION:
        raise DogfoodError("bound version must be the private RC package")
    if channel != rc.CHANNEL:
        raise DogfoodError("bound channel must be private-rc")
    if minted_version != rc.MINTED_VERSION:
        raise DogfoodError("bound minted version must remain 0.1.0")
    return BoundCandidate(
        version=version,
        channel=channel,
        minted_version=minted_version,
        source_revision=source,
        image_repository=str(payload.get("image_repository") or ""),
        image_digest=_require_digest(payload.get("image_digest"), label="bound image digest"),
        previous_image_digest=_require_digest(
            payload.get("previous_image_digest"), label="bound previous digest"
        ),
        github_repository=github_repository,
        dogfood_host=host,
        captain_github_login=captain,
        original_branch=branch,
        epic_url=epic_url,
        issue_url=issue_url,
        wrapper=wrapper,
        trusted_receipts_sha256=receipts_hash,
    )


def _canonical_github(payload: Mapping[str, object], bound: BoundCandidate) -> dict[str, object]:
    _walk_schema(payload, GITHUB_SPEC)
    _reject_secret_leaves(payload)
    pull_in = _require_mapping(payload.get("pull"), label="GitHub pull")
    html_url = str(pull_in.get("html_url") or "")
    if not _pr_url_re(bound.github_repository).match(html_url):
        raise DogfoodError("GitHub pull must name one bound-repository pull request")
    number = _require_int(pull_in.get("number"), label="GitHub pull number")
    if str(number) != html_url.rsplit("/", 1)[-1]:
        raise DogfoodError("GitHub pull number must match the pull URL")
    head_in = _require_mapping(pull_in.get("head"), label="GitHub pull head")
    sha = _require_head(head_in.get("sha"), label="GitHub pull head")
    ref = str(head_in.get("ref") or "")
    if ref != bound.original_branch:
        raise DogfoodError("repair must remain on the original bound branch")
    pull = {
        "html_url": html_url,
        "number": number,
        "head": {"sha": sha, "ref": bound.original_branch},
    }

    raw_reviews = payload.get("reviews")
    if not isinstance(raw_reviews, list) or not raw_reviews:
        raise DogfoodError("trusted Captain reviews are required")
    reviews: list[dict[str, object]] = []
    previous_time: datetime | None = None
    for item in raw_reviews:
        if not isinstance(item, Mapping):
            raise DogfoodError("trusted Captain reviews are required")
        user = _require_mapping(item.get("user"), label="GitHub review user")
        reviewer = str(user.get("login") or "")
        if reviewer != bound.captain_github_login:
            raise DogfoodError("reviews must be performed by the bound Captain")
        state = str(item.get("state") or "")
        if state not in REVIEW_STATE_MAP:
            raise DogfoodError("review outcome is not recognized")
        submitted_at = _canonical_timestamp(item.get("submitted_at"), label="GitHub review timestamp")
        reviewed_at = _require_timestamp(submitted_at, label="GitHub review timestamp")
        if previous_time is not None and reviewed_at <= previous_time:
            raise DogfoodError("review and repair events must be strictly ordered")
        previous_time = reviewed_at
        review_id = _require_int(item.get("id"), label="GitHub review id")
        html = str(item.get("html_url") or "")
        expected_html = f"{html_url}#pullrequestreview-{review_id}"
        if html != expected_html:
            raise DogfoodError("GitHub review URL must bind the review id")
        reviews.append(
            {
                "id": review_id,
                "user": {"login": bound.captain_github_login},
                "commit_id": _require_head(item.get("commit_id"), label="GitHub review commit"),
                "state": state,
                "submitted_at": submitted_at,
                "html_url": html,
            }
        )

    raw_commits = payload.get("commits")
    if not isinstance(raw_commits, list) or not raw_commits:
        raise DogfoodError("GitHub pull commits are required")
    commits: list[dict[str, object]] = []
    previous_commit_time: datetime | None = None
    seen_shas: set[str] = set()
    for item in raw_commits:
        if not isinstance(item, Mapping):
            raise DogfoodError("GitHub pull commits are required")
        sha = _require_head(item.get("sha"), label="GitHub commit")
        if sha in seen_shas:
            raise DogfoodError("GitHub pull commits are required")
        seen_shas.add(sha)
        committed_at = _canonical_timestamp(item.get("committed_at"), label="GitHub commit timestamp")
        committed_time = _require_timestamp(committed_at, label="GitHub commit timestamp")
        if previous_commit_time is not None and committed_time <= previous_commit_time:
            raise DogfoodError("review and repair events must be strictly ordered")
        previous_commit_time = committed_time
        commits.append({"sha": sha, "committed_at": committed_at})

    raw_priors = payload.get("prior_repairs", [])
    if raw_priors is None:
        raw_priors = []
    if not isinstance(raw_priors, list):
        raise DogfoodError("prior repairs must be a list")
    prior_repairs: list[dict[str, object]] = []
    seen_priors: set[str] = set()
    for item in raw_priors:
        if not isinstance(item, Mapping):
            raise DogfoodError("a clean review must cite a real prior repair")
        prior_url = str(item.get("html_url") or "")
        if (
            not _pr_url_re(bound.github_repository).match(prior_url)
            or prior_url == html_url
            or prior_url in seen_priors
        ):
            raise DogfoodError("a clean review must cite a real prior repair")
        seen_priors.add(prior_url)
        head = _require_head(item.get("head"), label="prior repair head")
        head_before = _require_head(item.get("head_before"), label="prior repair head_before")
        if head == head_before:
            raise DogfoodError("a clean review must cite a real prior repair")
        prior_repairs.append(
            {
                "html_url": prior_url,
                "head": head,
                "head_before": head_before,
                "repaired_at": _canonical_timestamp(
                    item.get("repaired_at"), label="prior repair timestamp"
                ),
            }
        )
    return {
        "pull": pull,
        "reviews": reviews,
        "commits": commits,
        "prior_repairs": prior_repairs,
    }


def load_github_evidence(path: Path, bound: BoundCandidate) -> dict[str, object]:
    """Load an independently bound GitHub API snapshot from the trusted set."""

    payload = _load_json_mapping(path, label="GitHub receipt")
    return _canonical_github(payload, bound)


def _gh_api(command: list[str]) -> object:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise DogfoodError("live GitHub pull-request head is required") from exc
    try:
        payload = json.loads(completed.stdout or "null")
    except json.JSONDecodeError:
        payload = None
    if completed.returncode != 0:
        raise DogfoodError("live GitHub pull-request head is required")
    return payload


def _live_pull_bundle(repo: str, number: str, *, gh: str) -> tuple[object, object, object]:
    header = ["--header", "Accept: application/vnd.github+json"]
    pull = _gh_api([gh, "api", *header, f"repos/{repo}/pulls/{number}"])
    reviews = _gh_api([gh, "api", *header, f"repos/{repo}/pulls/{number}/reviews"])
    commits = _gh_api([gh, "api", *header, f"repos/{repo}/pulls/{number}/commits"])
    return pull, reviews, commits


def _live_review_rows(reviews: object) -> list[dict[str, object]]:
    if not isinstance(reviews, list):
        raise DogfoodError("live GitHub pull-request head is required")
    rows: list[dict[str, object]] = []
    for item in reviews:
        if isinstance(item, Mapping) and item.get("state") in REVIEW_STATE_MAP:
            rows.append(
                {
                    "id": item.get("id"),
                    "user": {
                        "login": _require_mapping(
                            item.get("user"), label="live GitHub review user"
                        ).get("login")
                    },
                    "commit_id": item.get("commit_id"),
                    "state": item.get("state"),
                    "submitted_at": item.get("submitted_at"),
                    "html_url": item.get("html_url"),
                }
            )
    return rows


def _live_commit_rows(commits: object) -> list[dict[str, object]]:
    if not isinstance(commits, list):
        raise DogfoodError("live GitHub pull-request head is required")
    rows: list[dict[str, object]] = []
    for item in commits:
        if not isinstance(item, Mapping):
            continue
        rows.append(
            {
                "sha": item.get("sha"),
                "committed_at": _require_mapping(
                    _require_mapping(item.get("commit"), label="live GitHub commit").get(
                        "committer"
                    ),
                    label="live GitHub committer",
                ).get("date"),
            }
        )
    return rows


def _fetch_live_prior_repair(
    html_url: str,
    published_pr: str,
    bound: BoundCandidate,
    *,
    gh: str,
) -> dict[str, object]:
    if html_url == published_pr or not _pr_url_re(bound.github_repository).match(html_url):
        raise DogfoodError("a clean review must cite a real prior repair")
    number = html_url.rsplit("/", 1)[-1]
    try:
        pull, reviews, commits = _live_pull_bundle(bound.github_repository, number, gh=gh)
    except DogfoodError as exc:
        raise DogfoodError("a clean review must cite a real prior repair") from exc
    if not isinstance(pull, Mapping):
        raise DogfoodError("a clean review must cite a real prior repair")
    if str(pull.get("html_url") or "") != html_url:
        raise DogfoodError("a clean review must cite a real prior repair")
    head = _require_head(
        _require_mapping(pull.get("head"), label="prior repair head").get("sha"),
        label="prior repair head",
    )
    commit_rows = _live_commit_rows(commits)
    commit_times = {
        str(item.get("sha")): _canonical_timestamp(
            item.get("committed_at"), label="prior repair timestamp"
        )
        for item in commit_rows
        if HEAD_RE.match(str(item.get("sha") or ""))
    }
    if head not in commit_times:
        raise DogfoodError("a clean review must cite a real prior repair")
    head_time = _require_timestamp(commit_times[head], label="prior repair timestamp")
    head_before = ""
    if not isinstance(reviews, list):
        raise DogfoodError("a clean review must cite a real prior repair")
    for item in reviews:
        if not isinstance(item, Mapping) or item.get("state") != "CHANGES_REQUESTED":
            continue
        user = item.get("user")
        if not isinstance(user, Mapping):
            continue
        if str(user.get("login") or "") != bound.captain_github_login:
            continue
        before = str(item.get("commit_id") or "")
        before_stamp = commit_times.get(before)
        if not HEAD_RE.match(before) or before == head or before_stamp is None:
            continue
        try:
            submitted_at = _require_timestamp(
                item.get("submitted_at"), label="GitHub review timestamp"
            )
        except DogfoodError:
            continue
        if (
            _require_timestamp(before_stamp, label="prior repair timestamp") < head_time
            and submitted_at < head_time
        ):
            head_before = before
            break
    if not head_before:
        raise DogfoodError("a clean review must cite a real prior repair")
    return {
        "html_url": html_url,
        "head": head,
        "head_before": head_before,
        "repaired_at": commit_times[head],
    }


def fetch_live_github(
    published_pr: str,
    bound: BoundCandidate,
    *,
    gh: str = "gh",
    prior_repairs: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Read live pull, review, commit, and cited prior-repair evidence."""

    if not _pr_url_re(bound.github_repository).match(published_pr):
        raise DogfoodError("published_pr must name one bound-repository pull request")
    number = published_pr.rsplit("/", 1)[-1]
    pull, reviews, commits = _live_pull_bundle(bound.github_repository, number, gh=gh)
    if not isinstance(pull, Mapping):
        raise DogfoodError("live GitHub pull-request head is required")
    live_priors: list[dict[str, object]] = []
    seen_priors: set[str] = set()
    for item in prior_repairs or ():
        if not isinstance(item, Mapping):
            raise DogfoodError("a clean review must cite a real prior repair")
        prior_url = str(item.get("html_url") or "")
        if prior_url in seen_priors:
            raise DogfoodError("a clean review must cite a real prior repair")
        seen_priors.add(prior_url)
        live_priors.append(_fetch_live_prior_repair(prior_url, published_pr, bound, gh=gh))
    snapshot = {
        "pull": {
            "html_url": pull.get("html_url"),
            "number": pull.get("number"),
            "head": {
                "sha": _require_mapping(pull.get("head"), label="live GitHub head").get("sha"),
                "ref": _require_mapping(pull.get("head"), label="live GitHub head").get("ref"),
            },
        },
        "reviews": _live_review_rows(reviews),
        "commits": _live_commit_rows(commits),
        "prior_repairs": live_priors,
    }
    return _canonical_github(snapshot, bound)


def github_evidence_matches(live: Mapping[str, object], trusted: Mapping[str, object]) -> None:
    for key in ("pull", "reviews", "commits", "prior_repairs"):
        if live.get(key) != trusted.get(key):
            raise DogfoodError("live GitHub state must match the trusted GitHub receipt")


def _validate_image(image: Mapping[str, object], bound: BoundCandidate) -> dict[str, object]:
    if image.get("repository") != bound.image_repository:
        raise DogfoodError("image repository is not the bound candidate image")
    digest = _require_digest(image.get("digest"), label="image digest")
    if digest != bound.image_digest:
        raise DogfoodError("dogfood must pin the bound candidate digest")
    if image.get("version") != bound.version:
        raise DogfoodError("image version must be the bound candidate package")
    raw_tags = image.get("tags")
    if not isinstance(raw_tags, (list, tuple)) or not raw_tags:
        raise DogfoodError("image tags are required")
    tags = [str(tag) for tag in raw_tags]
    if any(tag in {bound.minted_version, f"{bound.minted_version}-rc"} for tag in tags):
        raise DogfoodError("numbered 0.1.0 tags are not minted by this candidate")
    return {
        "repository": bound.image_repository,
        "digest": digest,
        "version": bound.version,
        "tags": tags,
    }


def _validate_merge(merge: Mapping[str, object]) -> dict[str, object]:
    when_clean = _require_bool(merge.get("when_clean"), label="merge.when_clean")
    policy = str(merge.get("policy") or "")
    directive = merge.get("directive")
    if when_clean:
        raise DogfoodError("dogfood evidence cannot grant unattended merge authority")
    if policy != "captain_approval":
        raise DogfoodError("default merge behavior requires explicit Captain approval")
    if directive not in (None, ""):
        raise DogfoodError("default merge behavior requires explicit Captain approval")
    return {"policy": policy, "when_clean": False, "directive": None}


def _commit_by_sha(commits: Sequence[Mapping[str, object]], sha: str) -> Mapping[str, object]:
    for commit in commits:
        if commit.get("sha") == sha:
            return commit
    raise DogfoodError("repairs must bind live GitHub commits")


def _validate_reviews_and_repairs(
    reviews: object,
    repairs: object,
    published_pr: str,
    current_head: str,
    bound: BoundCandidate,
    github: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    pr_re = _pr_url_re(bound.github_repository)
    github_reviews = github["reviews"]
    github_commits = github["commits"]
    assert isinstance(github_reviews, list)
    assert isinstance(github_commits, list)
    if not isinstance(reviews, list) or not reviews:
        raise DogfoodError("every dogfood head must be reviewed")
    if len(reviews) != len(github_reviews):
        raise DogfoodError("reviews must bind trusted GitHub review identifiers")
    commit_shas = {
        str(commit.get("sha"))
        for commit in github_commits
        if isinstance(commit, Mapping)
    }

    parsed_reviews: list[dict[str, object]] = []
    previous_time: datetime | None = None
    for item, github_review in zip(reviews, github_reviews):
        if not isinstance(item, Mapping) or not isinstance(github_review, Mapping):
            raise DogfoodError("review entries must be objects")
        review_id = _require_int(item.get("review_id"), label="review id")
        if review_id != github_review.get("id"):
            raise DogfoodError("reviews must bind trusted GitHub review identifiers")
        reviewer = str(item.get("reviewer") or "")
        if reviewer != bound.captain_github_login or reviewer != github_review["user"]["login"]:
            raise DogfoodError("reviews must be performed by the bound Captain")
        head = _require_head(item.get("head"), label="review head")
        if head != github_review.get("commit_id"):
            raise DogfoodError("review head must match the GitHub review commit")
        if head not in commit_shas:
            raise DogfoodError("review head must match a GitHub pull commit")
        outcome = str(item.get("outcome") or "")
        expected_outcome = REVIEW_STATE_MAP[str(github_review["state"])]
        if outcome != expected_outcome:
            raise DogfoodError("review outcome is not recognized")
        reviewed_at_text = _canonical_timestamp(item.get("reviewed_at"), label="review timestamp")
        reviewed_at = _require_timestamp(reviewed_at_text, label="review timestamp")
        github_time = _require_timestamp(
            github_review.get("submitted_at"), label="GitHub review timestamp"
        )
        if reviewed_at != github_time:
            raise DogfoodError("review timestamp must match the GitHub review")
        if previous_time is not None and reviewed_at <= previous_time:
            raise DogfoodError("review and repair events must be strictly ordered")
        previous_time = reviewed_at
        entry: dict[str, object] = {
            "head": head,
            "outcome": outcome,
            "reviewed_at": reviewed_at_text,
            "review_id": review_id,
            "reviewer": bound.captain_github_login,
        }
        if outcome == "clean":
            prior = str(item.get("prior_repair") or "").strip()
            if not prior or MANUFACTURED_RE.search(prior) or not pr_re.match(prior):
                raise DogfoodError("a clean review must cite a real prior repair")
            entry["prior_repair"] = prior
        parsed_reviews.append(entry)

    reviewed_heads = {str(review["head"]) for review in parsed_reviews}

    if str(parsed_reviews[-1]["outcome"]) != "clean":
        raise DogfoodError("readiness requires a current-head Captain review")
    if str(parsed_reviews[-1]["head"]) != current_head:
        raise DogfoodError("final clean review must equal the current pull-request head")

    if repairs is None:
        repairs = []
    if not isinstance(repairs, list):
        raise DogfoodError("repair entries must be a list")
    unused_repairs: list[dict[str, object]] = []
    for item in repairs:
        if not isinstance(item, Mapping):
            raise DogfoodError("repair entries must be objects")
        if item.get("published_pr") != published_pr:
            raise DogfoodError("repairs must update the same pull request")
        if item.get("force_push") is not False:
            raise DogfoodError("repairs must not force-push")
        before = _require_head(item.get("head_before"), label="repair head_before")
        after = _require_head(item.get("head_after"), label="repair head_after")
        if before == after:
            raise DogfoodError("repairs must publish a new head")
        repaired_at_text = _canonical_timestamp(item.get("repaired_at"), label="repair timestamp")
        repaired_at = _require_timestamp(repaired_at_text, label="repair timestamp")
        commit = _commit_by_sha(github_commits, after)
        commit_time = _require_timestamp(
            commit.get("committed_at"), label="GitHub commit timestamp"
        )
        if repaired_at != commit_time:
            raise DogfoodError("repairs must bind live GitHub commits")
        unused_repairs.append(
            {
                "published_pr": published_pr,
                "head_before": before,
                "head_after": after,
                "force_push": False,
                "repaired_at": repaired_at_text,
                "_at": repaired_at,
            }
        )

    parsed_repairs: list[dict[str, object]] = []
    for index, review in enumerate(parsed_reviews[:-1]):
        next_review = parsed_reviews[index + 1]
        needs_transition = (
            str(review["outcome"]) == "changes_requested"
            or str(review["head"]) != str(next_review["head"])
        )
        if not needs_transition:
            continue
        expected_after = str(next_review["head"])
        match_index = next(
            (
                pos
                for pos, repair in enumerate(unused_repairs)
                if repair["head_before"] == review["head"]
                and repair["head_after"] == expected_after
            ),
            None,
        )
        if match_index is None:
            raise DogfoodError("review head changes require a same-PR transition")
        repair = unused_repairs.pop(match_index)
        repair_time = repair.pop("_at")
        assert isinstance(repair_time, datetime)
        before_time = _require_timestamp(review["reviewed_at"], label="review timestamp")
        after_time = _require_timestamp(
            next_review["reviewed_at"], label="review timestamp"
        )
        if not (before_time < repair_time < after_time):
            raise DogfoodError("review and repair events must be strictly ordered")
        parsed_repairs.append(repair)

    if unused_repairs:
        raise DogfoodError("repairs must bind to a changes-requested head")
    observed_heads = {current_head}
    for repair in parsed_repairs:
        observed_heads.add(str(repair["head_before"]))
        observed_heads.add(str(repair["head_after"]))
    if not observed_heads.issubset(reviewed_heads):
        raise DogfoodError("every observed published pull-request head must have a Captain review")
    raw_train = github.get("prior_repairs") or []
    if not isinstance(raw_train, list):
        raise DogfoodError("a clean review must cite a real prior repair")
    verified_train: set[str] = set()
    for item in raw_train:
        if not isinstance(item, Mapping):
            raise DogfoodError("a clean review must cite a real prior repair")
        url = str(item.get("html_url") or "")
        head = str(item.get("head") or "")
        before = str(item.get("head_before") or "")
        repaired_at = str(item.get("repaired_at") or "")
        if (
            not url
            or not HEAD_RE.match(head)
            or not HEAD_RE.match(before)
            or head == before
            or not repaired_at
        ):
            raise DogfoodError("a clean review must cite a real prior repair")
        _require_timestamp(repaired_at, label="prior repair timestamp")
        verified_train.add(url)
    repaired_heads = {str(repair["head_after"]) for repair in parsed_repairs}
    for review in parsed_reviews:
        if str(review["outcome"]) != "clean":
            continue
        prior = str(review.get("prior_repair") or "")
        head = str(review["head"])
        if parsed_repairs:
            if head not in repaired_heads:
                raise DogfoodError("a clean review must cite a validated same-PR repair")
            if prior != published_pr and prior not in verified_train:
                raise DogfoodError("a clean review must cite a real prior repair")
        elif prior == published_pr or prior not in verified_train:
            raise DogfoodError("a clean review must cite a real prior repair")
    lapses: list[dict[str, object]] = [
        {"sha": str(commit.get("sha")), "committed_at": str(commit.get("committed_at"))}
        for commit in github_commits
        if isinstance(commit, Mapping) and str(commit.get("sha")) not in reviewed_heads
    ]
    return parsed_reviews, parsed_repairs, lapses


def _validate_rollback(
    rollback: Mapping[str, object], image_digest: str, bound: BoundCandidate
) -> dict[str, object]:
    previous = _require_digest(
        rollback.get("previous_image_digest"), label="rollback previous digest"
    )
    restored = _require_digest(
        rollback.get("restored_image_digest"), label="rollback restored digest"
    )
    if previous != bound.previous_image_digest:
        raise DogfoodError("previous tested rollback digest is required")
    if restored != image_digest or restored != bound.image_digest:
        raise DogfoodError("rollback must restore the bound candidate digest")
    rebuilt = _require_bool(rollback.get("rebuilt"), label="rollback.rebuilt", expected=False)
    visibility_changed = _require_bool(
        rollback.get("visibility_changed"),
        label="rollback.visibility_changed",
        expected=False,
    )
    minted = _require_bool(
        rollback.get("minted_0_1_0"), label="rollback.minted_0_1_0", expected=False
    )
    preserved = _require_bool(
        rollback.get("preserved_state"), label="rollback.preserved_state", expected=True
    )
    return {
        "previous_image_digest": previous,
        "restored_image_digest": restored,
        "rebuilt": rebuilt,
        "visibility_changed": visibility_changed,
        "minted_0_1_0": minted,
        "preserved_state": preserved,
    }


def _validate_receipt(
    name: str,
    receipt: object,
    seen_hashes: set[str],
    expected: tuple[str, ...],
) -> dict[str, object]:
    if not isinstance(receipt, Mapping):
        raise DogfoodError(f"Codex dogfood check {name} is required")
    _walk_schema(receipt, RECEIPT_SPEC)
    _reject_secret_leaves(receipt)
    command = receipt.get("command")
    if not isinstance(command, list) or not command:
        raise DogfoodError("runtime receipts must record argv command output")
    if any(not isinstance(part, str) or not part or " " in part for part in command):
        raise DogfoodError("runtime receipts must record argv command output")
    if tuple(command) != expected:
        raise DogfoodError("runtime receipts must record argv command output")
    if receipt.get("exit_code") != 0:
        raise DogfoodError(f"Codex dogfood check {name} is required")
    _require_timestamp(receipt.get("observed_at"), label="runtime receipt timestamp")
    output = receipt.get("output")
    if not isinstance(output, str) or output == "":
        raise DogfoodError("runtime receipts must record argv command output")
    digest = hashlib.sha256(output.encode("utf-8")).hexdigest()
    if digest in seen_hashes:
        raise DogfoodError("runtime receipts must be distinct observed outputs")
    seen_hashes.add(digest)
    return {
        "command": list(expected),
        "exit_code": 0,
        "observed_at": receipt.get("observed_at"),
        "output_sha256": digest,
    }


def _load_json_mapping(path: Path, *, label: str) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DogfoodError(f"{label} is required") from exc
    if not isinstance(payload, Mapping):
        raise DogfoodError(f"{label} is required")
    return payload


def load_observed_receipts(
    receipts_dir: Path, bound: BoundCandidate
) -> tuple[dict[str, object], Mapping[str, object], dict[str, object]]:
    """Load the hash-bound Codex, flow, observation, and GitHub receipt set."""

    digest = trusted_receipts_sha256(receipts_dir)
    if digest != bound.trusted_receipts_sha256:
        raise DogfoodError("observed receipts are not the trusted receipt set")
    seen_hashes: set[str] = set()
    checks: dict[str, object] = {}
    for name in CODEX_CHECKS:
        checks[name] = _validate_receipt(
            name,
            _load_json_mapping(receipts_dir / f"{name}.json", label=f"Codex dogfood check {name}"),
            seen_hashes,
            CODEX_CHECK_COMMANDS[name],
        )
    flow_commands = {
        "epic_resume": ("hermes-helmet", "epic", bound.epic_url),
        "issue_dispatch": ("hermes-helmet", "issue", bound.issue_url),
    }
    for name in FLOW_CHECKS:
        checks[name] = _validate_receipt(
            name,
            _load_json_mapping(receipts_dir / f"{name}.json", label=f"Codex dogfood check {name}"),
            seen_hashes,
            flow_commands[name],
        )
    observations = _load_json_mapping(
        receipts_dir / "observations.json", label="runtime observations"
    )
    _walk_schema(observations, OBSERVATION_SPEC)
    _reject_secret_leaves(observations)
    github = load_github_evidence(receipts_dir / "github.json", bound)
    return checks, observations, github


def _validate_runtime(
    runtime: Mapping[str, object],
    bound: BoundCandidate,
    checks: Mapping[str, object],
) -> dict[str, object]:
    if runtime.get("dogfood_host") != bound.dogfood_host:
        raise DogfoodError("runtime dogfood host must be Codex")
    claude_required = _require_bool(
        runtime.get("claude_required"), label="claude_required", expected=False
    )
    hermes_required = _require_bool(
        runtime.get("hermes_host_required"),
        label="hermes_host_required",
        expected=False,
    )
    return {
        "dogfood_host": bound.dogfood_host,
        "claude_required": claude_required,
        "hermes_host_required": hermes_required,
        "checks": dict(checks),
    }


def _validate_observations(
    observations: Mapping[str, object],
    image_digest: str,
    bound: BoundCandidate,
) -> dict[str, object]:
    worker = _require_mapping(observations.get("worker"), label="worker")
    worker_out = {
        "merged": _require_bool(worker.get("merged"), label="worker.merged", expected=False),
        "force_pushed": _require_bool(
            worker.get("force_pushed"), label="worker.force_pushed", expected=False
        ),
    }
    adopted = _require_bool(
        observations.get("adopted_existing"), label="adopted_existing", expected=True
    )
    announcement = _require_bool(
        observations.get("operator_announcement"),
        label="operator_announcement",
        expected=False,
    )
    rollback = _validate_rollback(
        _require_mapping(observations.get("rollback"), label="rollback"),
        image_digest,
        bound,
    )
    visibility_in = _require_mapping(observations.get("visibility"), label="visibility")
    visibility = {}
    for key in ("repository", "release", "package", "image"):
        if visibility_in.get(key) != "private":
            raise DogfoodError("visibility must remain private")
        visibility[key] = "private"
    if visibility_in.get("publication_change") is not False:
        raise DogfoodError("publication or visibility change is forbidden")
    visibility["publication_change"] = False
    checkpoint_in = _require_mapping(observations.get("checkpoint"), label="checkpoint")
    if checkpoint_in.get("skill") != EPIC_SKILL:
        raise DogfoodError("helmet-epic checkpoint resume is required")
    if checkpoint_in.get("epic_url") != bound.epic_url:
        raise DogfoodError("helmet-epic checkpoint resume is required")
    checkpoint = {
        "persisted": _require_bool(
            checkpoint_in.get("persisted"), label="checkpoint.persisted", expected=True
        ),
        "resumed": _require_bool(
            checkpoint_in.get("resumed"), label="checkpoint.resumed", expected=True
        ),
        "skill": EPIC_SKILL,
        "epic_url": bound.epic_url,
        "dependency_ready": _require_bool(
            checkpoint_in.get("dependency_ready"),
            label="checkpoint.dependency_ready",
            expected=True,
        ),
    }
    dispatch_in = _require_mapping(observations.get("dispatch"), label="dispatch")
    if dispatch_in.get("skill") != DISPATCH_SKILL:
        raise DogfoodError("helmet-issue dispatch is required")
    if dispatch_in.get("issue_url") != bound.issue_url:
        raise DogfoodError("helmet-issue dispatch is required")
    if dispatch_in.get("wrapper") != bound.wrapper:
        raise DogfoodError("released wrapper dispatch is required")
    dispatch = {
        "skill": DISPATCH_SKILL,
        "issue_url": bound.issue_url,
        "wrapper": bound.wrapper,
    }
    wrapper_in = _require_mapping(observations.get("wrapper"), label="wrapper")
    if wrapper_in.get("name") != bound.wrapper:
        raise DogfoodError("wrapper must pin the bound candidate digest")
    wrapper_digest = _require_digest(
        wrapper_in.get("image_digest"), label="wrapper image digest"
    )
    if wrapper_digest != bound.image_digest:
        raise DogfoodError("wrapper must pin the bound candidate digest")
    return {
        "worker": worker_out,
        "adopted_existing": adopted,
        "operator_announcement": announcement,
        "rollback": rollback,
        "visibility": visibility,
        "checkpoint": checkpoint,
        "dispatch": dispatch,
        "wrapper": {"name": bound.wrapper, "image_digest": wrapper_digest},
    }


def validate_runtime_evidence(
    payload: Mapping[str, object],
    *,
    bound: BoundCandidate,
    receipts_dir: Path,
    expected_head: str,
    live_github: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Fail closed unless runtime dogfood evidence matches the trusted set."""

    _walk_schema(payload, EVIDENCE_SPEC)
    if payload.get("version") != bound.version:
        raise DogfoodError("evidence version must be the bound candidate package")
    if payload.get("channel") != bound.channel:
        raise DogfoodError("evidence channel must be private-rc")
    if payload.get("minted_version") != bound.minted_version:
        raise DogfoodError("minted version remains 0.1.0 until owner promotion")
    if payload.get("source_revision") != bound.source_revision:
        raise DogfoodError("dogfood must pin the bound candidate source")

    image = _validate_image(_require_mapping(payload.get("image"), label="image"), bound)
    task_id = str(payload.get("task_id") or "")
    if not TASK_ID_RE.match(task_id):
        raise DogfoodError("task identity is required")
    published_pr = str(payload.get("published_pr") or "")
    if not _pr_url_re(bound.github_repository).match(published_pr):
        raise DogfoodError("published_pr must name one bound-repository pull request")
    current_head = _require_head(payload.get("current_head"), label="current_head")
    expected = _require_head(expected_head, label="expected head")
    checks, observations, github = load_observed_receipts(receipts_dir, bound)
    pull = _require_mapping(github.get("pull"), label="GitHub pull")
    head = _require_mapping(pull.get("head"), label="GitHub pull head")
    live_head = str(head.get("sha") or "")
    if pull.get("html_url") != published_pr:
        raise DogfoodError("published_pr must match the live bound pull request")
    if current_head != expected or current_head != live_head:
        raise DogfoodError("final clean review must equal the current pull-request head")
    if live_github is not None:
        github_evidence_matches(_canonical_github(live_github, bound), github)

    merge = _validate_merge(_require_mapping(payload.get("merge"), label="merge"))
    reviews, repairs, lapses = _validate_reviews_and_repairs(
        payload.get("reviews"),
        payload.get("repairs"),
        published_pr,
        current_head,
        bound,
        github,
    )
    observed = _validate_observations(observations, str(image["digest"]), bound)
    runtime = _validate_runtime(
        _require_mapping(payload.get("runtime"), label="runtime"), bound, checks
    )

    return {
        "version": bound.version,
        "channel": bound.channel,
        "minted_version": bound.minted_version,
        "source_revision": bound.source_revision,
        "image": image,
        "task_id": task_id,
        "published_pr": published_pr,
        "current_head": current_head,
        "branch": bound.original_branch,
        "worker": observed["worker"],
        "adopted_existing": observed["adopted_existing"],
        "operator_announcement": observed["operator_announcement"],
        "merge": merge,
        "reviews": reviews,
        "repairs": repairs,
        "late_review_lapses": lapses,
        "rollback": observed["rollback"],
        "runtime": runtime,
        "visibility": observed["visibility"],
        "checkpoint": observed["checkpoint"],
        "dispatch": observed["dispatch"],
        "wrapper": observed["wrapper"],
    }


def build_runtime_evidence(
    *,
    bound: BoundCandidate,
    task_id: str,
    published_pr: str,
    current_head: str,
    reviews: Sequence[Mapping[str, object]],
    receipts_dir: Path,
    expected_head: str,
    repairs: Sequence[Mapping[str, object]] = (),
    merge: Mapping[str, object] | None = None,
    live_github: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build canonical evidence from the trusted receipt set and bound candidate."""

    payload = {
        "version": bound.version,
        "channel": bound.channel,
        "minted_version": bound.minted_version,
        "source_revision": bound.source_revision,
        "image": {
            "repository": bound.image_repository,
            "digest": bound.image_digest,
            "version": bound.version,
            "tags": [f"dev-{bound.source_revision}", bound.version],
        },
        "task_id": task_id,
        "published_pr": published_pr,
        "current_head": current_head,
        "merge": dict(
            merge
            or {
                "policy": "captain_approval",
                "when_clean": False,
                "directive": None,
            }
        ),
        "reviews": [dict(item) for item in reviews],
        "repairs": [dict(item) for item in repairs],
        "runtime": {
            "dogfood_host": bound.dogfood_host,
            "claude_required": False,
            "hermes_host_required": False,
        },
    }
    return validate_runtime_evidence(
        payload,
        bound=bound,
        receipts_dir=receipts_dir,
        expected_head=expected_head,
        live_github=live_github,
    )
