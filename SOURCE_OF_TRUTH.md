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

## Development environment

- Host operating system: Windows.
- Verified Python: CPython 3.14.3. The project supports Python 3.12 and newer.
- Codex CLI: not installed on the host as of 2026-09-02. No launcher exists under `%APPDATA%\npm`.
- Claude CLI: not installed on the host as of 2026-09-02. No launcher exists under `%APPDATA%\npm`.
- `%APPDATA%\npm` holds `node_modules`, `openclaw`, `openclaw.cmd`, and `openclaw.ps1`, and nothing else. Verified from an independent PowerShell window where `CLAUDECODE` is unset, by `Test-Path` and by a Python `os.listdir` that agree.
- Tool observations made inside a Claude Code agent session are not authoritative about installed CLIs. That session showed `claude.cmd` and `codex.cmd` in `%APPDATA%\npm`, and `claude --version` and `codex --version` answered there, but neither file exists on the host. An `npm install -g` run from that session did not reach the host either. Verify agent CLI presence only from an independent terminal.
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

The bounded loop is covered by 47 unit tests with scripted agents. It has not been driven end to end by two live models, because neither agent CLI is installed on the host. Three live orchestration attempts on 2026-09-02, runs `20260901T194823Z-56b438`, `20260901T200938Z-3994b1`, and `20260901T201050Z-cd908c`, each validated the workspace, entered `PRIMARY_RUNNING`, failed to resolve the agent executable, and blocked with that reason recorded. The last two ran from an independent terminal. They are evidence that the harness refuses to start an unverifiable agent, not evidence that the loop works.

The two-model fixture run, sanitized public export, and upstream contribution preparation are not complete.
