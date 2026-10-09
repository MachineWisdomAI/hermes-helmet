# Claude subscription workers

The worker image bundles the Claude Code CLI and NousResearch's
[Claude Subscription DirectSDK](https://github.com/NousResearch/hermes-plugin-claude-subscription-directsdk)
model-provider plugin. With them, the policy `assignee` profile can run on
Claude Sonnet 5.5 through a Claude Pro or Max subscription instead of a
provider API key. Hermes keeps its own agent loop, tools, approvals, and
compaction; each model request runs the official `claude` CLI once.

The plugin is experimental. Upstream describes it as a review build, not a
full-parity or production-readiness claim.

| Component | Pin |
| --- | --- |
| Claude Code CLI | `2.1.286` native `linux-x64` / `linux-arm64` binary, sha256-checked |
| Provider plugin | `0.3.3` at commit `4bc79c78031d1a042b5d8a7314ceea283db5c5e2` |
| Hermes Agent base | `0.21.5` (the plugin requires `0.21.4` or newer) |

The provider id is `claude-subscription-directsdk-experimental`. The plugin is
installed root-owned under `/opt/hermes/plugins/model-providers/`, so every
Hermes profile in the container can select it.

The published preview digest in [runtime-image.md](runtime-image.md) predates
this integration and runs Hermes 0.21.3. Use the development build described
there until a newer preview is pinned.

## 1. Create a subscription token

On a machine with a browser, signed in to the Claude account the worker should
use:

```sh
claude setup-token
```

It prints a long-lived OAuth token. Add it to the owner-only `deploy/.env`:

```sh
HERMES_CLAUDE_CODE_OAUTH_TOKEN=<token from claude setup-token>
```

Then recreate the worker so the entrypoint picks it up:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  claude auth status
```

`auth status` should report `"loggedIn": true` and
`"authMethod": "oauth_token"`.

The token follows the worker PAT's path. The entrypoint writes it to
`/run/hermes-helmet/claude-code-oauth-token` (tmpfs, mode `0600`) and removes
`CLAUDE_CODE_OAUTH_TOKEN` before Hermes starts. `/usr/local/bin/claude` reads
that file for each run of the pinned binary. Worker processes do not inherit
the token, but anything running as the worker user can invoke `claude` and
spend subscription usage, just as it can invoke `gh`.

Claude Code keeps its non-secret state in `CLAUDE_CONFIG_DIR`
(`/opt/data/claude-code` on the worker volume).

## 2. Point the worker profile at Claude

In [quickstart step 4](quickstart.md#4-create-the-worker-profile-and-complete-provider-login),
use these commands instead of the OpenAI provider, model, and `auth add`
commands. Claude Code owns the login, so there is no `hermes auth add`.

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  /opt/hermes/.venv/bin/hermes profile create builder
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  /opt/hermes/.venv/bin/hermes -p builder config set \
  model.provider claude-subscription-directsdk-experimental
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  /opt/hermes/.venv/bin/hermes -p builder config set model.default claude-sonnet-5-5
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  /opt/hermes/.venv/bin/hermes -p builder config set agent.max_turns 2000
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  /opt/hermes/.venv/bin/hermes -p builder config set \
  providers.claude-subscription-directsdk-experimental.stale_timeout_seconds 900
```

`claude-sonnet-5-5` selects Sonnet 5.5 with its 1M-token context. The `sonnet`
alias follows whatever the plugin maps it to. Haiku 5.5 needs Claude Code
2.1.293, which is newer than this pin.

Without `stale_timeout_seconds`, a call that hangs without output holds the
turn for Hermes' 1,800-second read-idle limit before Hermes retries. The stale
timeout bounds the whole call, so keep it above your longest healthy turn.

Run the quickstart's bounded one-shot call to confirm the profile answers.

Record the same choice in the policy so the crew contract and the poller
installer agree:

```json
"inference_provider": "claude-subscription-directsdk-experimental",
"inference_model": "claude-sonnet-5-5"
```

Installing the poller without `--skip-model-config` writes these values to the
assignee profile.

## Limits

- Requests draw on the subscription's Agent SDK allowance, metered like
  `claude -p`. Turn off extra usage in the Claude account if you do not want
  overage charges. Fable models need usage credits on Pro.
- The plugin refuses to start Claude Code when `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_FOUNDRY_API_KEY`, or
  `CLAUDE_CODE_USE_BEDROCK`/`_VERTEX`/`_FOUNDRY` is set. Keep them out of
  `deploy/.env` and out of any Hermes profile `.env`.
- After a 401, Hermes suggests `hermes auth add ... --type oauth`. That does
  not apply here: replace the token in `deploy/.env` and recreate the worker.
- `/usage` cannot show plan limits with token authentication; the plugin reads
  those from an interactive Claude Code login.
- Host `helmet setup` and `helmet doctor --live` probe only OpenAI-compatible
  executors and reject this provider. Use the Compose route above, with the
  one-shot call as the provider check.

## Update the pins

For Claude Code, choose a version that supports the models you select. Copy
`platforms["linux-x64"].checksum` and `platforms["linux-arm64"].checksum` from
`https://downloads.claude.ai/claude-code-releases/<version>/manifest.json` into
`CLAUDE_CODE_SHA256_AMD64` and `CLAUDE_CODE_SHA256_ARM64` in
`deploy/Dockerfile`, set `CLAUDE_CODE_VERSION`, and update the expected version
in `scripts/smoke-dev-image.sh`.

For the plugin, review the upstream diff, then set
`CLAUDE_SUBSCRIPTION_PLUGIN_COMMIT` to the full commit SHA and
`CLAUDE_SUBSCRIPTION_PLUGIN_VERSION` to the `version` in that commit's
`plugin.yaml`.

Rebuild and run `scripts/smoke-dev-image.sh`; it checks the CLI version, that
the provider registers, and that neither runtime token reaches a process
environment.
