# 0028. django-stubs #3685, the sixteenth merge upstream

Date: filed 2026-10-07 02:07 UTC, merged 2026-10-07 20:34 UTC. Private run
`20261006T181244Z-73d1c1`. Sanitized from local evidence and the public pull
request thread.

The sixteenth patch merged upstream and the first in `typeddjango/django-stubs`.
It merged as pull request [#3685](https://github.com/typeddjango/django-stubs/pull/3685),
merge commit `ef4b7c9`, about 18 hours after filing.

## Target and defect

A generic Django model whose type variable bound names a class defined later in
the file (`class SomeClass[T: "Something"](models.Model)`) crashed mypy with
`Cannot serialize PlaceholderType instance`. Our change makes
`process_model_class` defer while any type variable bound is still a
placeholder, unless it is mypy's final iteration. +69 -0 across 2 files
(`mypy_django_plugin/transformers/models.py` and
`tests/typecheck/models/test_metaclass.yml`), two commits of ours.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-07 02:07 UTC from `wolfgang-aura/django-stubs` |
| Maintainer review | sobolevn asked for tests that read the bound `T` back with `reveal_type`, and suggested a `skip` for Python below 3.12 |
| Revision | `c5214aa` reworked the test cases to check `T` with `reveal_type` |
| Approval and merge | sobolevn approved and merged the same day |

## What the maintainer changed

One commit on top of ours, `3e4fedd`, applying his own `skip: sys.version_info < (3, 12)`
suggestion to `test_metaclass.yml`. The plugin change merged as written.

## Lessons

- The review point was test strength. Our first cases showed the crash was
  gone but did not show that `T` still resolved to its bound. For a
  type-checker plugin, the prompt should ask for a `reveal_type` assertion on
  the thing the fix touches, not only for "no crash".
- PEP 695 syntax needs a version skip. The new-style case parses only on
  Python 3.12+, and our test did not say so until the maintainer added the skip.
