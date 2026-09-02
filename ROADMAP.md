# Roadmap

## v0.1, repository foundation

- [x] Define public and private artifact boundaries.
- [x] Add run metadata, workflow states, command capture, timeouts, redaction, and local health checks.
- [x] Add a provisional engineering skill and provenance format.
- [x] Add tests and continuous integration.
- [x] Run the first Codex-to-Claude task against a disposable fixture repository.

## v0.2, first complete local loop

- [x] Add configurable Codex and Claude CLI adapters behind `EngineeringAgent`.
- [x] Prove the Codex adapter against a disposable failing fixture.
- [x] Clone a pinned target commit into an isolated private workspace.
- [x] Give the primary agent the issue and repository instructions.
- [x] Pass repository state and machine-captured evidence to the reviewer.
- [x] Allow one bounded revision cycle. Unit-covered; no live evidence yet.
- [x] Re-run verification and stop at `READY_FOR_HUMAN_REVIEW`.

The acceptance test is one recorded run in which two real agent processes complete the loop. Mocks alone do not count. Met on 2026-09-02 by private run `20260901T201921Z-0b85ed`, recorded in `docs/runs/0003-two-model-fixture.md`.

## v0.3, the knowledge flywheel

- [x] Decide the flywheel architecture, artifacts, and promotion gates.
- [x] Add the failure and success taxonomy and the weighted learning channels.
- [x] Add the retrospective schema, template, and `mailman retrospective`.
- [x] Add the lesson registry with gated states and rule provenance.
- [ ] Ingest a completed retrospective back into its JSON record.
- [ ] Version the engineering skill and record that version at `init-run`.
- [ ] Build the skill regression suite over historical issues.
- [ ] Aggregate categories across runs.

The acceptance test is one rule promoted into `SKILL.md` through the registry,
with the evidence, the validation, and the provenance that the gates require.
Not met. No lesson has entered the registry yet.
## v0.4, first real external issue

- [x] Capture a GitHub issue into the run record without leaking its accepted fix.
- [x] Build primary and reviewer prompts from the captured issue.
- [x] Install a target repository's dependencies outside its working tree.
- [x] Export a reviewable patch, summary, and draft pull request text.
- [x] Carry one live two-model run on an external repository with real dependencies.
- [ ] Record live evidence for the revision cycle and at least one blocking path.

The acceptance test is one recorded run against a repository Mailman does not
own, from issue URL to exported patch, with no hand-written prompt. Met on
2026-09-02 by private run `20260902T051904Z-f0cd07` against
`ayukhno/autosound-tcc` issue #4, recorded in
`docs/runs/0004-first-external-issue.md`.

## Later, only after real-run evidence

- Sanitized public run export.
- Historical issue replay without accepted-fix leakage.
- Usage and cost normalization across adapters.
- GitHub pull request preparation after human approval.
- Comparison reports across agents and prompt versions.

Dashboards, autonomous issue selection, vector databases, and hosted orchestration are out of scope until recorded runs show a concrete need.
