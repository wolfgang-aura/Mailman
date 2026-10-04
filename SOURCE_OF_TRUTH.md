# Source of truth

Last verified: 2026-10-01 in `Asia/Singapore`.

## Luna work-order checkpoint

Code checkpoint `fd1ca3a` on `main` changes the PRHunt agent contract based on
hunt `20260910T013711Z-019af0`. Command counts are no longer default completion
gates. If an optional cap races with a completed Codex turn, Mailman keeps the
report and candidate. A reviewer-only cap can resume without rerunning the
verified primary. Stream stops and timeouts kill the full Windows process tree,
so a Codex child cannot keep the output pipes open after its wrapper stops.

`build-prompts` now writes `work-order.json` and refuses a prepared Git
workspace unless it can verify at least one exact start file from the issue,
reproducer or `--start-file`. Primary prompts tell the agent to stop after the
patch, focused check and report. Reviewer prompts carry a bounded candidate
diff, including untracked files, before the reviewer starts. Orchestration now
defaults Codex reasoning effort to `medium`; the PRHunt procedure makes that the
Luna default. The focused regression set passed 133 tests, the full suite passed
800 tests and 54 subtests in 5m19s before the final reviewer-resume addition,
and the current orchestrator and direct-agent set passed 63 tests afterward.
The exact pushed checkpoint then passed 801 tests and 54 subtests in 4m30s.

A controlled Luna `medium` replay used a synthetic two-file Python bug outside
the repository. With both start files named and one allowed test command, Luna
read the files, made the correct one-branch fix, attempted the test, wrote its
report and stopped in about 35 seconds. It used two commands and reported
68,355 input tokens, including 49,408 cached. The synthetic prompt named bare
`python`, which the elevated agent environment could not resolve; running the
same test with the host's resolved Python executable passed both tests. Real
`build-prompts` records that resolved executable, so this was a replay-fixture
error rather than a candidate or model error. This proves the bounded Luna
behavior, not a complete PRHunt under 30 minutes.

## Current local procedure

