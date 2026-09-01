# Roadmap

## v0.1, repository foundation

- [x] Define public and private artifact boundaries.
- [x] Add run metadata, workflow states, command capture, timeouts, redaction, and local health checks.
- [x] Add a provisional engineering skill and provenance format.
- [x] Add tests and continuous integration.
- [ ] Run the first Codex-to-Claude task against a disposable fixture repository.

## v0.2, first complete local loop

- Add configurable Codex and Claude CLI adapters behind `EngineeringAgent`.
- Clone a pinned target commit into an isolated worktree.
- Give the primary agent the issue and repository instructions.
- Pass repository state and machine-captured evidence to the reviewer.
- Allow one bounded revision cycle.
- Re-run verification and stop at `READY_FOR_HUMAN_REVIEW`.

The acceptance test is one recorded run in which two real agent processes complete the loop. Mocks alone do not count. Claude CLI availability currently blocks that test on the development machine.

## Later, only after real-run evidence

- Sanitized public run export.
- Historical issue replay without accepted-fix leakage.
- Usage and cost normalization across adapters.
- GitHub pull request preparation after human approval.
- Comparison reports across agents and prompt versions.

Dashboards, autonomous issue selection, vector databases, and hosted orchestration are out of scope until recorded runs show a concrete need.
