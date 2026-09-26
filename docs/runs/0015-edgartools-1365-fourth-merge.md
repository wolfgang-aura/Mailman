# 0015. edgartools #1365, the fourth merge upstream

Date: run 2026-09-24, filed 2026-09-26 11:37 UTC, merged 2026-09-26 12:33
UTC. Private run `20260924T222329Z-301fab`. Sanitized from local evidence.

The fourth patch a maintainer merged, and the first filed after asking on the
issue first. `dgunning/edgartools` carries it as pull request
[#1365](https://github.com/dgunning/edgartools/pull/1365), merged by the
maintainer as `0ec15fe`. Three commits under the operator's own name, pushed
from the user-owned fork `wolfgang-aura:mailman/issue-1337-r2`: +108 −37
across five files.

## Target and defect

`dgunning/edgartools`, MIT (2,749 stars on 2026-09-26). Issue
[#1337](https://github.com/dgunning/edgartools/issues/1337), an outside
report: `edgar_read` returned `holdings: None` for every 13F-HR while the
summary said `Total Holdings: 211`. `_extract_13f_section` tested a DataFrame
for truth, which raises, and the `except` logged at `DEBUG`. Reproduced at
base `e46440ef` by `repro_1337.py`: exit 1.

## What the harness did

| Stage | Result |
| --- | --- |
| Reproduction | exit 1 at the base commit, recorded before any agent ran |
| Primary and reviewer | Claude `claude-opus-5-5` in both roles, one review cycle |
| Issue comment, 2026-09-25 | the issue had no maintainer reply, so we offered the fix on the issue before filing |
| Maintainer reply, 2026-09-26 | "yes please, open the PR", reuse the #1136 row loop in `_get_fund_holdings` instead of a third copy |
| Revision, 2026-09-26 | by hand, not through `resume-review`: the loop moved to `base._holding_rows`, both tools call it; NaN-cell test added; touched-tests gate 148 passed |
| First CI push | `test-fast (3.13)` failed: the offline audit found no `fast` test in the changed, network-classed file |
| Second push | new class marked `@pytest.mark.fast`; CI green, 11 checks |
| Maintainer review | `APPROVED`; he ran it against Berkshire's Q2 2026 13F-HR himself |
| Upstream | merged as `0ec15fe` at 12:33 UTC |

## What this case settles

**Asking first works on an untriaged issue.** The handoff flagged the issue as
unanswered by anyone who speaks for the project. One comment with the
reproduction and the planned change got a yes, a design constraint, and a
merge 56 minutes after filing.

**The maintainer's constraint was the review.** He named the helper to reuse
before any code reached him. Doing that turned a second copy into a refactor
that also serves `edgar_ownership`, and the approval cited it.

## What this case found wrong with Mailman

- Rebasing the run branch onto upstream made `export-patch` diff against the
  pinned base and blame upstream commits on `author-identity`. Filed as
  [issue #136](https://github.com/wolfgang-aura/Mailman/issues/136).
- The touched-tests gate runs without markers and never ran edgartools'
  `check_offline_audit.py`, so the first push went red. Filed as
  [issue #137](https://github.com/wolfgang-aura/Mailman/issues/137).
- There is no command for revising a candidate on an issue comment before a
  pull request exists; `fetch-review` reads filed pull requests only. Noted in
  #136.

## Left with the maintainer

Two non-blocking notes in the approval: the header can say "30 of 211" when
`_holding_rows` skips an empty row, and the holdings count (securities) sits
next to the summary's "Total Holdings" (information-table lines) without a
label. He called them follow-ups; nothing was filed for them.

## Timeline

| UTC | Event |
| --- | --- |
| 2026-09-22 00:48 | #1337 opened by ic3guy |
| 2026-09-24 22:23 | run initialized at base `e46440ef` |
| 2026-09-24 22:33 | `ENGINEERING_COMPLETE` |
| 2026-09-25 10:35 | offer posted on #1337 |
| 2026-09-26 10:02 | dgunning: open the PR, reuse the #1136 loop |
| 2026-09-26 11:37 | #1365 filed from `wolfgang-aura:mailman/issue-1337-r2` |
| 2026-09-26 11:39 | `test-fast (3.13)` red on the offline audit |
| 2026-09-26 11:41 | `1acac03d` pushed with the `fast` mark; CI green |
| 2026-09-26 12:33 | `APPROVED` and merged as `0ec15fe` |
