# Lesson registry

`lessons.json` is the intermediate store between a run and the reusable
engineering skill. Nothing enters `skills/open-source-engineering/SKILL.md`
without travelling through it.

```text
OBSERVATION -> HYPOTHESIS -> CANDIDATE_LESSON -> VALIDATED -> PROMOTED_TO_SKILL
                                                          -> REFINED / REJECTED
```

A lesson reaches `VALIDATED` only with evidence from at least two distinct runs,
at least one piece of evidence a machine or a human produced rather than an
agent's account of its own work, and at least one recorded validation result.
`mailman/knowledge/lessons.py` enforces those gates.

The registry is public, so it holds sanitized text only. Run identifiers are
fine. Raw agent output, target repository content, and local paths are not.
Private retrospectives stay under `.mailman/`.

The registry is edited by a person today. See
[`docs/decisions/0005-knowledge-flywheel.md`](../docs/decisions/0005-knowledge-flywheel.md)
for the design and the commands that do not exist yet.
