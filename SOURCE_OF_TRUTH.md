# Source of truth

Last verified: 2026-09-02 in `Asia/Singapore`.

## Repository

- Canonical remote: `https://github.com/wolfgang-aura/Mailman.git`
- Default branch: `main`
- The public remote contains the foundation on `main`.
- License: Apache-2.0.
- Foundation deployment: commit `c1cc179` passed GitHub Actions on Python 3.12 and 3.14 in run `33546831897`.
- CLI adapter deployment: commit `29a5a53` passed GitHub Actions on Python 3.12 and 3.14 in run `33548539986`.
- Toolchain deployment: commit `318c9fe` passed GitHub Actions on Python 3.12 and 3.14 in run `33549213202`.
- Workspace deployment: commit `91e84ae` passed GitHub Actions on Python 3.12 and 3.14 in run `33549771368`.
- Orchestration deployment: commit `929caf5` passed GitHub Actions on Python 3.12 and 3.14 in run `33552625334`.
- Issue #9 deployment: commit `a371be1` passed GitHub Actions on Python 3.12 and 3.14 in run `33558633354`.
- Knowledge flywheel deployment: commit `d2be47a` passed GitHub Actions on Python 3.12 and 3.14 in run `33592165392`.
- External issue deployment: commit `78d048e` passed GitHub Actions on Python 3.12 and 3.14 in run `33596869316`. It carries issue ingestion, environment preparation, patch export, and the first external run records.
- Run record deployment: commit `46d0862` passed GitHub Actions on Python 3.12 and 3.14 in run `33596947293`. This is the current head of `main`.

## Development environment

- Host operating system: Windows.
- Verified Python: CPython 3.14.3. The project supports Python 3.12 and newer.
- Codex CLI: installed at `%APPDATA%\npm\codex.cmd` and authenticated. It executed a real fixture on 2026-09-02.
- Claude CLI: installed at `%APPDATA%\npm\claude.cmd` and authenticated. It executed a real review on 2026-09-02. Its adapter flags are verified against the installed build: `--print`, `--input-format`, `--output-format`, `--permission-mode` (`acceptEdits`, `plan`), `--disallowedTools`, `--model`, and the undocumented but accepted `--max-turns`.
- Both CLIs were absent from the host earlier on 2026-09-02 and were installed with `npm install -g @anthropic-ai/claude-code @openai/codex` from an independent terminal. Before that install, a Claude Code agent session reported both as present while the host did not have them. Confirm agent CLI presence from an independent terminal, never from inside an agent session.
- GitHub CLI: installed and authenticated. The user authorized the first public push on 2026-09-02.

## Target selection

Set by the repository owner on 2026-09-02, after the first external run used
`ayukhno/autosound-tcc`, a repository with almost no stars or usage.

- External targets must be projects a reader would recognize as real and
  maintained, not whatever repository happened to have an open issue.
- Small issues are fine. Obscure projects are not. The size of the issue and the
  standing of the project are separate choices.
- A candidate is only viable here if a fresh clone is clean, its test suite runs
  on Windows without native, GPU, or Qt dependencies, and a specific test can
  serve as a verification gate.
- Vet a candidate against those checks before any agent runs. Two of the first
  three candidates failed them.

## Authority and artifact boundaries

- The target repository checkout and machine-observed command results outrank agent self-reports.
- Live runs, agent transcripts, cloned target workspaces, and raw logs stay under `.mailman/` and are not public artifacts.
- Tracked examples must contain synthetic or human-reviewed, sanitized data.
- Upstream pushes, pull requests, comments, issue changes, and branch changes require explicit human approval.

## Current capability

The code can initialize a run record, capture a GitHub issue into it, generate the primary and reviewer prompts from that issue, prepare an isolated repository at the exact base commit, install the target's dependencies outside its working tree, register digest-pinned toolchain executables, enforce allowed state transitions, run one configured Codex or Claude CLI adapter, run the bounded primary and reviewer loop, execute a verification command without a shell, export a reviewable patch package, redact common token formats, and report missing local tools. `run-agent` requires a clean primary workspace at the exact base commit. Reviewer workspaces may contain changes descended from that base. The command stores private execution evidence and never changes workflow status by itself.

`mailman orchestrate` runs the bounded loop: primary work, independent verification, review, at most one revision, a second review, final independent verification, then `READY_FOR_HUMAN_REVIEW`. Approval requires a parsed `MAILMAN-VERDICT: APPROVE` line and a passing verification that Mailman runs itself. A missing, unparseable, or contradictory verdict, a second revision request, a failed verification, an agent that exits zero without a report, or an unexpected error all end the run at `BLOCKED`. See `docs/decisions/0004-bounded-orchestration.md`.

Agent executables are resolved before launch and may be pinned per run through `probe-tool` under the agent's name. A missing executable now names the tool and the command that would register it.

