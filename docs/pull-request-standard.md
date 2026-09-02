# Pull request standard

How a Mailman run becomes a pull request a maintainer wants to read.

This covers the step Mailman deliberately does not automate. The harness stops
at a patch package; a person opens the pull request. These are the rules that
person follows, and the rules `mailman prepare-submission` writes into every
draft it generates.

Scope note: nothing here is in
[`skills/open-source-engineering/SKILL.md`](../skills/open-source-engineering/SKILL.md).
That skill governs the agent doing the engineering and admits new rules only
through the lesson registry, on evidence from two runs. These are conventions
for the human filing the result, adopted from outside practice, and they are
recorded here instead.

## Title

Take the convention from the target, not from habit. Read the last ten or
fifteen merged pull requests before writing anything:

```bash
gh pr list --repo OWNER/NAME --state merged --limit 15 --json number,title
```

Projects differ sharply. `encode/starlette` uses plain imperative sentence case
with no prefix ("Reject malformed Host authorities consistently"). Others
require conventional-commit prefixes. Copying the wrong house style marks a
contributor as someone who did not look.

The title states the change and why it matters, not the mechanism, and never
the bug. A pull request title usually becomes a commit message.

- Bad: `BackgroundTask starts before the response has been sent when BaseHTTPMiddleware is in the stack`
  — this is the issue title. It names the defect, not the fix.
- Good: `Defer background tasks until BaseHTTPMiddleware has sent the response`

## Body

Open with the problem in plain language, as a user would hit it. Then the fix,
briefly. Do not lead with an inventory of what was touched — a maintainer who
has to read a list of deleted symbols before learning what broke will not
finish the paragraph.

Order:

1. The problem, in the reporter's terms, with the observable symptom.
2. The cause.
3. The fix, in a sentence or two, with its size.
4. Testing.
5. Any design alternative that was considered and rejected, with the trade-off.
6. How the change was made.

Section 5 is the one that earns replies. A maintainer who sees a stated
trade-off has something to answer; a maintainer who sees a diff and a green
check has nothing to say.

## Testing

Report what a machine did, using the captured results in `verification.json`,
not an agent's account of its own work. Give the before and after counts.

State the limits of the verification in the same breath. If every result came
from one operating system and one interpreter, say so. Concealing that costs
credibility when CI disagrees; stating it turns CI into the check you asked for.

## Disclosure

End with a short paragraph naming the model, the reviewer model, and the
harness, and take responsibility for the change.

Do this even when the target's policy does not require it, unless the target
forbids AI trailers outright — `mailman prepare-submission` reads the policy and
says which case applies. Mailman exists to keep a public record of attempted
agent work. A contribution that hides how it was made cannot be part of that
record, and being found out later costs more than disclosure ever does.

## Filing

- Confirm no pull request already exists for the change. On a target that treats
  duplicates as a ban-level offence, rerun `mailman duplicate-search`
  immediately before pushing; a search from yesterday is not a search.
- Rebase onto the target's current default branch. Verify rather than assume:

  ```bash
  gh api repos/OWNER/NAME/compare/BASE_COMMIT...DEFAULT_BRANCH --jq '{ahead:.ahead_by, behind:.behind_by}'
  ```

- Read the diff yourself against that branch before filing. The patch package is
  evidence, not absolution.
- Open a real pull request, not a draft. Drafts do not get CI or review-bot
  coverage, which is usually the verification a Mailman run is missing.

## After filing

- Act only on findings newer than the last push.
- Verify every bot finding against the source before changing anything. Fix the
  real ones; reject the false ones with a written reason.
- Separate a genuine break from a known infrastructure flake before claiming
  either.
- When nothing is new, say nothing. Filler comments on someone else's repository
  are a cost to them.
- Never merge. It is not your repository, and Mailman's boundary does not move
  because a pull request is open.

## Provenance

Adopted 2026-09-03 from a practitioner's published pull-request guidance, and
checked against `encode/starlette` before adoption: the merged-title survey and
the base-commit comparison above are both real commands run against that
repository. The rules are conventions, not findings from Mailman runs, which is
why they live here rather than in the engineering skill.
