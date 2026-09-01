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

## Development environment

- Host operating system: Windows.
- Verified Python: CPython 3.14.3. The project supports Python 3.12 and newer.
- Codex CLI: no longer present on this machine as of 2026-09-02. It is not on `PATH` and no launcher exists under `%APPDATA%\npm`, `%LOCALAPPDATA%\Programs`, or `~/.codex`. The previously recorded `codex-cli 0.152.0` is gone.
- Claude CLI: installed at `%APPDATA%\npm\claude.cmd`, version `2.1.198`. Its adapter flags are verified against that build: `--print`, `--input-format`, `--output-format`, `--permission-mode` (`acceptEdits`, `plan`), `--disallowedTools`, `--model`, and the undocumented but accepted `--max-turns`. The CLI rejects unknown options, so acceptance is evidence the option exists.
- The Claude CLI is not reachable from a Claude Code agent session. Subprocesses launched from that session see a substituted `%APPDATA%\npm` directory in which the `claude` launcher is absent. Run Mailman's live agent commands from a plain PowerShell terminal instead.
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

The bounded loop is covered by 47 unit tests with scripted agents. It has not been driven end to end by two live models: Codex is uninstalled, and the Claude CLI is unreachable from the agent session that would launch it. Private run `20260901T194823Z-56b438` is the only live orchestration attempt. It validated the workspace, entered `PRIMARY_RUNNING`, failed to resolve the Claude CLI, and blocked with that reason recorded.

The two-model fixture run, sanitized public export, and upstream contribution preparation are not complete.
