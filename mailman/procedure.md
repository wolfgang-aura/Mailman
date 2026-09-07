# PRHunt procedure, version 1

This is the procedure for every Mailman coordinator, regardless of model.
Read it on `/PRHunt N`, `$prhunt N`, or a request to hunt for N pull requests.
N counts distinct, complete PR candidates awaiting filing approval. A dropped
candidate, an issue draft, or a passing test alone does not count.

## Start and resume

Read AGENTS.md, SOURCE_OF_TRUTH.md and SESSION_HANDOFF.md when present. Inspect
Git status, branch and recent commits. Resume an existing hunt before creating
another. Ask once which primary and reviewer adapter/model IDs to use if the
user has not supplied them. Record exact IDs, never choose a vendor default
or silently substitute another model. Mailman currently supports Codex and
Claude CLI adapters. Confirm each selected CLI is installed and authenticated
with a harmless local fixture before spending a target run.

Create the hunt with `mailman hunt init N --primary ADAPTER --primary-model ID
--reviewer ADAPTER --reviewer-model ID`. `mailman hunt status HUNT_ID` gives the
next missing step and last-check timestamp. Update it after each run. Its
records survive a new conversation. The coordinator performs the actions;
the command does not spawn a background agent or discover targets itself.

## Find and screen

1. Read the target's contributor instructions and AI policy. Run
   `mailman screen-target OWNER/REPO --refresh`. Reject a failed screen.
   Human-only authorship declarations, assignment requirements and bans on
   generated descriptions are reasons to pick another target for this flow.
2. Search open and closed PRs by exact affected symbol, behaviour and issue
   number, using more than one query where needed. Read overlapping patches
   and maintainer responses. A broad empty search is not sufficient evidence.
3. Initialize a run at an exact current upstream commit with the hunt's model
   configuration. Add it with `mailman hunt add HUNT_ID RUN_ID`.
4. Run `fetch-issue`, `duplicate-search`, `prior-art`, `target-intel` and
   `claims`. Record evidence for every acknowledgement. Never acknowledge an
   overlap just to clear a gate. Choose another candidate when uncertain.

## Prepare and prove

5. Run `prepare-workspace`. Use the personal fork account and configured
   GitHub noreply identity. Keep environments and scratch outside the target.
6. Run `draft-environment RUN_ID`, inspect its draft against CI and contribution
   instructions, adjust it and run `prepare-environment --plan PATH`. Resolve
   wheel/interpreter mismatches from the exact installation error. Do not
   install compilers or weaken the target screen to rescue a candidate.
7. Capture a baseline and a focused reproducer at the base commit. Separate
   missing dependencies from the reported defect. Run `reproduce`, then
   `check-target`. A bug that no longer reproduces means replace the candidate.
8. Use `build-prompts RUN_ID -- EXECUTABLE ARG ...` to record verification argv.
   `orchestrate RUN_ID` reads that same command. Do not supply custom prompts
   or call `run-agent` to bypass this sequence. Both model roles must use the
   same recorded procedure and the independent verification gate.

## Repair without escalating routine work

9. Run `orchestrate`. An `ENGINEERING_COMPLETE` outcome means finish the
   package. A `BLOCKED` run is a local stop, not a request for the user.
10. On a failure, read the exact failed command, stage, exit code and output.
    Fix the evidenced cause. After one failed fix, reproduce and instrument
    before another edit. Never retry an identical command indefinitely.
    For an unusable review, preserve the patch, repair the environment, then
    run `resume-review`. Do not restart a dirty primary workspace.
11. Drop duplicate, assigned, prohibited, unreproducible or unsuitable targets.
    Record why and continue searching until N candidates pass. A dropped run
    costs no user decision. A failed candidate may be replaced after bounded
    repair; the quota must never lower the quality bar.

## Complete the package

12. After engineering completes, export the patch and prepare the submission.
    Fix hygiene, missing coverage and formatting findings yourself. Write the
    final PR body with the trigger, before/after behaviour, cause, scope and
    actual verification results. Follow docs/pull-request-standard.md and the
    target's template. Remove placeholders and claims that only the human can
    make true. Include required AI disclosure. Do not invent human testing.
13. Write decision.json using `decision --init` and the schema in
    docs/review-page-standard.md. Keep evidence classes distinct. Questions
    must be genuine user choices, never tasks you can do. Use [] otherwise.
    Do not claim SEND for an incomplete or unverifiable candidate. Run
    `decision`, then `finalize-review`. If candidate bytes changed after the
    final verification, re-review and verify them before finalizing.
14. Commit the reviewed change locally with the configured identity. Check
    authors, refresh duplicates and claims, then prepare `handoff` with the
    exact local branch and final body. Run `handoff-check`. Keep all filings
    and upstream writes pending. For a self-sourced defect, prepare any required
    issue text alongside the PR and ask for approval of the ordered filings.
15. `mailman hunt finish HUNT_ID` must exit 0. It counts only SEND decisions
    with passing filing checks and generates the existing packet format.
    Inspect that generated packet visually. Never hand-write review HTML.
    Present the packet and ask for approval of the exact filings once.

## What reaches the user

Only escalate unavailable user-controlled authentication, an explicit budget
or scope decision, conflicting user instructions, or final publication
approval. Record the exact error, attempted remedy, why another candidate or
local remedy cannot solve it, and the smallest action the user must take.
Use `hunt escalate` with that evidence. Missing files, unknown commands,
reviewer formatting, stale searches, failed tests and full candidate queues
are coordinator work. Do not call them human blockers.

The CLI validates evidence and completion. It cannot guarantee that a model
will obey prose, nor guarantee N suitable defects exist. At an explicit user
budget limit, preserve the partial work and report the measured shortfall.
Do not make up candidates, quietly change models, or spend beyond that limit.

## Approval and follow-through

Approval applies only to the exact packet, body, branch and destinations shown.
After approval, refresh expired searches, run the filing checks again and
publish only if the checks still pass. If material bytes or destination change,
regenerate the packet and obtain approval for that change. Use body files for
GitHub commands. Record filing URLs and provenance. Keep issues open until the
change is deployed and live-verified. No automatic upstream messages.

For Mailman defects discovered during a hunt, search existing GitHub issues.
Update local evidence for existing issues. Draft new issues privately and
include them in the final filing approval, unless the user separately
authorized immediate issue filing. Capture repeatable lessons in the existing
retrospective and knowledge records; never promote one run into a universal rule.
