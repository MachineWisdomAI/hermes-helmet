# First-officer plugin for Claude Code and Codex

Give Hermes the wings to carry your plan through, under its own identity.

Install the bundled `setup-helmet`, `helmet-issue`, and `helmet-epic` skills
on a configured first-officer host. This repository is the plugin
marketplace. It packages those existing skills for Claude Code and Codex; it
does not fork them, change the worker, or replace `helmet` setup.

You remain the Captain. Claude Code or Codex runs on the Captain host with
the Captain GitHub identity (`captain_github_login`). The published Docker
image runs Hermes with a separate worker identity (`worker_github_login`).
Choosing Claude as first officer does not change the worker model and does
not put Claude inside that container.

The plugin supplies skills. The host still needs the `helmet` CLI, `gh`,
model login for the first-officer host, the authority policy, and access to
the worker. Use the public [CLI](quickstart.md) and
[setup](setup-helmet.md) guides. Plugin installation does not provision
Docker, tokens, or provider login.

If you already installed the bundled skills with `helmet install-skills` or
by copying `skills/`, you can switch to this plugin as the distribution
path. Do not delete unrelated host skills. Canonical behavior stays in
`skills/` and the delivery docs.

## Install

The Codex plugin also carries Captain’s Bridge: a read-only panel that
explains one recorded piece of work in the invoking chat and opens its
supporting records. It is Codex-only and starts no second server beyond the
plugin's own MCP server. From an open Bridge, “Update walkthrough” can ask the
first officer to start one read-only background preparation, cancellable from the
panel; see the lifecycle in the Bridge guide. See the
[source and test guide](../mcp/captains-bridge/README.md). The Claude Code port
is tracked in
[issue #55](https://github.com/MachineWisdomAI/hermes-helmet/issues/55); see
[Captain's Bridge in Claude Code](#captains-bridge-in-claude-code) for what the
Claude plugin ships so far.

### Claude Code session records

`helmet bridge read --session <id> [--transcript <path>] [--max-bytes 600000]`
reads one Claude Code session's saved records for the Claude port of the
Bridge. It opens files read-only, finds `<id>.jsonl` under
`$CLAUDE_CONFIG_DIR/projects` (default `~/.claude/projects`) by exact file name
only, and prints `hermes-helmet.bridge.records/1` JSON. Errors exit 2 with
`session-not-found`, `ambiguous-session`, `session-mismatch`,
`unsupported-format` or `unreadable`. Supported Claude Code versions are
2.0.0 up to (not including) 2.2.0; others are `unsupported-format`.

Every record's `ref` (and each `parentRef`) is a full 32-hex identity derived
from that record's own source UUID, so it does not change when records are
appended, a partial final line completes, or subagent records appear. When one
source UUID expands into several output records, the first keeps the UUID-derived
ref and later blocks get a deterministic 32-hex ref from the UUID and block index.

### Claude Code

```sh
claude plugin marketplace add MachineWisdomAI/hermes-helmet
claude plugin install hermes-helmet@hermes-helmet
```

Claude namespaces plugin skills. After a new session starts, or after you
reload plugins in an existing session, invoke them as
`/hermes-helmet:setup-helmet`, `/hermes-helmet:helmet-issue`, and
`/hermes-helmet:helmet-epic`.

### Captain's Bridge in Claude Code

The Claude plugin now includes the Captain's Bridge mod. A mod runs code inside
your Claude Code sessions, unlike the Codex plugin, which still copies
instructions (and its own MCP server) only. The skills above are unchanged and
keep working if your organization policy disables mods; only the Bridge is
skipped.

This release adds the reader and the Changes First view. Run `/captains-bridge` in any session to open
the Captain's Bridge pane beside the conversation. It is bound to that exact
session ID, starts no turn and messages no one, so it can be opened while the
first officer is working and at narrow terminal widths. It never opens by
itself and runs no timers. In VS Code and headless or SDK sessions, where no
pane draws, the command returns a short text status instead. The background
explanation arrives in a later release under the same state
contract.

The Bridge now reads the session's saved records and draws the Changes First
view. Opening the pane (and **Refresh records**) runs
`helmet bridge read --session <id>` for this exact conversation, adding
`--transcript <path>` when the plugin saw the path at session start; otherwise
the reader looks the session up by its exact ID. The reader is read-only, the
call stops after 30 seconds, and no model is called and no message is sent.
Until an explanation exists the pane shows record counts, coverage and
**Refresh records**, never the raw transcript. **Update explanation** arrives
in a later release; a walkthrough shown here is drawn from the plugin state
contract, with its objective and outcome first, then *What changed*, *What
remains unresolved* and *Other recorded activity*. Opening an item shows its
explanation, actor-labeled handoff, review and repair steps, the completion
report and the disposition as separate labeled boxes, **View before and
after** only where the records support one, links whose address appears in a
cited record, elapsed time from the cited record timestamps, and the
supporting records collapsed. **Back** restores the previous item and scroll
position. A completed run or a quiet interval is never presented as accepted
delivery or a stall.

**Refresh records** rereads only the saved records. It keeps your place and
never changes the time the explanation read the records. When the records have
moved on, the header says "Explanation read records at {readAt}; {n} newer
records since." and lists read time and coverage warnings. If `helmet` cannot
be started or is too old, exits without a reader error, prints something that
is not the expected JSON, or speaks an incompatible schema, the pane names the
cause and the fix and keeps the last useful view; set `helmetCommand` to an
absolute path when Claude Desktop does not inherit your shell `PATH`.

Show Me and Retro run your own installed commands in this same session. The
Bridge checks `$.command.list()` when it opens and on every refresh, and accepts
the configured name (`showMeCommand`, default `show-me`; `retroCommand`, default
`retro`) or `<plugin>:<name>`. When the command is present, the button calls
`$.command.run` once, with the scope "this session" or, for a selected item, its
title with its evidence refs and the timestamps of the records it cites. Claude
Code queues that run until the first officer is idle, so it never interrupts a
turn; the Bridge submits no prompt, starts no subagent and sends nothing to
another session. Retro asks for suggestions and applies none. While a request
is unsettled the button shows "(queued)" and repeat presses are ignored. When
the command is missing the action is disabled and sends nothing: "Show Me isn't
installed. Install a skill named show-me, or set showMeCommand." (and the same
for Retro). If the host rejects the name as unknown, the action returns to that
missing state. The Bridge bundles neither skill and works fully without them.

If the conversation changes (`/clear`, `/resume`, a fork, or the session
ends), the Bridge ends its binding and shows exactly: "This conversation
changed. Run /captains-bridge to open the Bridge for it." It never rebinds the
open pane on its own; run `/captains-bridge` again.

Requirements:

- Claude Code 2.1.293 or newer. An older host names the required version in
  the command and the pane. Mods are early access and their API can change
  between Claude Code releases; this release is verified on Claude Code
  2.1.296 by an exact, checksum-pinned CLI in CI (`claude plugin validate` and
  `claude plugin test`, with no account credentials). Re-verify when the
  minimum changes. CI is not an installed-host check: the Terminal and Desktop
  Code tab are confirmed by hand before a release.
- An organization policy that allows mods.
- The `helmet` CLI reachable from Claude Code. Sessions started by Claude
  Desktop may not inherit your shell `PATH`; set `helmetCommand` to an absolute
  path then.

Settings (plugin `userConfig`, defaults in parentheses): `helmetCommand`
(`helmet`), `explanationModel` (`sonnet`), `explanationTimeoutSeconds` (`180`),
`showMeCommand` (`show-me`), `retroCommand` (`retro`). `showMeCommand` and
`retroCommand` take effect now; the other three take effect with later releases.

Update with `claude plugin update hermes-helmet@hermes-helmet`, then
`/reload-plugins` or start a new session. Third-party marketplaces do not
auto-update, so each release carries a version bump.

The mod's calls are limited in CI to a read-only allowlist: `$.session.id`,
`$.session.version`, `$.process.run`, `$.model.complete`, `$.command.register`,
`$.command.list`, `$.command.run`, `$.ui.open`, `$.ui.resolve`, `$.ui.status`,
`$.ui.toast`, `$.state.get`, `$.state.set` and `$.clock.after`. It cannot write
files, spawn agents, submit prompts, send messages or reach the network. This
slice uses only `$.command.list`, `$.command.register`, `$.command.run`,
`$.session.id`, `$.session.version`, `$.state.get`, `$.state.set`, `$.ui.open`
and `$.ui.resolve`.

### Codex

```sh
codex plugin marketplace add MachineWisdomAI/hermes-helmet
codex plugin add hermes-helmet@hermes-helmet
```

Codex reads the same `.claude-plugin/marketplace.json` catalog. Skills load
on the next Codex session after install. There is no second marketplace file
in this repository.

Confirm with `claude plugin list` or `codex plugin marketplace list` when
those CLIs are on the host.

#### Codex: update and activation

Each release carries a version bump in `.codex-plugin/plugin.json` (kept equal
to the Claude manifest, `pyproject.toml` and the package version by
`scripts/mw-version.py`), because a third-party marketplace does not
auto-update. To update, use your Codex CLI's plugin-marketplace refresh for
`hermes-helmet`, then `codex plugin add hermes-helmet@hermes-helmet` again (run
`codex plugin marketplace --help` for the refresh verb in your version; it is
not pinned here because the Codex CLI was not available on the host that
produced this guide).

Then start a new Codex session or chat. Skills, the plugin's MCP server
(`codex.mcp.json` → `mcp/captains-bridge/server.py`) and the Bridge panel are
read when a session starts, so one new session after the update is the
activation step. Repeated global Codex restarts are not an update procedure and
are not needed. Check the installed version with `codex plugin marketplace list`
or the plugin's `plugin.json`.

The plugin package is the repository tree selected by the marketplace entry. It
needs no local marketplace, private toolkit or personal path. It does not
contain chat records, screenshots, research artifacts, credentials or local
prototypes; the Bridge reads the invoking chat's existing records at run time
and writes nothing.

#### Captain’s Bridge: host and storage compatibility

| Area | Supported | Not supported or unverified |
| --- | --- | --- |
| Host | Codex with plugin MCP apps and the `CODEX_THREAD_ID` environment variable. | Other Codex-compatible hosts; the Claude plugin does not load this server (see the Claude Code section for its own pane). |
| Runtime | Python 3.11 or newer on the host running the plugin's MCP server. | Older Python: the server does not start. |
| Chat storage | Read-only access to the selected chat's existing local Codex records in the format the reader recognizes. | Any other or newer record format: rejected with an explicit message, never guessed. |
| Background “Update walkthrough” | Only on a host that advertises `hostCapabilities.backgroundWalkthroughDelivery`. | The measured installed Codex does not: the button ends immediately, keeps the last view and starts no snapshot, agent or timer. |
| Show Me / Retro | Sent on an explicit click to the originating chat when a skill with that name is discoverable. | Missing skill: the chat reports it and stops; nothing is substituted. |

Failure states are visible in the panel and keep the last good view: missing
chat identity (needs binding), unknown or foreign chat, empty or unsupported
records, partially written final record, refresh error, stale source (flagged
older), and the unsupported-background boundary. A successful tool call is not
proof that a panel rendered; rendering is established only by opening the
Bridge in Codex. The protocol, package and fixture tests in `scripts/verify.sh`
do not stand in for that, and the Bridge guide records which cases were
exercised in an installed Codex and which only with fixtures (for example,
missing optional skills were tested with fixtures, not by removing installed
skills).

## Starting a first-officer session

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

## Claude Desktop Code tab

Use the Claude Desktop **Code** tab for a local session on this machine, or an
SSH session to a Linux or macOS Captain host such as `captain.example.com`.
Both can use plugins. An SSH session reads plugins, `~/.claude`, and any
user-owned first-officer env file from the remote host, not from your local
desktop. Run `helmet` and `gh` on that same host.
