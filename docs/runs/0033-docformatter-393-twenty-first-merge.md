# 0033. docformatter #393, the twenty-first merge upstream

Date: filed 2026-10-07 12:48 UTC, merged 2026-10-09 23:00 UTC. Private run
`20261007T085752Z-c351b5`. Sanitized from local evidence and the public pull
request thread.

Merged as pull request [#393](https://github.com/PyCQA/docformatter/pull/393),
merge commit `00b23e7`, two days after filing, by weibullguy.

## Target and defect

A nested function or class with a one-line docstring lost the blank line that
ruff and black expect before the next statement of the enclosing scope, and
`--blank` gave a nested class two blank lines. The two formatters then undid each
other. `_get_function_docstring_newlines` and `_get_class_docstring_newlines` now
compare the next line's indent with the docstring's. +28 -1 across 2 files.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-07, one commit with a round-trip test |
| Review | none; no review or comment from a maintainer |
| Merge | weibullguy merged `master` into the branch twice (`b94098f`, `258cfc6`), then merged the PR |

## What the maintainer changed

Nothing in the code. Both extra commits are merges of `master`.

## Lessons

- A merge with no review at all. The run needed no revision; the one failure
  mode, a branch behind `master`, was fixed by the maintainer.
- The merge came 2 days after filing, so a narrow fix with a test that
  round-trips real input is the cheapest kind to review.
