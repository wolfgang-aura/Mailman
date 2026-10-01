---
name: pr-followup
description: Check every filed upstream pull request for updates, do the work the ones that moved need (maintainer replies, red checks, a base that moved on, merges, closes), and bring the status docs up to date. Use when the operator says "check PRs", "PR updates", "follow up on filed PRs", "what moved upstream", "/pr-followup", or starts a session with no open hunt and asks what needs doing.
---

# PR follow-up

One pass covers all filed pull requests: read them, sort them, do the work,
put anything public behind one approval gate, then update the docs. The
machine reading is `mailman hunt watch`. This skill says what to do with it.
`mailman/procedure.md`, "Approval and follow-through" and "Watch what was
filed", is the authority; read both sections before acting.

## 1. Preflight

- `git status --short`, `git log --oneline -5`. Leave uncommitted changes
  from other sessions alone and stage only your own paths.
- Read `SESSION_HANDOFF.md` ("Upstream state", "Blocked on the operator") and
  the SOURCE_OF_TRUTH.md tally entry with the latest date. Treat both as claims
  and check them against the reading in step 2.
- Parked rows are listed in one line and get no work. Today these are
  pretix#6564 (CLA, operator parked it) and biopython#5336 (AI-policy hold,
  no comment). A row comes off the parked list when a maintainer comments,
  the PR closes, or the operator asks about it.

## 2. Read

```
mailman hunt watch
mailman contributions --refresh
```

`hunt watch` exits 1 while any row is `attention` or `unknown`. Exit 1
means there is work, not that the command broke. Read the trailing "changed
since" block first; it lists only what moved. Watch the GitHub core limit:
parallel screens have closed the API for 15 minutes before. Check
`gh api rate_limit` before a long run of `gh` reads.

## 3. Triage each row

| Status | Action |
| --- | --- |
| `ok`, `approved`, `inherited` | Nothing. One line each. `inherited` is out of scope even when it is red. |
| parked | One line. |
| `unknown` | Read the row again with `gh pr view`. If it is still unreadable, report the exact error. A reading with a hole in it proves nothing. |
| `merged` | Step 4a. |
| `closed` | Step 4b. |
| `attention` | Step 4c, by the reason printed on the line after the row. |

## 4. Work

Work happens in the run's own workspace, `.mailman/runs/RUN_ID/workspace`.
Nothing here pushes, comments or deletes anything upstream; that waits for
step 5.

### 4a. Merged

1. `mailman provenance RUN_ID --pr N` so the run records `MERGED`. If a
   maintainer pushed on top, provenance records `maintainer_commits`.
2. Write `docs/runs/NNNN-<target>-<pr>-<ordinal>-merge.md` in the shape of the
   latest record: target and defect, what the harness did (table), what the
   maintainer changed, and lessons. Add it to the merge sentence in
   `README.md` ("The loop has run end to end...").
3. Fork cleanup: delete `wolfgang-aura/<repo>` only when the merge commit is
   on the upstream default branch and no other open PR uses that fork. Put
   the delete command in the step-5 gate. Deleting the fork while a PR from it
   is open closes that PR.

### 4b. Closed unmerged

Every close has a reason. Find it before you write anything.

1. Read the PR timeline and the linked issue's timeline: who closed what,
   when, and in favour of what.
2. If another PR superseded ours: `mailman provenance RUN_ID --superseded-by N`.
3. Write the run record, then search for a Mailman issue covering the
   mechanical miss and file or update one. Land the gate with a test when the
   fix is contained.
4. Post nothing on the closed PR, the issue or the winning PR. One short
   step-back reply is the most a closed case gets, and only if the operator
   wants one.

### 4c. Attention

First, for every reason, check the PR head:
`gh pr view N --repo OWNER/REPO --json headRefOid,commits,reviews,mergeStateStatus`.
Compare it with the last head we pushed (`provenance.json`). If someone else
pushed or approved a head we did not push, stop. Read what they did. Do not
rebase, force-push or draft a revision over it. Usually the next move is
theirs.

