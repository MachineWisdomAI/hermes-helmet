# Model-specific worker effort

Deployments can opt into Sonnet 5.5 effort control without changing provider
ordering, other models, task budgets, native review requests or scheduler slots.
Set `worker_roles.sonnet_55_effort` to a mapping of every configured role to
`medium` or `high`. This setting takes precedence over the older role-wide
`reasoning_effort` for Sonnet only. Provisioning also migrates root and discovered
retired profiles; it preserves their histories and non-Sonnet preferences.

The deployment adapter installs the native plugin from `effort_policy.plugin_files`,
enables it in each consuming profile, and supplies protected root-owned JSON
files outside worker-writable ancestors. The policy contains `captain` and a
`profiles` mapping of profile names to standing effort. The selection store maps
`board/task` to a Captain-authenticated record containing board, task, effort,
Captain, reason and the last observed `after_run_id`. These files must be backed
up with deployment state. The worker must never authorize its own exception.

A selection applies only to the first native task run after that boundary.
Workers snapshot the selection at plugin registration, so running conversations
are unchanged. Renewals and later retries require a new selection. A concurrent
launch may win the selection race; saved intent is not proof of effective effort.
Native task `--reasoning` is also checked, including after fallback re-resolves
profile settings. Use task selection for medium/high; xhigh additionally needs
explicit Captain authorization and why high is insufficient. Max is prohibited.

The guard checks the actual model and request on every inference. It supports
native Anthropic Messages and the subscription/OpenRouter chat-completions wire.
Aliases include `sonnet`, dotted/dashed and provider-prefixed model IDs, dated
snapshots and `[1m]`. Non-Sonnet calls pass through unchanged. Unsupported wire
modes, conflicting fields, disabled reasoning and invalid effort stop before
provider execution. Selected effort takes precedence over the model-specific
profile value, then global/provider default; absent effort uses the standing
medium/high default. An invalid existing value fails visibly instead of being
silently lowered at runtime.

Hermes currently swallows ordinary middleware exceptions and registration
failures. The execution callback therefore uses SystemExit for refusal, never
a synthetic model answer. Failed policy loading registers a refusing callback
so discovery cannot silently drop enforcement. A deployment must prove fresh
native registration in every profile and check refusal through the installed
middleware. The opt-in YAML setting alone is not runtime enforcement.

Each permitted route/effort combination and refusal is logged as `helmet_effort`
with board, task/run, profile, provider/model, requested/effective effort and
Captain exception provenance. No prompt, credentials or model reasoning are
recorded. Normal subsequent reviews provide completion and quality evidence;
this feature makes no latency guarantee and adds no watcher or queue.
