# PRHunt procedure, version 2

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
--reviewer ADAPTER --reviewer-model ID`. It prints a lease with an `owner`
token. Keep that token: every action that changes the hunt takes `--owner
TOKEN`. `mailman hunt status HUNT_ID` gives the next missing step and last-check
timestamp. Update it after each run. Its records survive a new conversation.
The coordinator performs the actions; the command does not spawn a background
agent or discover targets itself. The hunt records one fixed deadline at
creation, two hours unless `hunt init --time-budget-hours FLOAT` says
otherwise. Ask for a different budget before creating the hunt: the deadline is
written once and every later command reads it. Screening, setup, every
candidate, review, repair and replacement all spend that same clock.

One hunt has one coordinator. If `hunt status` shows a live lease you do not
hold, you are the second task on someone else's hunt. Do not poll it and do not
work around it. Either take a separate hunt of your own, or, when the other
coordinator is genuinely gone, take over with `mailman hunt lease HUNT_ID
--owner YOUR_TOKEN --takeover --reason "..."`, which records what you took and
why. Waiting on another coordinator is not progress; it costs the same model
allowance and produces nothing.

Renew the lease with `mailman hunt lease` during long stages. Release it with
`mailman hunt release --owner TOKEN` when you stop.

The lease covers one hunt. Two hunts in one data root still compete for the
same issues, so before you attach a candidate run `mailman hunt targets`. It
lists every `owner/repo#issue` any hunt in this root holds, which of them are
live, and which are already filed. Read that, not directory timestamps: a hunt
that says `RUNNING` with a dead lease has no coordinator, and `hunt list`
reports it as `ABANDONED` in `effective_status`. `hunt add` refuses a target
another live hunt holds, and refuses one that any hunt already filed.

Keep what you print small. A coordinator is a conversation, so every command's
output stays in context and is re-sent on every later turn. `hunt status` omits
the reasons and evidence for replaced candidates by default; the record keeps
them, and `--full` prints them when you actually need one. Do not poll: a
status check you did not act on is pure cost.

## Find and screen

A hunt is a general Python hunt. Any recognizable, maintained Python project is
a candidate, whatever field it serves. Finance is a preference, not a filter:
order the shortlist so finance targets are screened and worked first, and drop
no repository for sitting outside that field. The gates reject enough on their
own without a domain narrowing the pool before they see it.

1. Read the target's contributor instructions and AI policy. Run
   `mailman screen-target OWNER/REPO --refresh --hunt HUNT_ID --owner TOKEN`.
   Reject a failed screen.
   Human-only authorship declarations, assignment requirements and bans on
   generated descriptions are reasons to pick another target for this flow.
   A refusal phrased as an outcome fails the gate too: "we won't review
   AI-generated PRs" closes the door as firmly as "no AI-generated code".
   The gate does not stop at the guide. A link whose text or path names ai,
   llm, genai or policy is fetched through the same path — including an
   organization's `.github` repository and paths relative to the guide — and
   gated on as well; cattrs keeps its refusal in
   `python-attrs/.github/AI_POLICY.md`. The document that decided is named in
   `source`, every document read in `followed_documents`. A linked policy that
   cannot be read leaves `result` at `unknown` and fails the gate: unknown is
   not permission. A
   guide that requires a maintainer to have answered the issue first is
   recorded as the `prior-discussion` constraint, and `prescreen` then refuses
   any issue nobody from the project has replied on, under `no-maintainer-reply`.
   Freshness asks whether outside work merges here; the `responsiveness` gate
   asks how long a stranger waits for a first word, because a collaborator's
   merge satisfies freshness and says nothing about a stranger's silence. Of
   the twelve pull requests filed since 2026-09-01, one merged and six were
   closed unmerged, from repositories where an outside pull request waits
   weeks for any maintainer response. The gate reads up to 50 outside pull
   requests opened in the last 90 days (`--responsiveness-days`), excluding
   bots and the maintainers' own, and for each finds the first review, review
   comment or issue comment from an OWNER, MEMBER or COLLABORATOR. It fails
   when the median wait is over 14 days, when fewer than half were answered
   within 14 days, or when at least five were decided and more were closed
   unmerged than merged. An unanswered pull request has waited its whole age.
   Fewer than three outside pull requests in the window is `unknown`, which
   fails: a repository nobody outside has written to in three months is not
   one where ours will be read quickly. The median, the share, the merged and
   closed counts and the number sampled are recorded under the gate and
   printed in the screen.
