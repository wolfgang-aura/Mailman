# 0048. authlib #964, closed for a fix one layer up

Date: filed 2026-10-18, closed 2026-11-05 11:17 UTC, closed out the same day.
Private run `20261016T030105Z-717d17`. Sanitized from local evidence.

The maintainer used our analysis and shipped a different patch.

## Target and defect

`authlib/authlib`, issue [#759](https://github.com/authlib/authlib/issues/759):
an implicit-only client registered with `client_secret_basic` gets
`invalid_client` at the authorization endpoint. Our
[#964](https://github.com/authlib/authlib/pull/964) overrode
`authenticate_token_endpoint_client` in `ImplicitGrant` so an implicit-only
client skipped the endpoint auth method check.

## What happened

| UTC | Event |
| --- | --- |
| 2026-10-18 16:26 | #964 filed from a personal fork |
| 2026-11-03 | PR went `dirty`; upstream had removed `@hooked`. Merge resolved in the workspace, never pushed |
| 2026-11-05 11:16 | azmeuk opens #1003 against #759 |
| 2026-11-05 11:17 | azmeuk closes #964: "the analysis pointed at the right line", but the authorization endpoint does not authenticate clients at all (RFC 6749 section 3.2.1) |
| 2026-11-05 11:19 | #1003 merges |

#1003 makes the implicit grant look the client up the way the code grant does.
That also fixes a client registered for both `implicit` and
`authorization_code`, which #964 still rejected. Our own PR body said so.

## What the harness did

| Stage | Result |
| --- | --- |
| Reproduction | machine-checked: `invalid_client` at base, `ok` with the patch |
| Review | approved, with the implicit plus `authorization_code` rejection left in place |
| Filing | personal fork, no duplicate PR existed on 2026-10-18 |

## Lessons

- **The close is the fix being better, not the process failing.** No upstream
  duplicate existed at filing, and the maintainer wrote #1003 after our PR
  had sat 18 days.
- **A patch that keeps a rejection it names in its own body is the narrow
  version.** The body disclosed that implicit plus `authorization_code` stays
  rejected. The reviewer accepted that. The cheaper question was whether the
  endpoint should authenticate anyone at all, and the RFC answers it.
- No mechanical gate follows from one case. If a second close shows a patch
  that leaves a disclosed rejection for the reported symptom, the gate is a
  `decision.json` question when the body names a case the patch still rejects.

## Handling

`provenance --superseded-by 1003` recorded. The workspace was reset to the
pushed head `a4d68765`; the unpushed merge commit `ea5e1088` is tagged
`mailman-moot-merge-ea5e1088` in the workspace. Nothing posted on #964, #759
or #1003. The fork branch `fix-implicit-only-client-auth` is left for the
operator to delete, see SESSION_HANDOFF.md.
