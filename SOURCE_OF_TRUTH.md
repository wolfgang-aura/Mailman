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
- Verified Codex CLI: `codex-cli 0.152.0`.
- Claude CLI: not installed or not on `PATH`.
- GitHub CLI: installed and authenticated. The user authorized the first public push on 2026-09-02.

## Authority and artifact boundaries

- The target repository checkout and machine-observed command results outrank agent self-reports.
- Live runs, agent transcripts, cloned target workspaces, and raw logs stay under `.mailman/` and are not public artifacts.
- Tracked examples must contain synthetic or human-reviewed, sanitized data.
- Upstream pushes, pull requests, comments, issue changes, and branch changes require explicit human approval.

## Current capability

The code can initialize a run record, prepare an isolated repository at the exact base commit, register digest-pinned toolchain executables, enforce allowed state transitions, run one configured Codex or Claude CLI adapter, execute a verification command without a shell, redact common token formats, and report missing local tools. `run-agent` requires a clean primary workspace at the exact base commit. Reviewer workspaces may contain changes descended from that base. The command stores private execution evidence and never changes workflow status by itself.

The Codex adapter completed a disposable fixture on 2026-09-02. It produced the expected one-line patch under the elevated native Windows sandbox. A later private run registered a bundled Python executable in the run toolchain, and Codex used it to pass the unittest. Mailman then passed the same test independently with the same executable. The Claude adapter follows Anthropic's documented print-mode flags but remains unexecuted because Claude CLI is absent.

The complete primary-reviewer loop, sanitized public export, and upstream contribution preparation are not implemented.
