# 0001. Start with a local, standard-library harness

Status: accepted on 2026-09-02.

## Decision

Build the first Mailman milestone as a Python 3.12+ command-line package with no runtime dependencies. Store private working data under `.mailman/`. Keep agent execution behind an `EngineeringAgent` interface. Capture verification commands through a shell-free subprocess runner. Require a valid workflow transition before changing run status.

Apache-2.0 is the repository license because its explicit patent grant fits agent tooling better than a bare permissive copyright license.

## Why

The first hard problem is trustworthy execution evidence and a clear approval boundary, not framework selection. A standard-library core is easy to run in unfamiliar repositories and cheap to maintain. It also exposes which dependencies real runs justify.

Raw run output cannot safely be public by default. Git ignore rules protect the working copy, while a later export command will create a reviewed and sanitized public record.

## Consequences

The CLI and schemas are plain. There is no rich terminal UI, database, hosted service, or agent SDK. Agent adapters may initially wrap installed CLIs. If API-specific features become necessary, a later decision can add narrowly scoped dependencies.

This milestone is incomplete until a real Codex and Claude run succeeds. The current development machine has Codex but not Claude, so tests cover local mechanics without claiming integration success.
