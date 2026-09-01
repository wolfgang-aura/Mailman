# Rule provenance

This file records lessons promoted into `SKILL.md`. Do not add generic advice or rules based on one unexamined preference.

For each rule, record:

```yaml
rule: A concrete instruction that changes agent behavior.
reason: The observed failure or success pattern.
evidence:
  - run-id
introduced: skill version or commit
follow_up: How later runs supported, narrowed, or contradicted the rule.
```

```yaml
rule: Treat agent exit code and report presence as transport facts, then judge the candidate state and independent verification.
reason: The first Codex fixture process exited zero and wrote a report after every repository command was blocked. It made no patch and the regression test still failed.
evidence:
  - docs/runs/0001-codex-fixture.md
introduced: skill v0.1 on 2026-09-02
follow_up: A corrected adapter produced the patch, and a later toolchain-backed run passed both agent-side and host-side verification.
```
