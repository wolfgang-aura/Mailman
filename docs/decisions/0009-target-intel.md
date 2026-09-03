# 0009. Read how a target hands out work, before a run is spent on it

Status: accepted on 2026-09-03. `mailman target-intel` is implemented and is a
blocking precondition in `check-target`. The lesson registry is seeded. Feeding
the record into the agent prompts is named future work below, not a claim.

## Decision

No run may start against a repository nobody has read. `mailman target-intel
RUN_ID` records how the target actually merges outside work, and `check-target`
refuses without a successful record. The refusal is not clearable by
`--acknowledge-prior-attempts`, because that flag answers a different question.

## Why this is a gate and not a habit

The 2026-09-03 session screened ninety candidate repositories by hand. It was
most of that session's cost, none of it was recorded, and the next session would
have skipped it. It nearly did: `langchain-ai/langchain` was dropped on the
previous session's conclusion rather than re-tested, and when the operator asked
why, re-testing showed it scored better on both screens than the target that was
actually chosen — 15 human outside merges in a fortnight against `encode/starlette`'s
4, and 53 unassigned open bugs against roughly none anywhere else.

A procedure that only runs when someone remembers it is not a procedure.

## What it reads, and why each screen exists

| Screen | What it answers | The case that produced it |
| --- | --- | --- |
| Freshness, human accounts only | Does this repository merge outside work at all? | `author_association` calls dependabot a `CONTRIBUTOR`. `PyCQA/bandit` scored a fresh outside merge on a bot bump and has merged no human outside pull request since May 2026, with roughly twenty sitting open. |
| Saturation, state-aware | Is there any open ground? | Counting any referencing pull request as a claim read langchain as saturated. Counting only open or merged rows as a claim changed 213 to 285. |
| Automated enforcement | What will close the pull request without reading it? | The bot leaves an HTML marker and states its rule in plain text. Against langchain this finds `require-issue-link` and `block-fork-main` and quotes both. |
| Merge path | How did the people who succeeded get the work? | Every langchain code fix that merged had its linked issue assigned to its author first, and one of those assignments went to somebody other than the reporter, which the recorded policy denied. |

The merge-path screen also stores the comment thread that preceded each merged
pull request, with the winning author's comments marked. That is the operator's
standing instruction made mechanical: never judge a target by its policy text,
read the pull requests that landed and the threads that won them.

## What it deliberately does not do

It does not decide. `check-target` blocks only on the record being absent or
failed; a target with zero outside merges warns. Every derived judgement is
reported as a count next to its denominator rather than as a verdict, because
the first version reported `assignment_looks_required: false` for langchain on
the strength of one documentation pull request merging unassigned, which is the
opposite of what the code fixes show. A ratio invites the follow-up question that
a boolean closes off.

## Compounding

`knowledge/lessons.json` was empty from the day the registry shipped. It now
holds six lessons written through the registry's own dataclasses, so the state
machine, the evidence weights and the promotion gates are enforced rather than
asserted. All six sit at `CANDIDATE_LESSON`. `VALIDATED` needs two distinct
supporting runs, and only the interpreter-resolution lesson has that much
evidence today; nothing was promoted on the strength of a single run.

## Future work, not claims

- The record is not yet in the agent prompts. A primary agent that knew the
  target's enforcement rules would not have to be told them.
- Nothing yet re-reads a target when a run ends, so a lesson cannot gain its
  second supporting run automatically.
- The reproduction gate, [#37](https://github.com/wolfgang-aura/Mailman/issues/37),
  is the other half of this decision and is still open: `target-intel` says
  whether a repository will take the work, and nothing yet says whether the bug
  is still there to fix.
