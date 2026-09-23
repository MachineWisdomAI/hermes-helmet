# Security policy

## Supported versions

Hermes Helmet is available as an Apache-2.0 public source preview. The packaged
CLI version is `0.1.0rc1`; the dated public GitHub release identifies this source
publication separately from earlier development candidates. See
[public preview notes](docs/public-preview.md).

The container promotion policy below applies to separately distributed images.
The scanned and published candidate image uses the same `0.1.0rc1` version
label. Security fixes land on the default branch and are included in the next
accepted image digest. A numbered `0.1.0` image is not minted until an explicit
owner decision against a coherent `0.1.0` image. Owner-gated promotion of this
candidate retags and GitHub-prereleases `0.1.0rc1` only.

## Reporting a vulnerability

Do not open a public issue with a working exploit, a PAT, a provider key, or
other secrets.

Use GitHub private vulnerability reporting on this repository when it is
available. Otherwise contact the repository maintainers through a private
GitHub channel.

Include:

- the affected commit or image digest
- a minimal reproduction that does not contain live credentials
- the impact (identity confusion, secret leakage, allowlist bypass, merge)

## Runtime secrets

Authority policy, crew contracts, logs, task prose, and examples must never
contain PATs or provider secrets. Startup validation names the invalid field
and redacts secret values. Worker tokens are injected only at runtime into
owner-only files and are unset from process environment before Hermes starts.

## Identity

The executor must be the configured `worker_github_login`. Captain, unknown,
and mismatched identities fail closed.

## Container image gate

Source scanning blocks HIGH or CRITICAL dependency findings and secret
findings. Container scanning compares each linux/amd64 and linux/arm64 release
candidate with the exact immutable Hermes base used to build it. Both reports
use the same Trivy version and frozen operating-system and Java vulnerability
database state.

Private development-image publication fails when scan evidence is missing or
inconsistent, the platform or base-image identity drifts, the derived layers do
not extend the scanned base, any secret is detected, or the wrapper adds or
worsens a HIGH or CRITICAL vulnerability identity. CI retains the raw reports
only inside the ephemeral runner, then publishes a sanitized machine-readable
delta plus the Trivy database identity for review. Secret matches and
surrounding source material are never uploaded as workflow artifacts.

An inherited upstream finding is reported rather than silently treated as a
Hermes Helmet regression. It is not automatically accepted. The private
`dev-<source-commit>` image is a CI verification artifact, not a numbered
release or approval to make the image public. CI verifies that its GHCR package
is private before publishing it. Promoting a numbered release or making an
image publicly accessible with inherited HIGH or CRITICAL findings requires an
explicit owner decision bound to the exact candidate evidence, exact base digest,
both platform scan reports and their checksums, the inherited HIGH/CRITICAL
counts that match those reports, the previous tested image digest, and
expiry/review conditions. Promotion validates the full sanitized report
contract and fails closed on platform or base-reference drift, report failures,
added HIGH/CRITICAL identities, secrets, or mismatched Trivy versions. The owner
decision is bound to the
SHA-256 of the exact canonical `release-evidence.json`. Expired, overlong, or
mismatched decisions fail closed. The mechanical expiry window is at most 30
days; required review triggers are `next-stable-hermes`, `base-digest-change`,
and `emergency-security`. Promotion must reuse the already-scanned immutable
`dev-<source-commit>` manifest without rebuild, and that image must already
self-identify as `0.1.0rc1`. Owner-gated Git tag and private GitHub prerelease
creation is dry-run by default and is not invoked by CI. The printed command is
not executed through the shell; Python runs structured `gh` argv after
confirming the candidate tag is absent or already the approved source and that
`SHA256SUMS` matches bound evidence. Inspection and mutation share one bound
repository (`--repo`); a conflicting `GH_REPO` fails closed. Reuse requires the
complete asset set with digests matching local evidence. Mutation creates the
exact tag with fail-if-exists semantics, re-peels it, then runs
`gh release create --verify-tag`. That decision expires
at the earliest of 30 days, the
next stable Hermes release, a base-digest change, or an emergency security
trigger. See `docs/release-candidate.md`.
