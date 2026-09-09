# 0010. pytest #14992, a filed pull request closed unmerged

Date: filed 2026-09-09, closed the same day. Reconstructed on 2026-09-09 from
the upstream record only — the run that produced the patch is not recorded in
this repository and no local run directory was available when this was written.
Everything below is from the public GitHub record and is cited as such.

The second patch a human sent upstream, and the first that a maintainer closed.
`pytest-dev/pytest` pull request
[#14993](https://github.com/pytest-dev/pytest/pull/14993), "Keep usefixtures
fixtures ahead of autouse dependencies", was opened on 2026-09-09 at 02:13 UTC
against issue
[#14992](https://github.com/pytest-dev/pytest/issues/14992) and closed unmerged
by the maintainer `bluetech` at 18:08 UTC. Commit
`0556ce448bc461de43a2c5b06b0972525eb2fc4c`, authored by `wolfgang-aura
<169568318+wolfgang-aura@users.noreply.github.com>`, sat on base
`3fd8675d6d798507c06cf9c60753be6d9d7b0e17`. 58 additions, 10 deletions, 4 files,
1 commit.

## Target and defect

`pytest-dev/pytest`, MIT. The issue was filed by `eldjh3`, not by this project:
a 9.1 regression in which a fixture requested through `usefixtures` is no longer
set up before the dependencies of an earlier autouse fixture, so a guard fixture
runs after the work it exists to protect. The reporter's reproduction failed on
9.1.0 and 9.1.1 and passed on 8.3.5 through 9.0.3, and the reporter added an
independent corroboration from `pytest-common-subject`, whose
`it_evaluates_preconditions_before_invoking_subject` fails on 9.1.1.

The reproduction depends on a `pytest_generate_tests` hook that reorders
`metafunc.fixturenames`, standing in for `pytest-fixture-order`'s
`@pytest.mark.late`. Without that hook the ordering is identical on every
version, which the reporter stated plainly in the issue. That dependency is what
the close turned on.

## The change

`src/_pytest/fixtures.py`, +9 −5: the closure is built by putting `initialnames`
at the front and appending the depth-first traversal minus the names already
there, then sorting by scope, instead of sorting the traversal alone.

The other three files are test and changelog changes, and this is the part worth
recording: besides the new regression test, the patch **rewrote three existing
order assertions** — in `testing/example_scripts/fixtures/test_getfixturevalue_dynamic.py`
and twice in `testing/python/fixtures.py`, including a `fix_a, fix_b, fix_x` to
`fix_a, fix_x, fix_b` change in the circular-dependency test. Those assertions
are the project's record of intended ordering. Editing them is a claim that the
existing behaviour was also wrong, and the pull request body did not make that
claim or defend it; it described them as "updates the affected order
expectations".

## Why it was closed

The maintainer left no comment on the pull request itself, no review, and no
label beyond the changelog bot's `bot:chronographer:provided`. The reasoning is
on the issue, posted 13:23 UTC, about five hours before the close:

> It's hard to understand the use case with all of the AI verbosity. Please, use
> human-speak.
>
> In any case, what `pytest-fixture-order` does in the `pytest_generate_tests`
> hook, namely reodering `metafunc.fixturenames`, is not the intended usage of
> the hook, and accesses private and undocumented parts of pytest. As long as it
> works, that's great, but I am tempted to say it is not supported by core
> pytest.
>
> If you a use case which doesn't involve modifying `metafunc.fixturenames` then
> I can take a look.

Issue #14992 is still open. It was not closed as invalid, and the maintainer
named the condition under which they would look again.

## What this run got wrong

Three separate failures, none of them in the patch's logic:

- **The premise was never checked with the maintainer.** Every mechanical gate
  passed: the issue was fresh, precisely reported, unclaimed, reproduced, and
  had no competing pull request. What none of those gates can see is whether the
  maintainer agrees the reported behaviour is a guarantee at all. Here they did
  not: the ordering only matters through an unsupported use of
  `metafunc.fixturenames`, so from the maintainer's side there is no bug to fix. A
  well-formed issue from a third party is not evidence that the project accepts
  its premise, and eight hours of unattended work went into a question that one
  comment on the issue would have answered first.
- **The pull request body was too long, and a maintainer said so.**
  `SOURCE_OF_TRUTH.md` records that `pytest-dev/pytest` welcomes AI-assisted
  contributions with human accountability. It does, and it still closed this
  one, leading with verbosity. The body carried a "How this was tested" section
  naming the host OS and unrelated Windows cache-permission failures, an "An
  alternative not taken" section, and a disclosure paragraph naming adapter and
  model IDs. None of that is what a reviewer needs in order to decide.
- **An internal state sentence shipped upstream.** The disclosure ended "Human
  review and filing remain pending" — true when `prepare-submission` wrote it,
  false the moment it was filed, and visible to the maintainer as text the
  filer did not read before posting.

## What it got right

The disclosure was present and specific, the author identity on the commit was
the noreply address rather than a personal email (the `docs/runs/0009` lesson
held), the branch was on a personal fork, the changelog entry existed, and the
close cost the maintainer nothing but the time to read.

## Not established

Whether the patch is correct. It was never reviewed on its merits, upstream CI
results were not read for this record, and the three rewritten assertions were
not independently re-derived here. Nothing in this record should be read as
"the fix was good and the maintainer was wrong" — the disagreement is about
whether the behaviour is a guarantee, and that is the maintainer's to settle.