- **Maintainer comment or review.** `mailman fetch-review RUN_ID --pr URL`.
  It misses PR conversation comments (Mailman #128), so append the non-own
  `repos/OWNER/REPO/issues/N/comments` bodies to the run's
  `maintainer-review.md` by hand. Then:
  - Change requested: `build-prompts`, then `resume-review` with an absolute
    path to the run env's python, then `revision-response RUN_ID --init`.
    Answer every point and run `revision-response` until it exits 0. Re-run
    the touched tests yourself.
  - Question or decision: draft the reply in
    `.mailman/drafts/<target>-<pr>-<slug>.md`. Keep it under 120 words unless
    the maintainer asked several questions, and answer only what was asked.
- **`behind` / `dirty`.** Read CONTRIBUTING and recent merged PRs to see
  whether the project wants contributors merging main in. Some maintainers
  update the branch themselves. If it does, merge main in the run workspace
  (no rebase, no force-push), resolve the conflicts, and re-run the touched
  tests. A release that moved the changelog section is a conflict to resolve
  with care: follow where the project now puts unreleased entries.
- **Failing check.** `gh run view RUN --repo OWNER/REPO --log-failed`. If the
  failure is in code or tests we touched, reproduce it locally, fix it in the
  workspace, and re-run. If it is a flake or a base-branch break that
  `hunt watch` missed, say so with the evidence and file the watch gap as a
  Mailman issue. Do not push an empty commit to retrigger CI.

A harness gap found here (a missed signal, a wrong status) gets a Mailman
GitHub issue in the same turn. Write upstream references in code spans,
never `owner/repo#N` or a github.com URL, so nothing backlinks upstream.

## 5. Approval gate

Upstream writes need the operator's approval for that specific action:
pushes, comments, PR edits, fork deletes. The only exception is when the
operator has authorised them explicitly in this session. Collect every write
into one message:

- for each push: repo, branch, old head..new head, a diff summary, and the
  tests re-run with their results;
- for each comment: the full text, inline, plus the draft path;
- the exact commands, one per fenced block, with each flag checked against
  the installed `gh` help. Comments go out as
  `gh pr comment N --repo OWNER/REPO --body-file "<absolute draft path>"`.

After approval, run them and record the result: the new head in provenance and
the comment URL in the handoff. If the bytes change after approval, show them
again.

## 6. Docs

Update only what changed. Every entry carries a date and the evidence it rests on.

- `SOURCE_OF_TRUTH.md`, under "## Repository": a new dated bullet, "Re-read
  YYYY-MM-DD (`mailman hunt watch`, `mailman contributions --refresh`)", giving
  filed / merged / closed / open counts and one sub-bullet per PR whose state
  moved since the previous entry. If nothing moved, add one line saying the
  re-read found nothing new, so the doc shows when somebody last looked.
- `SESSION_HANDOFF.md` (gitignored): rewrite "Upstream state", "Blocked on the
  operator" and "Next step" to match the reading. Drop resolved items.
- `README.md` merge sentence and `docs/runs/` for merges and closes (step 4).
- Memory: a new standing rule from the operator goes in a memory file. A
  parked row that is no longer parked comes off the list in this skill.

Commit the tracked docs with explicit paths (`git add <paths>`), message
`docs: PR follow-up YYYY-MM-DD`, and push to Mailman's origin.

## 7. Report

One line per PR that needs or got work. Everything else is a count:

```
xarray#11637  attention: dcherian asked X -> revision 905bc308 ready, push awaiting approval
nicegui#6345  merged -> record 0021, fork delete awaiting approval
14 quiet (ok/approved/inherited), 2 parked
```

Then the Mailman issues touched, in the `#N title — fixed in sha / filed`
format, then the status line from the global instructions.
