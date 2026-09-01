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
- Current `main`: commit `0ba4387` passed GitHub Actions on Python 3.12 and 3.14 in run `33555224274`.

## Development environment

- Host operating system: Windows.
- Verified Python: CPython 3.14.3. The project supports Python 3.12 and newer.
- Codex CLI: installed at `%APPDATA%\npm\codex.cmd` and authenticated. It executed a real fixture on 2026-09-02.
- Claude CLI: installed at `%APPDATA%\npm\claude.cmd` and authenticated. It executed a real review on 2026-09-02. Its adapter flags are verified against the installed build: `--print`, `--input-format`, `--output-format`, `--permission-mode` (`acceptEdits`, `plan`), `--disallowedTools`, `--model`, and the undocumented but accepted `--max-turns`.
- Both CLIs were absent from the host earlier on 2026-09-02 and were installed with `npm install -g @anthropic-ai/claude-code @openai/codex` from an independent terminal. Before that install, a Claude Code agent session reported both as present while the host did not have them. Confirm agent CLI presence from an independent terminal, never from inside an agent session.
- GitHub CLI: installed and authenticated. The user authorized the first public push on 2026-09-02.

## Authority and artifact boundaries

- The target repository checkout and machine-observed command results outrank agent self-reports.
- Live runs, agent transcripts, cloned target workspaces, and raw logs stay under `.mailman/` and are not public artifacts.
- Tracked examples must contain synthetic or human-reviewed, sanitized data.
- Upstream pushes, pull requests, comments, issue changes, and branch changes require explicit human approval.

## Current capability

The code can initialize a run record, prepare an isolated repository at the exact base commit, register digest-pinned toolchain executables, enforce allowed state transitions, run one configured Codex or Claude CLI adapter, run the bounded primary and reviewer loop, execute a verification command without a shell, redact common token formats, and report missing local tools. `run-agent` requires a clean primary workspace at the exact base commit. Reviewer workspaces may contain changes descended from that base. The command stores private execution evidence and never changes workflow status by itself.

`mailman orchestrate` runs the bounded loop: primary work, independent verification, review, at most one revision, a second review, final independent verification, then `READY_FOR_HUMAN_REVIEW`. Approval requires a parsed `MAILMAN-VERDICT: APPROVE` line and a passing verification that Mailman runs itself. A missing, unparseable, or contradictory verdict, a second revision request, a failed verification, an agent that exits zero without a report, or an unexpected error all end the run at `BLOCKED`. See `docs/decisions/0004-bounded-orchestration.md`.

Agent executables are resolved before launch and may be pinned per run through `probe-tool` under the agent's name. A missing executable now names the tool and the command that would register it.

The Codex adapter completed a disposable fixture on 2026-09-02. It produced the expected one-line patch under the elevated native Windows sandbox. A later private run registered a bundled Python executable in the run toolchain, and Codex used it to pass the unittest. Mailman then passed the same test independently with the same executable.

The bounded loop is covered by 47 unit tests with scripted agents, and it completed a live two-model run on 2026-09-02. Private run `20260901T201921Z-0b85ed` used Codex as primary and Claude as reviewer on a disposable `slugify` fixture that started with 2 of 3 tests failing. Codex changed one line in 43.4s, Mailman's own verification passed, Claude reviewed in 29.0s and returned a parsed `APPROVE`, the final verification passed, and the run stopped at `READY_FOR_HUMAN_REVIEW` with no revision. Independent confirmation afterwards reproduced three passing tests against the same diff. See `docs/runs/0003-two-model-fixture.md`.

Private run `20260901T202957Z-ec044a` rehearsed public Mailman issue #9 against exact commit `0ba4387fd4187faff88d9a3d900412d4ad2fc367`. The issue text was captured manually. Codex changed `mailman/artifacts.py` and `tests/test_artifacts.py` in the isolated workspace. Mailman's verification passed all 47 tests after the primary stage and again after Claude returned `APPROVE`. The run reached `READY_FOR_HUMAN_REVIEW` with no revision. The candidate is not applied to `main`, issue #9 remains open, and no upstream state changed.

Codex reported that it could not run the pinned Python executable inside its own sandbox and exited `0` anyway. Mailman's independent verification is what carried that run, which is the case the harness exists for.

Three earlier attempts on the same day, runs `20260901T194823Z-56b438`, `20260901T200938Z-3994b1`, and `20260901T201050Z-cd908c`, blocked because the agent executable could not be resolved. They are evidence that the harness refuses to start an unverifiable agent.

The revision path, the second review, and the blocking paths have unit coverage but no live evidence yet. Issue ingestion, target dependency installation, and patch export also remain unimplemented. The issue #9 rehearsal did not exercise dependency installation because Mailman has no runtime dependencies.

The sanitized public export command and upstream contribution preparation are not implemented.
