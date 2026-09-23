# OpenViking shared company working context

OpenViking is an **optional** integration. It is operational working context under
**AGPL-3.0**, not governed company truth. Hermes Helmet starts and operates when
OpenViking is declined or unavailable.

Promotion of any working-context item into **FAVA Trails** remains an explicit
operator action. Neither telemetry nor runtime memory becomes a governed
decision automatically.

This document targets the deployed OpenViking **0.4.19** contract: home-alias
URIs (`viking://~/...`) and official Codex / Claude Code memory plugins. Helmet
does not duplicate those plugins with a custom MCP adapter.

## What this integration provides

- One templated company account and one least-privilege **shared-all**
  company-context user (`role=USER`, never root/admin). This is the company
  working-context boundary, not a helper identity.
- The Captain/operator is the sole credential owner for that USER key.
- Three logical clients into the same account/user namespace: Codex, ChatGPT, and
  Hermes.
- Pairwise-distinct logical peer identifiers (defaults: `codex`, `chatgpt`,
  `hermes`).
- Separate owner-only client configuration files (mode `0600`).
- `hermes-helmet doctor` verification of shared namespace + distinct provenance.
- Guided setup templates that use placeholders instead of adopter names.
- Helmet CLI `write-marker` / `shared-proof` for intentional shared company
  markers. Official client plugins handle their own recall/capture.

## Provenance mapping (do not over-claim)

Map each logical peer through the surface its supported client adapter actually
provides:

| Client | Provenance surface | Notes |
| --- | --- | --- |
| Codex | Official `openviking-memory` plugin + `actor_peer_id` / `X-OpenViking-Actor-Peer` | Do not install a Helmet MCP duplicate |
| ChatGPT | Operator-gated connected app/tunnel; `actor_peer_id` header | Intentional tools only; no transcript capture |
| Claude Code | Official `openviking-memory` plugin | Not a fourth Helmet client type; do not duplicate |
| Hermes | `OPENVIKING_AGENT` / `actor_peer_id` → `X-OpenViking-Actor-Peer`; peer-scoped URIs under `viking://~/peers/<agent>/memories/` | Native Hermes provider + Helmet CLI for shared markers |

OpenViking 0.4.19 uses the actor-peer header for request context and peer view.
A `remember` user-role session message may **not** serialize that actor peer into
the stored JSONL record. Validate provenance through client configuration,
request/header flow, and the supported peer-or-agent view. **Do not claim** a
stored JSONL `actor_peer` field exists when the installed version does not
provide one.

Uid-less current-user spellings such as `viking://user/memories` are rejected.
Use `viking://~/memories/...` or an explicit `viking://user/{user_id}/...`.

## Setup (guided, optional)

```sh
# Non-secret company template from authority policy peers/company slug
# plus optional adopter account/user/service overrides
PYTHONPATH=src python3 -m hermes_helmet.cli openviking setup \
  --config config/policy.json --write-templates \
  --openviking-root config/fixtures/exampleco/openviking \
  --account exampleco --user company-context \
  --service-url http://127.0.0.1:1933

# Render one client example from the emitted company template
PYTHONPATH=src python3 -m hermes_helmet.cli openviking template \
  --config config/policy.json --client codex \
  --company-template config/fixtures/exampleco/openviking/company.template.json
```

Provision a USER key out of band (never commit it). Write each client config
from a key file so the credential never appears on a command line or in model
context. `write-client` consumes the customized `company.template.json` (via
`--company-template` or `--openviking-root`) instead of regenerating defaults:

```sh
# api_key_file must be owner-only and contain only the USER key
PYTHONPATH=src python3 -m hermes_helmet.cli openviking write-client \
  --config config/policy.json \
  --openviking-root ~/.hermes-helmet/openviking \
  --company-template ~/.hermes-helmet/openviking/company.template.json \
  --client codex \
  --api-key-file /path/to/user.key \
  --output ~/.hermes-helmet/openviking/clients/codex/ovcli.conf
```

Repeat for `chatgpt` and `hermes` with distinct output paths. All three share
the same account/user/URL and the same USER key material, but keep **separate
files** and distinct `actor_peer_id` / `OPENVIKING_AGENT` values.

### Shared-all boundary, owner, and key repair

This is a **shared-all** company working-context boundary: one account, one
`company-context` USER, one USER key, three owner-only client files. Do not add
a helper identity, a second shared user, or a rotation service.

