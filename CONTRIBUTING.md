# Contributing to Hermes Helmet

Thanks for helping keep the public Captain-and-Crew control loop portable.

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

Reusable Helmet behavior lives in this repository. Concrete company logins,
checkouts, PATs, and private services belong in an adopter overlay; see
[docs/private-overlay.md](docs/private-overlay.md). Optional OpenViking is
AGPL-3.0; do not treat it as a minimum-runtime dependency.

## License

Contributions are accepted under Apache-2.0. See [LICENSE](LICENSE) and
[NOTICE](NOTICE).
