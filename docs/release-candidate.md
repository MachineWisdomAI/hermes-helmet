# Container release-candidate tooling

For the public source release, see [public-preview.md](public-preview.md).
The following workflow describes private container candidate promotion. It is
not a prerequisite for using or contributing to the public source preview.
The public repository does not run the private image publication job.

This document is the private release-candidate contract. It does **not** mint a
numbered `v0.1.0` git tag, GitHub Release, or GHCR tag. Those stay a later
owner-gated step after a coherent `0.1.0` image exists.

The candidate is the multi-architecture `dev-<source-commit>` image labeled
`0.1.0rc1` plus the `0.1.0rc1` CLI package. Repository, release, package, and
image visibility stay private. No publication or visibility change is performed
here.

## What the candidate contains

- Hermes Helmet CLI (Python package `hermes-helmet`) version `0.1.0rc1`.
  Current source installs `helmet` and the compatible `hermes-helmet` command;
  the dated preview wheel provides only `hermes-helmet`.
- Bundled portable skills: `setup-helmet`, `helmet-issue`, `helmet-epic`
- linux/amd64 and linux/arm64 image (the already-scanned `dev-<source-commit>`
  manifest)
- One immutable image digest, the exact source revision, and SHA-256 checksums
  for every non-image artifact (wheel, sdist, changelog, license, notice, source
  SBOM, sanitized platform reports, and provenance)
- Changelog, Apache-2.0 `LICENSE`, `NOTICE`, source SBOM, and BuildKit
  provenance/SBOM attestations bound to the private image digest

## Package and prove

```sh
scripts/package-release-candidate.sh
scripts/prove-packaged-release.sh
```

Packaging creates the project-owned `dist` parent when it is missing, then
refuses a pre-existing or unsafe `HERMES_HELMET_DIST_DIR`. Proof work
directories are created by that invocation and only those directories are
removed.

The proof installs the wheel with `PYTHONPATH` empty and runs the installed
console entry points:

- `helmet setup`
- `helmet doctor`
- read-only `helmet status` (creates no ledger or checkpoint state)
- bundled `install-skills` for `setup-helmet`, `helmet-issue`, and `helmet-epic`
- the minimum-runtime quickstart without skills packs, FAVA Trails, OpenViking,
  or optional model services
- the optional company-skill path against the ExampleCo default integration
  profile

CI packages from the exact recorded `SOURCE_COMMIT` (the pull-request head SHA,
not GitHub's synthetic merge ref). Source scanning and SBOM generation use the
same checkout.

## Image identity

In a private deployment repository, image publication uses the
`dev-<source-commit>` image after the supply-chain gates. The public repository
does not publish that image. Scan builds, the final manifest, smoke acceptance, and
`dist/image-identity.json` use image version `0.1.0rc1`. Record
`dist/image-identity.json` and `dist/rc/release-evidence.json`. Both name one
digest, the exact source revision, and that image version. Do not rebuild for
promotion. Do not retag a `0.0.0-dev` or `0.1.0` identity.

## Rollback

Before changing a private deployment, record its currently running immutable
image digest and preserve its persistent volumes. Roll back by restoring that
digest through the deployment's existing Compose configuration, then check
service health and read-only status. The source preview does not provide a
public prebuilt image or a shared rollback target.

## Owner-gated numbered promotion

Minting GHCR tag `0.1.0rc1` and the matching private GitHub prerelease
`v0.1.0rc1` requires an explicit owner decision that names:

- the exact source revision, candidate image digest, candidate image version
  `0.1.0rc1`, and SHA-256 of the canonical `release-evidence.json`
- the exact Hermes base digest recorded in candidate evidence
- both platform scan reports, their SHA-256 checksums, and inherited
  HIGH/CRITICAL counts matching the sanitized reports, which must name the
  expected platform and pinned base, share one Trivy version and database
  identity, and contain zero failures, zero added HIGH/CRITICAL identities, and
  zero secrets
- the previous tested rollback digest
- expiry and canonical review conditions (timezone-aware ISO-8601; expiry is at
  most 30 days; expired or overlong decisions fail). Required triggers are
  `next-stable-hermes`, `base-digest-change`, and `emergency-security`. Prose
  notes may remain explanatory.

The decision must keep `publication` false and `visibility` private. Promotion
reuses the already-scanned immutable `dev-<source-commit>` manifest with
`docker buildx imagetools create` and **without rebuild**. It does not retag
that manifest as numbered `0.1.0`. The GitHub path is dry-run by default: it
validates the exact source, bound `SHA256SUMS`, and assets, then prints a
shell-quoted fail-if-exists tag create plus `gh release create --repo
--verify-tag` for a private prerelease that attaches the
wheel, sdist, checksums, changelog, license/notice, SBOM, provenance, scan
evidence, and release evidence. Mutation executes that argv list from Python
and never `eval`s the printed string. Before create, the candidate tag must be
absent or already resolve to the owner-approved source revision; an already
correct private prerelease is left in place only when remote assets match the
bound evidence digests. A conflicting `GH_REPO` fails closed. Actions artifacts expire; this
owner path is the durable attachment. CI never invokes promotion and this
repair does not execute it.

See `config/release-owner-decision.example.json`, `SECURITY.md`, and
`scripts/promote-release-candidate.sh`. CI never invokes promotion.
