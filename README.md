# Mailman

Mailman is an open-source harness for testing coding agents on real software engineering issues. One agent owns the task. A second agent reviews the resulting repository state and evidence. Mailman runs important checks itself and stops before anything reaches an upstream project.

The repository starts with an intentionally narrow v0.1. It can create a private local run record, invoke one configured Codex or Claude CLI adapter, capture process evidence with a timeout and redaction, enforce the workflow state machine, and report whether the local machine has the required tools. It does not yet clone a target repository, orchestrate the complete primary-reviewer loop, or publish changes to a target repository.

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
  --reviewer claude
```

Mailman writes live run data under `.mailman/`, which Git ignores. Capture a verification command with:

```powershell
mailman verify RUN_ID -- python -m unittest discover -s tests -v
```

The command runs without a shell, has a timeout, and records its exit code, duration, and redacted output. A later export command will produce a reviewed public artifact. Do not commit `.mailman/` by force.

Run one configured agent against an already prepared Git workspace:

```powershell
mailman run-agent RUN_ID `
  --role primary `
  --prompt .mailman\runs\RUN_ID\primary-prompt.md `
  --workspace C:\path\to\target-workspace `
  --timeout 3600
```

Mailman checks that a primary workspace is clean and exactly at the run's base commit. A reviewer workspace may contain uncommitted changes or candidate commits descended from that base. Mailman sends the prompt through stdin, records the process result, and leaves workflow status unchanged. A zero process exit does not prove that the patch is correct. Review and independent verification remain separate gates.

## Human boundary

Mailman may eventually prepare branches, patches, and pull request text. It must not push, open a pull request, comment on an issue, or otherwise change an upstream project without a separate human approval step.

## Project status

This is foundation work, not a claim that the complete agent loop works. The [first Codex fixture record](docs/runs/0001-codex-fixture.md) documents both the failed and successful attempts. See [SOURCE_OF_TRUTH.md](SOURCE_OF_TRUTH.md) for verified environment facts and [ROADMAP.md](ROADMAP.md) for the next thin slice.

## License

Apache-2.0. See [LICENSE](LICENSE).