A hunt has a deadline only when `hunt init --time-budget-hours` sets one
(#166, 2026-09-29; before that every hunt got a fixed two hours). When set,
every attached orchestration reads that deadline, and neither a replacement
candidate nor `--time-budget-override-reason` can reset it. Without one, an
attached run keeps its own budget from run creation, and each model role keeps
its ten-minute limit. `hunt add` refuses an expired
hunt and any target already used by that hunt, including a dropped run.
Agent work now requires a machine-checked reproduction. Before the primary
starts, Mailman runs the exact verification argv recorded by `build-prompts`
on the clean base tree and blocks if it fails or changes candidate bytes.
GitHub Actions runs `34388619436` and `34388871841` passed for the earlier
checkpoint. Commit `09c8e66` binds screening, environment, agent, verification
and packaging subprocesses to the hunt's remaining wall time. It also records
Codex input and cached-input usage per execution, totals input per role, blocks
an over-budget or unaccounted turn, and refuses later resumes. Local
verification for this commit passed 789 tests and 54 subtests in 5m41s. Codex
reports usage only when a turn completes, so the wall deadline, not the token
setting, remains the hard in-flight stop. Live run
`20260909T132347Z-ddf1ac` is the evidence for this rule: its first primary turn
reported 9,753,232 input tokens, including 9,515,520 cached tokens, against a
configured 2,000,000. Issue #81 tracks these limits; issues #82 and #83 track
the earlier gates.

Commit `d975f45` is on `origin/main`. GitHub Actions run `34392659095` passed
on Python 3.12 and 3.14.

`520ab2a` on `main`, 2026-09-09, passed GitHub Actions run `34298470558`. It
adds three commands and one run status on top of the procedure below:
`mailman prescreen OWNER/REPO#N` screens an issue before a run exists and
`init-run` requires a fresh passing pre-screen; `hunt finish` re-reads the
target for every ready candidate before gating, with `--no-refresh` for
offline use; `fetch-review` and `revision-response` handle a maintainer's
requested changes on a filed pull request through the new
`MAINTAINER_CHANGES_REQUESTED` status. `procedure.md` changed with them, so
any hunt record pinned to an earlier digest needs `hunt refresh-procedure`
before it resumes.

`e6fd34d` on `main`, 2026-09-09, passed GitHub Actions run `34333042954`.
`mailman contributions` now prints when each filed pull request's state was
read and marks a reading over a day old as stale; `contributions --refresh`
re-reads every one of them from GitHub and rewrites the stored state, merge
commit and `checked_at`. A lookup that fails keeps the state already on disk,
names the run on stderr and exits 1. Nothing in `procedure.md` changed, so no
hunt needs `hunt refresh-procedure`.

PR #61 (`Enforce one unattended PRHunt procedure`) merged into `main` as
`5fbff826` on 2026-09-07. It adds the canonical `mailman/procedure.md`,
repository skill and Claude command, persistent hunt quotas, completion gates,
review recovery and authorship checks. This section supersedes older local
capability descriptions below. Historical deployment entries remain historical.

The new flow passed a scripted end-to-end fixture and the local test suite.
The merge commit passed GitHub Actions on Python 3.12 and 3.14 in run
`34134989101`. It has not run an unattended live hunt with user-selected
models. The earlier failure in run `34057041023` was fixed by making the
mocked adapter fixture use the Python executable on Linux.

Mailman is a CLI, with no running service to restart after these edits. Each
new invocation reads the current installed package. `procedure.md` is included
as package data. Models receive the same procedure in prepared prompts; hunt
records pin its digest and exact model choices. A changed procedure requires
an explicit coordinator refresh. Routine run failures are coordinator work.
Final PR and issue filings require the user's approval for this workflow.

The existing review renderer is unchanged. Browser security policy blocked
opening the local packet URL during this session; visual state is unverified.


## Repository

- Canonical remote: `https://github.com/wolfgang-aura/Mailman.git`
- Default branch: `main`
- The public remote contains the foundation on `main`.
- License: Apache-2.0.
- Foundation deployment: commit `c1cc179` passed GitHub Actions on Python 3.12 and 3.14 in run `33546831897`.
- CLI adapter deployment: commit `29a5a53` passed GitHub Actions on Python 3.12 and 3.14 in run `33548539986`.
- Toolchain deployment: commit `318c9fe` passed GitHub Actions on Python 3.12 and 3.14 in run `33549213202`.
- Workspace deployment: commit `91e84ae` passed GitHub Actions on Python 3.12 and 3.14 in run `33549771368`.
- Orchestration deployment: commit `929caf5` passed GitHub Actions on Python 3.12 and 3.14 in run `33552625334`.
- Issue #9 deployment: commit `a371be1` passed GitHub Actions on Python 3.12 and 3.14 in run `33558633354`.
- Knowledge flywheel deployment: commit `d2be47a` passed GitHub Actions on Python 3.12 and 3.14 in run `33592165392`.
- External issue deployment: commit `78d048e` passed GitHub Actions on Python 3.12 and 3.14 in run `33596869316`. It carries issue ingestion, environment preparation, patch export, and the first external run records.
- Run record deployment: commit `46d0862` passed GitHub Actions on Python 3.12 and 3.14 in run `33596947293`.
- Observability deployment: commit `d72d645` passed GitHub Actions in run `33627419317`. It carries the
  agent transcript normalizer, live streaming from the executor, Claude on `stream-json`, and `mailman show`.
- Bounded-loop deployment: commit `9fc4805` passed GitHub Actions in run `33641545669`. It carries the
  claimed-issue refusal (#15), the named turn budget and its new default of 120 (#14), and one revision
  granted when the post-primary verification fails (#13).
- Issue-sweep deployment: commit `299b1c8` passed GitHub Actions on Python 3.12 and 3.14 in run
  `33670366489`. It carries the fixes for #18, #21, #22, #23, #24, #25, #27 and #28, plus the
  recording halves of #20 and #26.
- Duplicate-search deployment: commit `d1d4a98` passed GitHub Actions on Python 3.12 and
  3.14 in run `33677777743`. It carries the pull request standard and the fix for #30, a
  duplicate search whose failed methods were recorded as a successful empty search.
- Duplicate-precision deployment: commit `ed933ea` passed GitHub Actions in run `33680128212`.
  It carries the fix for #31: the run's own issue is no longer a duplicate of itself, matches
  are split into index-backed hits that block and listing hits a human must read, and
  `mailman acknowledge-duplicates` records that reading against the exact rows it covers.
- Duplicate-state deployment: commit `eb85694` passed GitHub Actions in run `33681805068`.
  It carries the fix for #32, so a closed prior attempt is prior art rather than an
  unclearable duplicate, and a merged one blocks as `already-fixed-upstream`. It also
  records the `langchain-ai/langchain` target policy.
- Prior-art scoping deployment: commit `54206df` passed GitHub Actions in run `33682244519`.
  It carries the fix for #33 and its follow-up: `prior-art` reads the rows that are about
  the issue whatever their state, and only open or merged rows stop a run.
- Environment plan deployment: commit `d777218` passed GitHub Actions in run `33682705844`.
  It adds the `langchain-core` environment plan.
- Verification-resolution deployment: commit `41d9eb7` passed GitHub Actions on Python 3.12
  and 3.14 in run `33718702205`. It carries the fix for #39, so a verification command typed
  as a bare `python` resolves through the run toolchain instead of running whatever is first
  on PATH, and adds the `pytest` and `starlette` environment plans.
- No-test acknowledgement deployment: commit `87bbd93` passed GitHub Actions in run
  `33719939852`. It carries the fix for #40: `mailman acknowledge-no-test` lets a run answer
  the `no-test-change` gate with recorded evidence, pinned to the paths the diff touches.
- Run record deployment: commit `9e1174f` passed GitHub Actions in run `33720398351`. It adds
  `docs/runs/0008-starlette-3497-submission-ready.md`.
- Target intel deployment: commit `77f094c` passed GitHub Actions in run `33735205372`. It
  carries `mailman target-intel`, the `check-target` precondition that refuses a run against a
  repository nobody has read, and the first six lessons in `knowledge/lessons.json`.
- Reproduction gate deployment: commit `5009949` passed GitHub Actions in run `33766732724`.
- Merged-attempt deployment: commit `133d7dc` passed GitHub Actions in run `33771151452`.
  It carries the fix for #38: `check-target` splits prior attempts three ways, and a merged
  one blocks under `already-fixed-upstream` rather than being reported as a rejection that
  `--acknowledge-prior-attempts` clears.
- Comment-claim deployment: commit `156b8cb` passed GitHub Actions in run `33772783849`.
  It carries the fix for #36: `mailman claims` reads the target issue's own thread, and
  `check-target` refuses without that record, on an assignee, on a maintainer handing the
  work over, and on an unanswered claim until `--acknowledge-claims` is passed.
- Reviewer-execution deployment: commit `b68f886` passed GitHub Actions in run `33786866250`.
  It carries the fix for #20: an APPROVE from a reviewer whose transcript shows no command
  blocks the run instead of clearing it. REVISE is unaffected.
- Verification-claim deployment: commit `3bd94b8` passed GitHub Actions in run `33791836068`.
  It carries the real fix for #20: an APPROVE requires `MAILMAN-VERIFICATION: RAN`. Exercised
  live against a Codex reviewer in both states, which wrote BLOCKED under a read-only sandbox
  and RAN under workspace-write, and declined to approve in the blocked case.
- Repository screen deployment: commit `cf2dd1f` passed GitHub Actions in run `33790613015`.
  It carries #35: `mailman screen-target OWNER/REPO`, six gates, verdict cached under
  `screens/`. Exercised live against six repositories from the hand-screened table.
  It carries `mailman reproduce` and the two `check-target` refusals behind it.
- Defect-report deployment: commit `65a7b4d` passed GitHub Actions in run `33797195813`.
  It carries #45: `init-run --defect-report PATH` as an alternative to `--issue`, exactly one
  of the two required. `fetch-issue` renders the file through the same capture boundary and
  `claims` records that no thread exists. The duplicate search and the reproduction stay
  mandatory and carry the whole evidence burden. 420 unit tests, up from 412.
- Superseded-merge deployment: commit `bd7c5bd` passed GitHub Actions in run `33802398911`.
  It carries #46: prior art records `mergeCommit`, and a merged duplicate is cleared when its
  merge commit is an ancestor of the base commit and the reproduction failed at that same
  commit. `check-target` reports `merged-fix-already-in-base` and `prepare-submission` makes
  it a non-blocking finding. Also `examples/target-policies/ffn.json`, stance `unknown`, the
  first target with no written policy at all. 431 unit tests, up from 420.
- Handoff-gate deployment: commit `4023709` passed GitHub Actions in run `33836739688`.
  It carries #47: `mailman handoff` prints the whole body in one block with the single `gh`
  command that posts it, records the body's SHA-256, and emits `--body-file` so the previewed
  bytes are the posted bytes. `mailman handoff-check` re-hashes the file and exits non-zero
  once it changed after the preview. A first-person read-or-tested claim exits non-zero and
  names the line. 447 unit tests, up from 431.
- Provenance-gate deployment: commit `23107ba` passed GitHub Actions in run `33855131252`.
  It carries #48. `screen-target` now runs seven gates, with provenance reported first: a
  repository passes at a year old with 500 stars, or at ten outside authors in ninety days,
  and a fork is refused outright. It reuses the freshness gate's author count, so it costs no
  extra API call. Re-screened live on 2026-09-04: `pydantic/pydantic-ai` 44 authors,
  `langchain-ai/langchain` 34, `pmorissette/ffn` 11, all passing. An environment plan step may
  start only a Python interpreter or `git`; `check_executable` runs in `load_plan` and again on
  the substituted command in `prepare_environment`. This is not isolation. A hostile `setup.py`
  in a repository that clears the gate still executes as the invoking user, and no sandbox
  exists. 466 unit tests, up from 447.
- Screening-gate deployment: commit `fc0ccae` passed GitHub Actions in run `33837443444`.
  It carries #42, #43 and #44. Freshness fails when every merge in the window is by one author
  who wrote 35% or more of the pattern-window merges over a sample of at least eight, and the
  gate line names the authors counted and the bots excluded. The policy gate reads three rules
  rather than one, and `TargetPolicy.requires_own_words` blocks a generated body in
  `prepare-submission`. The pure-python gate reads `[build-system].requires`, failing a
  compiling back end and passing a wheel-only hook with the new `source-tree` environment plan.
  457 unit tests, up from 447.
- Publication-gate deployment: commit `a6bc0a4` passed GitHub Actions in run `33993117842`.
  It carries #49, #50, #51 and #52, all four found by what happened to pmorissette/ffn#328.
  `prepare-workspace` writes a configured identity into the clone's own git config and
  `check-authors` refuses a branch carrying an address outside the allowlist, so a run can no
  longer commit under the machine's global identity. The handoff block resolves the head fork
  owner's account type and warns that maintainer edits cannot be granted on an
  organisation-owned fork, and lists every preservation claim in the body next to the demand
  to name the command that showed it. `provenance` writes the run's commits as a patch file
  and records the pull request's state, merge commit and upstream permalink; `contributions`
  lists them; `deletion_is_safe` decides whether a fork can go. 510 unit tests, up from 477.
- Precondition deployment: commit `ac72921` passed GitHub Actions on Python 3.12 and 3.14 in run
  `34010191930`. It carries #10, #11, #12 and #41. An empty candidate reaches the reviewer with
  the fact stated in its prompt and can no longer leave the loop as a submission. `create_run`
  refuses a data root inside a working tree that is not this repository, `MAILMAN_DATA_ROOT` gives
  a shell elsewhere somewhere to put runs, and every child process runs with
  `PYTHONDONTWRITEBYTECODE`. `prepare-workspace` refuses a Windows path budget under 130
  characters before it clones, and records what is left. `handoff-check` refuses a pull request
  whose duplicate search or claims check is missing, failed, older than an hour, or covers another
  repository. 534 unit tests, up from 510.
- Closed-case deployment: commit `2490144` passed GitHub Actions in run `34584586612`. It
  carries #87: once provenance records a run's pull request as `CLOSED` or names a
  `superseded_by`, `handoff` refuses a comment on the issue, our pull request or the superseding
  one, and a new pull request; `handoff-check` re-reads provenance at publish time and refuses
  `run-closed`. `handoff --closing-reply` allows one courtesy reply to one thread, recorded in
  `closing-reply.json`. `provenance --superseded-by` prints the rule on stderr. 862 unit tests,
  up from 801. Run `20260908T220821Z-124828` (python/mypy#21961, superseded by #21967) carries
  the marker for the reply posted on #21967 on 2026-09-11.
- Foreign-thread deployment: commit `8ef48e7` passed GitHub Actions in run `34596949018`.
  `handoff` refuses an issue comment aimed at a pull request another author opened, in any run
  state (`gh api repos/O/R/pulls/N` names the author; unreachable means not refused), and warns
  when a reply passes 120 words. Live check: the open poetry run refused a comment on
  python-poetry/poetry#11050 with exit 2 and rendered one for its own #11052. 864 unit tests.
- Silent-close deployment: commit `c8f0b73` passed GitHub Actions in run `34598020147`. It
  carries #88: `claims` records `issue_closed_at`, `reporter_association` and
  `maintainer_replied`; `handoff-check` refuses a pull request whose issue is closed
  (`issue-closed`); `handoff` prints `UNTRIAGED ISSUE` for an outside report no maintainer has
  answered. Live check: the pdm run's real `claims.json` refuses with `issue-closed`. 871 unit
  tests. Run records 0010 and 0011 hold the four closed cases.
- Second upstream merge, 2026-09-16: securo-finance/securo#875 (run `20260909T023511Z-2177ae`,
  Luna primary and reviewer) merged as `651a238` from the user-owned fork, one commit, no
  maintainer change. Provenance reads `MERGED`; fork deleted. Record 0013. Issue #91 filed on
  `target-intel` counting CodeRabbit layout markers as rules.
- Layout-marker deployment: commit `9e88f27` passed GitHub Actions in run `35091538516`. It
  carries #91: `enforcement_markers` drops a `foo_start`/`foo_end` pair and a hash-stamped
  marker before counting. Live check: the real CodeRabbit body on securo#875 now yields zero
  rules. 878 unit tests, up from 871.
- Third upstream merge, 2026-09-22: dgunning/edgartools#1329 (run `20260916T134234Z-ec068b`,
  Claude `claude-opus-5` primary and reviewer) merged as `cb4b142` at 15:00 UTC from the
  user-owned fork, three commits, after `CHANGES_REQUESTED` on 2026-09-20 and one revision.
  Provenance reads `MERGED`. Record 0014.
- Fourth upstream merge, 2026-09-26: dgunning/edgartools#1365 (run `20260924T222329Z-301fab`,
  Claude `claude-opus-5-5` primary and reviewer) merged as `0ec15fe` at 12:33 UTC from the
  user-owned fork, three commits, after the maintainer approved the offer on #1337 and asked for
  the #1136 row loop to be shared. Provenance and `contributions --refresh` read `MERGED`.
  Record 0015. Issues #136 and #137 filed from it.
- Upstream tally, 2026-09-26 (`mailman contributions --refresh`, `mailman hunt watch`): 19 PRs
  filed. Merged 4: ffn#330 (maintainer re-land of our #328), securo#875, edgartools#1329,
  edgartools#1365. The open and closed rows are unchanged from the 2026-09-23 tally: open 9
  (pretix#6564, pymc#8442, nicegui#6345, tqdm#1837, openalgo#2021, openalgo#2022, poetry#11052,
  pdm#3883, openai-agents-python#4890), closed unmerged 6.
- Upstream tally re-read 2026-09-27 06:40 UTC (`mailman contributions --refresh`, `mailman hunt
  watch`): no state changed. 19 filed, merged 4, open 9, closed unmerged 6. Two runs never filed
  (starlette `20260903T052426Z-ad8196`, mypy `20260910T094019Z-7d1e6e`, superseded by #21966).
  `hunt watch` flags three: pretix#6564 (CLA unsigned), tqdm#1837 (`pre-commit.ci` B018 on two
  master lines the PR does not touch; inherited, Mailman #134) and poetry#11052 (`behind`; poetry
  has no branch protection requiring an up-to-date branch, so this does not block a merge).
- Hunt totals, 2026-09-27: 23 hunts, 5 FILED, 18 ABANDONED. Hunts `20260918T115409Z-903d20` and
  `20260924T231540Z-218236` were still marked RUNNING past their deadlines and were closed with
  `hunt abandon` on 2026-09-27. 146 run directories and 301 cached repository screens exist.
  Every hunt since 2026-09-18 ended with zero new candidates; on 2026-09-25 the operator chose
  to stop hunting new bugs and work the open PRs (hunt `20260925T144418Z-f692cc`).
- 2026-09-27, hunt `20260927T070426Z-537a3d` (Claude `claude-opus-5-5` both roles, ask-first mode
  approved by the operator): securo-finance/securo#1039 filed (run `20260927T072703Z-caa4d0`, fork
  `wolfgang-aura/securo`). First CI failed `ty` in the new test (Application Control blocks `ty.exe`
  here; Mailman #120). An ask-first offer was posted on dgunning/edgartools#1370 (run
  `20260927T071330Z-0be859`); its PR waits for a maintainer reply. Tally now 20 filed, merged 4,
  open 10, closed unmerged 6. Mailman #138 and #139 filed from this hunt.
- openai-agents-python#4890, 2026-09-27/28: jbeckwith-oai pushed `7586a91` and `6edb43b` to our
  PR branch (19:33 UTC), addressing every blocker in their consolidated review. markstuart-oai
  approved `6edb43b` at 20:03 UTC. jbeckwith-oai then pushed `bca5250` (20:29 UTC: after an
  alias commit, a diverged source is kept rather than removed), dismissed the now-stale
  approval, and re-requested review from seratch. After two more pushes (`3a8e95e` merge of
  main, `88e4572` Windows CI timeout), dpiet-oai and seratch both approved `88e4572` on
  2026-09-28 (06:35 and 06:44 UTC); seratch: "This looks good to merge". `reviewDecision`
  APPROVED, merge state CLEAN, 21/21 checks pass; merged later that day (below). seratch filed a
  non-blocking follow-up for the maintainers: moves onto an existing hardlink destination now
  report an incomplete move instead of completing. Still our PR, with our nine commits under
  their five. Our own revision of the same blockers, which passed
  fork CI, was not pushed. It is parked locally as `local/4890-our-review` in
  `.mailman/scratch/agents-4890`. The fork's CI draft PR #1 and branch `ci/4890-review` were
  deleted 2026-09-28. Mailman #140 filed: nothing detects maintainer pushes to our PR branch.
- Fifth upstream merge, 2026-09-28: openai-agents-python#4890 (run `20260906T104815Z-29582c`)
  merged by jbeckwith-oai as `71306bf` at 15:08 UTC, with the maintainers' commits on top of
  ours. `mailman contributions --refresh` reads `MERGED`. No run record written yet.
- nilearn/nilearn#6611 filed 2026-09-28 13:25 UTC (run `20260928T095530Z-2005c8`, head
  `wolfgang-aura:mailman/issue-6607`), open, no competing PR on #6607.
