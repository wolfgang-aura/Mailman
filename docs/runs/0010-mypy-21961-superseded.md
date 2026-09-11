# 0010. mypy #21961, superseded, and the comment that should not have gone

Date: run 2026-09-08, closed 2026-09-10, closed out 2026-09-11. Private run
`20260908T220821Z-124828`. Sanitized from local evidence.

The first run whose pull request was closed in favour of someone else's, and
the first time a person upstream asked this project to stop. Neither the
patch nor the close is the lesson. The lesson is what the harness let happen
in the minute after the close.

## Target and defect

`python/mypy`, issue [#21960](https://github.com/python/mypy/issues/21960).
The run produced a fix, a reviewer approved it, the operator filed
[#21961](https://github.com/python/mypy/pull/21961) from a personal fork on
2026-09-09 with the LLM disclosure mypy's CONTRIBUTING asks for, and squashed
it on request. A maintainer-side contributor opened
[#21967](https://github.com/python/mypy/pull/21967) against the same issue
on 2026-09-10; #21961 was closed at 20:31 UTC in favour of it.

## What went wrong, in one minute

Three writes, twelve seconds apart, all drafted here:

| UTC | Thread | Words | Content |
| --- | --- | --- | --- |
| 20:31:12 | #21967, not ours | 158 | review-style comment: probe cases, a test suggestion, a wording nit, a link to #21961 |
| 20:31:18 | #21961 | 184 | reply to the reviewer, linking #21967 |
| 20:31:24 | #21961 | | closed by us |

The next morning the author of #21967 replied that the repeated
cross-linking and AI-generated follow-ups made the thread harder to follow
than it needed to be, and that mypy discourages LLM contributions from new
contributors.

Every part of that is fair. The comment on #21967 was correct and unwanted,
which on a thread that was never ours is the same as wrong. And it went out
before the close, so a gate keyed on the close alone would not have caught
it. The write-closed gate below covers the day after; the foreign-thread
refusal covers the twelve seconds before.

## What changed

`closed_threads` in `mailman/handoff.py`. Once provenance records the pull
request as `CLOSED`, or names a `superseded_by`, the issue, our pull request
and the superseding pull request are write-closed for the run:

| Command | Behaviour after the case closes |
| --- | --- |
| `handoff` | exits 2 for a comment on any of the three threads, or a new pull request, and names the flag |
| `handoff-check` | re-reads provenance and refuses with `run-closed` when the case closed after the preview |
| `handoff --closing-reply` | allows one courtesy reply to one thread; a second thread refuses with `closing-reply-spent` |
| `provenance --superseded-by` | prints the rule on stderr at the moment the case is closed |
| `handoff`, any state | refuses a comment on a pull request somebody else opened; `--closing-reply` reaches only the superseding one, after provenance names it |
| `handoff`, any state | warns when a reply passes 120 words: that is a review, not a reply |

The operator posted a three-sentence step-back reply on #21967 on 2026-09-11
and deleted the fork branch. That reply is the run's one closing reply; the
marker is on disk, and the gate now refuses a second one.

## What to carry forward

- **A closed case has no threads.** Once our pull request is closed or
  superseded, the issue, our pull request and the winning one belong to
  other people. A correct comment there is still noise.
- **Do not review the competing pull request.** The comparison belongs in
  our own thread, before the close, or nowhere. Reviewing the winner reads
  as contesting the close. This is now refused whatever the run's state.
- **One thread, one reply, one event.** Two comments in the same minute that
  link each other are the "repeated cross-linking" the author named. Answer
  in the thread that asked; let the other thread find it.
- **The stopping reply is short.** Acknowledge, own the call, step back.
  Three sentences; no explanation of the process, no restated disclosure.
- **Record it as superseded, not lost.** `--superseded-by` keeps the ledger
  honest and is now the switch that closes the threads.

See [issue #87](https://github.com/wolfgang-aura/Mailman/issues/87).
