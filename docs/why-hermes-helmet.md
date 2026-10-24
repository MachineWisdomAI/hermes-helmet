# Why adopt Hermes Helmet

**Give Hermes the wings to carry your plan through, under its own identity**

Hermes Helmet is an open-source software factory from [Machine Wisdom AI](https://machine-wisdom.ai/) for builders who already work with coding agents and have a project they cannot avoid finishing. It carries an accepted GitHub plan through implementation, review, repair, and authorized merge, with controlled autonomy and Hermes working under its own identity. You keep the plan that holds your intent, one accountable first officer you can question about what was built, and a record on GitHub that you can inspect and correct.

## Why the project, not the agent, is the problem

If you already use a coding agent, you know where the time goes. The agent writes code quickly. You carry the work between steps: you assign the next task, read the pull request, relay the review comments, and decide whether merging is safe. Meanwhile the design you care about has to survive a large amount of arriving code, and noticing where the work has wandered from it is your job too.

That is the situation Hermes Helmet is built for. The [architecture overview](https://github.com/MachineWisdomAI/hermes-helmet/blob/95fc2d7bb05118f8b78ecd34e74c5e1aa7569cd5/docs/blueprints/architecture-overview.md) describes it as removing the relaying: you agree on a written plan with your coding agent, record it as GitHub issues, and delegate the authority to carry it through. The reasons below explain what that delegation leaves in your hands.

## Your written plan is the contract

The plan is where your architectural intent lives, so Hermes Helmet starts from it. You shape the next deliverable with your coding agent and record it as a parent issue with sub-issues and explicit dependencies. A small change can be a single issue. Writing the issues authorizes nothing. You accept the plan and state the outcome, the scope, and whether merge is authorized, and only then does work begin.

GitHub issues serve as the execution contract because they sit beside the repositories, branches, pull requests, checks, and reviews the work produces. Your planning tool stays your choice. Linear, Jira, GitHub Projects, or whatever you prefer can keep priorities and the roadmap, because Hermes Helmet [begins at delegation](https://github.com/MachineWisdomAI/hermes-helmet/blob/95fc2d7bb05118f8b78ecd34e74c5e1aa7569cd5/README.md). The agent that helped you write the plan then works from that same plan as your first officer.

A plan does not make an agent follow it. What it gives you is a stated intent that the implementation, the review, and your own later reading can all be measured against.

## One accountable first officer you can question

After you delegate, your coding agent becomes the first officer. In plain terms, it is the one agent that answers for the work. It coordinates the assignments, explains what was done, reviews each change, directs repairs, and accepts the result, all within the authority you gave it. You can ask it why a decision was made, and it is expected to have an answer, because it was the one reviewing.

The [identities and delegated authority Blueprint](https://github.com/MachineWisdomAI/hermes-helmet/blob/95fc2d7bb05118f8b78ecd34e74c5e1aa7569cd5/docs/blueprints/identities-and-delegated-authority.md) calls this the Captain's accountable counterpart. It is not a second human who approves every step, and it is not a separate GitHub identity. It runs inside a host such as Codex or Claude Code under your own operating-system account, and it reviews from a checkout separate from the worker's.

I call this responsibility the Directly Responsible Agent. The label is mine, and it names a responsibility rather than a new actor beside the Captain, the first officer, and the crew. It also does not mean one chat session that lives forever. The session is not what carries the work. Checkpoints on your machine and the records on GitHub let a later first officer pick the work up and adopt the existing task instead of starting another. Responsibility lasts across a long piece of work because the record does.

## A crew with its own identity, so the record says who did what

Hermes, the Nous Research agent, does the implementation. It runs in Docker under its own GitHub account and credentials. It writes the change, runs the tests, opens the pull request, and repairs that same pull request when review finds a problem. It does not merge and it does not force-push.

The practical effect shows up when you read a pull request. GitHub records the worker's commits and pull requests under the worker's login, and the first officer's reviews and merges under yours. The two accounts must differ, and your machine holds only your own credentials. The product's checks refuse to continue if the worker's login is found on the Captain host, and refuse a pull request the configured worker did not author. Review also belongs to a specific commit: only a formal review by the Captain login on the current head counts, and a merge is withheld if the head has moved since.

However, coding agents don't make this separation and they act as the user, regardless of whether they are running in the cloud or on the computer, directly or in a container. Here the author and the reviewer are different accounts, so the history tells you who did what without your reconstructing it.

## Work continues without you as the relay

When the first officer finds a consequential defect, it posts a formal changes-requested review on GitHub. The issue poller then creates one repair task, Hermes reads the review and corrects the same pull request, and the new head is reviewed afresh. A clean pull request gets no invented repair, and a failing check on its own does not start one. For a plan with dependent issues, independent children run in parallel, two at a time by default, and if the plan is rewired mid-flight the epic pauses until you accept the refreshed graph.

Your role in that is to set direction, accept the plan, decide merge authority, look at status whenever you want, correct what looks wrong, and close the parent epic. Under the merge authority you set, the first officer accepts and merges a pull request once its current head has a clean review and required checks pass, without a fresh routine approval from you. Merging is not releasing or publishing; those remain outside this guide. This is controlled autonomy: the Captain's authority is written down, and the work proceeds inside it.

## What this looked like on saga a2ui

Keeping the plan in writing means the design is still recognizable after agents have worked on it, and one first officer can be asked about it. Here is how that played out while I was building saga a2ui, an open-source framework from Machine Wisdom AI for building agentic apps with A2UI and MCP Apps.

**What I saw.** While I was building saga a2ui, agents departed from the intended architecture several times as they delivered a substantial design. I noticed because the first officer's explanations of the work contained leaky abstractions: responsibilities that the design assigned to one layer were being described as if another layer held them. I used [Captain's Bridge](https://github.com/MachineWisdomAI/hermes-helmet/blob/95fc2d7bb05118f8b78ecd34e74c5e1aa7569cd5/docs/blueprints/captains-bridge.md) to look at what the work had come to, and I judged it myself. The Bridge is a place to inspect an account of the work next to the records it cites. It does not detect architectural drift on its own; deciding whether an explanation matches the design remains the owner's judgment. What made the correction possible was that the first officer had the written plan, so I could ask it about the work against that plan instead of re-explaining the architecture from memory. That account is mine. The public record below shows what changed, not who noticed it or when.

**What the design said.** The [view-composition design](https://github.com/MachineWisdomAI/saga-a2ui/blob/7a78e7eae99285174a11c62d43c1f0035658ec96/blueprints/features/view_composition.blueprint.md) separates concerns plainly: the application states what its domain data means, and the SDK composes how it is rendered and delivered. The SDK holds no domain schema. In the design's words, "The application owns every domain schema and the whole-result logic." A neutral `DeliveredResult` type passes through the transport unchanged to the definition's single `result` reader, with no `classifyResult` override. That file is still marked as a draft awaiting the owner's decision at that commit. The approval is recorded afterwards, in the status text that [saga a2ui pull request 238](https://github.com/MachineWisdomAI/saga-a2ui/pull/238) and [pull request 240](https://github.com/MachineWisdomAI/saga-a2ui/pull/240) carry: the human owner approved the reviewed design at that commit. That approval covers the design only, not any code.

**Before.** The code had two competing readings of a tool result. The definition's reader looked only at the structured data. The MCP transport package rejected an error result before the application ever saw it, and a stack option named `classifyResult` replaced the reading whenever an application needed the whole result. So the transport package was deciding what an error result meant, which is exactly the kind of leak the design rules out: the meaning of a result was split across two mechanisms, and neither one was the application's alone.

**After.** [Saga a2ui pull request 240](https://github.com/MachineWisdomAI/saga-a2ui/pull/240), which gave the application one reader for the whole result, was merged as `3a37b200e44ba36fa785202d1b63498a4ac70be1`. It defines a neutral `DeliveredResult` in the core package, with the error flag, every content block in the order delivered, and the structured data left unchecked. The MCP mapping now converts a result into that value and, in the pull request's words, decides nothing. The application's single reader interprets the whole result, and the `classifyResult` option is removed from both stacks without a compatibility shim. In the example consumer, the error rule now sits in application code: the old function that received only the structured data became `readAnswerResult`, which checks `isError` itself.

I chose that one correction because it shows the mechanism: a responsibility the design gave to the application had drifted into a transport layer, and the fix moved it back. It is not the only one. [Pull request 238](https://github.com/MachineWisdomAI/saga-a2ui/pull/238), which aligned renderer failures and simplified Lit construction, merged separately as `6b2aabf8a73e47347f70ade99d7584227c6e943b`. It makes the React renderer report a failed update in the same shape as the Lit renderer, so changing renderer no longer changes error handling, and it lets an ordinary application create a Lit element without wiring up a session itself.

**What this does and does not show.** These two merged pull requests are public evidence that two specific points were corrected in source against the approved design. When they were written, they described other repairs, package publication, adoption by the repaired consumer, and acceptance in a named host as still open; that was their state then, and I make no claim about it now. The merges show source changes, not that any package, installation or host has accepted them. I am not claiming that Hermes Helmet finds drift for you. The claim is narrower. When an agent's work wanders from a plan, a written design gives you something to hold it against, an accountable first officer can answer from that design, and the resulting changes are public pull requests you can open and read for yourself.

After several weeks of using Hermes Helmet, and especially the most recent week, I trust it with more of my own work. That is my confidence, not a measurement and not evidence about your project.

## The choices you keep

Three choices stay separate, and changing one does not change the others.

The first officer's host and model are yours, chosen in that agent's own tooling. The documentation reports the first-officer workflow as exercised through Codex.

The worker's provider and model are set on the worker, inside its runtime. By design, moving the worker to another provider keeps its GitHub account, leaves your repository policy in force, and leaves merge authority with the first officer. The [worker runtime and model choice Blueprint](https://github.com/MachineWisdomAI/hermes-helmet/blob/95fc2d7bb05118f8b78ecd34e74c5e1aa7569cd5/docs/blueprints/worker-runtime-and-model-choice.md) presents this as the documented design and notes that no source tests a run with a changed model. Switching providers during an outage is a Hermes Agent matter, and I am not claiming Hermes Helmet does it for you.

Authority is a written policy that you own. It names the two logins, the repositories the worker may touch, and the limits on elapsed time and repair rounds. By default the first officer merges after reviewing the exact head with required checks passing, and any issue or epic can instead hold its merge for your explicit approval. You can also record your consent for accepted work to be sent to the worker's configured provider; that record guides the first officer and does not technically block data from leaving, and it never covers secrets, scope changes, or changes to credentials or billing. The [identities and delegated authority Blueprint](https://github.com/MachineWisdomAI/hermes-helmet/blob/95fc2d7bb05118f8b78ecd34e74c5e1aa7569cd5/docs/blueprints/identities-and-delegated-authority.md) holds the exact policy fields and wording.

Taken together, what you retain is source you can read and adapt, task and review records on GitHub, authority you set, and the freedom to change providers. That is a practical kind of independence. I am not promising zero cost or that every part of your setup will move to another provider unchanged.

## What adoption asks of you

The first officer is an agent you already use. The worker is a separate piece: it runs in Docker under a second GitHub account that you create and own, with its own token, a policy file, and a repository allowlist. The policy is yours, and a team can keep its policy and deployment settings in its own private repository without forking the core. This is real setup, and the argument for Hermes Helmet is not that it is quick. It is that the setup buys you a factory shaped like the one you were about to assemble yourself from agents, scripts, and manual handoffs.

If that trade fits a project you have to finish, you can see how Hermes Helmet works at [hermes-helmet.ai](https://hermes-helmet.ai/).