- Upstream tally re-read 2026-09-29 (`mailman contributions --refresh`, cross-checked against a
  GitHub search of PRs by `wolfgang-aura` outside the account): 23 filed. Merged 5: ffn#330
  (maintainer re-land of our #328), openai-agents-python#4890, securo#875, edgartools#1329,
  edgartools#1365. Open 12: pretix#6564, pymc#8442, nicegui#6345, tqdm#1837, openalgo#2021,
  openalgo#2022, poetry#11052, pdm#3883, securo#1039, nilearn#6611, nox#1191, ipython#15408.
  Closed unmerged 6: pdm#3884, mypy#21961, pytest#14993, rqalpha#1040, skfolio#316, pypdf#4105.
  Hunt `20260928T185953Z-92e95a` is FILED: runs `20260928T190015Z-6a59bf` (nox#302 ->
  https://github.com/wntrblm/nox/pull/1191) and `20260928T191349Z-27ec60` (ipython#9891 ->
  https://github.com/ipython/ipython/pull/15408), both from forks under `wolfgang-aura`.
- Sixth upstream merge, 2026-09-29: nilearn/nilearn#6611 merged by Remi-Gau as `a01cefe` at
  07:53 UTC, about 18 hours after filing, on top of a head we did not push (`a201c0a`, Remi-Gau's
  commit). No run record written yet.
- Upstream tally re-read 2026-09-30 (`mailman hunt watch`, ledger checked 2026-09-29 17:23 UTC):
  28 PRs filed. Merged 6: ffn#330, openai-agents-python#4890, securo#875, edgartools#1329,
  edgartools#1365, nilearn#6611. Closed unmerged 6 (unchanged). Open 16: biopython#5336,
  ipython#15408, openalgo#2021, openalgo#2022, pdm#3883, py-shiny#2511, pretix#6564,
  xarray#11637, pymc#8442, PyPSA#1958, anndata#2666, poetry#11052, securo#1039, tqdm#1837,
  nox#1191, nicegui#6345. The five filed on 2026-09-29 (biopython, py-shiny, PyPSA from hunt
  `20260929T020844Z-f0e56`; anndata from `20260929T011552Z-b6bc8`; xarray) were missing from the
  2026-09-29 tally above.
- State changes behind that tally, verified with `gh`:
  - pymc#8442: ricardoV94 approved on 2026-09-28 07:59 UTC after our `60b0a46`. Merge state
    `BLOCKED` with `reviewDecision` APPROVED; waiting on a maintainer merge. Codecov comment green.
  - biopython#5336: peterjc commented on 2026-09-29 14:58 UTC: on hold pending agreement of the
    AI policy, and `biopython#5241` (draft no-AI policy, open) "would reject this outright". Treat
    as parked; no reply from us. Repos with an open no-AI policy proposal now fail the policy
    gate (`ea9e6e0`), which postdates this filing.
  - xarray#11637: dcherian left two review comments on 2026-09-29 16:54-16:56 UTC on the new
    `dtype.kind == "M"` branch in `xarray/backends/zarr.py`: handle `timedelta` too, add a test,
    and "I dont understand this comment" about our code comment. Merge state `DIRTY`
    (conflicts with main) and `pre-commit.ci` fails. Revised 2026-09-30: `02958fa` (fork branch
    `zarr-append-datetime64-encoding`, fast-forward push, `main` merged in) extends the fix to
    `timedelta64` (a stored `timedelta64[s]` appended as `[3600, 6]` before), parametrizes the test
    over both kinds (the timedelta case fails without the fix), and rewrites the comment. Local
    `test_backends.py -k "Zarr and append"` 76 passed; ruff clean; upstream CI pending at push.
    Reply draft in `.mailman/scratch/xarray-11637-reply.md`, not posted.
  - nicegui#6345: falkoschindler pushed `cdb0892` and `475a454` to our branch (weak-reference None
    check, merged tests) and set the `review` label on 2026-09-29 19:28 UTC. Approved by
    falkoschindler, who enabled auto-merge (merge commit) at 20:02 UTC. Re-read 2026-09-30:
    still `OPEN` (`reviewDecision` APPROVED, `mergeStateStatus` BEHIND, head `475a454`); #6339
    open. Not merged as far as GitHub shows, so the tally stays at 6 merged. Nothing for us to
    do; if the merge is stuck on BEHIND, only a maintainer should update the branch.
  - anndata#2666, ipython#15408, pymc#8442 (other checks), tqdm#1837: red checks are inherited
    (fail only in `.github` files or on other open PRs), not ours.
  - PyPSA#1958 went `blocked` -> `clean`; py-shiny#2511 is `blocked` on required review only.
