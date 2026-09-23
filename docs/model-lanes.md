# Provider and model lanes

Hermes Helmet selects the **Hermes executor** provider/model independently from
optional FAVA Trails generation, OpenViking semantic generation, and embedding
lanes. Hosted providers and local OpenAI-compatible endpoints use the same
documented contracts. One model artifact is **never** treated as suitable for
multiple lanes without independent probe evidence.

Declining every optional local-model path leaves the **minimum runtime**
operational. The public Compose stack does not start a local inference server.

## Contracts (secret-free)

Keep PATs and provider keys in owner-only runtime files or process environment.
Authority policy may name models, base URLs, timeouts, bind mode, and embedding
dimensions — never credentials.

| Lane | Contract | Example path |
| --- | --- | --- |
| Hermes executor | chat completions | `https://api.openai.com/v1/chat/completions` |
| FAVA generation | chat completions | `http://127.0.0.1:11434/v1/chat/completions` |
| OpenViking semantic generation | chat completions (separate model) | `http://127.0.0.1:11434/v1/chat/completions` |
| Embeddings | embeddings | `http://127.0.0.1:11434/v1/embeddings` |

Generation and embedding models are configured and probed as **separate**
contracts. Embedding **dimensions** are fixed in the lane document and verified
by the embeddings probe before any index is used.

Optional policy shape (omit the block, or omit optional keys, for minimum
startup):

```json
"model_lanes": {
  "fava_generation": {
    "kind": "chat_completions",
    "provider": "openai-compatible",
    "model": "example-fava-chat",
    "base_url": "http://127.0.0.1:11434/v1",
    "timeout_seconds": 60,
    "bind": "loopback",
    "structured_output": true
  },
  "openviking_semantic_generation": {
    "kind": "chat_completions",
    "provider": "openai-compatible",
    "model": "example-ov-semantic",
    "base_url": "http://host.docker.internal:11434/v1",
    "timeout_seconds": 60,
    "bind": "container-host"
  },
  "embeddings": {
    "kind": "embeddings",
    "provider": "openai-compatible",
    "model": "example-embed",
    "base_url": "http://127.0.0.1:11434/v1",
    "timeout_seconds": 30,
    "bind": "loopback",
    "dimensions": 768,
    "quantization": "q4_k_m"
  }
}
```

`inference_provider` / `inference_model` remain the Hermes executor fields. A
`model_lanes.hermes_executor` override is allowed only when provider and model
match those fields.

## Setup and probes

```sh
PYTHONPATH=src python3 -m hermes_helmet.cli models setup --config config/policy.json
PYTHONPATH=src python3 -m hermes_helmet.cli models setup --config config/policy.json --probe
PYTHONPATH=src python3 -m hermes_helmet.cli doctor --config config/policy.json
```

Without `--probe`, omitted optional lanes stay declined and the minimum runtime
remains operational (`ok=true`, `skipped_optional=true`) but setup is **not**
validated (`setup_success=false`). The Hermes executor is always selected;
`--probe` must succeed against it before setup reports success. Selected
optional lanes also require `--probe`.

Success requires the documented OpenAI-compatible response shape:

- Chat lanes: `POST {base_url}/chat/completions` with a tiny prompt, then a
  usable assistant `choices[0].message` (non-empty content). HTTP 200 bodies
  such as `{}` or `{"error": "..."}` fail the probe. When `structured_output`
  is true, the assistant content must be JSON.
- Embedding lanes: `POST {base_url}/embeddings` and require a finite numeric
  vector with `len(embedding) == dimensions`.
- Then a **disposable-index** retrieval check embeds two throwaway documents
  and one query in memory, ranks them, and discards the index. It must not
  read or write production data.

Authentication uses an `Authorization` bearer token from an owner-only
credential file or env name recorded in the runtime config (mode `0600`,
regular file, not a symlink). Pass `--runtime-dir` so setup and live
`doctor --live` can resolve `credential_env` names without putting secret
values in policy, CLI output, or model context. Live HTTP probes refuse
credential-bearing cross-origin redirects.

Changing an embedding model, dimension, or quantization during setup requires
`--embedding-index-state` and `--reindex-confirmed`. Helmet compares the
candidate read-only before probes and persists the new identity only after
every required probe succeeds. Failed or unprobed setup leaves the stored
fingerprint unchanged.

```sh
PYTHONPATH=src python3 -m hermes_helmet.cli models rollback-embedding \
  --embedding-index-state /path/to/embedding-index-state.json \
  --index-restore-confirmed
```

Rollback restores the previous fingerprint only after the operator confirms a
compatible preserved-index restore or a deliberate re-index with the old
identity. A metadata swap alone is not a rollback of the embedding index; the
command fails closed without `--index-restore-confirmed`.

## Loopback, routing, and timeouts

- **loopback** bind: serve on `127.0.0.1` / `localhost` so the endpoint is not
  published on a LAN interface.
- **authentication**: require a local token even on loopback when the server
  supports it; never put the token in the URL.
- **container-to-host**: from Compose/Docker use `http://host.docker.internal:<port>/v1`
  (or an equivalent host-gateway). Do not assume `localhost` inside the
  container reaches a Mac host process.
- **timeout**: set `timeout_seconds` per lane; probes fail closed on timeout.
- **structured-output**: enable only on chat lanes that actually return JSON;
  the probe fails if the content is not JSON.

## Mac local quantized models

On Apple Silicon, local quantized models can be served through host-native
Apple Metal. Hermes Helmet treats **Ollama**, **Unsloth Studio**, and
**OrbStack** as equally valid options beside each other. There is no preferred
Mac local runtime in this repository.

- Ollama: OpenAI-compatible `/v1` on loopback when enabled.
- Unsloth Studio: OpenAI-compatible `/v1` when the studio server is bound
  locally.
- OrbStack: container runtime on Mac; pair with a host or container
  OpenAI-compatible server and `host.docker.internal` as needed.

Pick one server per lane. Do not claim that a single GGUF/MLX artifact is
valid for Hermes execution, FAVA generation, OpenViking semantic generation,
and embeddings unless each contract has been probed independently.

## Re-index and rollback

Changing an embedding **model**, **dimension**, or **quantization** requires a
deliberate re-index. Helmet refuses the change until `reindex_confirmed` is
set, and it does not persist the candidate until probes succeed. The previous
fingerprint is retained so operators can restore a compatible preserved index
or re-index with the old identity, then run
`hermes-helmet models rollback-embedding --embedding-index-state PATH --index-restore-confirmed`
to commit the prior fingerprint. The command does not re-embed production data
and will not claim the live vectors match the old identity without that
confirmation.

Evaluation and candidate probes use the disposable index only. They preserve
production data: never re-embed or delete the live store as part of a probe.

## Minimum runtime

`integrations.fava_trails` and `integrations.openviking` stay independent of
model-lane selection. Omitting `model_lanes` (or omitting every optional lane)
is the default ExampleCo fixture. Offline `hermes-helmet models setup` and
`doctor` then report optional lanes skipped and the control loop remains
available. Live `doctor --live` still probes the always-selected Hermes
executor before reporting success.
