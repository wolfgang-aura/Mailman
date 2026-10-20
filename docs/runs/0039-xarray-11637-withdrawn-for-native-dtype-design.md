# 0039. xarray #11637, withdrawn for a native-dtype design

Date: filed 2026-10-04, closed by us 2026-10-20 09:23 UTC. Private run
`20260929T011709Z-a26132`. Sanitized from local evidence and the public pull
request thread.

## Timeline

| UTC | Event |
| --- | --- |
| 2026-10-04 | #11637 filed: reuse the stored `units` and `calendar` when appending native `datetime64` to Zarr (closes #10639), with a regression test |
| 2026-10-05 to 2026-10-11 | main moved past the head three times; each time the harness merged main in the workspace and the operator pushed (`5e255d1` last) |
| 2026-10-19 01:13 | spencerkclark: prefers a more robust native-dtype fix, because a non-epoch reference date still gives wrong values with this patch; open to other opinions |
| 2026-10-20 09:23 | we reply that the point is fair and close; the reply offers an issue with the `units` plus `dtype` reproducer |

## Why it closed

The patch worked for the reported case but narrowed the bug instead of fixing
it. A maintainer who owns the encoding design asked for the broader fix, and
no one else weighed in. Nothing was wrong mechanically: tests passed and the
head merged cleanly each time main moved.

## What changed

Nothing in the harness. No Mailman gap fits this close, so no issue was filed.

## Lessons

- A fix inside a design a maintainer wants to change is a stopgap, even when
  the linked issue is open and the reproducer is exact. Read the issue thread
  for a stated design direction before choosing a narrow patch.
- The close followed the closed-case rule: one reply on our own thread, and
  nothing on the issue or elsewhere.
- The fork `wolfgang-aura/xarray` holds the head. It stays until the
  reachability check says it can go.
