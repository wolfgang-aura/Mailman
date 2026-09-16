# 0013. securo #875, the second merge upstream

Date: run 2026-09-09, filed 2026-09-09 04:10 UTC, merged 2026-09-16 11:15
UTC. Private run `20260909T023511Z-2177ae`, hunt `20260909T023328Z-e5810b`.
Sanitized from local evidence.

The second patch a maintainer merged, and the first that went in as filed:
one commit, no maintainer re-land, no revision asked for. `securo-finance/securo`
carries it as pull request [#875](https://github.com/securo-finance/securo/pull/875),
merged by the maintainer as `651a238` with the one comment "Thanks for this
@wolfgang-aura. LGTM!". Commit `ecf218e` sits directly on base `2869ff4`
under the operator's own name.

## Target and defect

`securo-finance/securo`, AGPL-3.0, a self-hosted personal finance manager
(FastAPI, SQLAlchemy async, React). 3,206 stars, 38 human outside merges in
the fourteen days before the run, CodeRabbit on every pull request. Policy
stance `permitted_with_disclosure`, disclosure not required.

Issue [#830](https://github.com/securo-finance/securo/issues/830), an outside
report from 2026-09-04 with no maintainer reply when the run started: the
payees list endpoint returned 500 for any database holding a payee whose
`type` predates the current enum. `PayeeRead.type` was `Literal["person",
"company"]`, and persisted `transfer`, `employer` and `merchant` rows failed
response validation. Reproduced at the base commit in one second: three
`literal_error`s from Pydantic, exit 1.

The fix is a read-side `field_validator` in `backend/app/schemas/payee.py`
that maps anything outside the two literals to `None`, so create and update
stay strict. Ten lines, plus a 32-line API test that inserts all three legacy
values and asserts HTTP 200 with `type: null`.

## What the harness did

| Stage | Result |
| --- | --- |
| Reproduction | exit 1 at the base commit, recorded before any agent ran |
| Primary, Codex `gpt-5.6-luna` | exit 0, 2 files, +41 −1 |
| Reviewer, first pass | `REVISE`, verification `BLOCKED`: the reviewer's Python environment returned `Access is denied` and its fallback lacked Pydantic and pytest. No code finding. |
| Primary, second pass | same diff, reran the focused tests |
| Reviewer, second pass | `APPROVE` |
| Final verification | reproducer exit 0, 33 focused tests pass, ruff and ty clean |
| Status | `READY_FOR_HUMAN_REVIEW`, 2 review cycles, 80 minutes from init to decision |
| Upstream | CodeRabbit: no actionable comments, seven pre-merge checks passed, merge risk "minimal". CI green. Silent for seven days, then merged. |

The one revision cycle was harness friction, not engineering: the reviewer
could not run the supplied interpreter. The candidate that merged is the
candidate the primary wrote in its first pass.

## What this case settles

**The user-owned fork works.** ffn#328 (record 0009) was closed and re-landed
by the maintainer because an organization-owned fork blocks "allow edits by
maintainers". This run pushed to `wolfgang-aura:mailman/issue-830` and the
maintainer merged the branch directly. The fork was deleted the same day, per
the standing rule.

**An unanswered outside report is a risk, not a verdict.** Record 0012
(skfolio#316) made an untriaged issue a blocking question on the review
page, and that rule was right for skfolio, where the "bug" was a design
opinion. #830 was also untriaged, five days old, and merged. The difference is
the evidence class: a 500 with a one-second machine reproduction at the base
commit against a schema that plainly does not admit the stored data. The
question the page asks should be answered with that, and the answer here
would have been "the reproducer is the triage".

**The full-suite gap did not matter.** `decision.json` recorded that the
target's backend suite was not clean locally (temporary-database setup
errors outside the payee path). The target's own CI was the gate that
counted, and it passed. The gap was honest; it was not a reason to hold the
patch.

## What this case found wrong with Mailman

`target-intel.md` listed fourteen "automated rules the repository enforces".
All fourteen were CodeRabbit layout markers (`review_stack_entry_start`,
`walkthrough_end`, `tips_end`, `cr-comment:v1:<hash>`) from one comment
template. The repository enforces nothing through them, and a reader cannot
tell that from the page. Filed as
[issue #91](https://github.com/wolfgang-aura/Mailman/issues/91).

## Timeline

| UTC | Event |
| --- | --- |
| 2026-09-04 | #830 opened by an outside reporter, no maintainer reply |
| 2026-09-09 02:35 | run initialized at base `2869ff4` |
| 2026-09-09 02:52 | reproduction exit 1 |
| 2026-09-09 03:05 | reviewer `REVISE` on a blocked environment |
| 2026-09-09 03:34 | reviewer `APPROVE`, final verification exit 0 |
| 2026-09-09 04:10 | #875 filed from `wolfgang-aura:mailman/issue-830`; CodeRabbit clean within a minute |
| 2026-09-16 11:15 | merged by the maintainer as `651a238` |
| 2026-09-16 11:20 | provenance refreshed, `contributions` reads `MERGED`; fork deleted |
