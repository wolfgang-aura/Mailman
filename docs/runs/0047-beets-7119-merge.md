# 0047. beets #7119, merged upstream

Date: filed 2026-10-28 09:44 UTC, merged 2026-11-01 12:03 UTC. Private run
`20261028T021116Z-8b7ab1`. Sanitized from local evidence and the public pull
request thread.

Merged as pull request [#7119](https://github.com/beetbox/beets/pull/7119),
merge commit `5ae86d1`, about four days after filing, by snejus.

## Target and defect

A failed metadata search logged a full traceback at error level. The patch logs
the failure without the traceback, adds a test in `test/test_metadata_plugins.py`
and a `docs/changelog.rst` entry.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-28, two commits after the operator pushed a `docstrfmt` rewrap of the changelog entry that CI's `Check docs` required |
| Review | snejus approved; codecov flagged two uncovered patch lines, no change request |
| Merge | snejus merged `master` into the branch (`0761a3d`) and merged the PR |

## What the maintainer changed

One merge commit of `master` into our branch. No code edits.

## Lessons

- The only red check was a changelog line-wrap rule the maintainer's tooling
  enforces. Reproducing `docstrfmt` locally before filing would have saved a
  push.
- Codecov patch coverage at 71% did not hold the merge.