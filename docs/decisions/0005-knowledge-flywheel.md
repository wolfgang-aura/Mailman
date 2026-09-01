# 0005. Turn each run into evidence through a knowledge flywheel

Status: accepted on 2026-09-02. The retrospective schema, the taxonomy, and the
lesson registry are implemented. Skill versioning, the regression suite, and
markdown ingestion are named future work below, not claims.

## Decision

Every Mailman run ends with a structured retrospective, and no rule reaches
`skills/open-source-engineering/SKILL.md` except along this path:

```text
experience -> evidence -> hypothesis -> validation -> reusable knowledge
```

The flywheel is four artifacts and one rule about who may write them.

| Artifact | Location | Written by | Public |
| --- | --- | --- | --- |
| Retrospective record | `.mailman/runs/<run_id>/retrospective.json` | Mailman | no |
| Retrospective template | `.mailman/runs/<run_id>/retrospective.md` | Mailman, then a human or agent | no |
| Lesson registry | `knowledge/lessons.json` | a human, from retrospectives | yes |
| Rule provenance | `skills/open-source-engineering/references/provenance.md` | a human, from validated lessons | yes |

Run data stays private because it quotes raw agent output. The registry and the
provenance file are curated, sanitized, and reviewed, so they are tracked. The
boundary is the one already stated in `SOURCE_OF_TRUTH.md`; the flywheel does
not move it.

## Mailman drafts, it does not conclude

`mailman retrospective RUN_ID` reads the run directory and writes both
retrospective files. It seeds only observations Mailman itself observed: a
failed verification, an agent process that timed out, an agent that exited zero
without a report, a reviewer verdict of `REVISE`, a missing or contradictory
verdict, and a run that reached `READY_FOR_HUMAN_REVIEW` without a revision.

It writes no opinion about why any of that happened. The remaining twelve
questions, including what went unusually well, which assumption caused the
outcome, what investigation was skipped, and whether the lesson generalizes,
are rendered as empty sections for a human or an agent to answer. The command
refuses to overwrite an existing retrospective without `--force`, because by
then the file may hold writing Mailman cannot reproduce.

The JSON file is the record. The Markdown file is the working surface. Parsing
completed Markdown back into the JSON record is future work; until then a
completed retrospective lives in the Markdown next to the seeded JSON.

## One taxonomy for successes and failures

`mailman/knowledge/taxonomy.py` holds the thirteen categories
(`ISSUE_UNDERSTANDING` through `MAINTAINER_ALIGNMENT`) as a `StrEnum`. The same
categories classify successes, failures, observations, and lessons, so a
recurring pattern can be counted across runs regardless of which channel found
it. An observation carries a category, an outcome, a scope, the assumption
behind it, and the evidence paths that support it.

Adding a category is a schema change and needs a reason from recorded runs.
Categories are deliberately about engineering behavior, not about the harness.

## Evidence channels are weighted, and the weights are enforced

A lesson is only as good as what found it. The channels carry explicit weights:

| Channel | Weight |
| --- | --- |
| `MAINTAINER_FEEDBACK` | 6 |
| `POST_MERGE_REGRESSION` | 6 |
| `AUTOMATED_VERIFICATION` | 5 |
| `HUMAN_REVIEW` | 5 |
| `PRIMARY_AGENT_FAILURE` | 4 |
| `REVIEWER_FINDING` | 2 |
| `AGENT_RETROSPECTIVE` | 1 |

A maintainer correction and a failing test are ground truth. A machine-observed
agent process failure is a fact about the run. A reviewing model's finding is an
independent opinion, which is worth more than nothing and less than a test. An
agent's own account of its work is the weakest evidence in the system, because
it is the only channel where the subject grades itself. The first promoted rule
in `provenance.md` exists because an agent reported success after doing nothing.

Machine-observed process facts about the reviewer, such as a reviewer that timed
out, are recorded under `AUTOMATED_VERIFICATION` rather than `REVIEWER_FINDING`.
They are harness observations, not reviewer opinions.

`OBSERVED_EVIDENCE_WEIGHT` is 4. Evidence at or above it was observed by a
machine or a human rather than asserted by a model.

