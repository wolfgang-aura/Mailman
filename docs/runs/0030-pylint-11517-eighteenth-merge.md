# 0030. pylint #11517, the eighteenth merge upstream

Date: filed 2026-10-01 03:07 UTC, merged 2026-10-08 18:50 UTC. Private run
`20260930T215739Z-794226`. Sanitized from local evidence and the public pull
request thread.

Merged as pull request [#11517](https://github.com/pylint-dev/pylint/pull/11517),
merge commit `1e5861d`, seven days after filing, by Pierre-Sassoulas.

## Target and defect

With scipy 1.17, `scipy.stats.entropy([1, 2, 3], nan_policy="omit")` reported
`E1123 unexpected-keyword-arg`. A pass-through decorator (`@xp_capabilities()`)
sat above a decorator whose wrapper takes `**kwds`, and
`_keyword_argument_is_in_all_decorator_returns` required every decorator return
to accept the keyword. The fix treats pass-through decorators as transparent. +151 -1
across 4 files.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-01 |
| Review | Pierre-Sassoulas requested changes twice: positional-only and rebound pass-through cases from a competing contributor's PR, then more coverage or removal of unreachable code |
| Revisions | `d884acd` through `a58b089`, including a merge of main (`660fdd3`) |
| Merge | approved at `877ee0f` and merged 2026-10-08 |

## What the maintainer changed

Two commits on top of ours: `6b91a27` (detect positional-only and rebound
pass-through decorators) and `877ee0f` (accept the keyword only when every
decorator return is uninferable). The final behavior is his narrower rule.

## Lessons

- The maintainer folded in test cases from another contributor's PR for the same
  issue and expected us to cover them. A duplicate-PR check at filing time found
  no open duplicate, but a PR opened after ours still shaped the review.
- Unreachable code was a review finding. The prompt should ask the agent to
  confirm each new branch has a test that reaches it.
