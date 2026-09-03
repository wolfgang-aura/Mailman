# 0010. Prove the bug is still there before a run is spent on it

Status: accepted on 2026-09-03. `mailman reproduce` is implemented and is a
blocking precondition in `check-target`, which `orchestrate` runs first. Feeding
the reproduction into the agent prompts is named future work below, not a claim.

## Decision

No run may start against a bug nobody has reproduced at the base commit.
`mailman reproduce RUN_ID -- <command>` runs the reporter's own steps in the
prepared workspace and records `reproduction.json`. `check-target` refuses when
that record is absent, and refuses again when it says the reported behaviour did
not happen. Neither refusal is clearable by `--acknowledge-prior-attempts`,
which answers a different question.

## Why this is a gate and not a habit

Run `20260903T045152Z-369e77` targeted
[pytest-dev/pytest#14964](https://github.com/pytest-dev/pytest/issues/14964), a
precise, same-day, unclaimed regression report. Every gate the harness had
passed: the duplicate search completed, `check-target` found no open attempt,
the workspace cloned clean at `51e9a9f`, and the environment built in four
steps. The bug was already fixed, by a commit that landed after the 9.1.1 tag
without closing its issue.

Nothing in the harness could see that. `check-target` asks who else is working
on the issue; `duplicate-search` asks whether the change already exists as a
pull request. An issue filed against a released version says nothing about
`main`, and a fix that lands without closing its issue leaves no trace in either
search. Only a hand-built reproduction caught it, and only after the environment
build had been paid for.

An agent run against an already-fixed issue is the most expensive way to learn
nothing, and it is the one failure that leaves a plausible-looking patch behind.

## What counts as the bug still being there

| Expectation | What it is for |
| --- | --- |
| Any non-zero exit, the default | A reproducer written as a failing test, which is most of them. |
| `--expect-output`, repeatable | The pytest #14964 shape: the fixed and unfixed trees both exit non-zero and differ only in what they print, `2 passed, 1 error` against `1 passed, 2 errors`. An exit-code check alone would have cleared this run a second time. |
| `--forbid-output`, repeatable | The fixed shape, stated directly: the warning that is gone, the line the patch removes. |
| `--expect-exit-code` | A bug whose signature is a specific code rather than mere failure. |

A timeout is recorded as a timeout and never as a reproduction. A reproducer
that hangs says the reproducer hung.

The command's executable is resolved through the run toolchain, the same way
`verify` resolves its own, so the reproduction runs on the interpreter the
environment built rather than on whatever is first on PATH. That hole cost run
`20260903T050831Z-bed67e` its one revision; see
[#39](https://github.com/wolfgang-aura/Mailman/issues/39).

## The honest record for an issue with no reproducer

`--not-machine-reproducible --note "..."` records that a person read the report
and no command can check it. It warns on every later `check-target` rather than
blocking. It is a worse record than a failing command and a better one than the
silence it replaces, and the distinction stays visible in `reproduction.json` as
`machine_checked: false` with `reproduced: null`.

A bug that demonstrably no longer happens is the opposite case and blocks
outright. The right response is to abandon the run, so there is no flag for it.

## Compounding

`LESSON-0001` in `knowledge/lessons.json` is this lesson, written from the run
that found it before the gate existed. It stays at `CANDIDATE_LESSON`: it has
one supporting run, and `VALIDATED` needs two.

## Future work, not claims

- The reproduction is not yet in the agent prompts. A primary agent handed the
  exact command that fails, and its output, starts from evidence rather than
  from a report.
- `prepare-submission` does not yet cite the reproduction. A pull request that
  quotes the failure at the base commit is more convincing than one that quotes
  the issue.
- Nothing re-runs the reproduction after the patch. That is the natural pair to
  this gate: the same command, expected to pass, is the strongest evidence a
  submission can carry.