The **Captain/operator** is the sole credential owner. Only that operator
provisions, writes, and rotates the USER key. Clients never mint keys.

Fail-closed rotation/recovery for all client configs:

1. Stop shared-proof and live writes until every client file is consistent.
2. Provision a replacement USER key out of band (never root/admin; never on a
   command line or in model context). Store it in an owner-only key file.
3. Rewrite **every** client config (`codex`, `chatgpt`, `hermes`) with
   `openviking write-client` from that same key file.
4. Run `hermes-helmet doctor --live`. If any file is missing, world-readable,
   mismatched, or otherwise fails, stop — fix that file and rerun. Do not leave
   mixed old/new keys across clients.
5. Only after all three pass, retire the old key on the OpenViking service.

### Codex (official plugin)

- Write owner-only `ovcli.conf` (`url`, `api_key`, `account`, `user`,
  `actor_peer_id`).
- Install the official OpenViking Codex memory plugin (`openviking-memory`)
  against that config. The plugin owns `/mcp` and lifecycle hooks.
- Helmet does **not** register a duplicate stdio MCP for Codex.
- Shared company markers still use Helmet `write-marker` / `shared-proof`
  (intentional; not plugin auto-capture).

### Hermes

- `hermes config set memory.provider openviking` (operator action).
- `OPENVIKING_ENDPOINT`, `OPENVIKING_API_KEY`, `OPENVIKING_AGENT=hermes`.
- Hermes sends `X-OpenViking-Actor-Peer` and writes peer-scoped memories for its
  native remember path under `viking://~/peers/<agent>/memories/`. Helmet shared
  markers use intentional `content/write` into common `events` memory instead
  (see shared-proof below).

### ChatGPT (operator gates required)

ChatGPT must not scrape or capture chats automatically. Required operator steps:

1. Create the Secure MCP tunnel in the operator Platform organization.
2. Create a **limited** tunnel runtime key (never an admin key).
3. Store the runtime key only in an owner-only private file (mode `0600`).
4. Enable ChatGPT developer mode and a custom app with **Connection: Tunnel**.
5. Select/approve the tunnel and intentional write tools only.
6. Keep tunnel health/UI on loopback or private connectivity.
7. Call tools intentionally from Work or ordinary chat; no transcript capture.

Helmet does not ship a ChatGPT MCP adapter or replace that tunnel. Claude Code
uses the official OpenViking memory plugin; that is a separate Anthropic client
path, not a fourth Helmet peer type.

## Doctor

```sh
PYTHONPATH=src python3 -m hermes_helmet.cli doctor --config config/policy.json
```

When `integrations.openviking` is `false`, doctor reports OpenViking as skipped
and remains successful so the minimum runtime stays healthy.

When enabled, provide the three owner-only client paths:

```sh
PYTHONPATH=src python3 -m hermes_helmet.cli doctor --config config/policy.json \
  --openviking-root ~/.hermes-helmet/openviking
```

Without `--live`, doctor is an **offline configuration check** only
(`verification_mode=offline`). It validates owner-only files, shared
account/user/URL and one shared USER key across the fixed client types
**and against the selected company template** (`company.template.json` under
`--openviking-root`, an explicit expected template, otherwise the policy
template), pairwise-distinct peer IDs, Hermes effective-setting consistency
(agent, endpoint, and credential aliases), and explicit ChatGPT operator-gate
settings in the ChatGPT client file (`connectivity=loopback_or_private_only`,
`intentional_tool_selection=true`, `transcript_capture=false`,
`operator_gates_required=true`). Keys are compared for equality without
printing them; mixed old/new keys fail closed. It does **not** authenticate
credentials and must not be read as proof that keys are safe or USER-scoped
on the wire. Three client files that agree with each other on the wrong
namespace fail closed offline. Legitimate explicit custom templates remain
accepted.

```sh
# Authenticate credentials in addition to the configuration-only namespace check
PYTHONPATH=src python3 -m hermes_helmet.cli doctor --config config/policy.json \
  --openviking-root ~/.hermes-helmet/openviking --live
```

`--live` additionally authenticates each client key as USER in the selected
company-template namespace. It does not replace the configuration-only
account/user/service URL comparison.

