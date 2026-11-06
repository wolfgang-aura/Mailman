# 0049. awkward #4398, merged upstream

Date: filed 2026-10-01 21:02 UTC, merged 2026-11-05 15:36 UTC. Private run
`20261001T170737Z-995068`. Sanitized from local evidence and the public pull
request thread.

Merged as pull request [#4398](https://github.com/scikit-hep/awkward/pull/4398),
merge commit `60bc14d`, about five weeks after filing, by ianna.

## Target and defect

`to_arrow` on a union whose option-type children are longer than the used
length failed. The patch changes `src/awkward/contents/unionarray.py` and adds
`tests/test_4228_to_arrow_union_option_child_length.py`.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-01, one commit (`fc54744`); tests moved into a new file at the project's request |
| Review | ianna approved, then waited on her own docs fix (awkward PR 4424, merged 2026-10-12) before asking for a rebase |
| Follow-up | 2026-11-05 the operator merged `main` into the branch (`465ace8`, no conflicts, 10 tests passed) |
| Merge | ianna merged the PR on 2026-11-05 |

## What the maintainer changed

Three commits by ianna on our branch: two merges of `main` and
`b28f85a`, which adds a test for repeated masked union indices in the Arrow
conversion. The source change stayed as filed.

## Lessons

- The review took five weeks because the approver was blocked on a docs
  workflow change, not on the patch. The wait needed no action from us.
- A nightly failure on the same area (TaiSakuma, 2026-11-05) came from a
  test-only predicate, fixed in the maintainers' own PRs 4494 and 4495. It did
  not hold the merge.
