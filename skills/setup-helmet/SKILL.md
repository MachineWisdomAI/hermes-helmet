---
name: setup-helmet
description: "Guide an adopter through deterministic Hermes Helmet setup and read-only diagnostics while keeping secrets out of agent context."
version: 1.0.0
author: Hermes Helmet
license: Apache-2.0
---

# setup-helmet

Use this skill to prepare or resume a Hermes Helmet installation on the
first-officer host. Collect non-secret choices first, then run `helmet
setup`. That command preflights, prompts for secrets locally, and mutates
state. Do not reproduce those checks in this skill.

1. Collect the company display name, human Captain GitHub login
   (`captain_github_login`), distinct worker login (`worker_github_login`),
   exact repository work/action allowlist, ready/dispatch labels, and
   provider/model in a local JSON answers file. Never put a PAT, API key,
   password, or credential in it.
2. Ask the operator to create a separate fine-grained worker PAT with the
   required metadata, contents, pull-request, and issue permissions on the
   selected repositories. Extra public visibility is not a Hermes Helmet
   failure; extra private visibility requires an explicit
   `worker_access_scope: broader` adopter choice (`selected` is the default).
3. Run `helmet setup --answers /path/to/answers.json`. The command first
   preflights every bundled-skill destination and stops with zero setup or
   GitHub mutation on conflict; it never overwrites a foreign skill. It then
   uses Python `getpass` for local non-echoing PAT entry, stores credentials
   in owner-only non-symlink files below owner-only directories, verifies the
   worker identity and repository boundary, creates missing labels without
   applying dispatch to an issue, probes the chosen model, writes
   `~/.hermes-helmet/policy.json` and the crew contract, and atomically
   installs bundled skills. The default probe supports OpenAI; another
   provider requires an explicit OpenAI-compatible
   `model_lanes.hermes_executor.base_url`. Unsupported provider configuration
   fails before labels or state are written.
4. Handle the result. Success records setup-state `complete`. If skill
   installation fails after preflight, new destinations roll back and setup
   records an incomplete, resumable skills stage. A destination conflict
   introduced after preflight also keeps state incomplete. Resolve it, then
   rerun the same answers to reuse the PAT and existing labels.
5. OpenViking and FAVA Trails are selected by default but remain disabled until
   the operator explicitly confirms each. Confirmation enables the policy; then
   use the existing `helmet openviking setup` and `helmet fava setup`
   commands to collect each service's private configuration. Opting out leaves
   the core usable.
6. Present the pinned Matt Pocock skills and gstack entries as recommendations.
   Do not install them. Keep any company skill pack in a separate company-owned
   repository and include a `repo-bootstrap` skill for local initialization rules.
7. After `~/.hermes-helmet/policy.json` exists, run
   `helmet doctor --config ~/.hermes-helmet/policy.json --home ~ --live`.
   Setup-state doctor is non-mutating. It always verifies the live worker,
   configured work/action allowlist, labels, and provider/model, and fails
   closed. It inspects this host's filesystem: supplied home, Hermes Helmet
   state/generated/secret paths, skill roots, file modes, and Git worktree
   origin URLs (credential-free canonical GitHub HTTPS or supported GitHub
   SSH). It does not see checkouts that exist only inside the worker
   container. The published Docker bootstrap, including those checkouts, is
   in `docs/quickstart.md`. The legacy doctor path without `--home` retains
   its offline default.

Re-running setup with identical answers reuses the existing owner-only PAT and
produces the same fingerprint. If answers change, show the new policy and crew
contract to the human Captain before dispatch.

Completion: setup-state is `complete`, doctor is clean for host paths that
exist, and the operator has a policy file they can copy or mount for Compose.
This skill does not start Docker.
