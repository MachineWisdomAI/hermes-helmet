# Changelog

All notable changes to Hermes Helmet are recorded here. The project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The public source preview
uses CLI package version `0.1.0rc1`; a stable release and public prebuilt container
image have not been published.

## Unreleased

### Added

- Short `helmet` CLI command, with `hermes-helmet` retained as a compatible alias.

### Changed

- Introduce Hermes Helmet as an open-source software factory for teams using
  coding agents, with a walkthrough of delegation, review, repair, and acceptance.
- Use the full product name in documentation and user-facing messages.
- Clarify the Captain role, setup requirements, and preview command compatibility.
- Complete the fresh-install quickstart so adopters clone the allowlisted
  worker checkout, set Git identity and the GitHub credential helper, and
  finish the named worker profile login before poller installation.

## Public source preview — September 23, 2026

- Publish the existing Hermes Helmet core under Apache-2.0 with separate worker
  identity, credentials, checkout, and Captain review/merge authority.
- Include setup/doctor, issue/epic orchestration, event waits, portable skills,
  and optional OpenViking/FAVA integrations.
- Provide source-build instructions and explicit preview limitations.
- Keep private deployment history and prebuilt container promotion separate.

### Added

- Runtime dogfood evidence for the owner-authorized private `0.1.0rc1` image
  digest. `hermes-helmet dogfood-evidence` validates an owner-selected bound
  candidate against an immutable trusted receipt set and a version-2 authority
  policy, live GitHub pull/reviews/commits, Captain review IDs and original-branch
  continuity, persisted `helmet-epic` resume plus released-wrapper
  `helmet-issue` dispatch, and rollback without minting numbered `0.1.0`.
  Published-head coverage uses observed head transitions rather than every
  pull commit. A clean review may cite a verifiable earlier same-repository
  repair from the completed train; that prior PR and its repair transition are
  independently fetched from GitHub and matched against the trusted snapshot.
  The live prior must bind a Captain changes-requested review whose submission
  time precedes the repair-head timestamp.
  Same-PR repairs remain required when this pull request actually has a repair.
  Unreviewed intermediate commits are
  recorded as late-review lapses instead of being treated as reviewed heads.
  Public fixtures stay generic ExampleCo identities; real adopter receipts
  belong in the private overlay. Epic and issue URLs may be different
  policy-allowed repositories. Unknown fields, secret-shaped values, and
  unauthenticated GitHub PR/head bypasses are rejected without echoing values.
  Setup and doctor keep the configured work/action repository allowlist and
  capability probes; extra public visibility is not a failure, and extra private
  visibility is allowed only when `worker_access_scope` is `broader`.
  Optional `worker_completion_contract` defaults to `github-pr` (H1 tasks bind
  the Hermes completion hook to the repository slug). `local-only` is the
  private-plan fallback when that hook cannot call the branch-rules API; workers
  still pass `metadata.published_pr`, and unattended merge still requires
  independently verified required checks or explicit Captain approval.

## 0.1.0rc1

This version began as a private release candidate. The September 23 source
preview subsequently published the source and CLI package at this version;
private container promotion remains separate. The numbered `0.1.0` image tag
is not minted until an owner decision names the exact base digest, both platform
scan reports, inherited HIGH/CRITICAL counts, and expiry/review conditions.

### Added

- Private candidate packaging for the CLI and bundled `setup-helmet`,
  `helmet-issue`, and `helmet-epic` skills.
- Release evidence for one immutable multi-arch `dev-<source-commit>` digest,
  exact source revision, checksums for every non-image artifact, changelog,
  licenses/notices, SBOM, and build provenance.
- Documented rollback to the last tested H11 image digest and an owner-gated
  promotion path that reuses the scanned manifest without rebuild. Promotion
  binds the owner decision to the canonical evidence digest, the pinned base
  digest, and sanitized reports that have zero failures, zero added
  HIGH/CRITICAL identities, and zero secrets. Scan and final images use version
  `0.1.0rc1`. Promotion retags that identity only (not numbered `0.1.0`),
  requires a shared Trivy version, requires canonical review triggers, and
  prints a dry-run private GitHub prerelease for `v0.1.0rc1`. The GitHub path
  validates `SHA256SUMS` against bound evidence, refuses a pre-existing tag
  that does not match the approved source, reuses an already-correct
  prerelease only when remote assets match bound digests, binds one GitHub
  repository into both API reads and `gh --repo` mutation, creates the
  candidate tag with fail-if-exists semantics then `gh release create
  --verify-tag`, and executes `gh` argv directly instead of `eval`. The mechanical
  approval window is at most 30 days.
- Managed `hermes-helmet wait` continuation for Captain hosts. One bounded,
  read-only worker-runtime process wakes on meaningful H1 ledger or Hermes
  Kanban transitions and returns a resumable secret-free cursor, avoiding
  scheduler dependence and model-driven status loops.
- Public Captain-and-Crew explanation, contribution guide, security policy,
  code of conduct, changelog, third-party notices, and GitHub issue/PR templates.
- Shared public-surface scan with an explicit project-provenance allowlist
  that stops at the pinned FAVA citation so query, fragment, or extra path
  suffixes cannot hide adopter identities.
- Doctor reports skipped company skill packs truthfully alongside optional
  OpenViking, FAVA Trails, Signal, and model lanes.
- Python 3.11+ startup guard and platform-dependent temporary paths for skill
  validation prefixes.
- Same-run base-versus-wrapper image scanning for linux/amd64 and linux/arm64.
  Publication blocks on wrapper-added or severity-worsened HIGH/CRITICAL
  findings, secrets, missing evidence, platform drift, or base-layer drift;
  sanitized deltas and the exact Trivy database identities remain available for
  review without uploading secret matches or surrounding source material.

### Changed

- The immutable Hermes Agent base advances from 0.21.1 to the current stable
  0.21.3 release before public image verification and publication.
- GitHub CLI installation now uses the current official 2.101.0 release archive
  with per-architecture SHA-256 verification instead of the older Debian
  package that downgraded base-image dependencies.
- Public documentation now treats private WisdomHelm services as an optional
  overlay, not a product requirement.
- Public-surface scanning no longer skips whole test modules or generic
  `assertNotIn(` lines. Negative fixtures construct forbidden values from split
  literals, and unreadable or invalid UTF-8 scanned text fails closed.

## 0.0.0.dev0

Incubation baseline extracted from the proven GitHub-to-Hermes control loop:

- H1 control-loop poller with trusted-review same-PR repair
- H2 adopter-owned authority policy and crew contract
- H3 `helmet-issue` and H4 `helmet-epic`
- H5 portable company skill import
- H6 optional OpenViking working context (AGPL-3.0 service boundary)
- H7 optional FAVA Trails governed brain
- H8 provider/model lanes
- H9 `setup-helmet` and deterministic setup/doctor
