# Becoming Captain

> Give Hermes the wings to carry your plan through, under its own identity

Hermes Helmet is an open-source software factory from [Machine Wisdom AI](https://machine-wisdom.ai/). This guide is for you if you already work with a coding agent and have decided to try it. It covers who does what, what to prepare, and how your first issue moves from delegation to an accepted result.

## What you need before you start

This part covers what you decide, what you prepare, and how much of the setup your coding agent can do for you. The commands live in the maintained guides; this guide tells you which one to open and when.

Three actors take part, and the rest of the guide uses these names:

- **You are the Captain.** You set the goal, choose the identities, decide which repositories Hermes may touch, pick the worker's model provider, and decide how much authority to hand over.
- **Your coding agent becomes the first officer.** After you delegate, it coordinates the work, helps with setup, reviews the pull request, directs repairs and accepts the result within the authority you granted. It runs on your machine under your OS account and your GitHub credentials. If you want to question someone about a piece of work, you question this agent.
- **Hermes is the crew.** The Hermes agent runs in Docker under its own GitHub account. It implements, tests, opens pull requests and repairs them when review finds a problem. It never merges and never force-pushes.

Because the crew has its own identity, GitHub shows who did what: your first officer's reviews and merges appear under your login, and the worker's commits and pull requests appear under the worker's login. [Captain, first officer, and crew](captain-and-crew.md) describes the roles in more detail. You can use any planning tool you like; Hermes Helmet begins at delegation.

### What to have ready

- A coding agent you already use as first officer, such as Codex or Claude Code. The documented workflow has been exercised through Codex; Claude Code and Hermes have installation and static validation only. Choosing a different first officer does not change the worker's model.
- A second GitHub account for the worker, with its own token. It must be a different account from yours; preflight checks that the live worker login differs from your Captain login.
- Docker with Docker Compose v2, and Python 3.11 or later for the host `helmet` command.
- Access to a model provider for the worker.
- One GitHub repository you are willing to let the worker change, and one small, bounded issue in it.

This is real setup work, not a one-line install. You own the worker account, its token and the policy file, and you control who has access to each. The policy file holds no secrets, and both the runtime and your first officer must be able to read the copy they use.

### Choices that stay yours

These are three separate decisions. Changing one does not change the others.

1. **The first officer's host and model.** You choose them in that agent's own tooling.
2. **The worker's provider and model.** You set them on the worker, inside its runtime. By design, moving the worker to another provider keeps its GitHub account, leaves repository policy in force and leaves merge authority with the first officer. The host setup wizard's default provider probe expects an OpenAI-compatible endpoint; that requirement belongs to the wizard, not to your choice of worker provider, and a Claude Pro or Max subscription has its own worker guide. Start from [provider and model configuration](model-lanes.md) or [Claude subscription workers](claude-subscription.md).
3. **Authority.** One [authority policy](authority-schema.md) names both GitHub logins, the repositories the worker may work in, the labels that start work, the merge mode, and limits on time and repair rounds. By default the first officer merges after it has reviewed the exact current head, required checks pass and GitHub reports the pull request mergeable. Writing `Merge when clean: no` on a single issue or parent epic withholds merge on that work until you approve it. That is a limit you chose, not a routine checkpoint.

Two points about access. The worker's token determines what it can see and change on GitHub; the repository allowlist determines where Hermes Helmet dispatches work. A dispatch list does not narrow a token, so give the token only the access you intend. Setup records `selected` access by default, and extra private-repository visibility needs an explicit `broader` choice from you.

You can also record standing consent for sending an accepted issue's content to the worker's configured provider. That record is an instruction to the agents, not a network control, and it never covers secrets, scope changes, or changes to credentials or billing.

### What your first officer does during setup

Your first officer drives the `setup-helmet` skill, which runs the `helmet` commands for you. It collects only non-secret values: your company name and slug, your Captain login, the worker's login, the repository allowlist and labels, and the worker's provider and model. You can run the same commands yourself.

Secrets never go through your first officer. The worker token and the provider credential are entered with local, non-echoing input on your machine. Setup stores them below `~/.hermes-helmet/secrets/` in owner-only files, and they stay out of the model's context, command arguments, shell history, the rendered policy and logs. Do not paste a token into the chat.

Nothing in this part asks you to approve each step. You set the goal, the identities, the allowlist, the provider and the authority; after that the first officer carries the setup and, later, the work.

### Two setup paths, two jobs

The host wizard and the Docker bootstrap are separate, and neither stands in for the other.

- **The host wizard (`helmet setup`)** configures the machine where your first officer runs. It writes `~/.hermes-helmet`, your owner-only secrets and the bundled skills. It does not start Docker, and it does not mount its output into the worker's Compose stack. `helmet doctor` then checks that host. See [setup-helmet](setup-helmet.md).
- **The Docker bootstrap** starts the published worker image, clones the allowlisted repository into the worker's volume, creates the worker profile and completes its provider login. Follow the [quickstart](quickstart.md) for every command.

Keep their policy files apart unless the same repository paths are valid in both places. The host wizard expects checkouts on your machine; the worker's checkouts live inside its container, which host `helmet doctor` cannot see. The container user also needs to be able to read the policy it mounts, which the host wizard's owner-only file may not allow. The quickstart explains both.

The first-officer skills install separately, either with `helmet install-skills` or as a Claude Code or Codex plugin. A plugin supplies the first-officer skills and a host-specific bridge (a Codex MCP Bridge server for Codex, a mod for Claude Code); it does not provision Docker, tokens or provider login. See [First-officer plugin](first-officer-plugins.md).

### What you can leave out

Memory and company setup are optional, and a clean setup and doctor pass is documented with all of them declined.

- **OpenViking** (working context across sessions) and **FAVA Trails** (governed decisions and observations) are selected by default in setup, safe to decline, and decided in the answers file before setup writes the policy. A later conversation cannot enable them. See [OpenViking](openviking.md) and [FAVA Trails](fava-trails.md). Neither is needed for the first issue.
- **Company skill packs and private overlays** matter when you want your own instructions or company-specific Compose and policy kept in a private repository. See [company skills](company-skills.md) and [private overlay](private-overlay.md).
- **Extra skill packs** are not required. Matt Pocock skills and gstack are pinned recommendations that setup never installs on its own.

### Which release you are installing

Hermes Helmet 1.0.0 is a stable [GitHub release](https://github.com/MachineWisdomAI/hermes-helmet/releases/tag/v1.0.0), not a PyPI publication. Its notes list the runtime image for `linux/amd64` and `linux/arm64`. Publication does not install or restart any worker, so a release existing tells you nothing about what your container runs. After you start the worker, check `/opt/hermes-helmet/SOURCE_COMMIT` inside it. The [published worker image guide](runtime-image.md) owns the image reference, the update steps and the rollback steps.

## Write the first issue

Your first delegation is one issue, written down before anything starts. Ask your coding agent to help you shape it, then read it as the contract the work will be judged against. A good first issue states three things:

- **Outcome.** What should be true when the work is finished. Write it so a reviewer can check it.
- **Scope.** What the change covers and what it leaves alone. Keep it bounded; a small change is one issue.
- **Authority.** Whether the first officer may merge once review and required checks are clean. Merge defaults to `unattended_when_clean`. To hold the merge for yourself, put `Merge when clean: no` on its own line in the issue.

Creating the issue authorizes nothing. Work starts when you accept the plan and ask your first officer to carry the issue through. You decide the outcome, the scope and the merge authority; the first officer coordinates from there.

### If the project is larger than one issue

For a larger project, record a parent issue with bounded sub-issues and explicit dependencies. A child whose blockers are not complete is held back, and independent children run in parallel up to a limit (two by default). Before the first officer dispatches an epic, you accept the graph it reads from GitHub. If you later add a child, remove one or change a dependency, the epic pauses until you accept the refreshed graph. The [quickstart](quickstart.md) points to the maintained commands. For a retrospective explanation of how existing epic coordination works, see the [epic coordination Blueprint](https://github.com/MachineWisdomAI/hermes-helmet/blob/95fc2d7bb05118f8b78ecd34e74c5e1aa7569cd5/docs/blueprints/epic-coordination.md), pinned to a fixed commit. Starting with one issue lets you see the loop before you rely on it for a graph.

## Delegate once

You delegate by asking your first officer to carry the issue through. The [quickstart](quickstart.md) owns the exact command and its options, so this guide doesn't repeat them. After that, you don't relay between the agents. By design, the loop runs like this:

1. The first officer checks identities and policy, then records a root task for the issue. If a root task already exists for that issue, it adopts it instead of creating another.
2. Hermes takes the task and works in its assigned checkout under its own GitHub account. It implements, tests and opens a pull request, then reports the pull request's address.
3. The first officer finds that pull request and reviews the exact head commit from its own checkout, as the Captain.
4. If the review finds a consequential defect, the first officer posts a formal changes-requested review on GitHub. Hermes repairs the same pull request. The new head gets a fresh exact-head review of what changed and what it affects; evidence that still holds for the new head is reused, and the review does not start over from scratch.
5. When a head is reviewed clean, required checks are verified and merge is authorized, the first officer merges. The merge command is refused if the head has moved since review.

A review belongs to a specific commit: only formal reviews by the Captain login on the current head count. A failing check on its own does not start a repair; it holds the merge. A clean pull request gets no invented repair.

The two GitHub accounts must differ, and the Captain host holds only your profile.

## Inspect and correct as it runs

You can look at the work whenever you want. Your first officer can report where the issue stands from its checkpoint, a small record on your host kept next to GitHub's own record of reviews, heads and merge state. GitHub remains authoritative for the pull request itself, so you can read the issue, pull request, checks and reviews directly.

If you see something wrong, tell your first officer or comment on the pull request. A trusted review on the pull request can create one same-pull-request repair, and an explicit hold from the first officer stops merging on that head. Recording a blocker or closing a pull request changes the first officer's record; it does not cancel a worker that is already running.

If your first-officer session ends, the work does not depend on it. A later session reads the checkpoint and GitHub, adopts the existing task and carries on. Elapsed-time windows and repair-round limits keep counting from the original start; they limit the work and don't promise continuous execution.

If you use Codex, the [Captain's Bridge](https://github.com/MachineWisdomAI/hermes-helmet/blob/95fc2d7bb05118f8b78ecd34e74c5e1aa7569cd5/docs/blueprints/captains-bridge.md) can show a cited walkthrough of what a chat did; the linked Blueprint is a retrospective explanation of existing behavior. The Bridge interprets recorded chat and checks that its citations name real records. It does not look at the pull request or confirm delivery.

## Know when the work is actually done

Several events can look like "done", and they are different:

- **The worker reports done.** Hermes finished its task and published a pull request. That is a report, not acceptance.
- **The first officer reviews clean.** A formal Captain review on the current head says that head met the issue.
- **The pull request merges.** For an issue implemented in its own repository, the merge completes the work and the first officer's record moves to done. For a tracking issue routed to another repository, the tracking issue stays open until acceptance is recorded for the merged head.
- **Release and deployment.** Hermes Helmet's issue loop has no release or publish step, so a merge alone does not prove a release or a deployment. A release existing says nothing about what any host or container is running.

Accepted outcome is the first officer's job under the authority you delegated: it checks the merged change against the issue's outcome and says what it found. Status output and the Captain's Bridge report what was recorded; they don't independently verify delivery. You may inspect the merged change or your deployment yourself whenever it matters to you, but that is your option, not an extra gate the loop requires of you.

## Next step

Open the [quickstart](quickstart.md), have your first officer follow it for setup, and then delegate your first issue.
