# 0050. strawberry-django #971, merged upstream

Date: filed 2026-10-07 07:15 UTC, merged 2026-11-09 18:41 UTC. Private run
`20261007T034216Z-7a77a5`. Sanitized from local evidence and the public pull
request thread.

Merged as pull request [#971](https://github.com/strawberry-graphql/strawberry-django/pull/971),
squash commit `b8ee313`, about five weeks after filing, by bellini666. It
shipped in release 0.96.2 the same minute.

## Target and defect

The debug toolbar middleware dropped its payload on batched GraphQL requests
(issue 786). The patch changes
`strawberry_django/middlewares/debug_toolbar.py` and the toolbar template, adds
`tests/test_debug_toolbar_middleware.py` cases and a `RELEASE.md`.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-07, one commit (`dbec457`) |
| Review | Sourcery left two comments at filing; bellini666 asked on 2026-11-07 whether they were valid |
| Follow-up | Revision `1a577b3`: batched introspection skips the payload, the template unwraps arrays, two new tests. The operator pushed it and replied on 2026-11-08 |
| Merge | bellini666 approved and squash-merged on 2026-11-09 |

## What the maintainer changed

Nothing. Both commits on the branch are ours, and the squash kept them as
filed.

## Lessons

- Both Sourcery comments were real bugs in the first commit, a JSON array body
  breaking `.get("operationName")` among them. The maintainer waited a month,
  then asked us to check the bot. Treating bot review findings as part of the
  package before filing would have saved that round.
