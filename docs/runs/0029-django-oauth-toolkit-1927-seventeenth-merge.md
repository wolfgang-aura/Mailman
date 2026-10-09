# 0029. django-oauth-toolkit #1927, the seventeenth merge upstream

Date: filed 2026-10-03, merged 2026-10-08 16:15 UTC. Private run
`20261003T054636Z-ae0406`. Sanitized from local evidence and the public pull
request thread.

Merged as pull request [#1927](https://github.com/django-oauth/django-oauth-toolkit/pull/1927),
merge commit `bafb69f`, five days after filing, by dopry.

## Target and defect

`AllowedURIValidator` accepted a redirect or post-logout URI with a malformed
port (`https://rp.example.com:abc/bye`, `:99999`). `URIValidator`'s regex is not
anchored at the end, so the prefix matched and the rest passed. Once stored,
`urlsplit(...).port` raised `ValueError` and RP-Initiated Logout returned HTTP
500, even for a valid URI registered beside the bad one. +145 -2 across 9 files,
three commits of ours.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-03 from `wolfgang-aura/django-oauth-toolkit` |
| First review | dopry confirmed the fix locally, asked for a view-level test of the reported 500 and a fuller CHANGELOG entry |
| Revision | `193ec4b` added logout and authorize view tests and the changelog text |
| Second review | dopry asked for an assertion that logout ends the session |
| Revision | `47b1dc1` added it; dopry approved and merged |

## What the maintainer changed

Nothing. The merge took our three commits as pushed.

## Lessons

- Both review points were test depth. The first patch fixed the validator and
  proved it with validator-level tests; the maintainer wanted the reported
  symptom (the 500, then the session staying alive) asserted through the view.
  For a bug reported as an HTTP failure, the prompt should ask for a test at the
  layer where the reporter saw it.
