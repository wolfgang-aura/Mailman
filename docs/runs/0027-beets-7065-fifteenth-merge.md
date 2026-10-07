# 0027. beets #7065, the fifteenth merge upstream

Date: filed 2026-10-01 03:07 UTC, merged 2026-10-07 05:03 UTC. Private run
`20260930T202626Z-7d2959`. Sanitized from local evidence and the public pull
request thread.

The fifteenth patch merged upstream and the first in `beetbox/beets`. It merged
as pull request [#7065](https://github.com/beetbox/beets/pull/7065), merge
commit `1a56ed566`, six days after filing.

## Target and defect

Issue [#5994](https://github.com/beetbox/beets/issues/5994). The `lastgenre`
plugin accepted an empty whitelist or canonical setting and failed later in a
way that did not name the setting. Our change raises `ui.UserError` at setup for
both. +34 -17 across 3 files, three commits of ours (one is a merge of
`master`).

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-01 03:07 UTC from `wolfgang-aura/beets` |
| Maintainer review | JOJ0 asked for yes/no wording in both error messages on 2026-10-02 |
| Revision | `18e981a` applied both suggestions on 2026-10-03; JOJ0 approved the same day |
| Upstream | merged 2026-10-07, four days after the approval |

## What the maintainer changed

Nothing in the patch. The review was two inline suggestions, both about wording
the docs already use.

## Lessons

- The only review point was house style. The run read the plugin docs for the
  option's values but not for how they are spelled in messages, so the first
  wording said "true/false". A prompt step that quotes the project's own
  phrasing for the same setting would have saved the round.
- A four-day wait between approval and merge was the maintainers' release
  rhythm, not a stall. The watch correctly showed `approved` and no work.