Doctor fails closed (without printing credentials) on blank, duplicate,
wrong-account, wrong-user, root-key, world-readable, credential-bearing URLs
(including unknown query names that embed the known key), Hermes peer/endpoint/
credential contradictions, non-private ChatGPT endpoints (loopback or private IP
only — DNS names are not certified private from spelling), and mismatched-client
configurations. Live mode redacts known keys even when a server error detail
echoes them (including independent percent-escape hex letter case per token),
and refuses credential-bearing cross-origin redirects.

### Wire contract (OpenViking 0.4.19)

Shared company markers use the ordinary-user **events** memory subtree (the
type-quota recall target) via the home alias:

- write: `POST /api/v1/content/write` with explicit `uri`, `content`, `mode=create`
  under `viking://~/memories/events/…`
- search: `POST /api/v1/search/search` with JSON `{query, target_uri?}`; result is
  a FindResult object (`memories` / `resources` / `skills`)
- recall: `POST /api/v1/search/recall` with JSON `{query, quotas?, render?}`;
  result is a RecallResult object (`entries` / `rendered` / `stats`)
- read: `GET /api/v1/content/read?uri=...` (result is content text)

Uid-less `viking://user/memories` (and the same shape for resources, skills,
peers, privacy, sessions) fails closed. Peer-scoped trees
(`viking://~/peers/<id>/...`) stay private to the actor peer. Hermes intentional
`remember` still targets peer-scoped URIs; that is a supported private path, not
the shared company-marker path. Helmet exposes the shared path as intentional
content/write via `openviking write-marker` and `openviking shared-proof`.
Provenance for shared markers is demonstrated through distinct
`X-OpenViking-Actor-Peer` headers and client config origin views — not a stored
JSONL `actor_peer` field.

### Intentional shared proof

Helmet CLI shared-proof is **not** official Codex, ChatGPT, or Hermes
foreground-app acceptance. Required real official-client acceptance remains a
separate Captain/owner check against the live service and official clients.

```sh
# Live HTTP against owner-only configs. Still not official-client acceptance.
PYTHONPATH=src python3 -m hermes_helmet.cli openviking shared-proof \
  --openviking-root ~/.hermes-helmet/openviking \
  --transport http --json
```

`--transport fake` is a hermetic **synthetic/non-acceptance** demo for tests.
It must not be presented or recorded as completed cross-client/restart
acceptance. `write-marker --transport fake` uses the same labeling
(`transport=fake`, `verification_scope=synthetic_non_acceptance`,
`acceptance=false`) so fake success cannot be read as live.

```sh
# Synthetic/non-acceptance only. Do not treat ok as official-client acceptance.
PYTHONPATH=src python3 -m hermes_helmet.cli openviking shared-proof \
  --openviking-root ~/.hermes-helmet/openviking \
  --transport fake --json
```

Use `--transport http` only against a live OpenViking service with the three
owner-only client configs. Shared-proof validates ChatGPT operator gates and
probes live identity first; it rejects unsafe/missing gates and root/admin or
wrong-account/wrong-user credentials **before** any write. It also
enforces one shared account/user/URL, one shared USER key, and pairwise-distinct
peer IDs before any mutation. Mixed old/new keys fail closed without printing
them. Marker URIs include a unique run id so retries do not collide with
`mode=create`, and retrieval uses a bounded wait for truthful queued indexing.
The command prints transport, verification scope, and redacted origin evidence
from the write request headers; it never prints credentials. Fake results are
labeled `verification_scope=synthetic_non_acceptance` with `acceptance=false`.

Helmet does not ship a duplicate stdio MCP adapter. Codex and Claude Code should
use the official plugins' `/mcp` proxy. Hermes may invoke the reviewed CLI from
its supported terminal/tool path.

## Minimum runtime boundary

The public Compose stack does **not** run OpenViking. Private overlays may add
tenancy. See [private-overlay.md](private-overlay.md) and
[authority-schema.md](authority-schema.md).

## Working context vs FAVA

| System | Role |
| --- | --- |
| OpenViking | Operational working context, search/recall across Codex/ChatGPT/Hermes |
| FAVA Trails | Governed company brain: reviewed observations, decisions, validation, lineage |

Use OpenViking for synthetic markers, drafts, and cross-client working memory.
Write Trail-worthy decisions to FAVA deliberately. Query FAVA for drafts,
validation, promotion, rejection, or supersession.
