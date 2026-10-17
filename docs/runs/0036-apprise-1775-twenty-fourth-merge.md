# 0036. apprise #1775, the twenty-fourth merge upstream

Date: filed 2026-10-07 07:15 UTC, merged 2026-10-16 20:17 UTC. Private run
`20261007T030302Z-a2ede4`. Sanitized from local evidence and the public pull
request thread.

Merged as pull request [#1775](https://github.com/caronc/apprise/pull/1775),
merge commit `df1e5f0`, nine days after filing, by caronc.

## Target and defect

`AppriseLocale.detect_language()` reads `LANGUAGE`, `LC_ALL`, `LC_MESSAGES` and
`LANG`. The translation tests cleared `LANGUAGE`, `LC_ALL`, `LC_CTYPE` and
`LANG` but not `LC_MESSAGES`, so a shell with `LC_MESSAGES=C.UTF8` leaked into
them and they never reached the mocked locale. The fix clears `LC_MESSAGES` in
the test setup. +23 -15 in one test file.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-07, one commit of ours |
| Review | No review and no conversation comment from a person; codecov only |
| Maintainer commit | caronc pushed `d191e33` ("LANGUAGE= too") on top of our commit |
| Merge | caronc merged the PR with both commits, 2026-10-16 |

## What the maintainer changed

One extra commit that sets `LANGUAGE=` in the same tests. Our commit kept its
change as filed.

## Lessons

- The issue named only `LC_MESSAGES`, but the same leak exists for any variable
  the locale code reads. The maintainer closed the other hole himself. The
  harness could have listed every variable `detect_language()` reads and
  cleared all of them in one pass.
- A maintainer commit on top with no comment is the usual shape of a small
  accepted fix here. `hunt watch` flagged it as "not pushed by us" for nine
  days, which was correct and needed no action.