2. Pre-screen every issue on the shortlist before opening a run on any of
   them: `mailman prescreen OWNER/REPO#N --symbols NAME NAME --hunt HUNT_ID
   --owner TOKEN`. It runs the same
   issue read, claim check, narrow duplicate search and prior-art read the run
   stage runs, writes the verdict beside the repository screens, and exits
   non-zero on a reject. The thread is read before the search: every pull
   request the issue's body, comments and timeline name is resolved with one
   `gh pr view` each, in whatever repository it lives in, and a live open one
   is `open-pull-request` while a merged one is `already-fixed-upstream`. The
   record names the reference that decided it under `cited_pull_requests`.
   A reference that turns out to be an issue is skipped without comment.
   An open pull request untouched for 60 days or more, and one closed without
   being merged, is a stale prior attempt rather than a claim: it lands in
   `stale_attempts` with the warning `stale-prior-attempt`, and the target
   passes. A dormant attempt by an OWNER or MEMBER still blocks, because that
   is a maintainer's own work in progress. An attempt a maintainer closed is
   not stale at all: the closing actor is read from the pull request's
   timeline, and a closer who is not its author and carries OWNER, MEMBER or
   COLLABORATOR blocks the issue under `maintainer-closed-attempt`. Somebody
   who speaks for the project read that change and said no. An attempt its own
   author closed is the case the stale rule is for, and a closer who cannot be
   determined leaves the attempt stale with that noted in the record. So does
   any open attempt, dormant or
   not, in a repository whose guide rejects duplicate pull requests: the screen
   records that as the `no-duplicate-pull-requests` constraint and the
   pre-screen refuses the issue under `duplicate-forbidden-open-attempt`. There
   is nothing to supersede where the second pull request is closed unread. A
   closed unmerged attempt is unaffected. Every stage reads the same rule, so
   `check-target`, `hunt status`, `hunt finish` and `prepare-submission` cannot
   disagree with the pre-screen about which attempts are dormant. A stale
   attempt is prior art you owe work: read its diff and its review comments
   before starting, and carry it into step 13.
   Closed issues and issues labelled as features,
   enhancements, questions, projects or tracking work stop after the issue
   read; do not spend search or agent work on them. Most targets fail here. One
   hunt opened 24 runs to file 3, and 14 of the 21 drops were "someone already
   fixed this": a question this answers for the price of one query instead of
   a whole run.
   `init-run` refuses an issue with no fresh passing pre-screen. Override with
   `--no-prescreen REASON` only when you mean it; the reason is recorded.
3. Search narrow first inside the run too. Give `duplicate-search` the issue
   number and the symbols the change touches with `--symbol`; the broad listing
   runs after. A record whose `decided_by` is `narrow` already found a
   duplicate and the candidate is finished. Read overlapping patches and
   maintainer responses. A broad empty search is not sufficient evidence, and
   neither is a narrow one.
4. Initialize a run at an exact current upstream commit with the hunt's model
   configuration, passing `--hunt HUNT_ID --owner TOKEN`. Add it with `mailman
   hunt add HUNT_ID RUN_ID`. A target may
   appear only once in a hunt, including after its run was dropped. Resume the
   preserved run or choose a different target.
5. Run `fetch-issue`, `duplicate-search`, `prior-art`, `target-intel` and
   `claims`. Record evidence for every acknowledgement. Never acknowledge an
   overlap just to clear a gate. Choose another candidate when uncertain.

## Prepare and prove

6. Run `prepare-workspace`. Use the personal fork account and configured
   GitHub noreply identity. Keep environments and scratch outside the target.
7. Run `draft-environment RUN_ID`, inspect its draft against CI and contribution
   instructions, adjust it and run `prepare-environment --plan PATH`. Resolve
   wheel/interpreter mismatches from the exact installation error. Do not
   install compilers or weaken the target screen to rescue a candidate.
8. Capture a baseline and a focused reproducer at the base commit. Separate
   missing dependencies from the reported defect. Run `reproduce`, then
   `check-target`. The reproduction must check the reported behavior by
   machine. A human reading or locally convenient proxy may be kept as
   evidence, but it does not authorize agent work. A bug that no longer
   reproduces means replace the candidate. Mailman snapshots any workspace file
   named by the reproduction command. The primary must compare that exact source
   with the report before running a command; a semantic mismatch stops the run
   before review or verification.
9. Use `build-prompts RUN_ID -- EXECUTABLE ARG ...` to record verification argv.
   Everything after `--` is run as a program, so it starts with an executable
   and carries no Mailman option; the CLI refuses the common mistakes but not
   all of them. `orchestrate RUN_ID` reads that same command. Do not supply
   custom prompts or call `run-agent` to bypass this sequence. `build-prompts`
   must resolve at least one exact existing start file from the issue or
   reproducer. Supply `--start-file PATH` when it cannot. Never start an agent
   with only a broad symbol. Both model roles have a ten-minute wall-clock
   limit. Mailman does not use command counts as a completion test. A completed
   report and Codex `turn.completed` event keep the candidate even when an
   optional operator-supplied command cap races with process exit. Both
   model roles must use the same recorded procedure and the independent
   verification gate.
   Before the primary starts, Mailman runs that exact argv on the clean base
   tree. It refuses a failing command or one that changes candidate bytes.
   The generated task carries the pre-screened symbols and the recorded
   baseline. The primary starts with the exact work-order files, runs only
   focused checks needed to guide the edit, and does not redo discovery,
   reproduction or the full gate. When the issue says how another tool or
   the spec handles the case, the work order quotes it and both roles must
   say which framing the patch follows. For `gpt-5.6-luna`, use
   `orchestrate --reasoning-effort medium`; higher effort is not the default for
   these bounded tasks and must be an intentional operator override.

