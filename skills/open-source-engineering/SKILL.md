---
name: open-source-engineering
description: Investigate, implement, verify, and review a change in an unfamiliar open-source repository while preserving evidence and the human approval boundary.
---

# Open-source engineering

Own the selected issue through diagnosis, implementation, tests, and a concise evidence report. Work from the checked-out repository and pinned base commit. Read repository instructions before editing and treat machine-observed results as stronger than prior summaries or agent claims.

## Required behavior

Keep the patch focused on the issue. Reproduce a reported defect before fixing it when the environment permits. Inspect existing tests and contribution conventions before designing regression coverage. Record commands, results, limitations, assumptions, and unrelated failures.

Review the final diff as a maintainer would. Check issue fit, cause, edge cases, regression coverage, repository conventions, security, reliability, and unrelated changes. Do not claim a check passed without a captured successful result from the candidate repository state.

Stop at a human review package. Do not push, open or update a pull request, comment on an issue, or change an upstream repository without explicit human approval for that specific action.

## Evidence report

Report the issue interpretation, reproduction, cause, changed behavior, tests, command results, reviewer findings and dispositions, known risks, and unresolved assumptions. Separate facts observed in the workspace from inferences.

This skill starts deliberately short. Add strong rules only when run evidence shows that they change engineering decisions. Record those rules in [references/provenance.md](references/provenance.md).
