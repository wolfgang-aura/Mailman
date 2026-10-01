# 0021. PyPSA #1958, the tenth merge upstream

Date: run 2026-09-29, filed 2026-09-29 15:00 UTC, merged 2026-10-01 12:33
UTC. Private run `20260929T061042Z-90625b`, hunt `20260929T020844Z-f0e56`.
Sanitized from local evidence and the public pull request thread.

The tenth patch merged upstream and the first in `PyPSA/PyPSA`. It merged as
pull request [#1958](https://github.com/PyPSA/PyPSA/pull/1958), merge commit
`e62ae04`, about 45 hours after filing. The issue's reporter was also the
maintainer who approved and merged it.

## Target and defect

Issue [#1938](https://github.com/PyPSA/PyPSA/issues/1938), opened
2026-09-24 by FabianHofmann, a maintainer. `define_growth_limit` in
`pypsa/optimization/global_constraints.py` picked an asset's build period with
`active.cumsum() == 1`. The cumulative sum stays at 1 after the asset retires,
so an asset active in exactly one period was counted against the carrier
growth limit in that period and every later one. The issue proposed the fix:
`active & (active.cumsum() == 1)`.

Our change was that one line, a regression test in
`test/test_lopf_multiinvest.py`, and a release note. It came to +32 −1 across
3 files.

## What the harness did

| Stage | Result |
| --- | --- |
| Reproduction | `repro_1938.py` exit 1 at base `02bdcbba`, before any agent ran |
| Primary, Claude (`claude-sonnet-5-5`) | one-line fix, test, release note |
| Reviewer, Claude | two cycles, one revision; `APPROVE` on the second |
| Touched tests | `test/test_lopf_multiinvest.py`, 59 passed |
| Decision | `SEND`; the operator owned the short PR description, as PyPSA's template asks |
| Filing | 2026-09-29 15:00 UTC from `wolfgang-aura:growth-limit-build-period`, head `0205e282` |
| Upstream | FabianHofmann merged master into the branch (`1894116`), approved ("thanks!") at 11:35, merged at 12:33 UTC on 2026-10-01 |

## What the maintainer changed

Nothing in the patch. His only commit on the branch merged master in.
Provenance records `1894116` under `maintainer_commits`.

## Lessons

- An issue opened by a maintainer and carrying the fix in its body is the
  cheapest kind of target. The reproduction came from the issue's own example,
  and review had one question to settle: whether the proposed expression was
  right at the edges (an asset active in both periods, or built before the
  first one). It was.
- `hunt watch` read the row `merged` on its next run. `contributions` listed
  it as a merge with no run record, which is how this record came to be
  written.
