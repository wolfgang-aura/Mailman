# 0006. Three blocked runs on pytest #14324, and the search that should have come first

Date: 2026-09-02. Private runs `20260902T110923Z-2eb44e`,
`20260902T111426Z-3fc769`, and `20260902T111843Z-01b5f6`. Sanitized from local
evidence.

The first target chosen for its contribution policy rather than for
convenience. Nothing was sent upstream. No branch, no comment, no pull request.

## Target

`pytest-dev/pytest`, issue
[#14324](https://github.com/pytest-dev/pytest/issues/14324): `RaisesGroup`
calls a `check` callable a second time with a contained leaf exception, so a
callable written for the group raises `AttributeError`. Base commit
`51e9a9f148cd2509a31e3fa0d2b1b3204c2b0dd7`.

pytest was chosen because its policy is the most explicit permissive one
surveyed: AI tools are welcome, a `Co-authored-by` trailer crediting the tool is
appreciated, and purely agentic pull requests whose author cannot explain the
change are closed and the author banned.

Verification gate: `python -m pytest testing/python/raises_group.py
testing/python/raises.py -q`, which passes 66 tests in 1.1s at the base commit
in an independent clean clone.

## Three runs, three ways to stop

| Run | Primary | Reviewer | Turn budget | Outcome |
| --- | --- | --- | --- | --- |
| `...110923Z-2eb44e` | Codex | Claude | 30 | `BLOCKED`, verification failed after the primary stage |
| `...111426Z-3fc769` | Claude | Codex | 30 | `BLOCKED`, agent hit its turn limit and wrote no report |
| `...111843Z-01b5f6` | Claude | Codex | 120 | `BLOCKED`, verification failed after the primary stage |

Both agents changed `src/_pytest/raises.py` and `testing/python/raises_group.py`,
and both added `changelog/14324.bugfix.rst` without being told to, which is the
newsfragment convention the project documents. On the harness side that is the
good news: the agents read the repository's own conventions.

Both candidates then failed the same gate. Codex's produced `9 failed, 57
passed`; Claude's produced `9 failed, 58 passed`. Six of the failures are the
same tests in `testing/python/raises.py` in both runs, including
`test_no_raise_message` and `test_raises_match_verbose_diff`, which suggests both
agents changed shared failure-message construction rather than only the
`RaisesGroup` path they were aiming at.

The middle run is the first live evidence for a blocking cause that previously
had only unit coverage: an agent that produces no report. Claude stopped at
`Error: Reached max turns (30)` after 184 seconds with real edits left in the
workspace. The run record carries that string; the console said only that no
report was produced, which is [issue #14](https://github.com/wolfgang-aura/Mailman/issues/14).

A failed verification after the primary stage ends the run immediately, with no
revision cycle, while a reviewer's `REVISE` verdict grants one. That asymmetry
is [issue #13](https://github.com/wolfgang-aura/Mailman/issues/13).

## What the duplicate search found afterwards

`mailman duplicate-search --query "RaisesGroup check"` returned four pull
requests against this issue:

- #14358, closed, "Fix RaisesGroup calling check() on contained exceptions instead of Ex..."
- #14411, closed, body says "Fixes #14324"
- #14502, closed, body says "Closes #14324"
- #14668, open since 2026-07-01

Three closed without merging. None carries a discussion. The issue is open
because maintainers keep rejecting attempts at it, not because nobody has tried.

That reframes all three runs. Even a candidate that passed the gate would have
been the fifth attempt at an issue with an open pull request already on it, from
a pipeline whose output pattern the maintainers have said they close on sight.

The lesson is not about patch quality. An open issue is not an unclaimed issue,
and the check that establishes the difference costs one command and was run last
instead of first. It is now [issue #15](https://github.com/wolfgang-aura/Mailman/issues/15).

## What held

The harness refused three candidates and shipped nothing. Every stop was
Mailman's own verification or its own precondition, not an agent's self-report.
Both agents exited zero in the runs that failed the gate.
