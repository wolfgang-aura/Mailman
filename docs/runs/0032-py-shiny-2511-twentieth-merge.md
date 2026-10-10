# 0032. py-shiny #2511, the twentieth merge upstream

Date: filed 2026-09-29 12:26 UTC, merged 2026-10-09 15:05 UTC. Private run
`20260929T094158Z-9ec7bc`. Sanitized from local evidence and the public pull
request thread.

Merged as pull request [#2511](https://github.com/posit-dev/py-shiny/pull/2511),
merge commit `163c833`, ten days after filing, by schloerke.

## Target and defect

A render function returning `dict[str, int]` or `list[str]` failed pyright when
decorated with a `Renderer[Jsonifiable]`, because `Jsonifiable` spelled its
container arms as invariant `List[...]` and `Dict[str, ...]`. The PR switched
them to `Sequence` and `Mapping`, as the issue suggested. +29 -4 across 3 files.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-09-29 |
| Review | schloerke asked for a code comment on the `Sequence`/`Mapping` trade-off and a CHANGELOG tweak, and wrote that the change cuts both ways |
| Revision | merged `main` in (`d464b70`), then once more with the comment and CHANGELOG credit (`9ecacfd`); no rebase, no force-push |
| Merge | schloerke merged 2026-10-09 |

## What the maintainer changed

One commit, `ad7804b`, a merge of `main` into the branch. No code edits.

## Lessons

- The maintainer hesitated over the trade-off in a conversation comment, not in
  review threads. `fetch-review` misses those (Mailman #128), so the comment was
  read by hand. Documenting the trade-off where the type lives answered it.
- A `dirty` branch plus a CHANGELOG conflict resolved cleanly by following where
  the project now keeps unreleased entries.
