# Fresh task context

Read this when setting up or checking a writing profile.

A writing card should start from its sources, guidance, the previous artifact and
the feedback, and nothing else. Native Hermes adds two unrelated things unless
told otherwise:

1. **Role history.** The task context lists the assignee's recent completed work
   on other tasks. Install the supported plugin to drop it from writing cards:
   `helmet write context-plugin PLUGINS_DIR`, then `hermes plugins enable
   helmet-writing-context` for the writing profile. It touches only cards
   created by `helmet write plan`.
2. **Persistent memory and retrieval.** Memory files and an external memory
   provider are profile settings in Hermes, not per-task. Give writing work a
   dedicated profile with built-in memory, user profile and any provider
   turned off. `helmet write profile-check CONFIG` reports each setting that is
   still on.

A new session id does not isolate either. Do not edit Hermes files or delete
global memory to get isolation.

Inspect what a worker will actually receive: `helmet write context BOARD TASK`
prints the task context from native Kanban, and `hermes prompt-size --json`
reports memory size for the profile. The role-history plugin acts on what the
worker reads at run time, so the printed context shows the native text; confirm
the plugin is enabled with `hermes plugins list`.