- Upstream tally re-read 2026-09-30 13:28 UTC (`mailman hunt watch`, `mailman contributions
  --refresh`, each change checked with `gh`): 31 PRs filed. Merged 8: ffn#330,
  openai-agents-python#4890, securo#875, edgartools#1329, edgartools#1365, nilearn#6611,
  nicegui#6345, securo#1039. Closed unmerged 6 (unchanged). Open 17: biopython#5336,
  fonttools#4212, ipython#15408, openalgo#2021, openalgo#2022, pdm#3883, py-shiny#2511,
  prefect#23237, pretix#6564, xarray#11637, pymc#8442, PyPSA#1958, poetry#11052, anndata#2666,
  sqlmesh#6105, tqdm#1837, nox#1191. New since the morning tally: sqlmesh#6105 (filed 2026-09-29),
  prefect#23237 and fonttools#4212 (filed 2026-09-30), all from hunt `20260929T150205Z-08d690`.
  - Seventh merge: nicegui#6345 merged by falkoschindler at 2026-09-29 20:54 UTC as `d0323a3`,
    through the auto-merge he enabled at 20:02, on his head `475a454`. Run record 0018.
  - Eighth merge: securo#1039 merged by tassionoronha at 2026-09-30 12:19 UTC as `210e1ce`
    ("LGTM"), head `350fb8c`. Our second merge in securo. Run record 0019. Nilearn#6611, the
    sixth, is record 0017.
  - `mailman provenance` refuses to re-record nilearn#6611 and nicegui#6345: the fork branch
    tip is the maintainer's commit, not the last commit in our workspace. `contributions
    --refresh` still reads both as `MERGED` with merge commits.
    The refusal overwrote each run's `submission/contribution.patch` before refusing; fixed in
    `4fcaea1` (#274). Since #275, provenance checks our commits against the pull request's own
    commit list when the fork tip differs or the fork is gone, and records commits a maintainer
    pushed on top as `maintainer_commits`. Both runs re-recorded `MERGED` on 2026-09-30.
  - xarray#11637: `test-py311-min-versions` fails on `02958fa`. Our new
    `test_append_native_time_dtype_encoding` raises `KeyError: '<M8[ms]'` / `'<m8[s]'` for
    `zarr_format=3` under zarr-python 3.0, which cannot store native time dtypes in format 3
    metadata. Reproduced locally with zarr 3.0.10 (4 failed). Fix committed locally as
    `905bc308` and pushed 2026-09-30 by operator approval: `skip_if_zarr_format_3(...,
    condition=not has_zarr_v3_dtypes)`, the pattern the neighbouring string-append test uses.
    With zarr 3.0.10: 4 passed, 4 skipped by the gate; with zarr 3.4.0: unchanged (8 passed).
    Merge state `DIRTY` because main released v2026.09.0 (`2f3339b`): our `whats-new.rst`
    entry merges cleanly but lands under the released section, and main has no unreleased
    section yet. While `DIRTY`, GitHub starts no test workflow for the new head, so the fix
    has no CI result yet. Reply posted by the operator 2026-09-30,
    https://github.com/pydata/xarray/pull/11637#issuecomment-5912666435, asking whether to add
    the unreleased section in the PR.
  - prefect#23237: the red checks are not ours. Every failing job shows
    `test_block_standards.py::test_has_a_valid_image` with `HTTP Error 402: Payment
    Required` from the block image host, in modules the PR does not touch.
  - biopython#5336: still on hold for the AI policy, no new comment. pymc#8442 approved, not
    merged.
  - Re-read 2026-09-30 14:46 UTC: no open PR has activity after our xarray reply. The one change
    is edgartools#1370: dgunning answered the offer at 14:33 and 14:35 UTC, "yes please, open a
    PR", endorsed the approach, and will run the section network regressions before merging.
    Run `20260927T071330Z-0be859` is `SEND`; no competing PR or claim; upstream `main` is 6
    commits past our base `b022ad29` and none touch our files. The 2026-09-27 handoff predates
    the reply; the auto-mode classifier refused `mailman handoff`, so the operator regenerated
    it, passed `handoff-check`, pushed and filed https://github.com/dgunning/edgartools/pull/1386
    at 16:23 UTC (head `aaf4e75d`). Provenance recorded `OPEN`. Tally now 32 filed, 18 open.
    CodeFactor flags `_get_element_text` complexity 18; advisory there (7 of the last 30 merged
    PRs merged with it red), so no revision.
- Re-read 2026-10-01 (`mailman hunt watch`, `mailman contributions --refresh`): 32 filed.
  Merged 9, closed unmerged 6, open 17. The only change is the ninth merge: edgartools#1386,
  merged by dgunning 2026-09-30 19:10 UTC as `da38745`; #1370 closed. Our `aaf4e75d` failed
  18 tests in `test-fast` and `regression`, six of them real losses (plain-text filings,
  block children of inline wrappers, 424B cover agents). dgunning pushed `2fda300` on top and
  merged. Provenance re-recorded `MERGED` with `maintainer_commits` = `2fda300`. Run record
  0020. The failures reproduce offline here (19 failed at `aaf4e75d`); touched-tests selected
  only direct importers of the changed module, filed as #302. Delete `wolfgang-aura/edgartools`
  now that it carries no open PR. Deleted by the operator 2026-10-01.
  - Mailman CI went red on `9128ca7`: `28734a8` (#276) left `_stream`'s pipes open, and two
    orchestrator CLI tests saw a `ResourceWarning` on stderr. Fixed in `c819de6` (#281).
- Re-read 2026-10-01 15:20 UTC (`mailman hunt watch`, `mailman contributions --refresh`, each
  change checked with `gh`; first run of the `pr-followup` skill): 37 filed. Merged 10, closed
  unmerged 7, open 20: beets#7065, fonttools#4212, ipython#15408, openalgo#2021, openalgo#2022,
  schwifty#318, pdm#3883, py-shiny#2511, prefect#23237, pretix#6564, xarray#11637, pylint#11517,
  pymc#8442, poetry#11052, typeshed#16461, anndata#2666, sqlmesh#6105, tqdm#1837, solara#1215,
  nox#1191. New filings since the morning: beets#7065, pylint#11517, typeshed#16461,
  solara#1215, schwifty#318.
  - Tenth merge: PyPSA#1958, merged by FabianHofmann 2026-10-01 12:33 UTC as `e62ae04` after he
    merged master into the branch (`1894116`) and approved. Provenance re-recorded `MERGED` with
    that commit under `maintainer_commits`. Run record 0021. Fork `wolfgang-aura/PyPSA` deleted by
    the operator 2026-10-02.
  - biopython#5336 closed by us 2026-10-01 09:44 UTC after peterjc put it on hold under the
    draft no-AI policy (biopython#5241). The gate already landed as `ea9e6e0` (#220). Run record
    0022. Nothing further to post.
  - Competitors: the beets row was a false positive, a merged PR that had not closed the issue;
    fixed in `27d60d6` (#363). typeshed#16461 supersedes a stale PR that has had changes
    requested since 2026-03-20 and no activity since, and says so in its body. pylint#11518 by
    another contributor was opened 15 minutes after ours. Ours stand in both repos; nothing
    posted on either.
  - pymc#8442 and ipython#15408 read `unknown` on network errors; both are red only on
    inherited failures (pymc's external-sampler `test_step_args` fails on pymc main too;
    ipython's downstream ipykernel step). openalgo#2021 fails an untouched timing test on
    windows-latest; the operator asked for a re-run at 09:44 UTC. xarray#11637 fails only the
    Windows `test_distributed` timeout; waiting on the maintainer after our 2026-09-30 reply.
    pretix#6564 stays parked on the CLA.
- Re-read 2026-10-02 afternoon (`mailman hunt watch`, `mailman contributions --refresh`, heads
  checked with `gh pr view`): nothing new upstream since 03:10 UTC; counts unchanged.
  - sqlmesh#6105: revision `d9cae3b5` and the review reply went out 2026-10-02 03:10 UTC. Waiting on
    mday-io.
  - solara#1215: `widgetti/solara` PR 1221 by another contributor opened 2026-10-01 23:44 UTC, nine
    hours after ours, for the same issue. Ours stands; nothing posted.
  - openalgo#2021 (same Windows timing run, no re-run yet) and pymc#8442 (approved, red only in
    untouched files, #372) still read `attention`; neither needs work from us.
  - `contributions --refresh` exited 1 on every run because ffn#328 read `unknown` despite a
    recorded `superseded_by`. Fixed by #388.
- Re-read 2026-10-02 later (`mailman hunt watch`, `mailman contributions --refresh`; unattended
  `pr-followup` run): counts unchanged. One row moved.
  - sqlmesh#6105: mday-io reviewed (commented) and merged main into the branch (`2291c5d`). He asked
    to drop the helper-level `_exchange_tables` test, assert the overwrite-level test's only `DROP`
    is the temp table, and confirm `make style` and `make fast-test`. Test-only revision `d9cae3b5`
    in the run workspace on top of his head; 35 clickhouse adapter tests pass, ruff clean. Push and
    reply (`.mailman/drafts/sqlmesh-6105-review-reply.md`) await the operator.
- Re-read 2026-10-02 (`mailman hunt watch`, `mailman contributions --refresh`): counts unchanged
  from the 2026-10-01 15:20 UTC entry (37 filed, 10 merged, 7 closed unmerged, 20 open).
  - beets#7065 and xarray#11637 went `attention` -> `ok`: the merges of the base branch (`3a935d6`,
    `f5e7bfa`) are on the PR heads and both read `blocked` (awaiting review), no longer `dirty`.
    xarray's Windows `test_distributed` and `Test Results` checks no longer fail.
- Re-read 2026-10-02 (`mailman hunt watch`, `mailman contributions --refresh`, each change
  checked with `gh`; unattended `pr-followup` run): 38 filed (awkward#4398 is newly watched).
  Merged 10, closed unmerged 7, open 21. No new merge or close since the 2026-10-01 entry.
  - beets#7065: master moved and `docs/changelog.rst` now conflicts. Merged master in the run
    workspace (both changelog entries kept), 155 `test_lastgenre.py` tests pass. Push awaits the operator.
  - xarray#11637: GitHub reports `dirty`; local merge of origin/main is clean, 116 append and
    datetime zarr tests pass. Push awaits the operator.
  - pymc#8442: ricardoV94 approved. Red checks are in `test_zarr.py` and `test_mcmc_external.py`,
    files the PR does not touch. Watch gap filed as #372. No work.
- Re-read 2026-10-02 (`mailman hunt watch`, `mailman contributions --refresh`; unattended
  `pr-followup` run, `gh pr view 1215`): one PR moved to merged.
  - solara#1215: maartenbreddels merged 08:22 UTC (merge commit `dc83310`), calling the three red
    integration jobs flaky and fixed on master. Record `docs/runs/0023-solara-1215-eleventh-merge.md`.
    Fork delete awaits the operator.
  - pretix#6564 read `attention` again (CLA check); parked, no work.
- Re-read 2026-10-03 (`mailman hunt watch`, `mailman contributions --refresh` not run; unattended
  `pr-followup` run, comments read with `gh api`): 40 rows on the watch table: 10 merged, 8 closed
  unmerged, 22 open. The 2026-10-02 entry said 7 closed; the table lists 8, so one close was uncounted.
  uproot5#1741 and markdown#1648 are newly watched.
  - beets#7065: JOJ0 requested changes (yes/no wording in two error strings). `19e23f0e9` in the run
    workspace; 27 whitelist/canonical tests pass. Push and reply await the operator.
  - markdown#1648: waylan asked for one more test. `e947c55` in the run workspace; it passes with and
    without the fix, as he wanted. Push and reply await the operator.
  - xarray#11637: `dirty` again after main moved; local merge `f77d712c` is clean, 8 datetime/timedelta
    append tests pass. Push awaits the operator.
  - openalgo#2021 (Windows timing flake), pymc#8442 (inherited red) and pretix#6564 (CLA, parked): no work.
- Re-read 2026-10-03 later (`mailman hunt watch`; unattended `pr-followup` run, `contributions --refresh`
  not run): 8 rows need work, 2 moved since 03:01 UTC. semantica#1869 is newly watched (filed 04:39 UTC,
  `behind`, no review yet, no work). tqdm#1837 went from inherited to attention: pre-commit.ci fails
  flake8 B018 in `benchmarks/benchmarks.py:20` and `tests/tests_tqdm.py:1201`, files our diff does not
  touch (it changes contrib tests and discord/telegram); 3 of the 6 newest open tqdm PRs fail the same
  check, so it is inherited again. beets#7065, markdown#1648 and xarray#11637 are unchanged and still
  await push approval.
- Re-read 2026-10-03 09:01 UTC (`mailman hunt watch`; unattended `pr-followup` run, `contributions --refresh`
  not run): 41 rows: 11 merged, 8 closed, 15 ok, 3 inherited, 4 attention (openalgo#2021, pymc#8442,
  tqdm#1837 and the parked pretix#6564, none changed). semantica#1869 merged 07:49 UTC (merge commit `858d5dd`);
  KaifAhmad1's one commit on it is a merge of `main`, record 0024. The beets#7065, markdown#1648 and
  xarray#11637 revisions are now upstream: heads `18e981a`, `488d96a` and `04560f7` were pushed at 07:37 UTC
  and the replies posted at 07:38 UTC, so the earlier push-approval items are done. xarray#11637 is no longer
  `dirty`; its two red checks are inherited.
- edgartools fork `wolfgang-aura/edgartools` deleted 2026-09-23, recreated 2026-09-26 for #1365.
  It still exists and now carries the #1370 PR.

### Reproduce gate, live-verified 2026-09-04

Run `20260903T190542Z-65ac37` against `pmorissette/bt` #461 refused with `bug-not-reproduced`.
0 failures in 1200 randomized trials at base `db6163e`; the same reproducer gives 3 failures
in 256 trials at `2a607df^`, the commit before merged PR #530. That control is what makes the
non-reproduction evidence. No agent ran. Issue #37 closed on it.

### Warning: the history was rewritten on 2026-09-02

Between 20:38 and 20:45 UTC every commit on `main` changed its SHA, on both the local clone
and `origin`, without this session asking for it. Nothing was lost: each new commit's tree
matches the old one byte for byte (`330d6b7` and `ed933ea` both point at tree `7d79d6ea`),
and author, committer, and dates are identical. Only the commit objects differ, which means
the rewrite began at an ancestor and propagated. `origin/hoplite/mytilene-39fb53cc` moved
from `21f47d8` to `d6f0da4` in the same period. The old commits are still reachable through
the GitHub API but not from any local ref. The cause is unidentified. Treat any SHA recorded
before 2026-09-02 20:45 UTC as unresolvable locally, and check `git log` against this file
before trusting either.

## Development environment

- Host operating system: Windows.
- Verified Python: CPython 3.14.3. The project supports Python 3.12 and newer.
- Codex CLI: installed at `%APPDATA%\npm\codex.cmd` and authenticated. It executed a real fixture on 2026-09-02.
- Claude CLI: installed from the Claude desktop app's terminal, so npm put it inside the MSIX package at `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\npm\claude.cmd`, not the real `%APPDATA%\npm`. Since `7244e10`, `mailman.hostpaths.find_executable` searches packaged npm folders after PATH, so `doctor`, the adapters and `resolve_tool` find it without `probe-tool`. It is authenticated. It executed a real review on 2026-09-02. Its adapter flags are verified against the installed build: `--print`, `--input-format`, `--output-format`, `--permission-mode` (`acceptEdits`, `plan`), `--disallowedTools`, `--model`, and the undocumented but accepted `--max-turns`.
- Both CLIs were absent from the host earlier on 2026-09-02 and were installed with `npm install -g @anthropic-ai/claude-code @openai/codex` from an independent terminal. Before that install, a Claude Code agent session reported both as present while the host did not have them. Confirm agent CLI presence from an independent terminal, never from inside an agent session.
- GitHub CLI: installed and authenticated. The user authorized the first public push on 2026-09-02.

- Codex's Windows sandbox will not execute any binary under the user profile. Verified 2026-09-02:
  the same `python --version` fails from `AppData\Local\Python` and succeeds from
  `C:\ProgramData\mailman-python`, while `git` in Program Files runs either way. A virtual
  environment inside a run directory works as long as its **base** interpreter sits outside the
  profile, because only the base process creation is refused.
- `C:\ProgramData\mailman-python` holds a copy of CPython 3.14.3 staged for exactly that reason.
  It needs no administrator rights to create. `mailman doctor` reports "agent-runnable python" and
  names the problem when the current base interpreter is inside the profile.

## Target selection

Set by the repository owner on 2026-09-02, after the first external run used
`ayukhno/autosound-tcc`, a repository with almost no stars or usage.

- External targets must be projects a reader would recognize as real and
  maintained, not whatever repository happened to have an open issue.
- Small issues are fine. Obscure projects are not. The size of the issue and the
  standing of the project are separate choices.
- A candidate is only viable here if a fresh clone is clean, its test suite runs
  on Windows without native, GPU, or Qt dependencies, and a specific test can
  serve as a verification gate.
- Vet a candidate against those checks before any agent runs. Two of the first
  three candidates failed them.

Upstream contribution policies observed on 2026-09-02, which constrain any future
submission:

- Pallets, covering Click, Flask, and Jinja, states that a contribution appearing
  to be LLM-generated will be closed and the author likely blocked.
  See `https://palletsprojects.com/contributing/llm-ai`.
