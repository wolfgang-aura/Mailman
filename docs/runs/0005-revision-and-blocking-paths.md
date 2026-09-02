# 0005. The revision cycle and a blocking path, live

Date: 2026-09-02. Private runs `20260902T054015Z-0e51db` and
`20260902T054112Z-ae9e1b`. Sanitized from local evidence.

Until these two runs, the revision cycle, the second review, and every blocking
path had unit coverage and no live evidence. Both runs used Codex as primary and
Claude as reviewer against disposable local fixtures.

## Run one: a contradictory issue ends `BLOCKED`

The `clamp` fixture. `clamp(value, low, high)` silently swaps inverted bounds.
The issue demanded that `clamp` raise `ValueError` when `low > high`, and in the
same breath declared `test_swapped_bounds_are_swapped`, which asserts
`clamp(5, 10, 0) == 5`, a supported guarantee that must keep passing unedited.
Those two requirements cannot both hold. Real issues contain contradictions like
this, usually less obvious.

| Stage | Result |
| --- | --- |
| Primary, Codex | exit `0` in 55.1s, report present, no change made |
| Verification after primary | exit `0` in 0.09s |
| Reviewer, Claude | exit `0` in 52.4s |
| Verdict | `REVISE` |
| Revision, Codex | exit `0` in 77.8s, `clamp.py` changed, new test file added |
| Verification after revision | exit `1` in 0.08s |
| Final status | `BLOCKED` after one revision |

Codex diagnosed the contradiction and deliberately changed nothing, saying so in
its report. Claude confirmed the workspace was still at the base commit, ran the
tests itself, and named the contradiction independently. On the revision Codex
implemented the issue's requirement and predicted, correctly, that the mandated
test would fail. It added `test_clamp_invalid_bounds.py` rather than editing the
test it was told not to touch.

Every agent behaved well. The run still ends `BLOCKED`, which is the right
ending: the request cannot be satisfied, and no patch should reach a human
carrying an implicit "we broke a test you said to keep".

### The finding this produced

The verification after the primary stage passed *because nothing had changed*. A
green check and an untouched workspace looked identical in the record. The loop
now records the changed paths after each primary stage, so the evidence
distinguishes work from no work. The reviewer, not the harness, is what caught it
here.

## Run two: a revision, a second review, then approval

The `shorten` fixture. `shorten(text, limit)` sliced to `limit` and then appended
a three-character ellipsis, so every truncated result overshot by three. One
existing test failed at the base commit. The issue also asked, in a closing
note, for truncation to stop at a word boundary. No test covered that.

| Stage | Result |
| --- | --- |
| Primary, Codex | exit `0` in 90.0s |
| Verification after primary | exit `0` in 0.09s |
| Reviewer, Claude | exit `0` in 104.3s |
| Verdict | `REVISE` |
| Revision, Codex | exit `0` in 78.2s |
| Verification after revision | exit `0` in 0.08s |
| Second review, Claude | exit `0` in 80.5s |
| Verdict | `APPROVE` |
| Final verification | exit `0` in 0.08s |
| Final status | `READY_FOR_HUMAN_REVIEW`, 1 revision, 2 review cycles |

The first attempt fixed the length bug and attempted the word boundary with
`max(..., default=-1)`, which conflates "no space found" with a valid index, so
`prefix[:-1]` trimmed exactly one character instead of dropping the partial word.
The tests passed anyway: the one new boundary case landed on a position where
the character was already a space, so it never exercised the new code.

Claude found it by running the function, not by reading it:

```
shorten(SENTENCE, 5)   -> 't...'
shorten('abcdefgh', 6) -> 'ab...'
```

It named the root cause, and separately noted the test that did not test
anything. Codex changed the fallback to `0`, added the two cases that do
exercise the path, and pushed back on one of the reviewer's suggested
expectations with the arithmetic showing it exceeds the limit. The second review
ran the suite again, traced the boundary arithmetic by hand, and approved.

## What both runs show together

The bounded loop's remaining paths now have live evidence: one revision, a
second review, an approval after a revision, and a run that ends `BLOCKED`
because independent verification failed.

They also show the reviewer earning its place twice. In the first run it caught
an empty candidate that verification had waved through. In the second it caught
a fix that passed the tests and was still wrong.

## What they do not show

Codex could not run the pinned interpreter in either run and said so both times,
so the primary's exit code carried no engineering information in any live run to
date. Two fixtures, one language, one file each. The other blocking causes, a
missing or contradictory verdict, an agent timeout, an agent that produces no
report, still have only unit coverage.
