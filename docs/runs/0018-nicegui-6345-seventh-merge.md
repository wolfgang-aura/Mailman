# 0018. nicegui #6345, the seventh merge upstream

Date: run 2026-09-17, filed 2026-09-18 05:17 UTC, merged 2026-09-29 20:54
UTC. Private run `20260917T221223Z-b51204`, hunt `20260917T211139Z-4b4329`.
Sanitized from local evidence and the public pull request thread.

The seventh patch merged upstream, and the first where the design changed in
review and the maintainer then handed the change back to us to make.
`zauberzeug/nicegui` carries it as pull request
[#6345](https://github.com/zauberzeug/nicegui/pull/6345), squash-merged by
falkoschindler as `d0323a3` under the operator's name, with Claude Opus 5 and
Falko Schindler as co-authors. Six commits on the fork branch
`mailman/issue-6339`: two from the operator (`f1e0075`, `861949d`) and four
from the maintainer. +55 −11 across six files at merge.

## Target and defect

`zauberzeug/nicegui`, MIT, 16,213 stars and 15 human outside merges in the
fourteen days before the run. Policy stance `permitted_with_disclosure`, with
a `Co-authored-by` trailer required for AI-assisted work.

Issue [#6339](https://github.com/zauberzeug/nicegui/issues/6339) was written
by falkoschindler on 2026-09-11: `Layer.current_leaflet`, a `ClassVar` that
`Leaflet.__enter__` and `Leaflet.__getattribute__` write so a new layer can
find its map, is cleared nowhere. The last `ui.leaflet` on a page, with its
layers, stays reachable after its client is deleted. The issue named two
fixes: a small one (clear the variable in `Leaflet._handle_delete`) and a
proper one for 4.0. It also supplied a `User` test. Reproduced at base
`9146d354` with that test in 1.8 seconds: exit 1, "the leaflet element is
still alive", machine-checked before any agent ran.

## What the harness did

| Stage | Result |
| --- | --- |
| Reproduction | exit 1 at the base commit |
| Primary and reviewer | Claude `claude-opus-5` in both roles |
| First cycle, 2026-09-17 | the small fix: 2 files, +21 −1. Reviewer `APPROVE` on cycle 1; `tests/test_leaflet.py` 3 passed at base, 4 passed with the change. 12 minutes from init to decision |
| Decision | `SEND`, with one non-blocking question: GitHub labels the issue author `CONTRIBUTOR`, so the harness read #6339 as untriaged |
| Filed, 2026-09-18 | after operator approval |
| Maintainer commit, 2026-09-18 | falkoschindler pushed `c6562d2`: `is_deleted` guards on both writers, the same fix for `ui.scene`, `WeakSet` tests. Auto-merge enabled |
| Collaborator review, 2026-09-19 | evnchn: the guard makes a late `map_a.marker(...)` land on another client's live map, a regression against the base. Repros on the `user` and `screen` fixtures |
| Options, 2026-09-21 | falkoschindler laid out five fixes (A to E); evnchn picked C, a weak reference in the class variable, with a late-access test; falkoschindler agreed, removed his guards in `1a3173e`, and asked us to make the change |
| Revision, 2026-09-21 | reviewer `REVISE` (the posted plan), primary wrote option C, then two blocked resumes (below). Reviewer `APPROVE`; `tests/test_leaflet.py tests/test_scene.py` 33 passed. Pushed as `861949d`, 26 minutes after our reply on the thread |
| Silence | auto-merge disabled by the maintainer 2026-09-21 13:30, no activity for eight days |
| Maintainer commits, 2026-09-29 | `cdb0892` (`is not None` for the weak-reference check, stale test comment), `475a454` (the two tests per element merged into one) |
| Approval | falkoschindler `APPROVED`: "exactly option C as agreed in the thread". The process-wide variable moved to [#6361](https://github.com/zauberzeug/nicegui/issues/6361) for 4.0 |
| Upstream | auto-merge 20:02, merge queue 20:28, merged as `d0323a3` at 20:54 UTC |

## What this case settles

**An issue written by the lead maintainer is triage, whatever GitHub's
association label says.** falkoschindler appears as `CONTRIBUTOR` on the
issue, on his comments and on his approval, and he merged the pull request.
The handoff still carried a triage warning comparing this run to
pytest#14993. The decision page asked the right question and recommended
filing; the merge answers it.

**A correct fix can still be the wrong final design.** The first commit did
what the issue asked and the maintainer called it spot on. The review then
went wider than the issue: the maintainer's own extension regressed, a
collaborator caught it with two reproductions, and the thread converged on a
different mechanism. The merged code is option C, and the commit that
implements it is ours. Unlike openai-agents-python#4890 (record 0016), the
maintainer did not take the branch over. He asked the author to make the
change, and his last two commits were polish.

**A maintainer's "no rush" can still mean eight silent days.** The revision
was pushed 51 minutes after the maintainer asked for it. The pull request then sat with
auto-merge disabled until the maintainer returned to it on 2026-09-29.

## What this case found wrong with Mailman

- `fetch-review` recorded zero requested changes (`maintainer-review.json`,
  `change_count: 0`), because every decision on this pull request was a
  conversation comment. The comments reached `maintainer-review.md` by hand.
  Filed as [issue #128](https://github.com/wolfgang-aura/Mailman/issues/128).
- The revision pass stopped twice after the primary had worked. First on
  `[WinError 2]`, from a relative verification program path resolved against
  the workspace
  ([issue #129](https://github.com/wolfgang-aura/Mailman/issues/129)). Then
  the final verification exited 2: the widened command collected
  `tests/test_scene.py`, which imports `numpy`, and the run environment had
  been built for the leaflet test only. It passed once `numpy` was present.
- The revising primary could not run any interpreter command; each returned
  "This command requires approval". Its report says the change was checked
  by static reading only, and Mailman's final verification was the first
  execution of option C.
- `mailman provenance 20260917T221223Z-b51204` now refuses: "branch points at
  `475a454...`, but this workspace ends at `861949db...`". The maintainer
  pushed to our branch, so provenance cannot be re-recorded for a pull
  request a maintainer finished. `submission/provenance.json` still lists
  only `f1e0075`, the filing-day commit, although `861949d` is ours too.
  `contributions --refresh` still reads `MERGED`. The command also writes
  `submission/contribution.patch` before it checks the branch tip, so a
  refused call still overwrites the patch on disk.

## Timeline

| UTC | Event |
| --- | --- |
| 2026-09-11 06:30 | #6339 opened by falkoschindler |
| 2026-09-17 22:12 | run initialized at base `9146d354` |
| 2026-09-17 22:15 | reproduction exit 1 |
| 2026-09-17 22:19 | reviewer `APPROVE` on cycle 1 |
| 2026-09-17 22:24 | `READY_FOR_HUMAN_REVIEW` |
| 2026-09-18 05:17 | #6345 filed from `wolfgang-aura:mailman/issue-6339` |
| 2026-09-18 16:51 | falkoschindler labels it `bug` and `review` |
| 2026-09-18 17:20 | maintainer commit `c6562d2`, auto-merge enabled at 17:26 |
| 2026-09-19 14:54 | evnchn: the guard puts a late marker on another client's map |
| 2026-09-21 08:05 | falkoschindler lays out options A to E, label `analysis` |
| 2026-09-21 11:22 | guards removed in `1a3173e`; option C handed to us |
| 2026-09-21 11:47 | operator replies with the plan |
| 2026-09-21 11:54 | orchestration blocked on `WinError 2` |
| 2026-09-21 12:00 | final verification exit 2, `numpy` missing |
| 2026-09-21 12:11 | reviewer `APPROVE`, 33 passed |
| 2026-09-21 12:13 | `861949d` pushed |
| 2026-09-21 13:30 | auto-merge disabled by the maintainer |
| 2026-09-29 19:28 | `review` label set again |
| 2026-09-29 19:45 | maintainer commits `cdb0892` and `475a454` (19:48) |
| 2026-09-29 19:57 | #6361 opened for the 4.0 follow-up |
| 2026-09-29 20:01 | falkoschindler `APPROVED` |
| 2026-09-29 20:02 | auto-merge enabled |
| 2026-09-29 20:54 | merged as `d0323a3`, #6339 closed |
