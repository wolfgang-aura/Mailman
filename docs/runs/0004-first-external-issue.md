# 0004. First external issue, end to end

Date: 2026-09-02. Private run `20260902T051904Z-f0cd07`. Sanitized from local evidence.

The first run against a repository this project does not own, from an issue URL
to an exported patch, with no hand-written prompt. Codex was the primary
engineer and Claude the reviewer.

## Target

`ayukhno/autosound-tcc`, Apache-2.0, issue
[#4](https://github.com/ayukhno/autosound-tcc/issues/4): `load_hardware_controls`
called `Path.read_text()` with no `encoding=`, so a `project.json` containing
Cyrillic text raised `UnicodeDecodeError` under a cp1252 locale and blanked two
panels with no message. Base commit `284d79918991fd29c15902f32dc879487ebf31fa`.

Nothing was sent upstream. No comment, no branch, no pull request.

## What the harness did on its own

| Stage | Result |
| --- | --- |
| `fetch-issue` | 4340 characters captured with the GitHub CLI, no comments |
| `prepare-workspace` | clean checkout at the base commit |
| `prepare-environment` | submodules initialized, virtual environment built, `pip install -e .[dev]`, interpreter registered and digest-pinned |
| `build-prompts` | primary and reviewer prompts generated from the captured issue |
| Primary, Codex | exit `0` in 139.8s, report present |
| Verification after primary | exit `0` in 0.80s |
| Reviewer, Claude | exit `0` in 95.3s, report present |
| Verdict | `APPROVE`, parsed from one line |
| Final verification | exit `0` in 0.74s |
| Final status | `READY_FOR_HUMAN_REVIEW`, no revision, 1 review cycle |
| `export-patch` | 2 changed files on suggested branch `mailman/issue-4` |

The change was one line plus a regression test:

```diff
-    data = json.loads(path.read_text())
+    data = json.loads(path.read_text(encoding="utf-8"))
```

The test writes a Ukrainian `project.json`, monkeypatches `Path.read_text` to
default to cp1252, and asserts the loader still reads it. That reproduces the
reporter's locale on a machine that does not have it.

## The verification command is narrower than the suite

The target's full test suite is not usable as a gate on this host. At the base
commit, before any agent ran, `pytest tests` produced roughly fifteen failures
and then died at about a third of the way through with a native access
violation, exit code `3221225477`. That is a property of running a Qt test
suite on this Windows machine, not of the issue.

Verification was therefore scoped to `tests/test_dsp_state.py`, the file that
covers the changed function: 35 tests passing in 0.24s at the base commit, 36
after the change. A narrower gate is weaker, and saying so is the point of
recording it.

## Codex again could not verify its own work

Its report says the pinned interpreter would not start because the sandbox
denied access to the base executable, so pytest never ran. It said so plainly
and did not claim a passing test, then exited `0`.

That is the second run in which the primary agent's exit code carried no
engineering information. Mailman ran the tests itself, twice, and those runs are
what allowed the final transition.

## The reviewer did more than read

Claude ran the tests itself, then reverted the source fix while keeping the new
test and confirmed it fails with the same `UnicodeDecodeError` the issue
reports. That is the strongest reviewer evidence any run has produced so far,
and it is exactly what the reviewer prompt asks for: a check the reviewer
captured, not a check it assumed.

## Independent confirmation

Afterwards, against a fresh clone of the upstream repository at the same base
commit, outside any run directory: `git apply` accepted `changes.diff`, 36 tests
passed, and reverting only the source fix made the new test fail with the issue's
exact error.

## Known flaw in the exported patch

The diff also removes a trailing blank line at the end of
`tests/test_dsp_state.py`. It is unrelated to the issue, the primary prompt asks
for no drive-by changes, and neither the primary nor the reviewer mentioned it.
It would have to come out before this went anywhere near a maintainer.

## What this does not show

One issue, one language, one small function, an approval on the first cycle,
and a scoped verification command. The revision path, the second review, and
every blocking path still have unit coverage and no live evidence. Nothing here
tests the harness against a change that should have been rejected.