## The registry states are gates, not labels

```text
OBSERVATION -> HYPOTHESIS -> CANDIDATE_LESSON -> VALIDATED -> PROMOTED_TO_SKILL
                                                          -> REFINED / REJECTED
```

`LessonEntry.transition` refuses an invalid move the same way `RunRecord`
refuses an invalid workflow transition, and it adds three content gates:

- `HYPOTHESIS` requires a written hypothesis.
- `CANDIDATE_LESSON` requires candidate guidance that would change behavior.
- `VALIDATED` requires evidence from at least two distinct runs, at least one
  piece of evidence at `OBSERVED_EVIDENCE_WEIGHT` or above, and at least one
  recorded validation result.

The two-run rule is what stops a single vivid run from becoming a rule. The
weight rule is what stops an agent's self-assessment from validating itself.
`REJECTED` is terminal: reviving a rejected idea means opening a new lesson with
the evidence that changed the answer, not quietly reopening the old one.

`REFINED` means the guidance was narrowed and has to re-earn its state, so it
leads back to `HYPOTHESIS` or `CANDIDATE_LESSON`.

## Core and conditional knowledge

Each lesson declares a `KnowledgeLayer`. `CORE` knowledge, repository
orientation, investigation discipline, root-cause reasoning, testing
principles, scope control, review, and verification, applies to every run and
carries no conditions. `CONDITIONAL` knowledge applies under stated conditions,
such as `python`, `typescript`, `monorepo`, or `concurrency`, and the schema
requires at least one condition for it. A conditional lesson cannot be promoted
into the core skill body without the conditions travelling with it.

An observation's `Scope` (`RUN_ONLY`, `REPOSITORY`, `ECOSYSTEM`, `UNIVERSAL`)
answers whether the lesson could generalize beyond this repository. It is the
input to the layer choice, not the same field: scope is about where the
observation was true, layer is about where the guidance will be applied.

## Provenance for every promoted rule

`SkillRuleProvenance` renders the YAML block that `provenance.md` already uses,
extended with the fields a promoted rule now has to carry: rule ID, date
introduced, skill version, motivating runs, the failure pattern addressed, the
expected behavioral change, validation results, and the lesson it came from.
The file stays human-curated. Mailman renders the block; a person decides
whether the rule belongs in the skill.

## Versions recorded on every run

A retrospective records `skill_version`, `primary_prompt_version`,
`review_prompt_version`, `orchestrator_version`, and the primary and reviewer
models. Prompt versions are the SHA-256 digests of the exact prompt files the
run used, taken from `prompts/` in the run directory, which is the same
digest-pinning idiom `toolchain.json` already uses. `orchestrator_version` is
the package version.

`skill_version` is read from a `version:` key in the skill front matter and
records `unversioned` until that key exists. Recording the field now means a run
retrospective written before skill versioning lands still says so honestly
instead of omitting it.

## What is implemented now

- `mailman/knowledge/taxonomy.py`, the categories, outcomes, channels, weights,
  scopes, and layers.
- `mailman/knowledge/retrospective.py`, the observation and retrospective
  schema, the twelve template sections, and the Markdown renderer.
- `mailman/knowledge/lessons.py`, the lesson entry, the gated state machine, the
  registry with atomic writes, and the provenance renderer.
- `mailman/knowledge/collect.py`, which drafts a retrospective from a run
  directory.
- `mailman retrospective RUN_ID`.

## Future work

- Parse a completed retrospective Markdown file back into the JSON record, and
  add `mailman lesson` commands so the registry is not edited by hand.
- Version the engineering skill and record that version in the run record at
  `init-run` time rather than reading it at retrospective time.
- Build the skill regression suite: keep historical issues as fixtures, replay
  the relevant ones with and without a proposed rule, and require the comparison
  as the validation result that unlocks `VALIDATED`.
- Aggregate categories across runs so a recurring pattern is visible without
  reading every retrospective.

Nothing above is evidence that the flywheel improves runs. No lesson has been
promoted through it yet, and the first promotion is the acceptance test.
