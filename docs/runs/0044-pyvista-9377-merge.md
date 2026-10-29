# 0044. pyvista #9377, merged upstream

Date: filed 2026-10-06 09:09 UTC, merged 2026-10-29 09:53 UTC. Private run
`20261006T022759Z-69dea1`. Sanitized from local evidence and the public pull
request thread.

Merged as pull request [#9377](https://github.com/pyvista/pyvista/pull/9377),
merge commit `476e066`, about 23 days after filing, by banesullivan.

## Target and defect

Issue 8104: `.xyz` is the PLOT3D grid extension, but PyVista treated it as a
plain point list. The patch makes `get_reader` return a
`MultiBlockPlot3DReader` for `.xyz` and has `PointSet` write the simple points
list to `.txt`. Saving a `PointSet` to `.xyz` still works but warns, and
becomes an error in 0.53.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-06, one commit |
| Base moved | Upstream replaced the extension dicts with per-format tables; we merged `main` in on 2026-10-17 and mapped `.xyz` into the new `PLOT3D` table |
| Follow-up commits | Two test fixes of ours on 2026-10-18: `.xyz` added to the class readers snapshot, and an equality test for the single `OpenFOAMReader` check |
| Review | banesullivan approved at 08:14 UTC on 2026-10-29 and merged 99 minutes later |

## What the maintainer changed

akaszynski merged `main` into the branch once (`1253adf`, 2026-10-17). No code
edits. The merged tree is the head we filed.

## Lessons

- A 23-day wait came from a branch the base had moved under, not from review
  objections. Two upstream-driven merges and a snapshot test were the whole
  cost.
- The snapshot test for the reader table failed only after `.xyz` was added to
  that table, so a patch that touches a format table should run the readers
  snapshot test before filing.