## Repair without escalating routine work

10. Run `orchestrate`. An `ENGINEERING_COMPLETE` outcome means finish the
    package. A `BLOCKED` run is a local stop, not a request for the user.
    Every run uses the hunt's one cumulative deadline from `hunt init`, not a
    fresh deadline per candidate or agent call. Mailman binds selection,
    setup and agent commands to that deadline and clamps each subprocess to the
    remaining time when it starts. Once engineering completes, deterministic
    review-page, handoff and filing gates remain available after the deadline;
    blocking them cannot save agent time and can strand a valid candidate.
    Pre-run commands require `--hunt` when the
    data root has more than one live hunt. Codex reports usage only when a turn
    completes. Mailman records input and cached-input tokens separately,
    totals input per role across resumed turns, and records an overrun without
    discarding a completed report or candidate. The configured token limit is
    advisory because Codex reports usage only after the turn; the wall deadline
    remains the enforceable in-flight limit. Later turns resume the same
    per-role session so they do not
    rediscover the repository. Revision prompts carry only the new failure or
    review findings. Reviewer prompts carry the changed paths, diff stat and primary
    report tail; the reviewer inspects logic, scope, tests and risk instead of
    duplicating the full verification command. Both roles receive run-owned
    temporary storage outside the candidate workspace, with pytest cache writes
    disabled, so environment noise cannot consume the candidate revision.
    Mailman owns that command and
    runs it after the primary and again after approval. The first primary stage
    and every revision stop before
    review if the candidate exceeds 8 files or 500 changed lines. Treat that as
    evidence that the issue is too broad for this hunt and replace it.
11. On a failure, read the exact failed command, stage, exit code and output.
    Fix the evidenced cause. After one failed fix, reproduce and instrument
    before another edit. Never retry an identical command indefinitely.
    For an unusable review, preserve the patch, repair the environment, then
    run `resume-review`. Do not restart a dirty primary workspace.
    Reviewer passes are budgeted per run, not per command: `--max-review-cycles`
    counts across every `orchestrate` and `resume-review`. When a run blocks on
    a spent budget, replace the candidate only if the hunt deadline still has
    time. An attached run cannot extend that deadline with
    `--time-budget-override-reason`. Do not resume repeatedly to buy more
    passes. Keep agent shell
    output narrow: never print a whole large file or an unrestricted
    repository-wide search when a bounded slice answers the question.
    A run whose `hunt status` carries a `health` state stopped for a reason
    outside the candidate. `USAGE_LIMIT` means the account, not the code, and
    the record holds the stage and the exact resume command; retrying the
    candidate spends the same allowance again. `INFRASTRUCTURE` means the host,
    such as an unwritable temporary directory. Neither is a candidate defect and
    neither is a reason to drop a target.
12. Drop duplicate, assigned, prohibited, unreproducible or unsuitable targets.
    Record why and continue searching until N candidates pass. A dropped run
    costs no user decision. A failed candidate may be replaced after bounded
    repair; the quota must never lower the quality bar.

## Complete the package

13. After engineering completes, export the patch and prepare the submission.
    Fix hygiene, missing coverage and formatting findings yourself. Write the
    final PR body with the trigger, before/after behaviour, cause, scope and
    actual verification results. Follow docs/pull-request-standard.md and the
    target's template. Remove placeholders and claims that only the human can
    make true. Include required AI disclosure. Do not invent human testing.
    When the run carries a `stale-prior-attempt`, the body must name the
    attempt it supersedes, link it, and say in one sentence how this change
    differs from it. `prepare-submission` records the attempts under
    `stale-prior-attempt` as a non-blocking finding; a body that ignores them
    reads to a maintainer as a second contributor racing the first.
14. Write decision.json using `decision --init` and the schema in
    docs/review-page-standard.md. Keep evidence classes distinct. Questions
    must be genuine user choices, never tasks you can do. Use [] otherwise.
    Do not claim SEND for an incomplete or unverifiable candidate. Run
    `decision`, then `finalize-review`. If candidate bytes changed after the
    final verification, re-review and verify them before finalizing.
