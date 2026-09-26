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
covers installation only.

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

## 5. Unchanged boundaries

Setup, issue and epic review, repair, identity separation, and merge authority
are unchanged. The worker never merges. No private company setup or keys belong
in this public package.
