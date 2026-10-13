# 0034. python-markdown2 #732, the twenty-second merge upstream

Date: filed 2026-10-07 01:12 UTC, merged 2026-10-13 00:33 UTC. Private run
`20261006T185837Z-0c561b`. Sanitized from local evidence and the public pull
request thread.

Merged as pull request [#732](https://github.com/trentm/python-markdown2/pull/732),
merge commit `eec056c`, six days after filing, by nicholasserra.

## Target and defect

Unclosed HTML tags sent a regex into catastrophic backtracking (issue 707, a
ReDoS). The fix bounds the scan and keeps the old rule that a tag split across
lines is not counted. +34 -3 across 3 files.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-07, three commits of ours |
| Review | Crozzers left two review comments with no body; one clarifying question, answered by us on 2026-10-10 |
| Merge | nicholasserra merged `master` into the branch (`1072f36`), then merged the PR with "Thank you!" |

## What the maintainer changed

Only the merge of `master`: `CHANGES.md` +1, `README.md` +4 -3, `lib/markdown2.py` -2.
No change to our fix.

## Lessons

- A question about behavior (why a multi-line tag is not counted) was answered
  from the old regex's missing DOTALL, with the code behind it. That ended the thread.
- The maintainer updated the branch himself again, so waiting on `behind` was right.
