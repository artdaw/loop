# Capability starter

This is a copyable **specification template**, not an installed Loop capability.
See [the extension contract](../README.md). The target scaffold command is:

```text
loop capability init my.checklist --role daily_life --mode agent --output ./my-checklist
```

For a manual start, copy this directory, rename the pack/operation IDs in
capability.yaml, and edit instructions.md, the two schemas, and examples/cases.yaml.
Keep dependencies/effects empty or local until real tools are needed. Add tools
by declaring their operation names and compatible versions; reuse shared runtime
ports and approval rules.

Then use the target validate/test/enable workflow. Metadata validation and example
schema checks can run without a model; actual instruction quality needs domain
evaluations. No Coordinator, scheduler, API or Telegram edits should be necessary.

This sample proposes checklist text. It does not create tasks, send messages,
fetch websites, or modify the vault. Add task.create only if that effect is part
of the new capability and its authority is clear.