- `python-attrs/attrs` forbids unsupervised agentic tools and refuses any pull
  request carrying an LLM co-author trailer. See its `.github/AI_POLICY.md`.
- `Textualize/rich` has effectively closed external pull requests, citing poor
  quality AI submissions.
- `pypa/packaging` carries no such policy in its repository.

Vetting a target now includes reading its contribution and AI policy before an
agent runs, not before a pull request is opened.

Three further screens, established by hand on 2026-09-03 across ninety candidate
repositories and recorded on
[#35](https://github.com/wolfgang-aura/Mailman/issues/35):

- **Freshness must exclude bots.** `author_association` treats dependabot as a
  `CONTRIBUTOR`. `PyCQA/bandit` scored one outside merge in fourteen days on that
  basis and has merged no human outside pull request since May 2026. Filtering on
  account type also takes `psf/black`, `psf/requests`, `encode/httpx`,
  `fastapi/typer` and `Textualize/textual` to zero. Repositories that pass, with
  human outside merges in the fortnight to 2026-09-03: `scrapy/scrapy` 48,
  `celery/celery` 40, `sqlfluff/sqlfluff` 24, `pydantic/pydantic` 11,
  `redis/redis-py` 11, `pypa/pipx` 11, `pdm-project/pdm` 9, `pypa/virtualenv` 9,
  `pytest-dev/pytest` 7, `encode/starlette` 4.
- **Saturation decides.** Every fresh bug in every recognizable Python repository
  already has a pull request, usually within a day or two. `sqlfluff/sqlfluff`
  #8354 had four attempts, all closed; `python-jsonschema/jsonschema` #1511 has
  seven. Subtracting every issue number mentioned by the last 200 to 400 pull
  requests is the cheap way to compute this: two REST calls per repository rather
  than one search call per issue.
- **Targets screen for automated accounts.** `sqlfluff/sqlfluff` runs an
  `agentscan` workflow that closes a pull request when the account looks
  automated, whatever the content says. `pytest-dev/pytest` uses an "ai rejected"
  label. `PyCQA/bandit` appears to refuse pull request creation from
  non-collaborators outright.

The freshness, saturation and enforcement screens are now mechanical: `mailman
target-intel` computes all three for one named repository and `check-target`
refuses to start without the record. Choosing which repositories to feed it is
still a hand pass, which is what
[#35](https://github.com/wolfgang-aura/Mailman/issues/35) asks for.

An issue's age says nothing about whether its bug still exists. pytest #14964 was
a precise same-day report with no pull request against it and was already fixed on
`main`; only a hand-built reproduction at the base commit caught it, after the
environment had been built. That reading is now mechanical and blocking:
`mailman reproduce` records it and `check-target` refuses without it.
[#37](https://github.com/wolfgang-aura/Mailman/issues/37) is fixed and exercised
through the CLI against a scratch run, including the shape where a fixed tree
still fails and differs only in its counts. It has not yet run against a live
target.

Vetting also includes searching the target's existing pull requests for the same
change before a run starts. On 2026-09-02, three runs were spent on
`pytest-dev/pytest` issue #14324 before anyone searched; the issue already had
four pull requests against it, three closed unmerged and one open. An open issue
is not an unclaimed issue. See `docs/runs/0006-pytest-14324-three-blocked-runs.md`.

Scope, set by the repository owner on 2026-09-16: a hunt is a general Python
hunt. Finance is a preference the coordinator orders candidates by, not a hard
filter. Any recognizable, maintained Python project that passes `screen-target`
is a valid target whatever field it serves; finance targets go first when the
shortlist offers a choice. The recognizability rule set on 2026-09-02 is
unchanged — obscure repositories are still out, in finance as anywhere else.

Screen windows widened by the repository owner on 2026-09-25, after hunt
`20260924T231540Z-218236` found 0 workable candidates in ~70 repositories: every
fresh bug in a passing repository was already claimed. `screen-target` now
defaults to a 730-day issue window (was 90) and a 45-day freshness window (was
14); responsiveness and the triage rule are unchanged. Measured before the
change on 12 near-miss repositories: 6 pass (ApeWorX/ape, agronholm/anyio,
conan-io/conan, copier-org/copier, ipython/ipython, redis/redis-py), and 43 of
68 sampled workable issues there have an OWNER, MEMBER or COLLABORATOR in the
thread. A cached verdict read under other windows is re-read, not reused.

Changed on 2026-09-29 (`685200d`): the responsiveness gate rejects a
repository when at least five outside pull requests were decided and under 30%
of them merged. The old rule (more closed than merged) failed
huggingface_hub at 20 merged and 21 closed. openai-agents-python still fails
at about 5% merged; its merge of #4890 came from maintainers taking the branch
over. A screen's first-response reads now run on four threads.

Observed on 2026-09-17: `urllib3/urllib3` enforces its no-duplicate rule
and treats an issue no maintainer acknowledged as no issue. Asked whether a
second PR for #5053 would be taken given the dormant #5084, maintainer
sigmavirus24 closed the issue `not_planned` and #5084 with it within the hour,
calling the report hypothetical and any further PR unwelcome. urllib3 is off
the target list. An issue with no maintainer reply is a run spent on a bug the
project may not agree exists; the `untriaged-issue` decision gate (#88)
already asks, and [#116](https://github.com/wolfgang-aura/Mailman/issues/116)
asks prescreen to warn earlier.

Decided by the repository owner on 2026-09-17: python-poetry/poetry#11052 sits
as filed. No rebase or force-push while it is merely `behind`; poetry fails the
responsiveness gate (median 14.6 days to first reply, 26% answered), so the
rebase would be effort against a repository that is not reading.

Projects whose policies permit AI-assisted contributions, read on 2026-09-02:
`pytest-dev/pytest` welcomes them with human accountability and appreciates a
`Co-authored-by` trailer; `encode/starlette` permits them and makes a duplicate
search mandatory; `pydantic/pydantic` welcomes them. `modelcontextprotocol/python-sdk`
permits disclosed assistance but closes any outside pull request whose issue a
maintainer has not assigned.

## Authority and artifact boundaries

- The target repository checkout and machine-observed command results outrank agent self-reports.
- Live runs, agent transcripts, cloned target workspaces, and raw logs stay under `.mailman/` and are not public artifacts.
- Tracked examples must contain synthetic or human-reviewed, sanitized data.
- Upstream pushes, pull requests, comments, issue changes, and branch changes require explicit human approval.

## Current capability

The code can initialize a run record, capture a GitHub issue into it, generate the primary and reviewer prompts from that issue, prepare an isolated repository at the exact base commit, install the target's dependencies outside its working tree, register digest-pinned toolchain executables, enforce allowed state transitions, run one configured Codex or Claude CLI adapter, run the bounded primary and reviewer loop, execute a verification command without a shell, export a reviewable patch package, redact common token formats, and report missing local tools. `run-agent` requires a clean primary workspace at the exact base commit. Reviewer workspaces may contain changes descended from that base. The command stores private execution evidence and never changes workflow status by itself.

`mailman orchestrate` runs the bounded loop: primary work, independent verification, review, at most one revision, a second review, final independent verification, then `READY_FOR_HUMAN_REVIEW`. Approval requires a parsed `MAILMAN-VERDICT: APPROVE` line and a passing verification that Mailman runs itself. A missing, unparseable, or contradictory verdict, a second revision request, a failed verification, an agent that exits zero without a report, or an unexpected error all end the run at `BLOCKED`. See `docs/decisions/0004-bounded-orchestration.md`.

Agent executables are resolved before launch and may be pinned per run through `probe-tool` under the agent's name. A missing executable now names the tool and the command that would register it.

The Codex adapter completed a disposable fixture on 2026-09-02. It produced the expected one-line patch under the elevated native Windows sandbox. A later private run registered a bundled Python executable in the run toolchain, and Codex used it to pass the unittest. Mailman then passed the same test independently with the same executable.

The bounded loop is covered by 75 unit tests with scripted agents, and it completed a live two-model run on 2026-09-02. Private run `20260901T201921Z-0b85ed` used Codex as primary and Claude as reviewer on a disposable `slugify` fixture that started with 2 of 3 tests failing. Codex changed one line in 43.4s, Mailman's own verification passed, Claude reviewed in 29.0s and returned a parsed `APPROVE`, the final verification passed, and the run stopped at `READY_FOR_HUMAN_REVIEW` with no revision. Independent confirmation afterwards reproduced three passing tests against the same diff. See `docs/runs/0003-two-model-fixture.md`.

Private run `20260901T202957Z-ec044a` rehearsed public Mailman issue #9 against exact commit `0ba4387fd4187faff88d9a3d900412d4ad2fc367`. The issue text was captured manually. Codex changed `mailman/artifacts.py` and `tests/test_artifacts.py` in the isolated workspace. Mailman's verification passed all 47 tests after the primary stage and again after Claude returned `APPROVE`. The run reached `READY_FOR_HUMAN_REVIEW` with no revision. The reviewed change shipped on `main` in `a371be1`, passed CI on Python 3.12 and 3.14, and issue #9 was closed.

Codex reported that it could not run the pinned Python executable inside its own sandbox and exited `0` anyway. Mailman's independent verification is what carried that run, which is the case the harness exists for.

Three earlier attempts on the same day, runs `20260901T194823Z-56b438`, `20260901T200938Z-3994b1`, and `20260901T201050Z-cd908c`, blocked because the agent executable could not be resolved. They are evidence that the harness refuses to start an unverifiable agent.

Private run `20260902T051904Z-f0cd07` was the first against a repository this project does not own: `ayukhno/autosound-tcc` issue #4 at commit `284d79918991fd29c15902f32dc879487ebf31fa`, from issue URL to exported patch with no hand-written prompt. `fetch-issue` captured the issue with the GitHub CLI, `prepare-environment` initialized submodules and installed the target with its development extra into a virtual environment in the run directory, and the workspace stayed clean. Codex changed one line plus a regression test in 139.8s, Claude approved in 95.3s after running the tests itself and confirming the new test fails without the fix, and both independent verifications passed. Independent confirmation afterwards applied the exported diff to a fresh upstream clone: 36 tests passed, and reverting only the source fix reproduced the issue's exact error. The target's full suite is unusable as a gate on this host, so verification was scoped to `tests/test_dsp_state.py`. The exported patch also carries an unrelated trailing-newline change that neither agent flagged. See `docs/runs/0004-first-external-issue.md`.

The revision cycle, the second review, and a `BLOCKED` ending have live evidence as of 2026-09-02, recorded in `docs/runs/0005-revision-and-blocking-paths.md`. Run `20260902T054112Z-ae9e1b` completed `REVISE`, revision, second review, `APPROVE`, and `READY_FOR_HUMAN_REVIEW` across two review cycles. Run `20260902T054015Z-0e51db` ended `BLOCKED` when verification failed after the revision, on an issue whose acceptance criteria contradicted an existing test. The remaining blocking causes, a missing or contradictory verdict, an agent timeout, and an agent that produces no report, still have only unit coverage.

A verification pass after the primary stage does not prove work happened: in the `clamp` run it passed because the agent changed nothing. The loop now records the changed paths after each primary stage. It does not block on an empty candidate; the reviewer caught that case.

An external target can be dirty on clone. `wandb/rai-toolkit` commits `rai_toolkit/redteam/attacks.py` with CRLF while its own `.gitattributes` declares `*.py text eol=lf`, so every fresh clone is modified before any agent runs and Mailman refuses to start. That is a property of the target, not of the run.

`mailman prepare-submission` checks a finished run against a target's recorded
contribution policy and writes a draft pull request, a human accountability
brief, and a machine-readable verdict. It blocks on diff noise, a missing
changelog entry, a missing or failed duplicate search, an unread or prohibitive
policy, a run that is not `READY_FOR_HUMAN_REVIEW`, and a run with no passing
verification. `mailman duplicate-search` records a GitHub CLI search of the
target's pull requests and issues. Both were exercised against live run data on
2026-09-02. Neither contacts an upstream repository for anything but a read.

`mailman acknowledge-no-test` records why a change ships without a test, pinned to
the exact paths its diff touches. With that record present the `no-test-change`
finding is still reported and still visible in `submission.json`, but no longer
blocks. It exists because run `20260903T052426Z-ad8196`'s reviewer proved, against
an export of the base commit, that a test would be dead coverage, and
`prepare-submission` had no way to accept that argument.

The verification command's executable is resolved through the run toolchain before
anything uses it, so the recorded command, the prompt text and the agent's allow
rules all name the same file. Before that fix a bare `python` ran whatever was
first on PATH, which failed run `20260903T050831Z-bed67e` on a missing dependency
of the host interpreter rather than on the candidate.

Run `20260903T052426Z-ad8196` is the first to reach `"ready": true` from
`prepare-submission`: `encode/starlette` #3497, Claude Opus 5 as both primary and
reviewer, one revision spent on the reviewer requiring the added test be removed.
A branch, a written pull request body and an accountability brief are staged under
the private run directory. Nothing has been sent. See
`docs/runs/0008-starlette-3497-submission-ready.md`.

`mailman target-intel RUN_ID` records how a target actually hands out and merges
outside work: human-only outside merges in a window, open issues no open or merged
pull request references, the automated rules a bot enforces quoted from its own
comments, and the thread that preceded each recent outside merge with the winning
author's comments marked. `check-target` refuses to clear a run without a
successful record, and `--acknowledge-prior-attempts` does not clear that refusal.
It reports counts against their denominators rather than verdicts, and it does not
choose the target. Exercised against `langchain-ai/langchain` on 2026-09-03, where
it found the `require-issue-link` and `block-fork-main` bot rules without being
told to look. See `docs/decisions/0009-target-intel.md`.

`mailman reproduce RUN_ID -- <command>` runs the reporter's own steps in the
prepared workspace at the base commit and records `reproduction.json`.
`check-target` refuses a run with no such record, and refuses again when the
record says the reported behaviour did not happen; neither refusal is clearable
by `--acknowledge-prior-attempts`. The default expectation is a command that
fails; `--expect-output`, `--forbid-output` and `--expect-exit-code` cover a bug
whose fixed and unfixed trees both fail and differ only in what they print, which
is the pytest #14964 shape. A timeout is recorded as a timeout, never as a
reproduction. `--not-machine-reproducible --note` records a human reading and
warns instead of blocking. The command's executable resolves through the run
toolchain. Covered by 19 unit tests, and exercised end to end through the CLI on
2026-09-03 against a scratch run: a bug that still fails passes the gate, a
command that now succeeds is refused, the counts-only pytest #14964 shape is
refused through `--expect-output`/`--forbid-output`, and
`--acknowledge-prior-attempts` clears neither refusal. It has not yet run
against a live target.
See `docs/decisions/0010-reproduction-gate.md`.

Sanitized public run export is not implemented. Nothing in this project has ever
pushed, commented, or opened anything on a repository it does not own.

Three runs against `pytest-dev/pytest` issue #14324 all ended `BLOCKED` on
2026-09-02: two on a failed verification after the primary stage, one when the
agent hit its turn limit and wrote no report. That last one is the first live
evidence for the missing-report blocking cause. See
`docs/runs/0006-pytest-14324-three-blocked-runs.md`.

## PRHunt performance controls

Verified locally on 2026-09-10 after hunt `20260910T081030Z-887058`. The useful
Luna primary completed in 277 seconds and the reviewer in 37 seconds. Mailman
then discarded that completed primary because its reported input was 108,049
tokens above a two-million-token threshold, and the hunt deadline later blocked
handoff packaging even though engineering had completed before it expired.
Two earlier candidates also reached setup because their pre-screens searched
only the issue number; title-based searches performed later found the open
overlaps that caused both drops.

The current fix keeps a completed agent result while recording its token
overrun, gives the primary the same run-owned temporary storage discipline as
the reviewer, uses the captured issue title as the pre-screen duplicate query,
and applies the hunt deadline to selection/setup/agent work rather than
post-engineering packaging. The earlier local checkpoint `2381203` is preserved
and keeps acknowledged target gates stable during packaging. Local verification:
812 tests and 54 subtests passed in 265.15 seconds. The real expired hunt now
reaches `handoff-check`; it correctly stops on 2.3-hour-old duplicate and claim
evidence instead of the expired deadline. Nothing was filed.

Verified on 2026-09-09 after two Luna Max hunts took 5h13m and 7h27m. The
engineering work was not inherently that slow: LangGraph run
`20260909T030115Z-1b980a` reached `ENGINEERING_COMPLETE` in 10.6 minutes. The
long PydanticAI run expanded to 13 files and 772 changed lines, spent five
review cycles and two revisions, and then hit the account usage limit. Agent
records show individual turns consuming millions of input tokens because each
ephemeral turn reread the repository and broad command output was repeatedly
fed back into context.

Local checkpoint `28b5efc` adds a cumulative two-hour run deadline, an explicit
reason for any extension, persistent per-role Codex sessions, a default
two-million-token turn budget, a 20,000-token tool-output limit, narrower prompt
discipline, and a pre-review scope gate of 8 files or 500 changed lines. A live
adapter probe resumed Luna session
`01a085eb-e321-7f33-8424-bf0048f4308d` successfully. The installed Codex CLI is
0.152.0 and accepts `token_budget.limit_tokens`; the older `rollout_budget`
configuration does not exist in that build.

Repository screening now verifies the exact `<!-- require-issue-link -->` bot
marker in closed pull-request comments and rejects targets that require an
assignment before outside work can be filed. Live checks rejected
`langchain-ai/langgraph` from markers on five sampled PRs and allowed
`openai/openai-agents-python` despite three loose search matches that contained
no marker. This prevents completed but unfileable patches such as LangGraph
issue #8850. The regression suite passes: 756 tests and 54 subtests. Tracking
issue: https://github.com/wolfgang-aura/Mailman/issues/81.

The structural workflow now assigns each expensive operation once. Pre-screen
reads the actual GitHub issue and immediately rejects closed or non-bounded
feature, enhancement, question, project and tracking work before duplicate
searches or a run. Its known symbols are copied into the run prompt, together
with Mailman's recorded baseline. The primary performs the edit and focused
checks; Mailman performs both full verification gates; the reviewer inspects
the candidate rather than repeating that gate. Codex revision and repair turns
resume the same role session with only the new findings, and prepared prompts
carry a short role boundary instead of the complete coordinator procedure. See
`docs/decisions/0013-separate-agent-and-harness-work.md`.

## Knowledge flywheel

`mailman retrospective RUN_ID` drafts `retrospective.json` and
`retrospective.md` in the private run directory from evidence Mailman already
holds. It seeds observations only for machine-observed facts and refuses to
overwrite an existing retrospective without `--force`. The taxonomy, the
weighted learning channels, the retrospective schema, and the lesson registry
with its promotion gates are implemented and unit-covered. See
`docs/decisions/0005-knowledge-flywheel.md`.

`knowledge/lessons.json` holds six lessons as of 2026-09-03, written through the
registry's own dataclasses so the state machine and promotion gates were enforced
rather than asserted. All six sit at `CANDIDATE_LESSON`: reproduction at the base
commit, executable resolution before a command is recorded, dead regression
coverage, human-only merge counting, closed-unmerged rows on a repository that
auto-closes, and how assignment is actually won. Only the executable-resolution
lesson has the two distinct supporting runs `VALIDATED` requires, and it was not
promoted on that basis alone.

Nothing yet writes a lesson automatically at the end of a run, so the registry
does not gain evidence without a hand pass. `mailman retrospective` has still not
been run against a live run. Skill versioning and the regression suite are not
implemented, so every retrospective records `skill_version` as `unversioned`.

- Re-read 2026-10-03 afternoon (`mailman hunt watch`, `mailman contributions --refresh`; unattended
  `pr-followup` run): 42 watched rows, 10 merged, 8 closed unmerged, 24 open.
  - django-oauth-toolkit#1927 (run `20261003T054636Z-ae0406`): dopry reviewed 14:07 UTC, five changes
    requested. Revision commit `193ec4b` is in the run workspace, not pushed. The two new view tests
    fail with `ValueError` on the pre-fix `models.py` and pass now; 603 tests in the five touched
    files pass.
  - beets#7065: JOJ0 approved 2026-10-03 11:52 UTC; waiting on a maintainer merge.
  - tqdm#1837 moved from attention to inherited (`pre-commit.ci` fails on 3 of 5 other open PRs).

- Re-read 2026-10-04 00:01 UTC (`mailman hunt watch`, unattended `pr-followup` run): 42 watched rows, counts
  unchanged.
  - Python-Markdown/markdown#1648 went from ok to attention: master gained `7a29324` (#1647), which conflicts
    with our changelog bullet only. Merge `d7fa2bb` is committed in run `20261001T232940Z-89206e`, not pushed.
    The suite passes (1117 tests, 52 skipped). waylan's test request was already answered on 2026-10-03.
    Pushed with operator approval the same day: head `488d96a` -> `d7fa2bb`, fast-forward.
  - django-oauth-toolkit#1927: revision pushed `fa20c4a` -> `193ec4b` with operator approval, reply posted
    (comment 5976958444).
