# 0016. openai-agents-python #4890, the fifth merge upstream

Date: run 2026-09-06, filed 2026-09-06 13:26 UTC, merged 2026-09-28 15:08
UTC. Private run `20260906T104815Z-29582c`. Sanitized from local evidence and
the public pull request thread.

The fifth patch merged upstream, and the first in a repository the operator
names when asked which projects took Mailman's work. `openai/openai-agents-python`
carries it as pull request
[#4890](https://github.com/openai/openai-agents-python/pull/4890), merged by
a maintainer as `71306bf`. Nine commits under the operator's name from the
fork branch `fix/apply-patch-case-only-rename`, then five maintainer commits
on top: +1710 −22 across twelve files at merge.

## Target and defect

`openai/openai-agents-python`, MIT (about 29,500 stars in September 2026).
Issue [#4889](https://github.com/openai/openai-agents-python/issues/4889) was
filed by the operator from this run, a self-sourced defect: `apply_patch`
`update_file` with a case-only `move_to` (`Readme.md` to `README.md`) wrote
the new name and then removed the old one. On a case-insensitive filesystem
both names are one entry, so the removal deleted the file the tool had just
reported writing. Reproduced at base `1d471a47` by `repro_case_rename.py`,
machine-checked before any agent ran.

## What the harness did

| Stage | Result |
| --- | --- |
| Reproduction | the file is gone after a reported-successful rename, at the base commit |
| Primary and reviewer | Claude `claude-opus-5` in both roles |
| Filed, 2026-09-06 | issue and pull request together, after operator approval |
| Change request, 2026-09-07 | seratch: the data loss is real, but a delete-first workaround can still lose the original; use backend filesystem identity and keep the original until the replacement is committed; no casefold as a proxy for identity |
| Revisions, 2026-09-07 to 09-12 | identity asked of the sandbox rather than the host, staged replacement, descriptor-relative rename on UnixLocal; nine commits after the history was rewritten |
| Maintainer decision, 2026-09-27 | jbeckwith-oai: "the need is demonstrated; pursue this fix, but do not merge this head as-is", with consolidated findings |
| Maintainer commits, 2026-09-27 to 09-28 | five commits: ordinary moves preserved, moves restricted to files and symlinks, main merged twice, Windows CI timeout |
| Approvals | dpiet-oai and seratch `APPROVED` on 2026-09-28 |
| Upstream | merged as `71306bf` at 15:08 UTC |

## What this case settles

**A famous repository takes a fix when the defect is undeniable.** The
change request opened by agreeing the data loss was real. Every later round
argued about how, never whether.

**The maintainers finished it.** Three weeks after the last revision, a
maintainer reviewed the full head, stated what still had to change, and
pushed those changes onto our branch instead of asking for another round.
The screen's responsiveness gate records this repository as fast to answer
(median 1.8 days) but closing far more outside pull requests than it merges;
this merge came from a maintainer taking the branch over, not from our last
revision passing as filed.

## What this case found wrong with Mailman

- The run was still `claude-opus-5`, before the hunt recorded exact model IDs
  for both roles; the run record carries no reviewer model.
- The revisions were made by hand over six days, and the history was
  rewritten before the review resumed. Mailman had no revision loop for a
  change request that asks for a different design, only for line fixes.
- A maintainer's pushed commits on our branch were found by reading the pull
  request, not by `fetch-review`. The rule since then: check the pull
  request head before revising; a branch `behind N` is a stop sign.

## Timeline

| UTC | Event |
| --- | --- |
| 2026-09-06 10:47 | #4889 opened from the run |
| 2026-09-06 10:48 | run initialized at base `1d471a47` |
| 2026-09-06 13:26 | #4890 filed |
| 2026-09-07 14:01 | seratch requests changes: backend identity, keep the original |
| 2026-09-12 20:02 | last operator revision, `5b3b0b97` |
| 2026-09-27 18:56 | jbeckwith-oai: pursue this fix, not this head as-is |
| 2026-09-27 19:11 | first of five maintainer commits |
| 2026-09-28 06:35 | dpiet-oai `APPROVED` |
| 2026-09-28 06:44 | seratch `APPROVED`, hardlink move-target follow-up noted |
| 2026-09-28 15:08 | merged as `71306bf` |
