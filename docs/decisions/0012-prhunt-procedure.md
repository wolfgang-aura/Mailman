# PRHunt procedure and completion

Accepted locally on 2026-09-07. Publication is pending.

Different session models previously had to reconstruct the workflow from
separate commands. Engineering could report human readiness without a decision,
verification could silently use different arguments, and reviewer edits to an
already changed file escaped the workspace check.

Use one packaged procedure, referenced by repository entry points and included
in every prepared role prompt. Persist the quota, exact model choices and
procedure digest. The coordinator acts on missing evidence and replaces poor
candidates. It brings the user an approval packet after all requested PRs pass.

| Gap | Change | Enforcement boundary |
| --- | --- | --- |
| Model-dependent steps | One procedure and pinned model IDs | CLI validates hunt membership; prose guides discovery |
| Premature ready status, #59 | Separate engineering completion and decision finalization | Candidate tree digest and valid decision required |
| Verification mismatch, #58 | Resolve and compare complete argv | Same interpreter with different arguments fails |
| Reviewer changes hidden by existing dirt | Compare candidate content before and after review | Includes previously changed paths |
| Missing environment plan, #55 | Draft from Python metadata | Coordinator must reconcile draft with CI and lock files |
| Wrong Git identity, #57 | Check candidate authors and committers | Submission and handoff refuse violations |
| Routine failures sent to user | Stage-specific next action and review resume | Escalation requires evidence of a user dependency |
| Incomplete requested count | Count distinct ready candidates | Packet completion refuses a shortfall |
| Inconsistent presentation | Existing decision schema and generators | No custom review HTML |

The CLI does not autonomously discover targets or guarantee enough suitable
bugs exist. Search quality, policy interpretation and the truth of written
explanations still require coordinator judgment. Existing lower-level commands
remain available for development; PRHunt must use the complete procedure.

Regression coverage includes exact verification arguments, changed candidate
bytes, reviewer edits, identity changes, recovery without rerunning the primary,
model substitution, quota shortfalls, stale bodies and new competing PRs.
The end-to-end fixture uses scripted agents and performs no upstream writes.
