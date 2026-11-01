# 0046. magic-wormhole #746, merged upstream

Date: filed 2026-10-22 03:04 UTC, merged 2026-10-30 17:47 UTC. Private run
`20261022T015740Z-48ca60`. Sanitized from local evidence and the public pull
request thread.

Merged as pull request [#746](https://github.com/magic-wormhole/magic-wormhole/pull/746),
merge commit `5557a8d`, about eight days after filing, by meejah.

## Target and defect

`wormhole` exported `WormholeStatus` and `DilationStatus` but not the types
their fields hold, so an application matching `status.mailbox_connection`
against `Connected` had to import from the private `wormhole._status` module.
The patch exports the four union types and their 16 member classes from
`wormhole`, adds them to `__all__`, and adds a test, a `NEWS.md` entry and a
line in `docs/api.rst`.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-22, one commit, from the rolling hunt |
| Review | meejah approved with "Great, thanks!". No change request, no comment |
| Merge | meejah merged the filed head on 2026-10-30 |

## What the maintainer changed

Nothing. The merged tree is the head we filed.

## Lessons

- An additive export with a test that checks each name against `__all__` and
  against the private module merged on the first review. The wait was the
  maintainer's schedule, not the patch.
- The target reopens for new candidates now that the one-open-PR rule no longer
  holds it.
