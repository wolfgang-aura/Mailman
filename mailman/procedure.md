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
agent or discover targets itself. A hunt has no deadline unless `hunt init
--time-budget-hours FLOAT` sets one; each model role's ten-minute limit and
each run's own budget already bound agent work (#166). When a budget is set it
is one fixed deadline, written once and read by every later command, and
screening, setup, every candidate, review, repair and replacement all spend
that same clock.

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

Pick targets with `mailman hunt targets --engaged-only`: it lists only issues a
maintainer filed or replied on, because an untriaged run never counts ready.
Without the flag those rows still come first, and rows from screens written
before the flags existed show `engagement: unknown`; refresh those screens with
the command the warning prints.

A shortlist is frozen when its screen is written, so issues opened since then
never reach `hunt targets`. When it runs dry, run `mailman hunt sweep HUNT_ID
[--since-days 60]` before screening new repositories: one paced core read per
passing screen, returning open, unassigned issues with a defect label or a
maintainer's invitation (`Needs PR`, `help wanted`, `confirmed`), no undecided
or already-fixed label, and no cross-referenced pull request, that nobody has
prescreened, engaged ones first. It exits non-zero when GitHub refused a read.
Pre-screen its rows as usual (#226, #259).

A failed screen is not re-read on its own. When the gate's rules change, the
`hunt targets` warning names the screens that failed only on responsiveness
under older rules and whose stored numbers could pass now, most stars first.
Refresh a few of them before screening new repositories; each refresh costs
about 200 core API calls (#227).

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

Build the pool before the clock starts. `screen-target` and `prescreen` run
without `--hunt` and spend no hunt time, and a passing pre-screen stays fresh
for 24 hours. Do not run `hunt init N` until at least N+1 issues have passed
both the pre-screen and your own read of the thread; the hunt's time is then
spent on engineering. Go wide before deep: at most three pre-screens per
repository, then move to the next one. Pre-screen only an issue where a
maintainer confirmed the bug, reproduced it or asked for a fix. A maintainer
reply on its own is not that. Hunt `20260928T094000Z-3b91d9` pre-screened 31
issues across 12 repositories and passed one: three repositories took 19 of
the 31, and 21 of the 28 hand rejections were threads where a maintainer
replied without confirming a bug (could not reproduce, works as designed,
design still open, the reporter's own environment). Screen one repository at
a time; a screen costs about 200 GitHub core calls.

1. Read the target's contributor instructions and AI policy. Run
   `mailman screen-target OWNER/REPO --refresh --hunt HUNT_ID --owner TOKEN`.
   Reject a failed screen.
   Human-only authorship declarations, assignment requirements and bans on
   generated descriptions are reasons to pick another target for this flow.
   A refusal phrased as an outcome fails the gate too: "we won't review
   AI-generated PRs" closes the door as firmly as "no AI-generated code".
   The gate does not stop at the guide. A link whose text or path names ai,
   llm, genai or policy is fetched through the same path — including an
   organization's `.github` repository, paths relative to the guide and a
   page on the project's own site — and gated on as well; cattrs keeps its
   refusal in `python-attrs/.github/AI_POLICY.md`, pretix keeps its
   disclosure rule at docs.pretix.eu. The document that decided is named in
   `source`, every document read in `followed_documents`. A linked policy that
   cannot be read leaves `result` at `unknown` and fails the gate: unknown is
   not permission.
   The `host` gate reads `pyproject.toml` against what this host is known to
   refuse: a required dependency Application Control blocks at import
   (numba, PyQt6), or a framework whose tests need a service the host lacks
   (a Frappe app needs a bench). Either fails the screen before a run is
   opened; the same package in an extra only warns, and the pre-screen
   should reject an issue whose traceback runs through that extra. A
   guide that requires a maintainer to have answered the issue first is
   recorded as the `prior-discussion` constraint, and `prescreen` then refuses
   any issue nobody from the project has replied on, under `no-maintainer-reply`.
   An issue labelled `needs-discussion`, `design`, `rfc` or the like is
   rejected under `issue-under-discussion`: the maintainers have said the
   design is not settled, and a patch on it is pressure, not help. The only
   upstream write for such an issue is a short comment with evidence, drafted
   for approval. A pull request body that cites a specification carries the
   clause number and the quoted sentence, read from the text; the patch's
   side in any disagreement between reference tools is named in the body.
   A guide that needs a signed Contributor License Agreement before a first
   pull request merges is recorded as the `cla` constraint (`requires_cla`);
   signing is the operator's act, so it goes to them before the run is filed,
   not after cla-bot fails the first check.
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
   within 14 days, or when at least five were decided and under 30% of them
   merged (until 2026-09-29 it was "more closed than merged", which failed
   huggingface_hub at 20 merged and 21 closed). An unanswered pull request has waited its whole age.
   Fewer than three outside pull requests in the window is `unknown`, which
   fails: a repository nobody outside has written to in three months is not
   one where ours will be read quickly. The median, the share, the merged and
   closed counts and the number sampled are recorded under the gate and
   printed in the screen.
   A passing screen prints the shortlist: every unclaimed workable issue the
   saturation gate found, ranked, and `screen-target --json` prints the same
   rows as JSON. Pre-screen from the top. The rank is a strict order of three
   reasons, and each row names the ones it holds: `maintainer-invited` (a
   comment by an OWNER, MEMBER or COLLABORATOR, or the report itself when its
   author is one, asking for the pull request in words such as "PRs welcome"
   or "feel free to open a PR", or a `help wanted`, `good first issue` or
   `contributions welcome` label) outranks everything; then `recent` (opened,
   or last written on by a maintainer, within 14 days); then `no-linked-pr`
   (no pull request linked to or cited by the issue, open, merged or dormant).
   Across the last two hunts 72 of 106 pre-screened issues died on somebody's
   open pull request, because the newest unclaimed issue is where every
   contributor looks first. An issue a maintainer asked for and nobody took
   is the one worth the pre-screen. The pre-screen record repeats the score
   under `ranking`, from the whole thread rather than one page of it, and that
   one is the one to trust when the two differ.
2. Pre-screen every issue on the shortlist before opening a run on any of
   them: `mailman prescreen OWNER/REPO#N --symbols NAME NAME --hunt HUNT_ID
   --owner TOKEN`. It runs the same
   issue read, claim check, narrow duplicate search and prior-art read the run
   stage runs, writes the verdict beside the repository screens, and exits
   non-zero on a reject. The thread is read before the search: every pull
   request the issue's body, comments and timeline name is resolved with one
   `gh pr view` each, in whatever repository it lives in, and a live open one
   is `open-pull-request` while a merged one is `already-fixed-upstream`,
   unless the issue's own body is where it is named: a reporter cites a
   merged pull request as the cause or the context of the report, not its
   fix, so that one is the `cited-merged-in-body` warning and the
   reproduction at base decides. One merged more than 90 days before the
   issue was opened shipped in releases the reporter had, so it is the
   `cited-merged-before-issue` warning for the same reason. A maintainer
   comment naming the issue inside a same-repository issue that
   cross-references it is the `maintainer-remark-elsewhere` warning; both
   prompts quote it, and a fix shape it prefers outranks the agent's. The
   record names the reference that decided it under `cited_pull_requests`.
   A reference that turns out to be an issue is skipped without comment.
   An open pull request untouched for 60 days or more, and one closed without
   being merged, is a stale prior attempt rather than a claim: it lands in
   `stale_attempts` with the warning `stale-prior-attempt`, and the target
   passes. A dormant attempt by an OWNER or MEMBER still blocks, because that
   is a maintainer's own work in progress. Each attempt the search finds
   records its author's association; a closed unmerged attempt written by an
   OWNER, MEMBER, COLLABORATOR or a login in the screen's maintainer set, and
   closed by no other maintainer (its author, a stale bot, inactivity), blocks
   under `maintainer-pending-fix`. That is the project's own fix parked, not
   an outsider's abandoned one. When the closer could not be read, the
   attempt keeps the older handling. An attempt a maintainer closed is
   not stale at all: the closing actor is read from the pull request's
   timeline, and a closer who is not its author and carries OWNER, MEMBER or
   COLLABORATOR blocks the issue under `maintainer-closed-attempt`. Somebody
   who speaks for the project read that change and said no. When a
   maintainer labels the issue confirmed (or bug, accepted, needs-PR) after
   that closure, the block becomes the warning
   `maintainer-closed-attempt-reaffirmed`: the closure turned down the
   change, not the bug. Read why, and do not repeat its approach (#378). An attempt its own
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
   Closed issues and issues labelled as questions, projects or tracking work
   stop after the issue read; do not spend search or agent work on them. A
   feature or enhancement label stops after the thread read unless a
   maintainer there asked for a pull request ("PR welcome", "happy to
   merge"); an invited one passes with an `invited-enhancement` warning and
   counts toward the quota. Most targets fail here. One
   hunt opened 24 runs to file 3, and 14 of the 21 drops were "someone already
   fixed this": a question this answers for the price of one query instead of
   a whole run.
   `init-run` refuses an issue with no fresh passing pre-screen. Override with
   `--no-prescreen REASON` only when you mean it; the reason is recorded.
   A maintainer comment that reserves the issue for human contributors, or
   warns that agent-written pull requests may be rejected, blocks it under
   `issue-reserved-for-humans`. A maintainer comment that leaves the design
   open ("not sure how we should", "one option is... another", "we could add
   a config option") with no later maintainer comment settling it blocks it
   under `design-undecided`. A project voice turning the report down ("works
   as intended", "I don't think we want to implement this") with no later
   invitation blocks it under `maintainer-declined`. A project voice whose
   latest word asks for logs, a retry or a reproducer, could not reproduce,
   or sends the report to another project blocks it under
   `maintainer-disputed` until a later one confirms the bug. A label naming
   the bug upstream or unverified (`upstream-bug`, `needs verification`,
   `needs info`) blocks it under `issue-not-triaged-here`. A pull request cited from
   a repository under another owner is neither a rival nor a fix. When you turn an issue down yourself after
   reading its thread, record it: `mailman prescreen OWNER/REPO#N --reject
   REASON --evidence LINK --hunt HUNT_ID --owner TOKEN`. An unrecorded
   rejection is offered again by `hunt targets` to the next session.
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

When a screened target has no workable issue, hunt for a defect with the
harness rather than by hand. `mailman baseline OWNER/REPO` clones the
default-branch head, installs the CI interpreter or the nearest one that
installs, runs the full suite and writes `baseline.json`; a failure that matches
an open issue is `known`, not a finding. `mailman fuzz --target M:F --model M:F
--generator M:F --output PATH` runs a seeded differential fuzz and refuses to
report findings when its self-check against the target fails. Record a defect
with `mailman finding PATH --init`, fill in its reproducer and the conditions it
needs with whether this host meets each, check it with `mailman finding PATH`,
and start the run with `init-run --defect-report PATH`.

## Prepare and prove

6. Run `prepare-workspace`. Use the personal fork account and configured
   GitHub noreply identity. Keep environments and scratch outside the target.
   When its summary carries `version_gap.related`, read those commit subjects
   (also in `version-gap.json`) before building. An issue reported on an older
   release may already be fixed at base by a commit that never cites it; if so,
   drop the run as `already-fixed-upstream`.
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
   before review or verification. Save a new reproducer script in the run
   directory, not the workspace: `reproduce` exits 4 on a dirty workspace
   because `orchestrate` would refuse it.
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
    When the hunt has a deadline, every run uses that one cumulative deadline,
    not a fresh one per candidate or agent call; without one, each run keeps
    its own budget from its creation. Mailman binds selection,
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
    a spent budget, replace the candidate only if the hunt deadline, when it
    has one, still has time. An attached run cannot extend that deadline with
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

Steps 13 to 15 are one command once the coordinator has written the target
policy, the final body and decision.json (#167): `mailman package RUN_ID
--policy target-policy.json --title TITLE --body body.md --repo OWNER/REPO
--head FORK_OWNER:BRANCH --base DEFAULT_BRANCH`; `--base` is required and
refused before any stage runs (#222). It runs export-patch, prepare-submission,
decision, finalize-review, the local commit of the exported paths,
check-authors, handoff, handoff-check and review, and stops at the first
failure with the stage name. Fix that finding and rerun it; finished stages
are safe to repeat. A `policy-requires-own-words` finding alone, for the
current export, does not stop it (#181): the handoff then prints no publish
command, `handoff-check` refuses with `own-words-pending`, and the review page
names the rewrite. The rewrite is the operator's at filing approval: rewrite
the body, set `own_words_confirmed`, rerun `prepare-submission` and `handoff`.
A decision question about that rewrite takes `"gate": "own-words"`; it is the
only blocking question such a run may carry besides `cla`. The individual commands below remain the reference for
what each stage checks. A target with a DCO check or contribution docs that
require `Signed-off-by` makes the commit stage refuse a `--commit-message`
file without the identity's sign-off line (#221); the operator states the
sign-off in the filing approval request, because it certifies the DCO.

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
    `prepare-submission` also runs the touched-tests stage once per export:
    it takes every changed non-test source file, derives its module names
    (`edgar/xbrl/xbrl.py` is `edgar.xbrl.xbrl`, `edgar.xbrl` and `xbrl`),
    selects every test file in the workspace whose text imports or names one
    of them, and runs those files with the run environment's interpreter via
    `-m pytest <files> -q -p no:cacheprovider` (`-m unittest` when pytest is
    not installed there), twenty minutes at most, capped at 25 files with the
    rest named under `omitted`. Files importing the full dotted module rank
    ahead of package and bare-stem matches before the cap, and `-m "not
    network"` is added when the target registers a `network` marker
    (recorded as `marker_filter`). A file that fails collection on `No
    module named 'x'` for a module outside the workspace (a missing optional
    extra) is moved to `omitted` with its reason under `omitted_reasons`, the
    rest run again, and a non-blocking `touched-tests-omitted` names it; if
    every file is omitted the stage is `touched-tests-not-run`. A `--deselect`
    in the run's frozen verification command (`prompts.json`) is applied to
    the touched run when its file is selected, recorded under `deselected`,
    and reported as the non-blocking `touched-tests-deselected`: it was
    chosen before the patch existed, for a test this host fails at base. The record in `submission.json` under
    `touched_tests` holds the files and why each was chosen, the exact
    command, exit code, passed and failed counts and duration. A failure is
    `touched-tests-failed`, a stage that could not run is
    `touched-tests-not-run`, and both block `prepare-submission` and
    `handoff-check`, which re-reads the record against the exported diff.
    This is not the primary's focused check and does not replace the recorded
    verification command: edgartools#1329 failed CI on
    `tests/xbrl/test_statement_drilldown.py`, a file that imports the changed
    module, that the primary never ran, and that fails locally in under a
    second. Pass `--workspace PATH` when the export did not record one.
    When a `.github/workflows/*.yml` step runs pytest with `-m EXPR`
    (edgartools' `pytest -n auto -m 'fast'`), the stage also runs that lane
    with the run interpreter, ten minutes per run at most
    (`MAILMAN_MARKER_LANE_CAP_SECONDS` overrides it), with the frozen
    verification marker and deselects and `-n` only when pytest-xdist is
    installed. If it fails, the same lane runs on the base commit; a test
    failing only with the patch, and again on a rerun, is listed under
    `marker_lane.regressions` and is `touched-tests-failed`. No lane, no
    base commit, a lane past the cap or one that cannot be collected falls
    back to the direct importers alone, with the reason in
    `marker_lane.reason` and a non-blocking `touched-tests-lane-fallback`
    when the target has a lane. edgartools#1386 failed 18 CI tests that
    reach the changed parser only through `Filing` (#302).
    When the target ships `scripts/check_offline_audit.py`, `prepare-submission`
    also runs it on the changed test files with the run interpreter; a
    non-zero exit is `offline-audit-failed` and blocks (recorded under
    `offline_audit`).
    When the target configures or runs ruff, flake8, black, isort, mypy or
    ty (a `[tool.x]` or `[x]` section in pyproject, setup.cfg or tox.ini, the
    tool's own config file, `.pre-commit-config.yaml` or a workflow), it runs
    each over the changed Python files with the run interpreter (`ruff
    check`, plus `ruff format --check` when CI formats; `black --check`;
    `isort --check-only`; `ty check`), installing the pinned version first if
    needed. A finding is `lint-failed` and blocks. A tool that cannot be
    installed or started here (ty.exe is blocked on this host) is
    `lint-not-run` and blocks too; record why with `mailman acknowledge-lint
    RUN_ID --tool NAME --note "..."`, which is pinned to the exported diff and
    turns only `lint-not-run` non-blocking. A target with no linter
    configured has no finding. Each tool's outcome is under `lint.tools`.
14. Write decision.json using `decision --init` and the schema in
    docs/review-page-standard.md. Keep evidence classes distinct. Questions
    must be genuine user choices, never tasks you can do. Use [] otherwise.
    Do not claim SEND for an incomplete or unverifiable candidate. Run
    `decision`, then `finalize-review`. If candidate bytes changed after the
    final verification, re-review and verify them before finalizing.
15. Commit the reviewed change locally with the configured identity. Check
    authors, refresh duplicates and claims, then prepare `handoff` with the
    exact local branch and final body. Run `handoff-check`; it refuses with
    `touched-tests-failed` or `touched-tests-not-run` until the touched-tests
    record in `submission.json` matches the exported diff and passed. Keep
    every upstream write pending until the operator approves it, but ask
    for that approval per run, as soon as `handoff-check` passes, not when
    the quota is met: a SEND-ready pandas-stubs run waited 1h40m for a third
    candidate and an outside pull request took the issue in that time
    (#315). File with `hunt ship` (below), which records `hunt file` for
    each run as it is filed. For a self-sourced defect,
    prepare any required issue text alongside the PR and ask for approval of
    the ordered filings.
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

Ask first on an untriaged issue, and on an unassigned issue where
`target-intel` says assignment looks required (`mailman decision` refuses SEND
there). The decision is then `ASK`, not `SEND`, and carries an `offer` block:
`{"path": "offer-comment.md"}`. The file sits inside the run directory, stays
under 120 words, names the reproduction and cites the run's base commit (a
prefix of `base_commit` in `run.json`). `mailman decision` refuses the ASK
without it. `hunt status` and `hunt finish` report these candidates as
`ready_to_ask`, apart from `ready`. They never count toward N, so the finish
exit code still reflects the PR quota alone. The offer text appears verbatim on
the run page, the checkpoint and the packet, where the operator approves it.
Mailman never posts the comment: the operator does. Hand the offer over with
`mailman handoff RUN_ID --offer --kind issue-comment --issue N --repo
OWNER/REPO --body <run>/offer-comment.md` and check it with `mailman
handoff-check RUN_ID --offer`. It is kept in `handoff-offer.json`, apart from
the pull request's `handoff.json`, and an ASK candidate is not ready to ask
without it. `mailman claims` (and `hunt refresh`) records maintainer comments
posted after the offer as `offer_replies`; `hunt status` then says "maintainer
replied to offer; switch decision to SEND". Read the reply. After a yes, change
the decision to `SEND`, remove the `offer` block and do the PR handoff. A
refusal ends the candidate.

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

Hand the operator one command per batch, not a script of blocks:
`mailman hunt ship HUNT_ID --owner TOKEN`. Running it is his approval for that
batch, so give it to him with the packet and do not run it for him. For every
run at filing approval (`hunt status` reads it `ready` at stage
`filing-approval`, not yet filed, within the hunt's open slots) it confirms the
run is packaged, forks the upstream into the account the handoff's `--head`
names, pushes the branch, opens the pull request with the handoff's title, body
file, head and base, and records `hunt file` with the URL and commit. It never
repackages: that would re-hash the body and post bytes nobody read. It
refreshes duplicate searches and claims first, as `finish` does (`--no-refresh`
skips that offline). A run whose decision or handoff check fails is skipped
with the gate's reason. A run that needs the operator (an own-words rewrite, a
CLA) or is an ask-first offer is skipped and never filed. A head owner other
than the account gh is signed in as is skipped too, because the fork would land
in the wrong account. Each step reads first, so a rerun reuses the fork, a
branch already at the approved commit and an open pull request from that
account. It refuses to overwrite a fork branch at another commit or to open a
second pull request after a closed one. It stops at the first failure, prints
what succeeded and the command to resume, and exits 1. `--dry-run` prints the
plan and writes nothing; `--json` prints the result as JSON.

When a run was filed by hand, record it as it happens: `mailman hunt file HUNT_ID RUN_ID
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

Each reading is compared with the one before it. The table ends with what
moved since then: a status change, a new outside comment or review, a check
that started or stopped failing, a `mergeable_state` change, or a commit
somebody else pushed. `changes` in the record holds the same list, and it is
empty when nothing moved, so a scheduled watch can stay quiet over a row that
already needed work yesterday. The desktop scheduled task `pr-watch` runs the
watch several times a day and notifies only on changes.

The `STATUS` column decides. `ok` is an open pull request with green or
pending checks, a base it can still merge onto, and no outside comment newer
than our last commit or reply. `attention` names the reason on the next line:
a failing check by name, `mergeable_state behind` (the base moved on; rebase
and push) or `dirty` (a conflict), or an unanswered comment or review from
somebody other than the author, bots excluded, newer than anything we did on
the thread. `approved` is that same green row whose newest outside word is an
approving review no push of ours has outdated: it asks for nothing and the
next move is the maintainer's merge, so it is not work. A later comment, a
later `CHANGES_REQUESTED`, or a red check puts the row back on `attention`.
`inherited` is a row whose only red checks are ones the base branch broke:
the check reports failures only in non-test files the pull request does not
touch, or most of up to five other open pull requests fail it too. A failing
`codecov/project` is inherited when `codecov/patch` passed on the same head:
every changed line is covered and the total moved with the base. It is not
work; widening the pull request to fix it is out of scope.
`unknown` is a row `gh` could not read, a 404, a rate limit, a
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
