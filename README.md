# Mailman

Mailman is an open-source harness for testing coding agents on real software engineering issues. One agent owns the task. A second agent reviews the resulting repository state and evidence. Mailman runs important checks itself and stops before anything reaches an upstream project.

The repository starts with an intentionally narrow v0.1. It can create a private local run record, clone a target repository at an exact base commit, pin the executables a run may use, invoke one configured Codex or Claude CLI adapter, run the bounded primary and reviewer loop, capture process evidence with a timeout and redaction, enforce the workflow state machine, and report whether the local machine has the required tools. It does not yet publish changes to a target repository, and the bounded loop has not yet been driven end to end by two live models.

## Why this exists

Agent prose is weak evidence. A useful engineering run needs the issue, exact base commit, diff, tests, command results, independent review, unresolved risks, and a human decision. Mailman treats those artifacts as the product.

The long-term outputs are:

- an interchangeable multi-model engineering harness;
- a public record of attempted open-source work, including failures;
- an evidence-driven engineering skill refined from those runs.

Read [the project direction](docs/project-direction.md) for the full intent and [the first architecture decision](docs/decisions/0001-foundation.md) for the initial boundaries.

## Local setup

Mailman requires Python 3.12 or newer. It currently has no runtime dependencies.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
mailman doctor
python -m unittest discover -s tests -v
```

Create a local run record:

```powershell
mailman init-run `
  --repository https://github.com/owner/project `
  --issue https://github.com/owner/project/issues/123 `
  --base-commit 0123456789abcdef0123456789abcdef01234567 `
  --primary codex `
  --primary-model MODEL_ID `
  --reviewer claude `
  --reviewer-model MODEL_ID
```

Clone the recorded repository into the ignored run directory and detach it at the exact base commit:

```powershell
mailman prepare-workspace RUN_ID --timeout 600
```

Mailman records the clone and checkout commands in `workspace.json`. A second call reuses the workspace only when its origin, commit, and clean state still match. It preserves the original command evidence and increments a reuse counter.

Mailman writes live run data under `.mailman/`, which Git ignores. Capture a verification command with:

```powershell
mailman verify RUN_ID -- python -m unittest discover -s tests -v
```

The command runs without a shell, has a timeout, and records its exit code, duration, and redacted output. A later export command will produce a reviewed public artifact. Do not commit `.mailman/` by force.

Register a runtime before an agent run:

```powershell
$pythonPath = (Get-Command python).Source
mailman probe-tool RUN_ID --name python --executable $pythonPath
```

The probe records the resolved path, version result, and SHA-256 digest in the private run directory. Mailman adds verified tool paths to the saved agent prompt and refuses to use a binary whose digest changed after probing. Host verification is still authoritative because a host-accessible executable may be unavailable inside an agent sandbox.

Run one configured agent against an already prepared Git workspace:

```powershell
mailman run-agent RUN_ID `
  --role primary `
  --prompt .mailman\runs\RUN_ID\primary-prompt.md `
  --workspace C:\path\to\target-workspace `
  --timeout 3600
```

Mailman checks that a primary workspace is clean and exactly at the run's base commit. A reviewer workspace may contain uncommitted changes or candidate commits descended from that base. Mailman saves the exact prompt, including registered tool paths, then sends it through stdin. It records the process result and leaves workflow status unchanged. A zero process exit does not prove that the patch is correct. Review and independent verification remain separate gates.

Run the bounded primary and reviewer loop:

```powershell
mailman orchestrate RUN_ID `
  --primary-prompt .\prompts\primary.md `
  --reviewer-prompt .\prompts\reviewer.md `
  --workspace C:\path\to\target-workspace `
  --agent-timeout 3600 `
  -- python -m unittest discover -s tests
```

The loop runs the primary agent, verifies the result itself, runs the reviewer,
and reads one `MAILMAN-VERDICT: APPROVE` or `MAILMAN-VERDICT: REVISE` line from
the review report. It allows at most one revision, verifies again after
approval, and only then reaches `READY_FOR_HUMAN_REVIEW`. Every other ending,
including a missing or contradictory verdict, is `BLOCKED`. The command exits
`0` only for a run that is ready for a human, and writes each step to
`orchestration.json`. See [the orchestration decision](docs/decisions/0004-bounded-orchestration.md)
for the reasoning.

Agent executables are resolved at launch. Register one for a run with
`mailman probe-tool RUN_ID --name claude --executable PATH` when it is not on
`PATH`, or when a run must be pinned to an exact binary.

## Human boundary

Mailman may eventually prepare branches, patches, and pull request text. It must not push, open a pull request, comment on an issue, or otherwise change an upstream project without a separate human approval step.

## Project status

This is foundation work, not a claim that the complete agent loop works. The [first Codex fixture record](docs/runs/0001-codex-fixture.md) documents both the failed and successful attempts. See [SOURCE_OF_TRUTH.md](SOURCE_OF_TRUTH.md) for verified environment facts and [ROADMAP.md](ROADMAP.md) for the next thin slice.

## License

Apache-2.0. See [LICENSE](LICENSE).
