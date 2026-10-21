# 0041. uproot5 #1741, the twenty-seventh merge upstream

Date: filed 2026-10-02 20:59 UTC, merged 2026-10-21 13:48 UTC. Private run
`20261001T222322Z-eab587`. Sanitized from local evidence and the public pull
request thread.

Merged as pull request [#1741](https://github.com/scikit-hep/uproot5/pull/1741),
merge commit `6c39a2a`, 19 days after filing, by ariostas.

## Target and defect

Reading a `TArray` member of a split `std::vector` without a header failed.
The fix reads those members. Issue 1529.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-02, with a test built on a sample file |
| Review | ariostas asked for three changes, then added the real sample to scikit-hep-testdata (#242) |
| Revision | Two points done in the workspace (`b8d268d`); the test switched to `uproot-issue-1529.root` once the release shipped (`085dd35`) |

## What the maintainer changed

ariostas pushed `f8437ec`, which tightens the test to check the NumPy output,
the awkward type and entry ranges, and merged `main` into the branch (`8c34e48`).
pre-commit-ci added `3d314ef`.

## Lessons

- The test data lived outside the repository. The merge waited on a separate
  testdata release, so a fix that needs a sample file can sit `BLOCKED` on
  someone else's release for weeks. The harness had no signal for that wait.
- The maintainer's test rework was about assertion depth, the same point as
  the django-oauth-toolkit and django-stubs records.
