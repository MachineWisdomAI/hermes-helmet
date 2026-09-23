# Public source preview — September 23, 2026

Hermes Helmet is an Apache-2.0 coding workflow built around separate human and
agent identities. The worker implements under its own GitHub identity; the
Captain reviews and decides whether to merge. This public preview releases the
existing working core so others can use, inspect, and improve it.

## Included

- A Docker-hosted Hermes executor with separate credentials and checkout.
- Configurable Captain/worker identities, repository scope, budgets, and merge policy.
- GitHub issue intake, pull-request review, and repair of the same PR.
- Persistent single-issue and epic orchestration, status, and managed event waits.
- Setup and doctor commands, portable Captain skills, and optional company packs.
- Optional OpenViking working memory and FAVA Trails governed records.
- Tests, source-build instructions, Apache-2.0 license, and third-party notices.

## Use and limitations

Start with the [quickstart](quickstart.md). Install the CLI from this repository
or the wheel attached to the dated public release. The Python distribution and
CLI retain the name `hermes-helmet` and version `0.1.0rc1`; the public release tag
`preview-2026-09-23` distinguishes this source publication. The complete source
archive includes Docker configuration, tests, and documentation.

This is a preview, with working deployments behind it, rather than a claim of
universal installation coverage or fully unattended operation. The Captain host
must remain able to run or resume the workflow. A waiting command cannot wake an
application that has stopped executing.

Hosted ChatGPT integration varies with the available app tools and authentication.
Its full post-restart integration matrix remains incomplete. It is optional and
is not needed for the GitHub issue/review/repair workflow. A complete formal
five-task autonomy pilot has not been claimed.

Separate identities make access and authorship attributable; they do not make
arbitrary agent-generated code safe. Use a dedicated worker account, configure
its GitHub permissions, and review changes before granting merge authority.
A repository allowlist constrains Helmet's actions, not the token's capabilities
outside Helmet. Do not mount personal or Captain credentials into the worker.

## Container builds

The source includes a Docker build using the pinned upstream Hermes Agent
0.21.3 base. The existing base scans reported 439 high/critical findings on amd64
and 522 on arm64, inherited from that base, with no additional high/critical
identities attributed to the wrapper in the tested candidate. Those counts are
scan findings, not a claim that every finding is exploitable. They are a known
limitation of that pinned runtime; review the base before deployment.

This release publishes source and the Python CLI, not a prebuilt container image
or a stable 1.0 security claim. Private image promotion stays disabled in public
CI. Building locally does not remove inherited base risks. Container upgrades
and image distribution can proceed independently of this source release.

## Source and contributions

This is the public home for reusable Helmet development. It starts from the
reviewed core at source revision `f8e4fce009905fac2211a0cd2b72f578b49636ef`, with
publication documentation and repository/CI routing adjustments. Private company
history, configuration, credentials, and skill packs are not included.

Keep portable skills in `skills/`; wheels and installed folders are generated
outputs. Keep company-specific configuration in your own overlay. See
[CONTRIBUTING.md](../CONTRIBUTING.md) for contributing and
[SECURITY.md](../SECURITY.md) for vulnerability reporting.