15. Commit the reviewed change locally with the configured identity. Check
    authors, refresh duplicates and claims, then prepare `handoff` with the
    exact local branch and final body. Run `handoff-check`. Keep all filings
    and upstream writes pending. For a self-sourced defect, prepare any required
    issue text alongside the PR and ask for approval of the ordered filings.
    A closed issue is refused; an issue with no maintainer reply is flagged
    in the block, and on one the operator decides whether to file or to ask
    on the issue first.
    After filing, the run writes to two threads only: its own issue and its
    own pull request. Never a pull request someone else opened. One reply per
    thread per event, under 120 words unless a maintainer asked questions. If
    our pull request is closed or overtaken, record `provenance --pr N
    --superseded-by M` first; after that, `handoff` refuses all three threads
    and allows one `--closing-reply`. Nothing else goes out.
16. Refresh the aging evidence for every ready candidate together with
    `mailman hunt refresh HUNT_ID --owner TOKEN` immediately before finishing.
    Duplicate searches and claim reads expire in an hour, and refreshing them
    one at a time is how a hunt with two ready candidates reported zero.
17. `mailman hunt finish HUNT_ID --owner TOKEN` must exit 0. It re-reads the
    target for every candidate still to be filed, because those are the ones
    about to be pushed and a rival pull request has appeared 94 minutes after a
    run finished. A candidate already filed counts toward the quota and is not
    re-read: its pull request is open, and rechecking it finds that pull
    request and calls the candidate replaceable. Pass `--no-refresh` only when you are offline. It counts only SEND decisions
    with passing filing checks and generates the existing packet format.
    Inspect that generated packet visually. Never hand-write review HTML.
    Present the packet and ask for approval of the exact filings once.

Do not hold finished work back until the quota is met. `hunt status` writes a
checkpoint page as soon as any candidate is ready. Show it. A candidate that is
ready and invisible is indistinguishable to the user from no candidate at all,
and that is what fifteen hours of silence looked like.

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

When a maintainer asks for changes on a filed pull request, the run is not
finished and must not be left reading `READY_FOR_HUMAN_REVIEW`. Run
`mailman fetch-review RUN_ID --pr URL`. It reads the review bodies and the
inline comments into the run and moves it to `MAINTAINER_CHANGES_REQUESTED`.
Run `build-prompts` again so both agents get the maintainer's own words, then
resume the run through `resume-review`. Before pushing the revision, run
`mailman revision-response RUN_ID --init`, answer every requested change with
`answered` or `declined`, give every declined one a note saying why, and run
`mailman revision-response RUN_ID` until it exits 0. A revision that silently
skips one of the maintainer's points costs a second review round.

Record each filing in the hunt as it happens: `mailman hunt file HUNT_ID RUN_ID
--owner TOKEN --pr-url https://github.com/OWNER/REPO/pull/N --commit SHA`. The
hunt becomes `FILED` once every requested candidate carries a pull request, and
a `FILED` hunt is read-only: `finish`, `refresh-procedure` and further checks
cannot rewrite the gate result and procedure digest that its open pull requests
rest on. Without this the record still reads `AWAITING_FILING_APPROVAL`, and the
next session offers the operator work he has already filed.

## Watch what was filed

A filed pull request is not finished work. Run `mailman hunt watch` before
starting a new hunt and once a day between hunts. It reads every filed pull
request from both ledgers, the `filed` rows in `hunts/*/hunt.json` and every
`runs/*/submission/provenance.json` that names a pull request, asks GitHub
about each one, prints one line per pull request, and writes the reading to
`.mailman/filed-watch.json`. That file's `checked_at` is the last time the
watch completed; a stale one means nobody has looked. `--json` prints the
record instead of the table.

The `STATUS` column decides. `ok` is an open pull request with green or
pending checks, a base it can still merge onto, and no outside comment newer
than our last commit or reply. `attention` names the reason on the next line:
a failing check by name, `mergeable_state behind` (the base moved on; rebase
and push) or `dirty` (a conflict), or an unanswered comment or review from
somebody other than the author, bots excluded, newer than anything we did on
the thread. `unknown` is a row `gh` could not read, a 404, a rate limit, a
timeout, and it counts as work because a reading with a hole in it proves
nothing. `merged` and `closed` are history and never need work. The command
exits 1 while any row is `attention` or `unknown`, so a scheduled run can page
on the exit code alone. Act on an `attention` row through the normal path:
`fetch-review` for a maintainer's request, a rebase for `behind`, the failing
test for a red check. edgartools#1329 failed CI three hours before anybody
looked; this is the look.

For Mailman defects discovered during a hunt, search existing GitHub issues.
Update local evidence for existing issues. Draft new issues privately and
include them in the final filing approval, unless the user separately
authorized immediate issue filing. Capture repeatable lessons in the existing
retrospective and knowledge records; never promote one run into a universal rule.
