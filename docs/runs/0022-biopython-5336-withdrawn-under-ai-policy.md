# 0022. biopython #5336, withdrawn under a draft no-AI policy

Date: filed 2026-09-29 13:34 UTC, put on hold 14:58 UTC, closed by us
2026-10-01 09:44 UTC. Private run `20260929T125324Z-bc9a72`, hunt
`20260929T020844Z-f0e56`. Sanitized from local evidence and the public pull
request thread.

## Timeline

| UTC | Event |
| --- | --- |
| 2024-11-05 | biopython#4878 opened (GenBank LOCUS strandedness written to the wrong columns), labelled `good first issue` |
| 2026-06-22 | biopython#5241, "First draft of no-AI policy.", opened. Still open |
| 2026-09-29 13:34 | #5336 filed: strandedness written in columns 45-47, +27 −1 across `Bio/SeqIO/InsdcIO.py` and `Tests/test_GenBank.py` |
| 2026-09-29 14:58 | peterjc: on hold until the AI policy is agreed; #5241 "would reject this outright" |
| 2026-09-29 15:10 | Mailman #220 filed: the screen misses a pending no-AI policy proposal |
| 2026-09-29 15:27 | `ea9e6e0` lands the gate (Fixes #220) |
| 2026-10-01 09:44 | we close #5336 with a short reply: the patch was AI-prepared, so it would not qualify under the draft; the column analysis stays on #4878 |

## Why it closed

The project's maintainers had an open proposal to refuse AI-assisted
contributions, and the patch was AI-assisted. Nothing was wrong with the
patch itself. The screen read merged policy files and maintainer comments,
not open proposals, so the target passed the policy gate when it should have
been held.

## What changed

`ea9e6e0` (Mailman #220): the screen searches open issues and pull requests
titled as an AI policy. A maintainer's refusing proposal fails the gate, and a
neutral one is recorded as pending. It landed two hours after this filing, so
this run predates it.

## Lessons

- A policy proposal that is still a draft already decides how maintainers
  read an AI-assisted PR. The gate treats an open refusing proposal as a
  refusal.
- The close followed the closed-case rule: one reply on our own thread, and
  nothing posted on the issue or elsewhere.
- The fork `wolfgang-aura/biopython` still holds the head. A closed PR's head
  can live only in the fork, so the fork stays until the reachability check
  says it can go.
