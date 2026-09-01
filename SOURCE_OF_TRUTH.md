# Source of truth

Last verified: 2026-09-02 in `Asia/Singapore`.

## Repository

- Canonical remote: `https://github.com/wolfgang-aura/Mailman.git`
- Default branch: `main`
- The public remote contains the foundation on `main`.
- License: Apache-2.0.
- Foundation deployment: commit `c1cc179` passed GitHub Actions on Python 3.12 and 3.14 in run `33546831897`.

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

The code can initialize a run record, enforce allowed state transitions, execute a verification command without a shell, redact common token formats, and report missing local tools. It does not yet run an engineering agent or communicate with GitHub.
