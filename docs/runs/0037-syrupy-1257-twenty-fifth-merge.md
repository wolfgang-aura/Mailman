# 0037. syrupy #1257, the twenty-fifth merge upstream

Date: filed 2026-10-05, merged 2026-10-17 18:49 UTC. Private run
`20261005T092652Z-10e847`. Sanitized from local evidence and the public pull
request thread.

Merged as pull request [#1257](https://github.com/syrupy-project/syrupy/pull/1257),
merge commit `61bc2f2`, twelve days after filing, by noahnu.

## Target and defect

`pytest --lf --snapshot-update` treated the unrun snapshots as unused and
deleted them. The fix keeps snapshots of tests that `--lf` did not run.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-05, one commit of ours (`1428b88`) |
| Waiting | Twelve days with no maintainer text. `hunt watch` reported `blocked`, then `behind`; the failing Windows checks were inherited, so nothing was pushed |
| Merge | noahnu pushed two commits, then merged on 2026-10-17 |

## What the maintainer changed

noahnu merged `main` in and added `296429d`. A `--lf` run that did not filter
anything (fresh run, empty cache) now counts as a full run, so obsolete
snapshots are deleted after a green run. He wrote that this narrows the scope
of the change, and offered to revert it. He also added us to the contributors
list.

## Lessons

- A quiet PR with red checks that `hunt watch` marks inherited should stay
  untouched. The maintainer fixed the base himself and merged without asking
  for anything.
- The fix assumed `--lf` always means a partial run. The cache can be empty,
  and the maintainer caught that case.
