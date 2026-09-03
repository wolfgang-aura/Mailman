# 0008. starlette #3497, the first run to clear `prepare-submission`

Date: 2026-09-03. Private run `20260903T052426Z-ad8196`. Sanitized from local
evidence.

The first run whose result a human could send upstream: `READY_FOR_HUMAN_REVIEW`
from the loop, then `"ready": true` from `prepare-submission` against the
target's recorded policy. Claude Opus 5 was both the primary engineer and the
reviewer, at the owner's instruction. Nothing was sent. No fork, no branch push,
no comment, no pull request.

## The hunt, which is most of what this run cost

Ninety candidate repositories were screened by hand. Three findings shaped
everything after.

**Freshness has to exclude bots.** Counting merged pull requests by
`author_association` treats dependabot as a `CONTRIBUTOR`. `PyCQA/bandit` scored
one outside merge in fourteen days on that basis; every merge there since May
2026 is a bot, and roughly twenty human pull requests sit open and untouched.
Filtering on the account type also took `psf/black`, `psf/requests`,
`encode/httpx`, `fastapi/typer` and `Textualize/textual` to zero.

**Every fresh bug in every recognizable Python repository is already claimed.**
In `sqlfluff/sqlfluff`, every bug filed since 2026-08-14 had a pull request, and
issue #8354 had four separate attempts, all closed. In `pytest-dev/pytest`, ten
of the eleven most recent bugs had one. `python-jsonschema/jsonschema` #1511 has
seven closed attempts. Across nineteen high-merge repositories, subtracting every
issue number mentioned by any of the last 200 to 400 pull requests left 46 open
issues since 2026-05-01, nearly all enhancements or proposals rather than bugs.

**Targets now screen for automated accounts.** `sqlfluff/sqlfluff` runs an
`agentscan` workflow that closes on the account looking automated rather than on
the content. `pytest-dev/pytest` has an "ai rejected" label in use.

