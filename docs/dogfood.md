# Private v0.1.0rc1 runtime dogfood

This document is the runtime dogfood contract for the owner-authorized private
release candidate `0.1.0rc1`. This path does not mint numbered `0.1.0`.
Repository, release, package, and image visibility stay private.

The candidate under test is the immutable image digest
`sha256:d37c470ed80a86bc7fd4b32430fff432fca05001ee6e0eb221347b267df9aa44`
from source `b2fed3dae7a9e5411511b1604e06a8fecbed35e8`. Mutable tags are not
the identity. Public fixtures in [config/dogfood-bound.json](../config/dogfood-bound.json)
and [config/dogfood-receipts](../config/dogfood-receipts) stay generic
(`example-org`, `example-captain`, `example-wrapper`). Real adopter receipts
and the owner-authorized digest pin live in the private overlay. The validator is generic and reads an owner-selected hash-bound
manifest plus that trusted receipt set. Captain login and repository
allowlists are derived from a version-2 authority policy, so a real
private overlay can pin a different digest without matching the
compiled ExampleCo fixture hash. Epic and issue URLs may belong to
different policy-allowed repositories. See [release-candidate.md](release-candidate.md) for packaging and
owner-gated promotion.

## Codex runtime

Runtime dogfood of the skill loop is Codex. Static validation still covers
Codex, Claude, and Hermes skill install targets. Claude Code and Hermes-host
runtime acceptance are **not** required for this candidate.

Codex must repeat, and the receipt must record argv plus an output digest for
each of:

- clean-machine `hermes-helmet setup`
- `hermes-helmet doctor`
- read-only `hermes-helmet status` (no ledger or checkpoint writes)
- persistence of ledgers, worktrees, and skills across restart
- skills and no-skills startup
- default integrations remaining skipped unless selected

Boolean claims are not evidence. Copied output digests are rejected. The
accepted command and observation set is the hash-bound trusted receipt
directory named by the bound manifest; replacing an output or boolean does not
create a newly acceptable digest.

## Issue-to-PR flow

`helmet-epic` resumes from a persisted checkpoint and invokes `helmet-issue`
only for dependency-ready children. Those invocations are recorded as exact
argv receipts (`hermes-helmet epic <epic-url>` and
`hermes-helmet issue <issue-url>`) through the released wrapper named in the
bound manifest. `helmet-issue` adopts an existing worker task or pull request
without an operator announcing that the PR exists. The wrapper image digest
must match the bound candidate.

The worker opens one tested pull request and records `metadata.published_pr`.
The worker never merges and never force-pushes. Genuine trusted-review findings
create dependent same-branch, same-PR repairs. Reviews bind authenticated
Captain review IDs to pull-request commits. Observed published heads come from
the live current head plus repair `head_before`/`head_after` transitions, not
from inferring a head for every commit in the pull. A multi-commit push may
expose only its final SHA as a head. Unreviewed intermediate commits are
recorded as `late_review_lapses` rather than treated as reviewed or required
heads. A changes-requested head is the `head_before` of the next repair, the
repair publishes a new `head_after`, and that observed head is re-reviewed
before readiness. Any consecutive reviews with different heads, including
`blocked` then `clean`, require the same ordered transition. The original
branch in that snapshot must remain the bound branch. The final clean review
must equal `current_head`, and `--head` plus the live GitHub pull, reviews,
and commits must match the trusted GitHub receipt. If this pull request has
repairs, those repairs stay same-PR and validated. If the current head is
genuinely clean, the review cites a real prior same-repository repair whose
prior PR, published head, and repair transition are independently fetched from
GitHub (or an equivalently independently verified receipt) instead of
manufacturing a defect or accepting a merely PR-shaped URL. That live prior
must include a bound-Captain changes-requested review submitted before the
repair-head timestamp. Boolean claims are
not evidence. Copied output digests are rejected. Exact permitted argv is
required for each Codex check.

Default merge behavior is explicit Captain approval. Silence is not approval.
A whole-line `Merge when clean: yes` directive is the only unattended grant.

## Evidence

Validate a secret-free receipt against the bound candidate. `--bound` is the
owner-selected pin; `--policy` supplies version-2 Captain and repository
authority. `--head` is required and must equal the named PR's live
head. Command, observation, epic, dispatch, and GitHub claims are read from
`--receipts`. The CLI always fetches live GitHub state and requires it to match
the trusted `github.json` snapshot, including independently fetched prior-repair
PRs named by that snapshot; there is no unauthenticated local PR/head bypass.

```sh
hermes-helmet dogfood-evidence config/dogfood-evidence.example.json \
  --bound config/dogfood-bound.json \
  --policy config/policy.example.json \
  --receipts config/dogfood-receipts \
  --head "$(git rev-parse HEAD)" \
  --json
```

Unknown fields are rejected. Secret-shaped keys and secret-bearing or
non-evidence command arguments are rejected without echoing values. The command
emits a newly constructed allowlisted object whose Codex hashes are derived
from the trusted receipt set.

A complete receipt records:

- the bound immutable image digest and exact source revision
- Kanban task identity, `metadata.published_pr`, `current_head`, and original
  branch bound to live GitHub
- persisted `helmet-epic` checkpoint resume, dependency-ready `helmet-issue`
  dispatch, and released-wrapper identity from the trusted observations
- distinct Codex and flow command receipts (exact argv, exit 0, timestamp,
  hashed output)
- ordered review and repair decisions with Captain identity and GitHub review
  IDs (same PR when this PR has a repair, no force-push, current-head review).
  Consecutive reviews with different heads require a transition record bound to
  a GitHub pull commit. Complete published-head coverage uses observed head
  transitions, not the full commit list. Unreviewed intermediate commits are
  recorded as late-review lapses. A clean review may cite verifiable prior
  repair provenance from elsewhere in the completed train.
- rollback to the previous tested digest and restore of this candidate without
  rebuild, retag of `0.1.0`, or visibility change

The example files are schema fixtures. Live private overlay runs substitute
observed receipts for the owner-authorized digest, the actual task id,
pull-request URL, current head, and authenticated GitHub binding.

## Rollback

Previous tested rollback target remains the last accepted default-branch image
documented in [release-candidate.md](release-candidate.md):

`sha256:316b6849cf6d663dc7fe40d7b3a543ae1f0d16d20b9dd58e0f0b2564c8424691`

Restore the dogfood digest without rebuild. Confirm doctor and read-only
status after both rollback and restore.
