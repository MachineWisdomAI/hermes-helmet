# Operational provider selection

Set an ordered list once, then switch routes without editing the Git policy:

```sh
helmet providers configure \
  --route primary=anthropic:claude-sonnet-4-6 \
  --route backup=openai:gpt-4.1
helmet providers status
helmet providers use backup
helmet providers auto
```

These commands require a deployment transport configured through
`HERMES_HELMET_WORKER_RUNTIME` (or `--worker-runtime` on the subcommand).
`configure` selects automatic mode: the first route is primary and the rest are
native Hermes fallbacks in order. `use NAME` pins exactly that route, with no
fallback. `auto` restores the ordered chain. Re-run `configure` to change model
IDs or add routes. Models are explicit; a named route does not silently upgrade
to a new release. Credentials remain in the existing worker setup.

Hermes Agent owns failover and primary recovery. Supported triggers and recovery
timing depend on its installed version and provider adapter. Validate the actual
subscription adapter's quota error and recovery before claiming live acceptance.
There is no Helmet retry loop or quota polling service.

## Deployment transport

The CLI invokes `<worker-runtime> providers JSON_REQUEST` using an argument
vector. Requests are `{"action":"status"}`, `{"action":"auto"}`,
`{"action":"use","name":"backup"}`, or
`{"action":"configure","selection":SELECTION}`. The response is a JSON
object on stdout; a nonzero exit indicates failure. Configuration is secret-free:

```json
{
  "version": 1,
  "selected": "auto",
  "routes": [
    {"name": "primary", "provider": "anthropic", "model": "claude-sonnet-4-6"},
    {"name": "backup", "provider": "openai", "model": "gpt-4.1"}
  ]
}
```

The deployment must authenticate the operator before mutation, protect and
serialize its persistent selection record, require drained workers during
application, and preserve the selection in every reinstall and effective-policy
projection. Its committed authority still owns identities, repositories, merge
permissions and capacity. Selection only changes worker inference. Auxiliary
and embedding lanes remain separate. Endpoint-bound executor lanes require an
explicit migration rather than silently inheriting an old endpoint or API key.

`provider_selection` supplies validation, selection persistence, policy projection
and native Hermes config projection. A managed selection replaces the legacy
`fallback_model` entry; otherwise Hermes can append unselected legacy routes.
Manual mode projects an empty fallback list. Role provisioning applies each
role's configured effort to both primary and backup models while preserving
unrelated overrides and session stores.
