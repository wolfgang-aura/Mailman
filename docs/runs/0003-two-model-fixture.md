# 0003. First complete two-model fixture run

Date: 2026-09-02. Private run `20260901T201921Z-0b85ed`. Sanitized from local evidence.

Codex acted as primary engineer and Claude as reviewer on a disposable fixture.
The loop reached `READY_FOR_HUMAN_REVIEW` with no revision.

## Fixture

A local Git repository at commit `b5f5822` holding a `slugify` function and
three unit tests. Two tests failed at the base commit: runs of separators were
not collapsed, and leading and trailing separators were not trimmed. The primary
prompt forbade editing the test file.

## What happened

| Stage | Result |
| --- | --- |
| Workspace check | clean at `b5f5822` |
| Primary, Codex | exit `0` in 43.4s, report present |
| Verification after primary | exit `0` in 0.11s |
| Reviewer, Claude | exit `0` in 29.0s, report present |
| Verdict | `APPROVE`, parsed from one line |
| Final verification | exit `0` in 0.11s |
| Final status | `READY_FOR_HUMAN_REVIEW` |

Codex changed one line:

```diff
-    return "".join(kept)
+    return "-".join(part for part in "".join(kept).split("-") if part)
```

The reviewer reported that all three tests pass and the logic is sound, noted
that an all-separator title yields an empty string and is untested, judged that
not a regression, and required no change.

## The part worth keeping

Codex could not verify its own work. Its report says the pinned Python
executable was unreachable inside its sandbox, so it fell back to
`git diff --check`. It still exited `0`.

That is the exact case Mailman exists for. A run that trusted the agent's exit
code would have accepted an unverified patch. Instead Mailman ran the tests
itself in the workspace, twice, and the second run is what allowed the final
transition. Independent confirmation afterwards reproduced three passing tests
against the same diff.

## Known gap in this run

The reviewer wrote to `reviewer-report.md`, while `create_run` scaffolds a
`review-report.md` placeholder that no stage updates. Anyone reading the run
directory sees a stale "Not started" file next to the real review. The verdict
was read from the correct file, so the loop was unaffected.

## What this does not show

One fixture, one language, one small function, and an approval on the first
cycle. The revision path, the second review, and every blocking path have unit
coverage with scripted agents but no live evidence yet. Nothing here has been
tried against a real upstream issue.
