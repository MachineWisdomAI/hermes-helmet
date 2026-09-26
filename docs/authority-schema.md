# Authority policy schema

Hermes Helmet uses **one** adopter-owned JSON policy document as the authority
configuration. H2 extends the H1 poller policy; it does not introduce a second
configuration system.

## Versions

| Version | Role |
| --- | --- |
| `1` | H1 control-loop policy. Still loadable by the poller for compatibility. |
| `2` | Full Hermes Helmet authority document. Required for crew-contract render, private overlays, and preflight that enforces Captain/worker separation. |

Load helpers:

- `hermes_helmet.authority.load_policy(path)` — version 1 or 2
- `hermes_helmet.authority.load_authority(path)` — version 2 only
- `hermes_helmet.github_issue_poller.load_policy(path)` — same document, raises `PollerError`

## Version 2 document shape

```json
{
  "version": 2,
  "company": {
    "display_name": "ExampleCo",
    "slug": "exampleco"
  },
  "captain_github_login": "example-captain",
  "worker_github_login": "example-agent",
  "schedule": "every 15m",
  "board": "default",
  "assignee": "builder",
  "inference_provider": "openai",
  "inference_model": "gpt-4.1",
  "worker_max_turns": 100,
  "ready_label": "ready-for-agent",
  "dispatch_label": "hermes-kanban-go",
  "cron_deliver": "local",
  "github_owners": ["example-org"],
  "trusted_review_bots": ["github-code-quality[bot]"],
  "trusted_human_associations": ["OWNER", "MEMBER", "COLLABORATOR"],
  "merge": {
    "default_mode": "explicit_captain_approval",
    "unattended_marker": "Merge when clean: yes",
    "narrow_marker": "Merge when clean: no"
  },
  "integrations": {
    "openviking": false,
    "fava_trails": false,
    "signal": false
  },
  "budgets": {
    "max_issue_runtime_minutes": 240,
    "max_repair_rounds": 10
  },
  "max_epic_parallelism": 2,
  "openviking_peers": [
    {"id": "codex"},
    {"id": "chatgpt"},
    {"id": "hermes"}
  ],
  "repositories": [
    {
      "slug": "example-org/demo-repo",
      "worktree": "/opt/data/repos/demo-repo"
    }
  ]
}
```

Optional executor company pack (omit for minimum startup):

```json
"skills": {
  "company_pack": {
    "source": "/opt/data/company-skills-pack",
    "allowlist": ["repo-bootstrap"]
  }
}
```

### Field notes

- `captain_github_login` and `worker_github_login` must be existing-style GitHub
  account names: 1–39 characters of `[A-Za-z0-9-]`, starting with alphanumeric.
  Legacy trailing hyphens (including consecutive hyphens) are accepted so real
  historical accounts load; empty, leading-hyphen, underscore/dot, and overlong
  values are rejected. Captain and worker must differ case-insensitively.
- `worker_github_login` is the executor identity. H1's `github_identity` remains
  accepted as an alias. When both are present they must match; conflicting
  aliases are rejected.
- `dispatch_label` is the intake trigger. H1's `required_label` remains accepted
  as an alias and is exposed on `Policy.required_label`. When both are present
  they must match exactly.
- `ready_label` is the triage label used by later orchestration stages. It must
  differ from `dispatch_label` case-insensitively so the specified→dispatched
  frontier cannot collapse.
- `trusted_human_associations` is limited to GitHub repository roles
  `OWNER`, `MEMBER`, and `COLLABORATOR`.
- `trusted_review_bots` is an exact-login allowlist (case-insensitive uniqueness).
- `merge.default_mode` is always `explicit_captain_approval`. Silence is never
  approval. Only an unambiguous whole-line directive equal to
  `Merge when clean: yes` (optional single markdown list prefix) grants
  unattended merge after review/checks/mergeability. Explanatory, negated,
  quoted, backticked, and near-match prose do not. The same directive on an
  epic root is inherited by children unless a child narrows with an unambiguous
  `Merge when clean: no`.
- `openviking_peers` entries must be objects with a single `id` field. Bare
  strings and undocumented aliases such as `peer_id` are rejected. Peer `id`
  values must be unique case-insensitively. Defaults in the ExampleCo fixture
  are `codex`, `chatgpt`, and `hermes`. Enabling `integrations.openviking`
  does not put secrets in this document; client credentials stay in separate
  owner-only files. See [openviking.md](openviking.md).
- `integrations.fava_trails` enables optional FAVA Trails doctor/setup surfaces.
  Defaults to `false`. Enabling it does not start FAVA in the public Compose
  stack; see [fava-trails.md](fava-trails.md).
- Optional `worker_access_scope` is `selected` (default) or `broader`.
  Hermes Helmet still requires every configured work repository and its metadata,
  contents, pull-request, and issue capabilities. Extra public repository
  visibility is not a setup failure. Extra private visibility fails unless the
  adopter explicitly selects `broader`. The effective choice is reported.
- Optional `worker_completion_contract` is `github-pr` (default) or
  `local-only`. Default H1 root and repair tasks bind Hermes
  `--completion-contract` to the repository slug. `local-only` is the
  private-plan fallback when that hook cannot call the branch-rules API; workers
  still complete with `metadata.published_pr`. Captain live PR identity,
  exact-head review, required-check, and merge gates stay on `helmet-issue`.
  `local-only` does not authorize unattended merge from GitHub
  `mergeable_state=clean` without independently verified required checks or
  explicit Captain approval.
- Repository slugs are unique case-insensitively. Repository `worktree` paths
  must be absolute, unique after normalization, and free of unsafe elements
  (`..`, null bytes, OS-sensitive roots).
- Optional `skills.company_pack` configures executor company skill import only:
  absolute `source` path plus an explicit `allowlist` of skill directory names.
  Omit the block for minimum startup (import status `skipped`). Captain bundled
  skills are **not** configured here; they install only via explicit
  `install-skills`. See [company-skills.md](company-skills.md).
- Optional `model_lanes` names secret-free OpenAI-compatible contracts for
  FAVA generation, OpenViking semantic generation, and embeddings. Hermes
  executor selection remains `inference_provider` / `inference_model`. Omit
  the block (ExampleCo default) so optional local inference is declined.
  Credentials stay in owner-only runtime files. See
  [model-lanes.md](model-lanes.md).

## Secrets

The policy, rendered crew contract, logs, task prose, and model context must
never contain PATs, provider secrets, OpenViking keys, FAVA credentials, or
other secrets. Validation rejects unknown schema fields and secret-shaped keys
throughout the nested document and never prints secret values in errors.

## ExampleCo fixture

- Source: `config/fixtures/exampleco/policy.json`
- Quickstart copy: `config/policy.example.json`
- Optional FAVA templates: `config/fixtures/exampleco/fava-trails/`
- Optional OpenViking templates: `config/fixtures/exampleco/openviking/`
- Optional model-lane examples: `config/fixtures/exampleco/model-lanes/`
- Render: `authority.render_crew_contract(policy)`
- Task language: `authority.render_issue_task_body(...)`

The fixture is deliberately generic. It must not embed private adopter
identities, hosts, paths, or service labels.

## Private overlay seam

See [private-overlay.md](private-overlay.md). A company repository supplies a
concrete version-2 policy document (and optional rendered contract) and mounts
it on the published image without forking Hermes Helmet source.
