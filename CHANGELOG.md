# Changelog

All notable changes to Hermes Helmet are recorded here. The project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### 0.1.0rc5 candidate

- Default Captain issue and epic windows to 8,640 minutes (6 days) from the
  original durable start, including managed waits. Explicit operator limits
  stay in force; existing checkpoints are not rewritten.
- Default each worker attempt to 24 hours wall-clock and 2,000 turns, and pass
  `--max-runtime` on native Kanban create for root and same-PR repair tasks.
- Announce effective Captain windows, worker quotas, repair limit, merge mode,
  and continuation at issue/epic startup, and expose start/deadline provenance
  in text and JSON status. These are bounded elapsed windows, not uninterrupted
  CPU or unlimited model spend.
- Package and plugin candidate versions are `0.1.0rc5` / `0.1.0-rc.5`.

### 0.1.0rc3 candidate

- Record explicit, durable Captain consent for configured Hermes model/provider
  use and honor existing task-specific consent without repeated prompts. This
  is instruction-level consent, not a runtime egress guard or host override.
- Require source-first repairs, independent review, and supported reinstall
  instead of patching installed skill bundles or approval records.
- Give the follow-up package and plugin cache a distinct candidate version;
  the published rc2 artifacts remain unchanged.

## 0.1.0rc2 — 2026-09-29

### Fixed

- Ask interactive Captains for merge mode once per issue or epic, persist the
  answer with an authority fingerprint, and reuse it across managed waits and
  restarts instead of repeatedly blocking for merge approval.
- Default new and omitted policies to unattended merge after exact-head Captain
  approval, required checks, and live mergeability, while preserving an
  explicitly configured approval policy as an installation-wide ceiling.
- Propagate epic choices to children, expose choice state in status output, and
  use policy defaults without prompting in scheduled or non-interactive runs.

### Added

- Claude Code and Codex first-officer plugin marketplace at the repository
  root, packaging the existing `setup-helmet`, `helmet-issue`, and
  `helmet-epic` skills. See [first-officer-plugins.md](docs/first-officer-plugins.md).
- Optional first-officer env file and local launcher pattern for repeatable
  Claude Code and Codex sessions. See
  [first-officer-plugins.md](docs/first-officer-plugins.md#starting-a-first-officer-session).
- Short `helmet` CLI command, with `hermes-helmet` retained as a compatible
  alias.
- Agent discovery index (`llms.txt`) with task routing, a README documentation
  link, and package `[project.urls]` for the public repository, documentation,
  and issue tracker.
- Manual preview publication of verified `linux/amd64` and `linux/arm64`
  runtime images to `ghcr.io/machinewisdomai/hermes-helmet/runtime` using tag
  `preview-<full-sha>`.

### Changed

- Collect OpenViking/FAVA answers before `helmet setup` writes policy, require
  host checkout roots before doctor, keep host and Compose policies separate
  when worktree paths differ, make `deploy/.env` owner-only before the token,
  and replace the remaining milestone shorthand with version 1/2 names.
  See [quickstart.md](docs/quickstart.md) and
  [setup-helmet.md](docs/setup-helmet.md).
- Correct the fresh-install sequence, Captain/first-officer/worker wording,
  first-officer plugin guide, setup-skill order, and `llms.txt` routing.
  See [quickstart.md](docs/quickstart.md) and
  [first-officer-plugins.md](docs/first-officer-plugins.md).
- Document the published GHCR preview runtime and a company deployment overlay
  that mounts policy and state without forking core source. Image selection
  uses `deploy/.env` with `docker compose ... pull hermes`; development builds
  select `hermes-helmet:local` explicitly. Image replacement pauses the
  `github-issue-poller` cron job and leaves the worker running.
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
