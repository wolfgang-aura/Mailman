# 0013. Separate Luna engineering from harness work

Status: accepted on 2026-09-09.

## Context

Two PRHunt tasks using Luna Max took 5h13m and 7h27m. The slow run was not a
single difficult implementation. Mailman sent the full coordinator procedure
on every agent call, replayed the original issue on revisions, and asked the
primary, reviewer and harness to repeat discovery, reproduction and
verification. A feature request that should not have entered PRHunt grew to 13
files and 772 changed lines, crossed five review cycles and two revisions, and
consumed millions of input tokens in individual turns.

## Decision

Assign each concern once.

- Pre-screen reads the real issue before creating a run. It rejects closed
  issues and work labelled as a feature, enhancement, question, project or
  tracking item before later searches or agent work.
- The primary receives the pre-screened symbols and Mailman's recorded
  baseline. It implements the bounded fix and runs only focused checks needed
  to guide the edit.
- Mailman, not either model, owns the complete verification command. It runs
  after primary work and again after reviewer approval.
- The reviewer receives the changed paths, diff stat and primary report. It
  reviews logic, scope, tests and risk; it does not rerun the complete gate to
  prove that it reviewed.
- Codex revisions resume the same role session and receive only the new
  verification failure or review findings. They do not receive the original
  task or full procedure again.

Prepared agent prompts retain the procedure digest for provenance but replace
the procedure body with a short role boundary.

## Consequences

This removes repeated work and context before a deadline has to stop it. The
reviewer no longer supplies an independent full test execution, so Mailman's
unchanged final verification is the mechanical safety gate. Review remains
independent at the code and reasoning layer. The existing cumulative deadline,
scope gate and cycle budget remain circuit breakers, not the speed strategy.

A full live Luna benchmark is deliberately deferred: the account was at its
weekly usage limit and the operator prohibited new candidate runs. Unit tests
cover the role split, compact resumed prompts, issue rejection and final
verification behavior.
