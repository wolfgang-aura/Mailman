# 0011. pdm #3884 and pytest #14993, two silent closes

Date: filed 2026-09-08 and 2026-09-09, closed 2026-09-09, closed out
2026-09-11. Private runs `20260907T173348Z-003915` and
`20260908T223126Z-2b2b81`. Sanitized from local evidence.

Two pull requests, two maintainers, no comment on either close. A silent
close reads like nothing to learn. Both threads said otherwise once read
end to end.

## pdm #3884: filed on an issue already closed

| UTC | Event |
| --- | --- |
| 2026-09-07 07:32 | pdm-project/pdm#3877 opened: a stale PEP 621 licence example in the docs |
| 2026-09-08 09:27 | the maintainer closes #3877 as completed, with his own commit `dc4e314` |
| 2026-09-08 18:18 | `mailman claims` reads the issue and records `"issue_state": "closed"` |
| 2026-09-08 18:19 | `mailman handoff` prints the block; nothing reads the field |
| 2026-09-08 18:20 | #3884 filed |
| 2026-09-09 07:06 | #3884 closed by the maintainer, no comment |

The evidence was on disk one minute before the handoff. `issue_state` was
written by the claims collector and read by nothing. #79 covers the wider
question, whether a fix this small should be attempted at all when the
maintainer will do it himself; this is the narrower, mechanical half.

## pytest #14993: filed on an issue nobody had triaged

| UTC | Event |
| --- | --- |
| 2026-09-08 13:14 | pytest-dev/pytest#14992 opened by an outside reporter, `author_association: NONE` |
| 2026-09-08 22:42 | another repository cross-references it as not reproducing |
| 2026-09-08 23:10, 00:11 | the reporter replies to himself twice, reverifying |
| 2026-09-09 02:10 | `mailman claims` reads the thread: two comments, both the reporter's, no maintainer |
| 2026-09-09 02:12 | #14993 filed, with a machine-checked reproduction at the base commit |
| 2026-09-09 13:23 | the maintainer's first reply on the issue: the use case reorders `metafunc.fixturenames` in a hook, which is private and unsupported; the report is hard to read through the AI verbosity |
| 2026-09-09 18:08 | #14993 closed by the maintainer, no comment |
| 2026-09-10 03:56 | the reporter withdraws the premise and closes the issue himself |

The reproduction was real and the fix was correct for the behaviour
described. The behaviour was never a guarantee. Nobody who could say so had
been asked, and the harness did not notice that nobody had answered.

## What changed

| Command | Behaviour |
| --- | --- |
| `claims` | records `issue_closed_at`, `reporter_association` and `maintainer_replied` |
| `handoff-check`, pull request | refuses `issue-closed` when the claims record says the issue is closed |
| `handoff`, pull request | prints `UNTRIAGED ISSUE` when the reporter is outside the project and no owner, member or collaborator has replied. A warning, not a refusal: the operator decides |

Both runs record `CLOSED` in provenance; the write-closed gate from run 0010
covers their threads. Nothing was posted on either after the close, and
nothing will be.

## What to carry forward

- **Read the closed thread before calling a close silent.** Both reasons
  were in the issue, not the pull request.
- **A closed issue is a decision.** Whoever closed it does not want a pull
  request against it; the claims record already knew.
- **No maintainer reply is a fact about the issue.** A reproduction proves
  the behaviour, not that the project considers it a bug. On an outside
  report nobody has answered, ask on the issue first or pick another one.

See [issue #88](https://github.com/wolfgang-aura/Mailman/issues/88).
