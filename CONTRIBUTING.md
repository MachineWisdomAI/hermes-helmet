# Contributing to Hermes Helmet

Help teams carry delegated software work through implementation, review,
repair, and acceptance. Contributions to Hermes Helmet can improve setup,
make a handoff clearer, or repair a defect in the shared workflow.

## Report an experience or propose a change

Open an issue with the task you attempted, what you expected, and what happened.
For setup problems, include the command and relevant diagnostic output with
secrets removed. For workflow problems, identify the step where work stopped
or lost its connection to the issue or pull request. Use the
[security policy](SECURITY.md) for vulnerability reports.

## Requirements

- Python 3.11 or later
- `scripts/verify.sh` (uses `unittest`, and `uv` plus `jj` for FAVA protocol checks)
- Work on a dedicated feature branch or git worktree. Do not commit to `main`.

## How to work

1. Read [AGENTS.md](AGENTS.md), [docs/captain-and-crew.md](docs/captain-and-crew.md),
   and [docs/authority-schema.md](docs/authority-schema.md).
2. Keep public fixtures generic (`ExampleCo`, `example-org`, `example-captain`,
   `example-agent`). Do not embed private adopter identities or paths.
3. Put secrets only in owner-only runtime files, never in policy, tests as
   live values, docs, or examples.
4. Add or update tests with the behavior change. Run:

```sh
scripts/verify.sh
```

5. Open a pull request. Do not merge from the worker path. Do not force-push.

## Public vs private

Reusable Hermes Helmet behavior lives in this repository. Concrete company logins,
checkouts, PATs, and private services belong in an adopter overlay; see
[docs/private-overlay.md](docs/private-overlay.md). Optional OpenViking is
AGPL-3.0; do not treat it as a minimum-runtime dependency.

## License

Contributions are accepted under Apache-2.0. See [LICENSE](LICENSE) and
[NOTICE](NOTICE).
