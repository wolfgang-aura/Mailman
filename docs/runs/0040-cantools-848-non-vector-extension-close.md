# 0040. cantools #848, closed by the maintainer as a non-Vector extension

Date: filed 2026-10-11, closed by andlaus 2026-10-20 15:47 UTC. Private run
`20261011T021156Z-0abf58`. Sanitized from local evidence and the public pull
request thread.

## Timeline

| UTC | Event |
| --- | --- |
| 2026-10-11 | #848 filed: accept the `BS_:` baudrate group (issue #734), read and discard it |
| 2026-10-19 10:09 to 10:13 | andlaus: candb++ fails on the `BS_: 500000 : 0,0` line and on the #734 example; asks for a real-world file |
| 2026-10-20 09:23 | we reply: no wild file, the only source is the DBC primer; offer to close |
| 2026-10-20 15:47 | andlaus: not worth teaching bad habits, close it; revive if many datasets use it |

## Why it closed

The reproducer came from a hand-written primer, not a file from the wild. Vector's
own tool rejects the syntax, so the maintainer judged it a non-Vector extension.
Tests and the head were fine. The defect was target evidence, not code.

## What changed

Nothing in the harness. Provenance records the close. The fork branch is safe to
delete because the patch is on disk in the run's `submission/contribution.patch`.

## Lessons

- An issue whose only evidence is a spec-like document needs a real file or a
  second tool that accepts the syntax before a PR. Here the second tool rejected it.
- The close followed the closed-case rule: nothing more on the issue, the PR or elsewhere.