Recorded as evidence on [#35](https://github.com/wolfgang-aura/Mailman/issues/35).

## The target that was rejected after the environment was built

Run `20260903T045152Z-369e77` targeted
[pytest-dev/pytest#14964](https://github.com/pytest-dev/pytest/issues/14964),
a precise, same-day regression report with no pull request against it. Every gate
Mailman has passed. The bug does not exist at the base commit: on pytest 9.1.1
the reported argument order gives "2 passed, 1 error", and at `51e9a9f` the same
tree gives "1 passed, 2 errors". Commit `fc8f56bd2` fixed it after the 9.1.1 tag.

Only a hand-built reproduction caught it, and the run was abandoned before an
agent started. Filed as
[#37](https://github.com/wolfgang-aura/Mailman/issues/37): nothing in the harness
asks whether the reported bug still happens at the base commit.

## Target

`encode/starlette`, BSD-3-Clause, 12,588 stars, an explicit `AI_POLICY.md` that
permits AI assistance and makes a duplicate search mandatory. Base commit
`39fd0ffac25593fce39466320c9a666957ce8b8c`.

Issue [#3497](https://github.com/encode/starlette/issues/3497), filed eight hours
before the run: anyio 4.15.0 turned `anyio.abc.BlockingPortal` into a deprecated
lazy alias, and `testclient.py:53` reads it in a runtime expression.

The duplicate search returned 11 rows, all read and recorded. Two of them, open
pull requests #3355 and #3491, add new uses of the deprecated alias, which is the
opposite of this change rather than a duplicate of it.

## What the loop did

| Step | Result |
| --- | --- |
| target | no prior attempt |
| primary (claude-opus-5, 120 turns) | exit 0, changed `starlette/testclient.py` and `tests/test_testclient.py` |
| verification after primary | exit 0 |
| reviewer (claude-opus-5) | verdict REVISE |
| revision | exit 0, changed `starlette/testclient.py` only |
| verification after revision | exit 0 |
| reviewer, second cycle | exit 0, 18 turns, $0.86, verdict APPROVE |
| final verification | exit 0 |

`revisions_used: 1`, `review_cycles: 2`. This is the first live revision cycle on
an external target, and the first whose reason was a design judgement rather than
a broken build.

## The revision: the reviewer deleted the test

The primary added a test asserting that importing the test client emits no
deprecation warning. The reviewer exported the base commit into a scratch
directory and ran the suite there. It does not fail a test; it dies before
collection, because `tests/conftest.py` imports `TestClient` and `pyproject.toml`
sets `filterwarnings = ["error"]`.

A dedicated test can therefore never be what catches a reintroduction: the suite
is already dead by the time it would run. The reviewer required the test be
removed, and named what the actual coverage gap is instead. `scripts/install`
runs `uv sync --frozen` and `uv.lock:15` pins `anyio 4.14.2`, the release before
the deprecation, so CI exercises neither the bug nor the fix.

`prepare-submission` then blocked the result on `no-test-change`, the one hygiene
gate a run had no way to answer. Filed and fixed as
[#40](https://github.com/wolfgang-aura/Mailman/issues/40): `mailman
acknowledge-no-test` records the argument against the exact paths the diff
touches, the way `acknowledge-duplicates` records the rows it covers.

## The patch

Three annotations in `starlette/testclient.py`, at lines 53, 374 and 417, now
name `anyio.from_thread.BlockingPortal`, which line 19 already imports.
`import anyio.abc` stays, because line 133 uses `anyio.abc.TaskStatus`, which is
not one of anyio 4.15's seven deprecated aliases.

## Independent confirmation

Afterwards, against a fresh clone of the upstream repository at the same base
commit, outside any run directory, with the run's own interpreter and anyio
4.15.0:

- Base: importing `starlette.testclient` under `-W error::DeprecationWarning`
  raises the alias warning; `pytest tests/test_testclient.py -q` and
  `pytest tests -q` both die at conftest import and collect nothing.
- Patched: the import is clean; `tests/test_testclient.py` is 58 passed; the full
  suite is 1187 passed, 14 failed, 4 skipped, 2 xfailed. All 14 failures are
  Windows-only, 13 symlink and directory-permission cases in `test_staticfiles.py`
  plus one POSIX-path regex in `test_config.py`. There is no "before" number for
  the full suite, because nothing collects.
- `ruff format --check`, `ruff check` and `mypy starlette/testclient.py` are clean.
- The declared floor holds: in the anyio 3.6.2 wheel, `class BlockingPortal` is
  defined in `anyio/from_thread.py` and `anyio/abc/__init__.py` only re-exports
  it, so the new spelling is valid across `>=3.6.2,<5`.

## What this run exposed in Mailman

Four defects, all found by running the harness rather than reading it.

[#37](https://github.com/wolfgang-aura/Mailman/issues/37), no reproduction gate,
described above. Open.

[#38](https://github.com/wolfgang-aura/Mailman/issues/38), filed from the
abandoned pytest run: `check-target` calls a merged prior attempt "closed" and
tells the operator the maintainers rejected it, while `prepare-submission`
already treats a merged duplicate as `already-fixed-upstream`. Open.

[#39](https://github.com/wolfgang-aura/Mailman/issues/39), fixed. A verification
command typed as a bare `python` was never resolved through the run toolchain, so
Mailman's own gate ran the host interpreter. The first attempt at this issue,
run `20260903T050831Z-bed67e`, failed verification on
`ModuleNotFoundError: No module named 'trio'` and spent its one revision on a
failure the candidate did not cause. The same hole passes a run whenever the host
happens to have the package installed, which is the worse half. The primary agent
was locked out for the same reason: the allow rules are built from the
verification command's first element, so the only rule generated was
`Bash(python:*)`, every absolute-path spelling of the run's interpreter was
refused, and the agent reported its reproduction from the host's anyio 4.13
instead of the environment's pinned 4.15.

[#40](https://github.com/wolfgang-aura/Mailman/issues/40), fixed, described above.

## What this does not show

One issue, one repository, one approval. The patch is three lines of annotation,
so it says nothing about whether the loop can carry a change with real design
risk. And a submission-ready package is not a merged pull request: nothing has
been sent, the account has no prior contribution to this project, and the
ecosystem screens new accounts for automation.
