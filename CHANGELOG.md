# Changelog

All notable changes to Hermes Helmet are recorded here. The project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### 0.10.0

- Show the Captain’s Bridge Changes First view in the Claude mod
  ([#69](https://github.com/MachineWisdomAI/hermes-helmet/issues/69), part of
  [#55](https://github.com/MachineWisdomAI/hermes-helmet/issues/55)). The pane
  runs `helmet bridge read` for the bound session (30 s limit, exact session ID,
  known transcript path when present), validates the schema major and session
  before adopting it, and draws one element tree for terminal and desktop:
  objective and outcome, then What changed, What remains unresolved and Other
  recorded activity, with the completion report and the disposition as separate
  labeled boxes. Item detail adds actor-labeled steps, View before and after
  only where supported, evidence-bound links, elapsed time from cited records,
  collapsed supporting records and Back with its place restored. Refresh records
  rereads saved records only, keeps the view stack and selection, never restamps
  the walkthrough’s `readAt`, and shows “Explanation read records at {readAt};
  {n} newer records since.” Reader failures name the cause and fix and keep the
  last useful view. The reader answer must carry the expected schema, valid
  records, coverage counts and warnings before it replaces anything. Long
  explanations and supporting records are drawn as word-wrapped pieces, so
  scrolling never skips a whole long block, and Back stays in the fixed header.
  Each overview item button now carries its title and status as one string
  label, which the advertised minimum Claude Code 2.1.293 requires to draw the
  pane. The call-allowlist parser accepts Claude Code’s `(via helper)` tag. No model
  is called and nothing is sent.

### 0.9.0

- Run the Captain’s installed Show Me and Retro commands from the Claude
  Bridge pane
  ([#71](https://github.com/MachineWisdomAI/hermes-helmet/issues/71), part of
  [#55](https://github.com/MachineWisdomAI/hermes-helmet/issues/55)).
  Availability comes from `$.command.list()` when the Bridge opens and on each
  refresh, for `showMeCommand`/`retroCommand` or `<plugin>:<name>`. A missing
  command is a disabled action with the install wording and sends nothing; an
  unknown-name rejection returns to that state. A present command calls
  `$.command.run` once with the scope “this session” or the selected item’s
  title, evidence refs and cited timestamps, relying on the host’s queue to wait
  for an idle first officer. `pending` ignores repeat presses until settlement.
  No prompt, subagent or cross-session send is used and neither skill is
  bundled. The CI allowlist check now accepts the `(via helper)` annotation that
  `claude plugin validate` adds to calls made in helper functions, while still
  checking every call name.

### 0.8.0

- Add the Captain’s Bridge mod skeleton to the Claude plugin
  ([#68](https://github.com/MachineWisdomAI/hermes-helmet/issues/68), part of
  [#55](https://github.com/MachineWisdomAI/hermes-helmet/issues/55)). A hooks
  module registers `/captains-bridge` (immediate) and opens a pane bound to the
  exact `$.session.id()` without starting a turn or messaging the first
  officer; non-drawing surfaces get a text status. `clear`, `resume`, `fork`
  and session end end the binding and show “This conversation changed. Run
  /captains-bridge to open the Bridge for it.” Claude Code older than 2.1.293
  is told which version is needed. The manifest names a `PluginState`/
  `BridgeState` type contract and `userConfig` (`helmetCommand`,
  `explanationModel`, `explanationTimeoutSeconds`, `showMeCommand`,
  `retroCommand`). CI installs an exact, checksum-verified Claude Code 2.1.296,
  runs `claude plugin validate` and `claude plugin test` without credentials,
  and fails on any call outside the read-only allowlist. MCP packaging and the skills
  are unchanged. The version moves to 0.8.0 in `pyproject.toml`, the Python
  package and both plugin manifests (the Codex manifest changes only its
  version field), synchronized by `scripts/mw-version.py`.

### 0.7.0

Claude Code record reader.

- `helmet bridge read --session ID [--transcript PATH] [--max-bytes N]` reads
  one Claude Code session's saved transcript and its subagent transcripts
  read-only and prints a bounded `hermes-helmet.bridge.records/1` summary with
  stable refs, origins (`person`, `hook`, `plugin`, `meta`, `assistant`,
  `agent`), coverage and warnings. Thinking is dropped, every record's session
  ID is verified, an incomplete final line sets `pendingTail`, and the summary
  budget keeps every person prompt and assistant text while excerpting and
  then dropping the oldest tool results. `--max-bytes` bounds the exact stdout
  bytes. Failures exit 2 with `session-not-found`, `ambiguous-session`,
  `session-mismatch`, `unsupported-format` or `unreadable`; malformed core
  text blocks or an unsupported version on any record return
  `unsupported-format`.
- Each ref is the record's full 32-hex UUID-derived identity, computed from that
  record's own UUID only. It does not depend on other records, timestamps or
  presentation order, so appends, partial-tail completion and subagent growth
  never change an existing ref or `parentRef`. When one source UUID expands
  into several output records, the first keeps the UUID and later blocks get a
  deterministic 32-hex identity from the UUID and block index.

### 0.6.0

- Captain’s Bridge can prepare an updated walkthrough in the background. The
  panel captures the originating chat and a verifiable snapshot (delivery is
  rejected if captured records changed or digests are inconsistent), asks the first
  officer to dispatch one bounded read-only agent without waiting, and keeps the
  current walkthrough visible. A timeout asks the first officer to stop the agent
  and reports the stop as requested, not confirmed. Cancel, supersession, timeout, failure, duplicate
  submission and late completion preserve the last useful view. Verified with
  synthetic protocol and panel tests only; acceptance in an installed Codex host
  is recorded separately in the pull request. Measured Codex limit: after the
  parent turn finishes, the originating panel does not render a delivery, so a
  request on the installed host now ends with an unsupported-boundary message
  (no capture, dispatch or timer) unless the host advertises background delivery.

### 0.5.0

- Captain’s Bridge Refresh records now rereads the exact bound chat directly
  through its read-only app tool, with no agent message, background explanation
  or project task. Selection and navigation are kept, cited records are
  validated against the chat as it is now, and “Records read” is shown apart
  from “Explanation prepared from records read”. New records mark the
  explanation older without changing its conclusions or restamping it. Read
  errors, unsupported formats, missing evidence, timeouts, foreign and
  out-of-order responses keep the last useful view and report the limitation;
  a partially written final record is flagged rather than shown as a complete
  chat.

### 0.4.0

- Captain’s Bridge now presents the Changes First layout: purpose and outcome
  in prose, grouped work items, and details for handoffs, review, evidence and
  before/after changes. Evidence opens the original referenced records, Back
  restores selection, scroll and focus, and elapsed time and gaps are collapsed
  on the overview. Explicit handoff and wakeup relations are recorded without
  implying causation or stalls; Hermes steps and readable names are validated
  against the selected chat. The panel paints its own foreground and
  background and follows the host's light or dark theme, so headings, strong
  text and buttons stay readable in a dark Codex panel.

### 0.3.0

- Add Captain’s Bridge to the Codex plugin: a read-only panel for the invoking
  chat that explains recorded work and opens its supporting records. It uses the
  plugin's own MCP server (`codex.mcp.json`) and the `observe-chat` skill, reads
  only the exact `CODEX_THREAD_ID`'s saved records, and writes nothing. The
  Claude plugin and the setup, issue, and epic skills are unchanged. Background
  preparation and cancellation are not included.

### 0.2.1

- Point the quickstart, private-overlay examples, and Claude subscription guide
  at the published 0.2.0 preview image from `80726d2`, including its immutable
  digest and build record.
- Clarify the bootstrap token comment and remove a time-sensitive claim from
  the existing dated `gh` scan exception reason.

### 0.2.0

- Bundle the Claude Code CLI 2.1.286 (sha256-pinned per architecture) and the
  experimental Claude Subscription DirectSDK model-provider plugin 0.3.3 in the
  worker image, so the assignee profile can run on Claude Sonnet 5.5 through a
  Claude Pro or Max subscription. See
  [Claude subscription workers](docs/claude-subscription.md).
- Accept an optional `HERMES_CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`.
  The entrypoint moves it to the tmpfs runtime directory and clears it before
  Hermes starts; `/usr/local/bin/claude` hands it to each CLI run, matching the
  worker PAT's handling. The image smoke test checks both tokens stay out of
  process environments.
- Unblock the preview image build on Hermes 0.21.6: the smoke readiness probe
  now matches the 0.21.6 gateway process, and the wrapper scan gate accepts
  two Go 1.27.1 standard-library denial-of-service CVEs in the pinned `gh`
  binary (CVE-2026-78667, CVE-2026-97031) until 2026-11-08. No `gh` release is
  built with the fixed Go 1.27.2 yet.

### 0.1.7

- Update the immutable Hermes Agent runtime base to v0.21.6, including the upstream October 8 security fixes.

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

### 0.1.0rc4 candidate

- Add named-request repository enrollment: when the human Captain explicitly
  asks the first officer to work on or review a named repository, that request
  authorizes enrolling that exact repo. `helmet prepare-repo` validates the
  request against current authority (Captain identity, allowed owners, and
  explicit `enrollment` ceilings in the policy), calls the worker-runtime
  `prepare-repo` transport verb with the normalized exact slug or canonical
  issue/PR reference, and persists a sanitized atomic receipt. Existing
  allowlisted repositories remain no-ops; read-only status never enrolls.
- Re-validate stored enrollment receipts against the current policy on every
  lookup, so owner narrowing, Captain changes, or disabled/restricted
  enrollment revoke stale receipts; repeat requests re-run the transport seam
  so access and clone validation are never skipped.
- Enforce review-only enrollment ceilings: review-enrolled repositories are
  never dispatched and never granted merge authority in issue or epic flows.
- Continue unattended after named-request enrollment instead of stopping at a
  fresh merge-mode question: the effective permitted policy default is used
  and persisted as merge source `named_request`. Explicit manual choices and
  installation ceilings are never overwritten or widened.
- Validate the canonical repository identity of timeline cross-referenced pull
  requests before adopting their number: valid foreign references are ignored,
  references with no usable identity grant no authority, and contradictory
  metadata claiming the local repository fails closed. Fixes a terminal
  failed run when a foreign PR cross-references a managed issue.
- Give the follow-up package and plugin cache a distinct candidate version;
  the published rc2 artifacts remain unchanged.

- Record explicit, durable Captain consent for configured Hermes model/provider
  use and honor existing task-specific consent without repeated prompts. This
  is instruction-level consent, not a runtime egress guard or host override.
- Require source-first repairs, independent review, and supported reinstall
  instead of patching installed skill bundles or approval records.

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

- Advance the pinned upstream Hermes Agent base to the supported stable
  v2026.9.24 tag (Hermes Agent 0.21.5), still pinned by immutable tag+digest,
  across the image build, Compose default, preview workflow platform refs,
  quickstart and tests.
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
