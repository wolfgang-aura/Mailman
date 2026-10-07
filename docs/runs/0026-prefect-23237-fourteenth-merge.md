# 0026. prefect #23237, the fourteenth merge upstream

Date: filed 2026-09-30 05:42 UTC, merged 2026-10-06 14:25 UTC. Private run
`20260930T032102Z-78a763`. Sanitized from local evidence and the public pull
request thread.

The fourteenth patch merged upstream and the first in `PrefectHQ/prefect`. It
merged as pull request [#23237](https://github.com/PrefectHQ/prefect/pull/23237),
merge commit `3fbbdf489`, six days after filing.

## Target and defect

Issue [#17504](https://github.com/PrefectHQ/prefect/issues/17504).
`prefect worker start` offered to install a missing worker integration,
installed it, then reported "Unable to start worker" anyway; a second run
worked. The cause was the module-level cache in `load_prefect_collections()`:
the first call, made before the install, stored the entry points it saw. The
fix reloads collections after the install in `_install_package` and retries
collections that failed to load. +138 -4 across 3 files
(`collections.py`, `_worker_utils.py`, `tests/test_plugins.py`), four commits
of ours.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-09-30 05:42 UTC from `wolfgang-aura/prefect` |
| Automated review | Devin Review commented twice; the revisions after the first commit answer it |
| Maintainer review | none until 2026-10-06 14:25:26 UTC, when desertaxle approved |
| Upstream | desertaxle merged `main` into our branch (`4807f1e`), approved, and merged nine seconds later |

## What the maintainer changed

Nothing in the patch. The one commit on top of ours is a merge of `main`. The
watch reported it as a head we did not push and flagged no work.

## Lessons

- The first commit put the reload on lookup miss. The second moved it into
  `_install_package`, where the cause is. Reviewers read the cause, not the
  symptom site, so a fix at the lookup would have drawn the same question.
- The red checks the watch recorded during review were not ours (see the
  2026-10-03 entries in `SOURCE_OF_TRUTH.md`). Classifying them as inherited
  kept us from touching the branch while a maintainer was already reading it.
