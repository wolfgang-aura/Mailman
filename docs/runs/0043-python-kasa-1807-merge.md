# 0043. python-kasa #1807, merged upstream

Date: filed 2026-10-23 02:25 UTC, merged 2026-10-24 16:11 UTC. Private run
`20261022T044919Z-8ba54f`. Sanitized from local evidence and the public pull
request thread.

Merged as pull request [#1807](https://github.com/python-kasa/python-kasa/pull/1807),
merge commit `0932f30`, about 38 hours after filing, by rytilahti.

## Target and defect

Issue 1524: without `rich` installed, the CLI printed multi-word tags such as
`[bold red]` as plain text. The fallback regex in `kasa/cli/common.py` only
matched single-word tags. The patch widens it to allow spaces and keeps it
lowercase-only, so device data such as `[ABC]` survives. It touches one line
in the source (+1/-1) and adds a regression test (+16) that blocks `rich` and
reloads the module.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-23, one commit |
| Review | rytilahti approved at 16:09 UTC on 2026-10-24 and merged two minutes later |
| Revision | None |
| Checks | Codecov comment only; no failing check on the head |

## What the maintainer changed

Nothing. The merged tree is the head we filed.

## Lessons

- A one-line fix with a test that forces the fallback path (blocking `rich`
  and reloading the module) needed no round trip.
- The issue was a year and a half old and labelled `no-stale`, so a
  maintainer-triaged old bug still merged quickly.