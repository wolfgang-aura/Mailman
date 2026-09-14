# 0012. skfolio #316, filed on an unanswered report, closed as a design choice

Date: filed 2026-09-14 14:12 UTC, closed 14:28 UTC, closed out the same
hour. Private run `20260914T124125Z-a8c05d`, hunt `20260914T115814Z-08bc70`.
Sanitized from local evidence.

The warning from run 0011 fired. It fired in the wrong place.

## Timeline

| UTC | Event |
| --- | --- |
| 2026-09-10 04:09 | skfolio/skfolio#307 opened by an outside reporter, `author_association: CONTRIBUTOR`: docstring examples pass only under the test suite's NumPy print options |
| 2026-09-14 12:41 | run created; `claims` records `maintainer_replied: false`, zero comments |
| 2026-09-14 13:34 | `handoff` prints `UNTRIAGED ISSUE` to the coordinator's terminal |
| 2026-09-14 13:38 | `packet` renders: SEND, **0 questions**, 2 gaps. The warning is on no page |
| 2026-09-14 14:12 | #316 filed after operator approval of the packet |
| 2026-09-14 14:28 | the maintainer closes #316: the print options are deliberate, readability over copy-paste fidelity |

Sixteen minutes. The reproduction was machine-checked (15 examples fail at
the base commit under NumPy's defaults, 0 after), the review path reached
APPROVE, and none of it was the question. The question was whether the
project wanted the behaviour changed, and nobody at the project had been
asked. The issue had no reply in four days; the pull request got one in
sixteen minutes.

## Why the warning did not reach the decision

Run 0011 made `handoff` print the warning and left the decision to the
operator. The operator never sees `handoff`'s stdout; a coordinating agent
does, and the agent wrote a `decision.json` with no question about it. The
review page — the one artifact the operator decides from — said zero
questions. A warning printed where nobody decides is a log line.

## What changed

| Command | Behaviour |
| --- | --- |
| `decision --init` | when the claims record shows an unanswered outside report, seeds a complete blocking question (gate `untriaged-issue`): ask on the issue first, file now, or drop |
| `decision`, `review`, `packet`, `finalize-review` | refuse a decision file for such a run unless a question carries that gate; the page cannot say zero questions |
| `triage_warning` | moved from `handoff` to `claims`, where the fields it reads are written |

The skfolio run's own `decision.json` now fails validation, which is the
test that matters: the file the operator approved from would not have
rendered.

## What to carry forward

- **A warning is a question or it is nothing.** Anything the operator must
  weigh goes into `decision.json` through the validator, not to stdout.
- **Two closes from the same cause is a gate, not a lesson.** 0011 recorded
  the lesson; 0012 lands the gate.
- **Read the reporter as well as the report.** A polished outside issue with
  no maintainer reply is a proposal, however good its reproduction.
