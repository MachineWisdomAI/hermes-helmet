# Plan file

`helmet write plan BOARD plan.json` reads one JSON object:

```json
{
  "project": "q3-update",
  "format": "document",
  "title": "ExampleCo Q3 Update",
  "audience": "ExampleCo customers",
  "purpose": "Explain what shipped this quarter.",
  "guidance": "Plain sentences. Name the product as ExampleCo Widgets.",
  "coordinator": "COORDINATOR_PROFILE",
  "editor": {"assignee": "EDITOR_PROFILE"},
  "sections": [
    {
      "key": "shipped",
      "title": "What shipped",
      "assignee": "WRITER_PROFILE",
      "brief": "Describe what shipped in March.",
      "sources": ["ExampleCo shipped 12 widgets in March."],
      "input_files": ["notes.md"]
    }
  ]
}
```

- `project` and each `key` are lowercase letters, digits and dashes. They make
  the card idempotency keys, so re-running the plan reuses its cards.
- `format` is `document` or `deck`.
- `coordinator` is the profile that receives submissions. It must differ from
  every writer and from the editor.
- `input_files` are paths relative to the plan file, attached once as native
  artifacts to that section's card.
- Sections are created held and released only after their input files are
  attached, so a writer never starts with an incomplete source packet. If
  `plan` fails partway, rerun the same plan.
- A plan is not amended in place. Rerunning a changed plan for the same
  `project` fails with `plan_changed` and creates nothing. Replace one part with
  `helmet write supersede`, or plan a new `project` key.
