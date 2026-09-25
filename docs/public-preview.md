# Hermes Helmet public preview — September 23, 2026

Hermes Helmet is an Apache-2.0 software factory for teams using coding agents.
It connects accepted GitHub work through implementation, review, repair, and
authorized merge while preserving who did what.

## What ships

- A Docker-hosted Hermes worker with its own GitHub identity, credentials, and
  checkout.
- Configurable Captain and worker identities, repository scope, budgets, and
  merge policy.
- GitHub issue intake, pull-request discovery, trusted-review handling, and
  repair of the same pull request.
- Persistent single-issue and epic orchestration with resumable status and
  managed waits.
- `setup-helmet`, `helmet-issue`, and `helmet-epic` skills for the Captain-side
  coding agent.
- Setup and doctor commands, a minimum Docker Compose deployment, and
  user-facing verification on Linux and macOS.
- Optional company skills, OpenViking working context, and FAVA Trails governed
  records.

## Proven delivery loop

The workflow has delivered multi-issue changes through worker-authored pull
requests, Captain review, repairs on the same pull request, and authorized
merge. The worker account remains the implementation author; the Captain-side
agent coordinates, reviews, and merges only within delegated authority.

GitHub holds issues, commits, checks, reviews, and merge history. Hermes Kanban
and the worker ledger hold durable execution assignments. The two records are
linked rather than duplicated.

## Install

Start with the [quickstart](quickstart.md), or point a compatible coding agent
at the [`setup-helmet` skill](../skills/setup-helmet/SKILL.md).

Install from current source to use the `helmet` command:

```sh
git clone https://github.com/MachineWisdomAI/hermes-helmet.git
cd hermes-helmet
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
helmet --help
```

The dated [`preview-2026-09-23`](https://github.com/MachineWisdomAI/hermes-helmet/releases/tag/preview-2026-09-23)
release contains the `0.1.0rc1` Python package. Its original console command is
`hermes-helmet`; current source keeps that name as a compatible alias and adds
the shorter `helmet` command.

## What you provide

- Docker Compose and Python 3.11 or later.
- A separate GitHub account and token for the Hermes worker.
- A Captain-side coding agent such as Codex or Claude Code.
- Access to the model provider selected for the worker.
- A repository and an accepted GitHub issue to carry through the workflow.

The worker token controls what the worker can access on GitHub. The repository
scope in Hermes Helmet controls where it dispatches work. Keep Captain
credentials on the Captain side and worker credentials inside the worker
runtime.

## Extend the factory

The core issue-to-merge workflow does not require a particular project tracker,
memory service, or company skill pack. Keep company-specific configuration in a
[private overlay](private-overlay.md). Add [OpenViking](openviking.md),
[FAVA Trails](fava-trails.md), [company skills](company-skills.md), or separate
[model lanes](model-lanes.md) when they serve your deployment.

See [Captain and Crew](captain-and-crew.md) for the operating model,
[single-issue orchestration](helmet-issue.md) and [epic orchestration](helmet-epic.md)
for delivery, and [SECURITY.md](../SECURITY.md) for credential and vulnerability
reporting guidance.
