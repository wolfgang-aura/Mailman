# 0014. edgartools #1329, the third merge upstream

Date: run 2026-09-16, filed 2026-09-16 14:42 UTC, merged 2026-09-22 15:00
UTC. Private run `20260916T134234Z-ec068b`. Sanitized from local evidence.

The third patch a maintainer merged, and the first that went through a
maintainer's `CHANGES_REQUESTED` and came back approved. `dgunning/edgartools`
carries it as pull request
[#1329](https://github.com/dgunning/edgartools/pull/1329), merged by the
maintainer as `cb4b142`. Three commits, all under the operator's own name,
pushed from the user-owned fork `wolfgang-aura:mailman/issue-1218`: +361 −35
across five files.

## Target and defect

`dgunning/edgartools`, MIT, a Python library for reading SEC EDGAR filings
(2,737 stars on 2026-09-23). Issue
[#1218](https://github.com/dgunning/edgartools/issues/1218), an outside report
filed as the follow-up the maintainer asked for in his review of #1214: when
a filing falls back to role names, a role family splits across `notes()` and
`disclosures()`. In gahc's 10-Q, `ConvertiblePromissoryNotesPayable` was
`disclosure` while its four Tables and Details children were `note`.
Reproduced at base `35e3c324` by `scratch/repro_1218.py`: exit 1,
"role family split across [disclosure, note]".

## What the harness did

| Stage | Result |
| --- | --- |
| Reproduction | exit 1 at the base commit, recorded before any agent ran |
| Primary and reviewer | Claude `claude-opus-5` in both roles |
| First submission | `SEND`; gate command 43 passed; new tests fail without the source change (10 failed) |
| Maintainer review, 2026-09-20 | `CHANGES_REQUESTED`: the XBRL-only Notes builder went empty once the family moved to `disclosure`, and our end-to-end assertion locked that in. Three smaller asks: CamelCase segment boundary on the stem match, skip an empty family key, and the `-ies` plural gap. |
| Revision, 2026-09-21 | `fetch-review`, `build-prompts`, `resume-review`: reviewer `REVISE` twice, then `APPROVE`; `ENGINEERING_COMPLETE`. Six review cycles over the run's life. gahc `Notes.from_xbrl` 0 to 7, aapl 0 to 16. |
| Maintainer review, 2026-09-22 | `APPROVED`. He measured it himself: main gives 0 notes for aapl, tsla, unp and aeon; the PR gives 16, 12, 20 and 12. |
| Upstream | merged as `cb4b142` at 15:00 UTC, CI green |

## What this case settles

**A maintainer's change request is recoverable inside the harness.** The
revision path (`fetch-review` → `build-prompts` → `resume-review` →
`revision-response`) carried a real four-point review to an approval in one
push.

**The reviewer missed what the maintainer caught.** The first `APPROVE` did
not trace the reclassification into the downstream Notes builder. The
maintainer did, and the test we shipped asserted the broken result. A
reviewer prompt that asks "who reads this field after you change it" would
have asked the right question.

## What this case found wrong with Mailman

- `fetch-review` counted the four-point review as one requested change and
  cut the body at 4,000 characters, so the fourth point reached the primary
  truncated. Filed as [issue #126](https://github.com/wolfgang-aura/Mailman/issues/126).
- `handoff-check` blocked the revision push on `touched-tests-failed`: 83
  network tests failing with `IdentityNotSetError`, identical at base. Filed
  as [issue #127](https://github.com/wolfgang-aura/Mailman/issues/127).
- `hunt watch` listed the approval as an unanswered comment. Fixed in
  `119e98e` ([issue #130](https://github.com/wolfgang-aura/Mailman/issues/130)).

## Timeline

| UTC | Event |
| --- | --- |
| 2026-09-01 | #1218 opened as the follow-up to #1214 |
| 2026-09-16 13:42 | run initialized at base `35e3c324` |
| 2026-09-16 13:48 | reproduction exit 1 |
| 2026-09-16 14:42 | #1329 filed from `wolfgang-aura:mailman/issue-1218` |
| 2026-09-20 22:43 | dgunning `CHANGES_REQUESTED` |
| 2026-09-21 09:01 | revision `ENGINEERING_COMPLETE` |
| 2026-09-21 12:10 | revision `0fe9fa0` pushed, PR body replaced |
| 2026-09-22 12:00 | dgunning `APPROVED` |
| 2026-09-22 15:00 | merged as `cb4b142` |
| 2026-09-22 21:40 | provenance refreshed, `contributions` reads `MERGED` |
