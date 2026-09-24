# Hermes Helmet

<p align="center">
  <img src="docs/assets/hermes-helmet-winged-mark-v1.png" width="320" alt="Hermes Helmet: a futuristic winged helmet with a glowing cyan visor and tether.">
</p>

**An open-source software factory for teams using coding agents.**

Hermes Helmet keeps delegated software work connected from a GitHub issue
through implementation, review, repair, and acceptance. Set the direction,
follow progress, and return when the work needs your judgment. The next step
continues from the task, branch, and review already recorded.

If your team maintains those handoffs with internal scripts and procedures,
Hermes Helmet gives you a shared workflow to operate and adapt. It builds on
[Hermes Agent](https://github.com/NousResearch/hermes-agent), Hermes Kanban,
and GitHub, with separate worker authority and a configurable model provider.

The name evokes the mythical helmet of Hermes, the Greek messenger god.
The product name is **Hermes Helmet**; the short CLI command is **`helmet`**.

## Follow one issue through review and repair

1. **Delegate a bounded issue.** Hermes Helmet links an eligible GitHub issue
   to a worker task in Hermes Kanban.
2. **Implement and open a pull request.** The Hermes worker writes and tests
   the change under its own GitHub account, then links the pull request to its task.
3. **Review the result.** The Captain reviews the current change from a separate
   checkout. A trusted review that requests a repair sends the worker back to
   the same branch and pull request, where it reads the feedback from GitHub.
4. **Accept under your policy.** The Captain checks the result and handles an
   authorized merge. A completed implementation task alone does not mean the
   pull request has been accepted.

For a small bug fix, you can follow the issue, worker assignment, pull request,
review, and any repair without reconstructing the task in a new conversation.
GitHub holds the code, review, checks, and merge record; the board shows worker
assignments. Larger efforts use dependent issues and bounded parallel work.

## Who runs the work?

You set the outcome and authority policy. The **Captain** is the coordination
and review role on your behalf: a coding-agent host runs the bundled
`helmet-issue` and `helmet-epic` skills. The **worker** is Hermes Agent running
in Docker with its own GitHub credentials and checkout. See
[Captain and Crew](docs/captain-and-crew.md) for the operating model.

Captain and worker use separate GitHub accounts. The worker implements,
opens pull requests, and repairs them; it never merges. The repository allowlist
controls where Hermes Helmet dispatches work. GitHub permissions determine
what each account can actually access. The worker never receives Captain
credentials. See the [authority configuration](docs/authority-schema.md).

The Captain host must remain able to execute or resume the workflow.
`helmet wait` follows changes; it cannot wake an application that has stopped.

## Try the public preview

The [September 23, 2026 source preview](https://github.com/MachineWisdomAI/hermes-helmet/releases/tag/preview-2026-09-23)
provides Apache-2.0 source, an installable CLI, and portable Captain skills.
The runtime builds from source. Read the
[preview notes and known limitations](docs/public-preview.md), including the
inherited container findings and current integration coverage.

You need Docker Compose, Python 3.11+, a separate GitHub account and token for
the worker, access to your chosen model provider, and a Captain host for
coordination and review. Install from the current source:

```sh
git clone https://github.com/MachineWisdomAI/hermes-helmet.git
cd hermes-helmet
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
helmet --help
```

The Python package remains `hermes-helmet`. Current source installs both
`helmet` and the compatible `hermes-helmet` command. The dated preview wheel
predates the short command and uses `hermes-helmet`.

Follow the [quickstart](docs/quickstart.md) to configure your identities,
repositories, model access, and Docker runtime. The ExampleCo fixture uses
`example-captain` and `example-agent`; replace these identities and its
repository entries with your own. Start with one small issue and follow its
linked task and pull request through review and acceptance.

The bundled Captain skills install into Codex, Claude Code, and Hermes.
The preview has been exercised through the Codex workflow; the other skill
targets have installation and static validation. See the
[single-issue guide](docs/helmet-issue.md) for commands and status fields.

## Choose the model, retain the workflow

The Hermes executor's provider and model are configurable. You can retain task
records, review practices, and authority policy while changing that choice.
Hosted providers and compatible local endpoints are documented in
[provider and model configuration](docs/model-lanes.md).

Local inference requires a model server and suitable hardware; the public
Compose stack does not start that server. Provider access, compute, retries,
and review contribute to operating cost.

## Add integrations when you need them

The core runs without a private company repository, private toolkit, or
external skill pack. These integrations are optional:

| Integration | What it adds |
| --- | --- |
| [FAVA Trails](docs/fava-trails.md) | Governed decisions and observations, with approval and lineage |
| [OpenViking](docs/openviking.md) | Working context across clients, using official memory plugins where supported; optional AGPL-3.0 service |
| [Company skill packs](docs/company-skills.md) | Adopter-owned instructions imported into Hermes-owned state |
| [Private configuration](docs/private-overlay.md) | Company identities, credentials, and deployment wiring outside the public source |

Promotion from working context into FAVA Trails is explicit. Captain skills
install separately from worker skill packs. Their canonical source is `skills/`;
the Python package includes those same assets.

## Develop and contribute

Try a bounded issue, then [report where setup or a handoff became unclear](https://github.com/MachineWisdomAI/hermes-helmet/issues).
Include the command, expected result, and observed behavior with secrets removed.
For code and documentation changes, follow [CONTRIBUTING.md](CONTRIBUTING.md).
Report vulnerabilities through the [security policy](SECURITY.md).

Run the repository checks before submitting a change:

```sh
scripts/verify.sh
```

Build a development image from the pinned Hermes Agent base:

```sh
scripts/build-dev-image.sh
```

The build records its source and image identity in `dist/image-identity.json`.
See [container candidate tooling](docs/release-candidate.md) for image evidence
and promotion requirements.

## Documentation

- [Setup and diagnostics](docs/setup-helmet.md)
- [Single-issue orchestration](docs/helmet-issue.md)
- [Dependent issues and epics](docs/helmet-epic.md)
- [Control loop](docs/control-loop.md)
- [Authority configuration](docs/authority-schema.md)
- [Public preview and limitations](docs/public-preview.md)
- [Changelog](CHANGELOG.md)
- [Code of conduct](CODE_OF_CONDUCT.md)

## License

Hermes Helmet is licensed under Apache-2.0. See [LICENSE](LICENSE) and
[NOTICE](NOTICE). OpenViking is an optional AGPL-3.0 service; enabling it is
an adopter choice.
