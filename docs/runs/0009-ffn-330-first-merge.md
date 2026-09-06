# 0009. ffn #330, the first merge upstream

Date: run 2026-09-03, merged 2026-09-06. Private run
`20260903T194455Z-140c59`. Sanitized from local evidence.

The first run whose patch a human sent upstream and a maintainer merged.
`pmorissette/ffn` carries the change as pull request
[#330](https://github.com/pmorissette/ffn/pull/330), merged as `fa6603a` on
2026-09-06 at 18:07 UTC with CI green across the Python 3.10 build and pandas
regression matrix. The merged history carries the operator's work under their
own name: commit `7cc05ab`, authored by `wolfgang-aura
<169568318+wolfgang-aura@users.noreply.github.com>`, sits directly on the
run's base commit `3e440f6` and is an ancestor of `master`.

## Target and defect

`pmorissette/ffn`, MIT, 2014, base commit `3e440f6`. There was no upstream
issue: the defect was found and written here while screening finance targets,
captured as a defect report through the same `fetch-issue` path a GitHub issue
takes. `calc_prob_mom(returns, other_returns)` subtracts a Series benchmark
from DataFrame returns directly, so pandas aligned the Series against the
frame's *columns* instead of its dates. The per-column call returned finite
probabilities (0.171, 0.193); the frame call returned a 252-element all-NaN
`ndarray` on the same data, reproduced at the base commit on CPython 3.14.3.

The same alignment defect had been fixed in `calc_information_ratio` by
upstream #310 and in `to_excess_returns` by #324. `calc_prob_mom` was the
remaining call site, and it consumes `calc_information_ratio`, so the two
functions disagreed on identical inputs.

## What the harness did

| Stage | Result |
| --- | --- |
| Reproduction | exit 1 at the base commit, as expected, recorded before any agent ran |
| Primary, Claude Opus 5 | exit 0, 2 files changed: `ffn/core.py` +14 −2, `tests/test_core.py` +31 |
| Verification after primary | exit 0 |
| Reviewer, Claude Opus 5 | exit 0, verdict `APPROVE` |
| Final verification | exit 0 |
| Status | `READY_FOR_HUMAN_REVIEW`, no revision, 1 review cycle, 39 minutes start to finish |
| `prepare-submission` | `"ready": true` against policy `permitted_with_disclosure` |

The one prior-art finding, upstream #312, was a merged pull request whose
merge commit is already an ancestor of the base commit — the reproduction
failed there, so it was not this change. Recorded as a non-blocking finding
with that reasoning, not waved through.

The fix itself was one line of intent: `returns.sub(other_returns,
axis="index")` instead of `-`.

## The path to merge, which took two pull requests

The human filed [#328](https://github.com/pmorissette/ffn/pull/328) from the
fork branch on 2026-09-04. The maintainer, Tim Paine, closed it the next day
with "the diagnosis and the fix are both right": he could not push to the
head branch because the fork was organization-owned, and GitHub's
"allow edits by maintainers" only grants push access on user-owned forks. He
re-landed it himself as #330 thirty seconds later, carrying the commit
unchanged and adding the mirror shape `series.calc_prob_mom(dataframe)`, a
shared `_diff_returns` helper, and one more test.

Two things came out of that hand-off:

- **A closed pull request was not a rejection.** Mailman's prior-art gates
  already treat closed attempts as acknowledgeable rather than blocking, on
  the reasoning that a close usually judges the approach. Here it judged
  neither the approach nor the code — it was a permissions detail — and the
  work merged anyway. The gates got that right without needing to change.
- **The org-owned fork cost a maintainer his afternoon.** He had to
  reconstruct the change on his own branch rather than push to ours. The
  session correction that forks go to the personal account predates this, but
  #330 is the receipt: the org route failed again, in the wild, at the hand
  of the maintainer who wanted to accept the work.

## The authorship question, answered by the maintainer

The commit on the fork carried the operator's personal email as author, a
local `git config` mistake, which would have entered ffn's history permanently
on merge. The operator asked the maintainer to rewrite the author before
merging. He did: what merged is `7cc05ab`, authored by `wolfgang-aura` with
the GitHub noreply address, and the personal email never reached ffn. The
message, the diff, and the work's attribution to its actual author all
survived. The lesson stands for every future run — check the author identity
of the exported branch before `handoff`, not after, because the maintainer
had to force-push a green branch to fix what a one-line local check would
have caught.

## What was not tested

The maintainer's added mirror shape and the DataFrame-against-DataFrame
return-type change (a bare `ndarray` became a labelled `Series`) were his
work, reviewed only by reading #330. The operator's local verification covered
the direction the run shipped.
