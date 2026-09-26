# First-officer plugin for Claude Code and Codex

Give Hermes the wings to carry your plan through, under its own identity.

This repository is a plugin marketplace. It packages the existing
`setup-helmet`, `helmet-issue`, and `helmet-epic` skills for Claude Code and
Codex. It does not fork those skills, change the worker, or replace `helmet`
setup.

## 1. You remain Captain

You stay the Captain. Claude Code or Codex runs on the Captain host with the
existing Captain GitHub identity, CLI, and skills. The published Docker image
runs Hermes with a separate worker identity. Choosing Claude as first officer
does not change the worker model and does not put Claude inside that container.

## 2. A plugin installs instructions, not the runtime

Installing the plugin copies first-officer instructions into the coding-agent
host. The host still needs:

- the `helmet` CLI
- `gh`
- model login for the first-officer host
- the authority policy
- access to the worker

Use the public [CLI](quickstart.md) and [setup](setup-helmet.md) guides. Plugin
installation does not provision Docker, tokens, or provider login, and it does
not invent a new transport.

If you already installed the bundled skills with `helmet install-skills` or by
copying `skills/`, you can switch to this plugin as the distribution path. Do
not delete unrelated host skills. The plugin does not migrate or overwrite
foreign skills automatically.

Canonical skill behavior stays in `skills/` and the delivery docs. This page
covers installation and an optional repeatable session.

## 3. Install

### Claude Code

```sh
claude plugin marketplace add MachineWisdomAI/hermes-helmet
claude plugin install hermes-helmet@hermes-helmet
```

Claude namespaces plugin skills. After a new session starts, or after you
reload plugins in an existing session, invoke them as
`/hermes-helmet:setup-helmet`, `/hermes-helmet:helmet-issue`, and
`/hermes-helmet:helmet-epic`.

### Codex

```sh
codex plugin marketplace add MachineWisdomAI/hermes-helmet
codex plugin add hermes-helmet@hermes-helmet
```

Codex reads the same `.claude-plugin/marketplace.json` catalog. Skills load on
the next Codex session after install. There is no second marketplace file in
this repository.

Confirm with `claude plugin list` or `codex plugin marketplace list` when those
CLIs are on the host. This packaging change does not itself run an
authenticated Claude or Codex task.

## 4. Claude Desktop Code tab

The Claude Desktop **Code** tab supports local sessions and SSH sessions, and
both can use plugins.

For a remote Captain host, choose or add an SSH connection to a Linux or macOS
machine such as `captain.example.com`, then install and use the plugin in that
environment. An SSH session reads plugins and `~/.claude` from the remote host,
not from your local desktop.

That is not Desktop WSL session mode, not a cloud session, and not ordinary
Chat. Cowork and claude.ai chat may list plugins; they are not a substitute for
the local CLI or Code-tab SSH workflow that can run `helmet` and `gh` on the
Captain host. Hermes Helmet does not ship an MCP adapter for this path.

## 5. Starting a first-officer session

After setup, keep non-secret deployment settings in a user-owned shell file
such as `~/.hermes-helmet/first-officer/env.sh`. Source it before launching
ordinary `claude` or `codex` so subprocesses inherit the same policy and
worker connection.

The plugin supplies skills. The environment selects the policy and worker
connection. Agent login, model, and permissions stay in their usual
configuration. A launcher adds neither authority nor a transport.

```sh
# ~/.hermes-helmet/first-officer/env.sh
export HERMES_HELMET_CONFIG="$HOME/.hermes-helmet/policy.json"

# Optional values from an already-configured deployment. The plugin and
# setup do not install placeholder adapters.
# export HERMES_HELMET_HERMES="/usr/local/bin/hermes"  # one executable path
# export HERMES_HELMET_WORKER_RUNTIME="your-adapter"   # command prefix
# export HERMES_HELMET_CHECKPOINT_DIR="$HOME/.hermes-helmet/other/checkpoints"
```

`HERMES_HELMET_CONFIG` is the generated policy path. The worker-transport
contract (verbs `ledger-root`, `ledger-watch`, `dispatch-root`, `wait`) is in
[helmet-issue.md](helmet-issue.md#checkpoint); this file only selects an
already-configured prefix. Credentials stay in existing `gh` and provider
login stores.

```sh
. "$HOME/.hermes-helmet/first-officer/env.sh"
claude
```

The same source line works with `codex`. An optional local wrapper can source
the file and `exec claude "$@"` (or Codex). A name such as `helmet-claude` is
your shortcut; Hermes Helmet does not ship it. Do not change the caller's
working directory.

On Desktop or SSH, the file must exist on the host that runs the tools.
Sourcing it in another terminal does not reconfigure an already-running app.
An agent can source it in the same shell as each `helmet` command:

```sh
. "$HOME/.hermes-helmet/first-officer/env.sh" && helmet status "$ISSUE_URL"
```

Check an existing configured issue without dispatching work:

```sh
gh api user --jq .login   # compare with captain_github_login in the policy
helmet status https://github.com/example-org/demo-repo/issues/123
```

## 6. Unchanged boundaries

Setup, issue and epic review, repair, identity separation, and merge authority
are unchanged. The worker never merges. No private company setup or keys belong
in this public package.
