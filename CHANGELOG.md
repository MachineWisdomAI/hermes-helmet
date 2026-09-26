# Changelog

All notable changes to Hermes Helmet are recorded here. The project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added

- Short `helmet` CLI command, with `hermes-helmet` retained as a compatible
  alias.
- Manual preview publication of verified `linux/amd64` and `linux/arm64`
  runtime images to `ghcr.io/machinewisdomai/hermes-helmet/runtime` using tag
  `preview-<full-sha>`.

### Changed

- Introduce Hermes Helmet as an open-source software factory for teams using
  coding agents, with a walkthrough of delegation, review, repair, and
  acceptance.
- Complete the fresh-install quickstart for the worker checkout, Git identity,
  GitHub credentials, and named worker profile.
- Focus the public repository on the reusable product. Machine Wisdom's private
  release-candidate promotion and runtime-dogfood evidence remain in its private
  operations repository.
- Replace the internal validation dossier in the public preview notes with a
  product-focused description of what ships, how it works, and how to install
  it.

### Removed

- Private release-candidate packaging, image-promotion gates, dogfood receipts,
  and their internal CLI and CI surfaces.

## Public preview — September 23, 2026

### Added

- Docker-hosted Hermes worker with a separate GitHub identity, credentials, and
  checkout.
- GitHub issue intake, worker-authored pull requests, trusted review, repair on
  the same pull request, and Captain-side merge authority.
- Persistent single-issue and epic orchestration with resumable status and
  managed waits.
- `setup-helmet`, `helmet-issue`, and `helmet-epic` Captain skills.
- One authority policy for identities, repositories, budgets, model selection,
  and merge rules.
- Optional company skills, OpenViking working context, FAVA Trails governed
  records, and separate model lanes.
- Source-build instructions, tests, Apache-2.0 license, and third-party notices.

### Changed

- Advance the pinned upstream Hermes Agent base to the accepted 0.21.3 release.
- Install the GitHub CLI from the official architecture-specific archive with
  SHA-256 verification.
- Keep company-specific deployment values in private overlays rather than the
  public product.

## 0.0.0.dev0

Incubation baseline extracted from the proven GitHub-to-Hermes control loop:

- issue and review poller with same-PR repair
- adopter-owned authority policy and crew contract
- single-issue and epic Captain orchestration
- portable company skill import
- optional OpenViking and FAVA Trails integrations
- provider and model configuration
- deterministic setup and doctor
