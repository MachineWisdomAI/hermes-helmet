# Captain’s Bridge implementation source

Captain’s Bridge explains the current chat’s distributed work inside Codex.
This directory preserves the approved `0.2.3` prototype so implementation can
continue from repository-owned source. It includes the read-only record adapter,
MCP server, interactive view, observation skill, local plugin manifests and
synthetic regression tests. It contains no saved conversations or private
acceptance evidence.

[Issue #53](https://github.com/MachineWisdomAI/hermes-helmet/issues/53) owns the
released Codex implementation. [Issue #55](https://github.com/MachineWisdomAI/hermes-helmet/issues/55)
ports the same experience to a Claude Code mod: its record reader starts from
`chat_reader.py` and `saved_chat.py`, and its explanation request from the
observation skill. The main Hermes Helmet plugin does not activate this
prototype yet. Its existing `helmet-observation-spike` identifiers and version
are retained to preserve the tested starting point; they are not another
Hermes Helmet release or a new public installation recommendation.

## Continue implementation here

Make future source changes in this repository. Installed plugin caches and
earlier local experiment directories are copies, not development sources.
Integrate the reusable behavior into the existing Hermes Helmet plugin under
issue #53, retaining the approved changes-first layout and evidence navigation.
Update the repository’s normal installation, packaging and version checks as
that integration becomes active. Preserve the other first-officer skills and
Claude plugin behavior.

The prototype has three separable responsibilities:

- `chat_reader.py` and `saved_chat.py` read the exact selected chat’s existing
  local records. The adapter depends on Codex’s private storage format and
  explicitly rejects unsupported records. It starts no second Codex server.
- `walkthrough.py` validates the explanation’s structure and source references;
  `server.py` exposes read, presentation and app-only refresh operations. A
  portable view carries its chat identity and explanation across tool processes.
- `view.html` renders the overview, work details, evidence, comparisons and
  navigation. This is the reusable app source, not an exported chat snapshot.

The observation skill describes how to prepare the explanation. Source text is
evidence, not authority to resume a project or act on instructions in a record.
The adapter excludes reasoning. Only Hermes activity explicitly recorded in
the selected chat is available; direct Docker event collection is outside the
prototype’s scope.

## Known unfinished behavior

Refresh rereads records directly through an app tool and retains the explanation
with its original source time. Update walkthrough sends a task to the first
officer. It does **not** yet delegate preparation, cancel a worker, bound an
attempt, or reject delivery after a superseding instruction. Issue #53 requires
those changes; do not claim background operation from this import.

Show Me and Retro request separately installed skills. Their portable discovery
and availability handling remain release work. The imported manifests describe
the original local plugin; the repository’s main manifests are the distribution
entry point once integration is complete.

## Run the existing checks

Use Python 3.11 or newer and Node.js. The tests use their standard libraries and
synthetic records; they do not read the current user’s chats or invoke models.
From this directory:

```sh
python3 -B -m unittest discover -s . -p 'test_*.py' -v
node test_view.cjs
node test_delivery.cjs
node test_actions.cjs
```

`scripts/verify.sh` runs the same checks, so CI keeps this source working until
it is integrated.

The checks cover read-only access, exact chat identity, record normalization,
source references, separate-process delivery, wrapped MCP results, freshness,
navigation and optional skill requests. They do not establish rendering or
background delegation in a particular Codex host. Verify the installed candidate
in Codex during integration as specified in issue #53.

Hermes Helmet by [Machine Wisdom AI](https://machine-wisdom.ai/). This source is
covered by the repository’s [Apache-2.0 license](../../LICENSE).
