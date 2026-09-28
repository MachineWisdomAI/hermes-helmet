# Security policy

## Supported versions

Security fixes land on the default branch and are included in the next tagged
release. The current public package is the September 23, 2026 preview; see the
[release notes](docs/public-preview.md).

## Reporting a vulnerability

Do not open a public issue containing a working exploit, personal access token,
provider key, or other secret.

Use GitHub private vulnerability reporting on this repository when available.
Otherwise contact the repository maintainers through a private GitHub channel.
Include the affected commit, a minimal reproduction without live credentials,
and the impact.

## Runtime credentials

The Captain and worker must use different GitHub identities. Captain
credentials stay on the Captain side. Worker tokens and provider credentials
are injected through owner-only runtime files and must not appear in authority
policy, crew contracts, task prose, examples, logs, or images.

The worker token determines what the worker can access on GitHub. Hermes
Helmet's repository scope determines where it dispatches work; it does not
reduce the token's permissions outside Hermes Helmet. Give the worker account
only the access required for its repositories and responsibilities.

## Review and merge authority

The worker never merges and never force-pushes. Merge defaults to explicit
Captain approval. `Merge when clean: yes` grants unattended merge only after
the reviewed head, required checks, and mergeability are verified again.

Separate identities make implementation and acceptance attributable. They do
not replace code review, required checks, or ordinary repository protections.

## Dependencies and images

Public CI scans source dependencies and secrets and publishes a source SBOM for
each run. The Dockerfile pins the upstream Hermes Agent image and verifies the
GitHub CLI download by architecture and checksum. Review dependency and base-
image updates through the normal pull-request workflow before deployment.

Preview runtime images are built for `linux/amd64` and `linux/arm64`, scanned
against the pinned base, and smoke-tested before publication. Manual publication
uses an explicit full commit SHA on protected `main` and tags
`ghcr.io/machinewisdomai/hermes-helmet/runtime:preview-<sha>`. These images are
not `latest` or `stable` releases. The first package is created private; the
owner decides public visibility from the exact candidate. Workflows do not
change package visibility. Anonymous digest pulls are checked only after that
decision.
