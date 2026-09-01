# Project direction

Mailman will test interchangeable coding agents on real open-source software engineering work. A primary agent owns an issue from investigation through implementation and tests. An independent reviewer inspects the original issue, pinned repository state, actual diff, command evidence, and the primary report. Mailman then verifies important claims itself and stops for human review.

## Principles

### One engineer owns the task

Do not split ordinary engineering work among artificial personas. The selected primary agent keeps the context needed to understand the issue, reproduce it, find the cause, implement the smallest suitable fix, add tests, and explain the result.

### Review the repository state

The reviewer receives the candidate workspace and machine evidence, not just the primary agent's summary. Its verdict is `APPROVE`, `REQUEST_CHANGES`, or `REJECT`. Findings are evidence for the primary agent to address or rebut. Revision cycles stay bounded.

### Measure what the machine can observe

Mailman captures command arguments, working directory, exit code, output, duration, and timeout state. Test, lint, typecheck, and build claims should point to those records.

### Keep upstream under human control

Local cloning, branches, edits, tests, commits, patches, and pull request drafts are allowed within a run. Pushing, opening a pull request, commenting, closing an issue, or changing an upstream branch requires explicit human approval.

### Use real work as evaluation

Live open-source issues test whether the system produces useful work. Historical issues provide repeatable evaluation at a commit before the accepted fix. Historical evaluation must hide the accepted patch from the agent, then judge behavior, regression tests, scope, safety, and reasoning rather than patch similarity.

### Preserve every attempt

A run should retain the repository and issue identity, base commit, agent configuration, reports, diff, command evidence, reviewer verdict, human decisions, timing, usage where available, failures, and lessons. Failed runs matter because they explain changes to the system and skill.

### Distill lessons with provenance

The engineering skill should contain rules supported by run evidence. Each strong rule records why it exists, which runs support it, when it entered the skill, and whether later runs changed the conclusion. Generic coding advice does not belong there.

## Intended outputs

Mailman should eventually produce a working agent harness, useful open-source contributions, and a reusable engineering skill. The first complete loop is deliberately small:

```text
GitHub issue
  -> primary engineering agent
  -> candidate repository state
  -> independent model review
  -> at most one revision
  -> independent verification
  -> human review
```

Architecture should follow problems observed in recorded runs. Complex orchestration, dashboards, autonomous issue selection, and retrieval systems can wait.
