# 0045. trafilatura #1036, merged upstream

Date: filed 2026-10-28 01:56 UTC, merged 2026-10-30 08:09 UTC. Private run
`20261028T010726Z-f36619`. Sanitized from local evidence and the public pull
request thread.

Merged as pull request [#1036](https://github.com/adbar/trafilatura/pull/1036),
merge commit `e5fc5c7`, about 54 hours after filing, by adbar.

## Target and defect

Issue 1017: `extract_robots_sitemaps()` discarded any `robots.txt` over 10000
characters, so large sites returned no sitemaps. The patch reads the limit from
`MAX_FILE_SIZE` in the config and passes `find_robots_sitemaps()`'s config
through to it.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-28, one commit (`69f2d42`), from the rolling hunt |
| Review | None. The only comment on the thread is Codecov's report |
| Merge | adbar merged the filed head on 2026-10-30, and the issue closed with it |

## What the maintainer changed

Nothing. The merged tree is the head we filed.

## Lessons

- A confirmed bug with a one-commit patch and a config value the project
  already had merged with no review and no revision. The maintainer who owns
  the issue tracker also owns the merge button, which kept the wait short.
- The target reopens for new candidates now that the one-open-PR rule no longer
  holds it.
