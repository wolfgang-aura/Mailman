# 0004. Run the primary and reviewer loop under a fixed bound

Status: accepted on 2026-09-02.

## Decision

`mailman orchestrate` runs one bounded loop for a run that is still `INITIALIZED`:
primary work, independent verification, review, at most one revision, a second
review, final independent verification, then a stop at
`READY_FOR_HUMAN_REVIEW`. Every other ending is `BLOCKED`. Mailman never reaches
a terminal success state on an agent's own account of its work.

Three rules carry that guarantee.

Verification belongs to Mailman. The verification command runs without a shell,
in the workspace, under a timeout, after each primary stage and again after the
reviewer approves. A failure ends the run before the reviewer is paid for
reading broken code, and a failure at the end blocks a run the reviewer already
approved.

A review is a parsed verdict, not prose. Mailman appends a verdict contract to
the reviewer prompt and reads exactly one line, `MAILMAN-VERDICT: APPROVE` or
`MAILMAN-VERDICT: REVISE`, from the review report. A missing, unparseable, or
contradictory verdict blocks the run. Silence never counts as approval.

The revision budget is one, and it belongs to the run rather than to any one
stage. A second request for changes blocks the run for a human instead of
starting an unbounded negotiation between two models. The revision prompt is the
original primary prompt with the reviewer's findings appended, so the revising
agent sees the task and the objection together.

Amended on 2026-09-02 for
[#13](https://github.com/wolfgang-aura/Mailman/issues/13): a verification that
fails after a primary stage may also spend that one revision. It was the only
stop in the loop that granted none, which is backwards, because a failing gate
is a more objective finding than a reviewer's opinion. The repair prompt is the
primary prompt with the command, its exit status, and the tail of its output
appended, since the agent never saw that output run. A revision spent on a
failed gate is one the reviewer cannot also ask for, so the total is unchanged:
at most two primary stages, and at most two reviews.

A run that has started must never claim to be in flight after it stops. An
unexpected `OSError` or `ValueError` inside the loop is recorded as `BLOCKED`
with the error text, and `orchestration.json` is written either way.

## Agent executables are resolved and may be pinned

Agent commands run without a shell, so a bare `codex` or `claude` name has to be
resolved before launch. On Windows a bare name misses the `.cmd` launcher that
npm installs, which surfaced as a bare `WinError 2` rather than an actionable
message. Mailman now resolves the executable at launch, prefers an executable
already registered for the run by `probe-tool` under the agent's name, and
otherwise fails with the tool name and the command that would register it.

## Evidence

Forty-seven unit tests cover the loop with scripted agents: approval, one
applied revision, a second revision request, a missing verdict, verification
failure before the reviewer runs, an agent that exits zero without a report, a
missing agent executable, a dirty workspace, a wrong base commit, and a run that
already started.

Private run `20260901T194823Z-56b438` ran the real command against a local
fixture repository. It validated the clean workspace at the base commit, entered
`PRIMARY_RUNNING`, failed to resolve the Claude CLI, and blocked with the
resolution message and a written `orchestration.json`.

The loop has not yet been driven end to end by two live models. See
`SOURCE_OF_TRUTH.md` for the tooling state that blocks it.
