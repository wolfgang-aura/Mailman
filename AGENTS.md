# Agent instructions for this repository

Applies to every model working in this repo — Claude, Codex, and anything added
later.

## PRHunt sessions

Treat `/PRHunt N`, `$prhunt N`, and requests to prepare N PRs as the same
workflow. Read [mailman/procedure.md](mailman/procedure.md) before acting.
Ask for primary and reviewer adapter/model IDs once if missing. Resume the
persistent hunt, finish every required stage, and run `mailman hunt finish`.
Its exit code, not an agent's summary, decides whether the quota is ready.

An individual run marked BLOCKED does not mean the operator is blocked.
Diagnose and repair routine failures, or drop and replace unsuitable targets.
Escalate only the dependencies allowed by the shared procedure, with evidence.
Keep new issue drafts and all upstream writes for final filing approval unless
the user has separately authorized them. Do not count issue drafts as PRs.

## Review pages for a human

Anything a person is asked to decide from is generated, never composed.

- **Never hand-write review HTML.** Not a run page, not a batch index, not a
  "packet". Write `decision.json` and run the generator.
- One run: `mailman decision RUN_ID --init`, fill it in, `mailman decision
  RUN_ID` until it exits 0, then `mailman review RUN_ID --no-open`.
- Several runs: the same per run, then `mailman packet RUN_ID ... --output
  .mailman/review-packet/index.html --no-open`.
- Both commands exit non-zero when a run has no valid decision. Treat that exit
  code as the gate; do not hand over a batch until it is 0.
- The format, the section order and the evidence classes are in
  [docs/review-page-standard.md](docs/review-page-standard.md). If a page needs
  something the format lacks, change the format there — in code, with a test —
  rather than writing a one-off page.
- The visual language is in [DESIGN.md](DESIGN.md). No CDN, no framework, no
  structural JavaScript, every agent-supplied string escaped.

A markdown README summarizing a batch is not a substitute for the packet page.
Write one if it helps, but the page is what the operator decides from.

## The rest of the harness

- `SOURCE_OF_TRUTH.md` holds durable environment and authority facts;
  `SESSION_HANDOFF.md` holds temporary resume state. Read both, and verify
  anything volatile against git and the target environment before acting.
- Tests are `unittest`, run under `pytest`. New behaviour lands with a test.
- Windows host: PowerShell syntax, Windows paths, and no compiler toolchain.
