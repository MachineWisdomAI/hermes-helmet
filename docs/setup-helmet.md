# setup-helmet

Portable adoption path for Hermes Helmet. The conversational `setup-helmet`
skill and the deterministic `hermes-helmet setup` / `hermes-helmet doctor`
commands produce the same validated configuration.

This project is Hermes Helmet. Do not introduce cluster package-manager terminology.

## What setup collects

Through the agent (non-secret only):

1. Company display name and slug, plus the human Captain GitHub login.
2. A separate worker GitHub account (never the Captain).
3. Exact repository work/action allowlist and ready/dispatch labels. Optional
   `worker_access_scope` (`selected` default, or `broader`) records whether
   extra private token visibility is accepted.
4. Worker provider/model selection.
5. OpenViking and FAVA Trails: selected by default, confirmed before any
   external action, and safe to decline.
6. Optional pinned recommendations (Matt Pocock skills, gstack) — never silent
   installs, never runtime dependencies.
7. A separate company skill-pack plan that includes `repo-bootstrap`. Private
   skills are never copied into this repository.

Secrets:

- The worker PAT and selected provider credential are entered with local
  non-echoing input (`getpass`).
- They are stored only below `~/.hermes-helmet/secrets/` in owner-only files
  (mode 0600).
- The state and secrets directories must be owner-owned mode 0700. Every
  existing component of state, generated output, credential, and bundled-skill
  paths must be current-user-owned, non-symlink, and not group/other writable.
- The supplied home must be a current-user-owned directory, may not be a
  symlink, and may not be group- or other-writable.
- They are never passed through the model, argv, command history, rendered
  policy, logs, or the crew contract.

## Commands

```sh
PYTHONPATH=src python3 -m hermes_helmet.cli setup --answers answers.json --home "$HOME" --json
PYTHONPATH=src python3 -m hermes_helmet.cli doctor --config "$HOME/.hermes-helmet/policy.json" --home "$HOME" --live --json
PYTHONPATH=src python3 -m hermes_helmet.cli install-skills
```

Setup is safe to resume after interruption: an existing owner-only PAT file is
reused and the policy fingerprint stays stable. If skill installation fails
after preflight, its new destinations are rolled back, setup records an
`incomplete` skills stage, and the error directs the operator to rerun the same
command. A destination conflict introduced after preflight also leaves this
explicit incomplete state; remove the foreign destination before retrying. The
rerun reuses credentials and existing labels, then marks setup
`complete`; it does not recreate either side effect.

The default provider probe supports OpenAI. A different provider requires an
explicit OpenAI-compatible `model_lanes.hermes_executor.base_url`; unsupported
or incomplete provider configuration fails before labels or state are changed.

Doctor is read-only. When `--home` selects setup state it always checks the live
worker identity, configured work/action repository allowlist and capabilities,
labels, and provider/model and fails closed; `--live` makes that pre-dispatch
intent explicit. Extra public repository visibility is not a failure. Extra
private visibility requires an explicit `worker_access_scope: broader`
adopter choice; `selected` remains the default. It also verifies
that each checkout is a Git worktree whose every `origin` fetch and push URL matches the configured
repository slug. Remotes must use credential-free canonical GitHub HTTPS,
`git@github.com:owner/repo(.git)`, or `ssh://git@github.com/owner/repo(.git)`;
userinfo, query strings, fragments, custom ports, and other schemes fail.
Doctor also checks file modes, bundled skills, OpenViking, FAVA Trails, and
policy drift, without applying labels or rewriting files. The legacy doctor
path without `--home` retains its established offline default.

A clean setup and doctor pass when OpenViking, FAVA, Matt Pocock skills, gstack,
and a company skill pack are all declined.

## Bundled skills

`setup-helmet`, `helmet-issue`, and `helmet-epic` install atomically into the
standard Codex, Claude Code, and Hermes skill locations. Conflicting hosts are
reported before any setup or GitHub mutation; foreign skill files are left
untouched. Existing host roots, skill roots, destination directories, and
`SKILL.md` files are checked component by component before they are read or
changed; symlink redirects and group/other-writable components fail closed.
