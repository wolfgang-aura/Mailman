# 0017. nilearn #6611, the sixth merge upstream

Date: run 2026-09-28, filed 2026-09-28 13:25 UTC, merged 2026-09-29 07:53
UTC. Private run `20260928T095530Z-2005c8`, started inside hunt
`20260928T094000Z-3b91d9` and filed after that hunt's deadline. Sanitized
from local evidence and the public pull request thread.

The sixth patch merged upstream, about 18 hours after filing. `nilearn/nilearn`
carries it as pull request [#6611](https://github.com/nilearn/nilearn/pull/6611),
merged by Remi-Gau as `a01cefe`. Two commits under the operator's name from
the fork branch `wolfgang-aura:mailman/issue-6607`, then one maintainer
commit on top: `a201c0a`, "Apply suggestion from @Remi-Gau". +28 −3 across
six files at merge.

## Target and defect

`nilearn/nilearn`, 1,441 stars and 28 human outside merges in the 45 days
before the run, per `target-intel.md`. Policy stance
`permitted_with_disclosure`, disclosure required, linked issue required, and
the repository's `AGENTS.md` asks for the PR template's AI checkbox, a
`[FIX]` title prefix and `@pytest.mark.ai_generated` on generated tests.

Issue [#6607](https://github.com/nilearn/nilearn/issues/6607) was filed by
Remi-Gau, a collaborator, at 05:52 UTC on the day of the run, labelled
`Priority: high` and `GLM`, with a question to bthirion asking him to
confirm the bug. `OLSModel.fit` divided the residual sum of squares by
`n - n_columns` while `df_residuals` already used `n - rank`. Every redundant
design column inflated the variance estimate, so t and F statistics came out
too small. Remi-Gau's audit issue
[#6610](https://github.com/nilearn/nilearn/issues/6610) listed fixing #6607
as its first step and proposed no code for it.

`repro_6607.py` reproduced it at base `6e108e6c`: a `SecondLevelModel`
two-group design with a redundant intercept gave median t over
`scipy.stats.ttest_ind` of 0.9770, the full-rank design 1.0000, exit 1 with
`BUG REPRODUCED`. Machine-checked before any agent ran.

The fix is one line in `nilearn/glm/regression.py`: divide by
`self.df_residuals`. The diff adds a regression test parametrized over
`OLSModel` and `ARModel` with a redundant column, and a changelog entry.

## What the harness did

| Stage | Result |
| --- | --- |
| Reproduction | exit 1 at the base commit, ratio 0.9770 |
| Primary and reviewer | Claude `claude-opus-5-5` in both roles |
| Primary | about 73 seconds; 3 files, the one-line fix |
| Reviewer | `APPROVE` on the first pass, required changes none, 1 review cycle |
| Final verification | five GLM test files, 192 passed at base, 194 with the candidate |
| First decision, 10:35 | `HOLD`, run status `READY_FOR_HUMAN_REVIEW` |
| Mailman repairs | lint ran black from a hook dependency (#144) and touched-tests collected example scripts (#145), fixed in `a5fd112`; a library-wide sweep file timed out (#148), fixed in `0caf9b4` |
| Decision rewritten, 12:15 | `SEND`; ruff clean; touched-tests 514 passed, 2 skipped across 16 GLM test files |
| Filed, 13:25 | from the fork branch; changelog cited the issue number |
| Second commit, 13:30 | `7046030`: changelog cites #6611 and credits the author, who is added to `AUTHORS.rst`, `CITATION.cff` and `doc/changes/names.rst` |
| Upstream CI on `7046030` | 39 passed, 5 skipped, 2 failed, both "flaky tests" jobs; Codecov: all modified lines covered |
| Maintainer confirmation, 21:15 | bthirion on #6607: "This is True", with advice against rank-deficient designs |
| Approvals, 2026-09-29 | bthirion `APPROVED` ("LGTM, thx."), then Remi-Gau |
| Maintainer commit | Remi-Gau suggested and committed the removal of `@pytest.mark.ai_generated` from our test |
| Upstream | merged as `a01cefe` at 07:53 UTC, #6607 closed with it |

## What this case settles

**A maintainer-filed issue is the fastest path to a merge.** Remi-Gau filed
#6607 four hours before the run started. The run went from init to an
approved, verified candidate in ten minutes, and the maintainers merged it
the next morning. The one open question on the review page was whether to
wait for bthirion's confirmation. We filed without it; he confirmed eight
hours later and approved.

**Mailman's gates held the patch, not the patch.** The engineering passed at
10:05. Mailman held the run for another two hours on three of its own gate
defects, not on anything in the candidate. Each was fixed in Mailman
code before the decision changed to `SEND`.

**The maintainer finished the policy bookkeeping.** We marked the new test
`ai_generated`, as nilearn's `AGENTS.md` asks. Remi-Gau removed the marker
in a suggestion he then committed himself, 23 seconds before merging. The
record does not say why.

## What this case found wrong with Mailman

- `mailman provenance 20260928T095530Z-2005c8` now refuses: the branch
  "points at a201c0a9…, but this workspace ends at 704603032…". It blames a
  force-push or a moved workspace. Neither happened. The maintainer pushed a
  fast-forward commit onto our branch. Provenance cannot be re-recorded for
  a pull request a maintainer finished. `submission/provenance.json` still
  holds both our commits, and `mailman contributions --refresh` still reads
  `MERGED` with merge commit `a01cefe`.
- The hunt never counted this pull request. Hunt `20260928T094000Z-3b91d9`
  asked for 2 runs with a deadline of 11:40 UTC. The deadline passed while
  the run waited on the gate repairs. `mailman claims` then exited 2 with
  `deadline expired`, so the stale duplicate evidence could not be refreshed
  and handoff could not pass (#149, fixed in `c315009`). The pull request was
  filed at 13:25. The hunt's checks at 13:59 and 14:16 still showed 0 ready,
  with this run at stage `export`, disposition `REPAIR`. The hunt was
  abandoned as stale at 22:01 and `mailman hunt finish` never ran. The filing
  went through `prepare-submission` and a fresh duplicate acknowledgement,
  but not through the hunt's exit-code gate.
- Five Mailman issues put visible cross-references on the upstream threads:
  #144, #145 and #146 on #6607 at 10:48, before anything was approved for
  filing, and #149 and #151 on #6611. That is the pattern the
  code-span rule for upstream references, written on 2026-09-29, now forbids.
- On 2026-09-28, while #6611 was open, `mailman provenance` reported
  `fork deletion: safe` because a patch was on disk. Following it would have
  closed the pull request. Filed as #151, now closed.

## Timeline

| UTC | Event |
| --- | --- |
| 2026-09-28 05:52 | #6607 opened by Remi-Gau, `Priority: high` |
| 2026-09-28 06:00 | Remi-Gau asks bthirion to confirm |
| 2026-09-28 09:40 | hunt `20260928T094000Z-3b91d9` initialized, deadline 11:40 |
| 2026-09-28 09:55 | run initialized at base `6e108e6c` |
| 2026-09-28 10:02 | reproduction exit 1, ratio 0.9770 |
| 2026-09-28 10:05 | reviewer `APPROVE`, final verification 194 passed |
| 2026-09-28 10:16 | #6610 opened by Remi-Gau, lists #6607 as step 1 |
| 2026-09-28 10:35 | decision `HOLD` |
| 2026-09-28 10:36 | commit `9e9fb82` |
| 2026-09-28 11:40 | hunt deadline passes |
| 2026-09-28 12:15 | decision `SEND` after Mailman `a5fd112` and `0caf9b4` |
| 2026-09-28 13:23 | Mailman `c315009` lets freshness checks run after the deadline |
| 2026-09-28 13:25 | #6611 filed from `wolfgang-aura:mailman/issue-6607` |
| 2026-09-28 13:30 | commit `7046030`, changelog and author credit |
| 2026-09-28 21:15 | bthirion confirms the bug on #6607 |
| 2026-09-28 22:01 | hunt abandoned as stale, 0 ready |
| 2026-09-29 07:40 | bthirion `APPROVED` |
| 2026-09-29 07:52 | Remi-Gau commits `a201c0a`, removing the `ai_generated` marker; `APPROVED` |
| 2026-09-29 07:53 | merged as `a01cefe`; #6607 closed |
