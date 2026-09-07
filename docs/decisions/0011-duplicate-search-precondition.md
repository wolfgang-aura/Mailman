# 0011. Make strong duplicate-search matches an early stop

Status: accepted on 2026-09-07. `check-target` now reads the strong rows in
`duplicate-search.json` directly. It does not wait for `prior-art.json` before
refusing a target with an open or merged match.

## Decision

No run may start its agents against a target whose completed duplicate search
already contains a strong open or merged match. `prior-art` remains useful: it
reads the pull-request body, changed files, and maintainer response for the
prompt, but it is not allowed to be the only copy of the live-state decision.

Weak listing matches still require human acknowledgement, and closed attempts
remain prior art rather than live duplicates. A related closed pull request
still requires `prior-art` and an explicit acknowledgement before the run can
start; missing context is not the same as a clean target.

## Evidence

Run `20260906T134628Z-3519ba` against `microsoft/qlib#2278` had strong open
matches for #2330 and #2279 in `duplicate-search.json`. `prior-art.json` was
absent. Before this change, `check-target --acknowledge-claims` returned zero,
so orchestration could start even though the search had already found live
work. This was the gap reported on issue #15.

## Trade-off

The early gate can refuse before the richer pull-request context has been
collected. That is intentional: reading a rival's body costs a few API calls,
while starting a run against a live rival costs the whole run. The operator can
still run `prior-art` when they need the details for a human decision.
