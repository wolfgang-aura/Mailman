# 0031. Python-Markdown #1648, the nineteenth merge upstream

Date: filed 2026-10-02 20:39 UTC, merged 2026-10-08 17:03 UTC. Private run
`20261001T232940Z-89206e`. Sanitized from local evidence and the public pull
request thread.

Merged as pull request [#1648](https://github.com/Python-Markdown/markdown/pull/1648),
merge commit `0bf535b`, six days after filing, by waylan.

## Target and defect

A raw HTML comment on its own line inside an inline element was always treated as
a block, so `<span>\n<!-- comment -->\n</span>` rendered as
`<p><span></p>\n<!-- comment -->\n<p></span></p>`. `HTMLExtractor` now keeps a
stack of raw inline tags open in the current paragraph, and `handle_comment`
treats the comment as inline only when one of them closes later in the same
paragraph. +202 -1 across 4 files.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-02 |
| Review | waylan reported a merge conflict after `master` moved |
| Revision | merged `master` into the branch twice (`d7fa2bb`, `b58cdef`); tests passed (133) |
| Merge | waylan merged 2026-10-08 |

## What the maintainer changed

Nothing. All four commits on the merged branch are ours.

## Lessons

- This project asks for merges of `master` into the branch rather than rebases,
  and the conflict came from a playground commit unrelated to our files. The
  `behind`/`dirty` rule in the PR follow-up skill (read CONTRIBUTING, then merge,
  never rebase) handled it without a force-push.
