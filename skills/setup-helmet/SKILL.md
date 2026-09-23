---
name: setup-helmet
description: "Guide an adopter through deterministic Hermes Helmet setup and read-only diagnostics while keeping secrets out of agent context."
version: 1.0.0
author: Hermes Helmet
license: Apache-2.0
---

# setup-helmet

Use this skill to prepare or resume a new Hermes Helmet installation. The same
secret-free answers file drives both this conversational workflow and
`hermes-helmet setup`; do not reproduce setup logic in agent prose.

1. Collect company/Captain names, the distinct GitHub worker login, exact
   repository work/action allowlist, ready/dispatch labels, and provider/model in a local
   JSON answers file. Never put a PAT, API key, password, or credential in it.
2. Ask the operator to create a separate fine-grained worker PAT with the
   required metadata, contents, pull-request, and issue permissions on the
   selected repositories. Extra public visibility is not a Helmet failure; extra
   private visibility requires an explicit `worker_access_scope: broader`
   adopter choice (`selected` is the default).
3. Run `hermes-helmet setup --answers /path/to/answers.json`. The command uses
   Python `getpass` for local non-echoing PAT entry, stores it in an owner-only
   non-symlink file below owner-only non-symlink directories, verifies the worker
   identity and repository boundary, creates missing
   labels without applying dispatch to an issue, probes the chosen model, writes
   policy/crew-contract state, and atomically installs bundled skills.
   The default probe supports OpenAI; another provider requires an explicit
   OpenAI-compatible `model_lanes.hermes_executor.base_url`. Reject unsupported
   provider configuration before creating labels or writing state.
4. Preflight every bundled-skill destination before prompting for credentials,
   probing providers, creating labels, or writing state. Report conflicts and
   stop with zero setup/GitHub mutation. Never overwrite a foreign skill.
5. OpenViking and FAVA Trails are selected by default but remain disabled until
   the operator explicitly confirms each. Confirmation enables the policy; then
   use the existing `hermes-helmet openviking setup` and `hermes-helmet fava setup`
   commands to collect each service's private configuration. Opting out leaves
   the core usable.
6. Present the pinned Matt Pocock skills and gstack entries as recommendations.
   Do not install them. Keep any company skill pack in a separate company-owned
   repository and include a `repo-bootstrap` skill for local initialization rules.
7. Run `hermes-helmet doctor --config ~/.hermes-helmet/policy.json --home ~ --live`
   before dispatch. Setup-state doctor is non-mutating, always verifies the live
   worker, configured work/action allowlist, labels, and provider/model, and fails closed. It also
   checks that the supplied home is current-user-owned, non-symlink, and not
   group/other writable. Every existing component of Helmet state/generated/
   secret paths and Codex/Claude/Hermes skill roots and destinations is checked
   the same way before reads or writes. It checks file modes and every Git worktree origin
   fetch/push URL, accepting only credential-free canonical GitHub HTTPS and
   supported GitHub SSH forms, plus bundled skills, optional integrations,
   and policy drift. The legacy doctor path without `--home` retains its offline
   default.

Re-running setup with identical answers reuses the existing owner-only PAT and
produces the same fingerprint. If an unexpected skill apply fails after
preflight, new skill destinations roll back and setup records an incomplete,
resumable stage. A destination conflict introduced after preflight also keeps
state incomplete. Resolve it, then rerun the same command to reuse credentials and existing
labels, finish installation, and mark setup complete. If answers change, show
the new policy and crew contract to the Captain before dispatch.
