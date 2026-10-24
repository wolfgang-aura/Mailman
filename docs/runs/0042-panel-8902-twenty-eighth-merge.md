# 0042. panel #8902, the twenty-eighth merge upstream

Date: filed 2026-10-23 02:29 UTC, merged 2026-10-23 13:48 UTC. Private run
`20261022T033329Z-c259af`. Sanitized from local evidence and the public pull
request thread.

Merged as pull request [#8902](https://github.com/holoviz/panel/pull/8902),
merge commit `3b8438a`, 11 hours after filing, by philippjfr.

## Target and defect

Syncing `value_throttled` as a query parameter did nothing and raised no
error. The fix raises when a constant parameter is synced with the URL.
Issue 7481. The patch touches `panel/io/location.py` (+11) and its test (+13).

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-23, from the rolling hunt, after the operator rewrote the body in his own words and ticked the two AI disclosure boxes |
| Review | philippjfr approved at 10:22 UTC with no change request |
| Revision | None |
| Red checks | `result:test` and the dataframe and tabulator tests failed on `Pandas4Warning` from `DataFrame.reset_index(inplace=...)`, in files the PR does not touch; `hunt watch` marked them inherited |

## What the maintainer changed

philippjfr merged `main` into the branch (`2ac700a`) and merged. The checks
that failed on our head passed on his.

## Lessons

- The inherited-failure rule worked. Three reads of a red PR said "nothing to
  do on our side", and the maintainer's own `main` merge fixed the checks.
- The curated-description requirement (holoviz asks for a human-written body)
  cost the operator a rewrite, and the PR merged without a question about it.