The Codex adapter completed a disposable fixture on 2026-09-02. It produced the expected one-line patch under the elevated native Windows sandbox. A later private run registered a bundled Python executable in the run toolchain, and Codex used it to pass the unittest. Mailman then passed the same test independently with the same executable.

The bounded loop is covered by 75 unit tests with scripted agents, and it completed a live two-model run on 2026-09-02. Private run `20260901T201921Z-0b85ed` used Codex as primary and Claude as reviewer on a disposable `slugify` fixture that started with 2 of 3 tests failing. Codex changed one line in 43.4s, Mailman's own verification passed, Claude reviewed in 29.0s and returned a parsed `APPROVE`, the final verification passed, and the run stopped at `READY_FOR_HUMAN_REVIEW` with no revision. Independent confirmation afterwards reproduced three passing tests against the same diff. See `docs/runs/0003-two-model-fixture.md`.

Private run `20260901T202957Z-ec044a` rehearsed public Mailman issue #9 against exact commit `0ba4387fd4187faff88d9a3d900412d4ad2fc367`. The issue text was captured manually. Codex changed `mailman/artifacts.py` and `tests/test_artifacts.py` in the isolated workspace. Mailman's verification passed all 47 tests after the primary stage and again after Claude returned `APPROVE`. The run reached `READY_FOR_HUMAN_REVIEW` with no revision. The reviewed change shipped on `main` in `a371be1`, passed CI on Python 3.12 and 3.14, and issue #9 was closed.

Codex reported that it could not run the pinned Python executable inside its own sandbox and exited `0` anyway. Mailman's independent verification is what carried that run, which is the case the harness exists for.

Three earlier attempts on the same day, runs `20260901T194823Z-56b438`, `20260901T200938Z-3994b1`, and `20260901T201050Z-cd908c`, blocked because the agent executable could not be resolved. They are evidence that the harness refuses to start an unverifiable agent.

Private run `20260902T051904Z-f0cd07` was the first against a repository this project does not own: `ayukhno/autosound-tcc` issue #4 at commit `284d79918991fd29c15902f32dc879487ebf31fa`, from issue URL to exported patch with no hand-written prompt. `fetch-issue` captured the issue with the GitHub CLI, `prepare-environment` initialized submodules and installed the target with its development extra into a virtual environment in the run directory, and the workspace stayed clean. Codex changed one line plus a regression test in 139.8s, Claude approved in 95.3s after running the tests itself and confirming the new test fails without the fix, and both independent verifications passed. Independent confirmation afterwards applied the exported diff to a fresh upstream clone: 36 tests passed, and reverting only the source fix reproduced the issue's exact error. The target's full suite is unusable as a gate on this host, so verification was scoped to `tests/test_dsp_state.py`. The exported patch also carries an unrelated trailing-newline change that neither agent flagged. See `docs/runs/0004-first-external-issue.md`.

The revision cycle, the second review, and a `BLOCKED` ending have live evidence as of 2026-09-02, recorded in `docs/runs/0005-revision-and-blocking-paths.md`. Run `20260902T054112Z-ae9e1b` completed `REVISE`, revision, second review, `APPROVE`, and `READY_FOR_HUMAN_REVIEW` across two review cycles. Run `20260902T054015Z-0e51db` ended `BLOCKED` when verification failed after the revision, on an issue whose acceptance criteria contradicted an existing test. The remaining blocking causes, a missing or contradictory verdict, an agent timeout, and an agent that produces no report, still have only unit coverage.

A verification pass after the primary stage does not prove work happened: in the `clamp` run it passed because the agent changed nothing. The loop now records the changed paths after each primary stage. It does not block on an empty candidate; the reviewer caught that case.

An external target can be dirty on clone. `wandb/rai-toolkit` commits `rai_toolkit/redteam/attacks.py` with CRLF while its own `.gitattributes` declares `*.py text eol=lf`, so every fresh clone is modified before any agent runs and Mailman refuses to start. That is a property of the target, not of the run.

Sanitized public run export and upstream contribution preparation are not implemented. Nothing in this project has ever contacted an upstream repository it does not own.

## Knowledge flywheel

`mailman retrospective RUN_ID` drafts `retrospective.json` and
`retrospective.md` in the private run directory from evidence Mailman already
holds. It seeds observations only for machine-observed facts and refuses to
overwrite an existing retrospective without `--force`. The taxonomy, the
weighted learning channels, the retrospective schema, and the lesson registry
with its promotion gates are implemented and unit-covered. See
`docs/decisions/0005-knowledge-flywheel.md`.

`knowledge/lessons.json` is empty. No lesson has been recorded, validated, or
promoted through the registry, and the command has not yet been run against a
live run. Skill versioning and the regression suite are not implemented, so
every retrospective records `skill_version` as `unversioned`.
