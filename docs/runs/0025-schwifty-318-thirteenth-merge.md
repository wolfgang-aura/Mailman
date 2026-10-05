# 0025. schwifty #318, the thirteenth merge upstream

Date: filed 2026-10-01 14:54 UTC, merged 2026-10-05 10:51 UTC. Private run
`20261001T142059Z-80c7ae`. Sanitized from local evidence and the public pull
request thread.

The thirteenth patch merged upstream and the first in `mdomke/schwifty`. It
merged as pull request [#318](https://github.com/mdomke/schwifty/pull/318),
squash commit `e40e32f`, four days after filing.

## Target and defect

Issue [#297](https://github.com/mdomke/schwifty/issues/297). The BIC
`OROACY2LXXX` (ORO PAY LTD, Nicosia) had no registry entry, so lookups returned
no bank. Our change added the entry to `schwifty/bank_registry/manual_cy.json`
with a test in `tests/test_bic.py`, plus a changelog line. +16 -0 across 3
files, one commit (`d019868`). The name and BIC came from the issue thread and
matched the entity's GLEIF record.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-01 14:54 UTC from `wolfgang-aura/schwifty` |
| Review | none; no review or comment from a maintainer before the merge |
| Upstream | mdomke merged `main` into our branch (`f5e9142`), then merged the PR on 2026-10-05 |

## What the maintainer changed

Nothing in the patch. The one commit on top of ours is a merge of `main`. The
watch reported it as a head we did not push and flagged no work.

## Lessons

- A data-only fix with a named BIC and a registry source needed no review
  round. The merge came after the maintainer updated the branch himself, as it
  did for semantica (record 0024).
- Four days from filing to merge with no comment means silence is not a stall
  signal for a one-maintainer project. The watch rule of leaving `behind` and
  foreign heads alone was right.
