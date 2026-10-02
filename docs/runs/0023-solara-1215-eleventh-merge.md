# 0023. solara #1215, the eleventh merge upstream

Date: filed 2026-10-01 14:54 UTC, merged 2026-10-02 08:22 UTC. Private run
`20261001T102951Z-c08092`. Sanitized from local evidence and the public pull
request thread.

The eleventh patch merged upstream and the first in `widgetti/solara`. It merged
as pull request [#1215](https://github.com/widgetti/solara/pull/1215), merge
commit `dc83310`, about 17 hours after filing.

## Target and defect

Issue [#1078](https://github.com/widgetti/solara/issues/1078). `IconButton`
declared `on_click=Callable[[], None]`, so the default was the `typing.Callable`
alias instead of `None`. `Button` ran `on_click and on_click()`; the alias is
truthy, and calling it raises `TypeError`. Our change was
`on_click: Callable[[], None] = None` in `solara/components/misc.py` and
`test_icon_button_click_without_on_click` in `tests/unit/button_test.py`.
+14 -1 across 2 files, one commit (`6186f9a`).

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-01 14:54 UTC from `wolfgang-aura/solara` |
| Competing PR | a second pull request, `widgetti/solara` 1221, opened nine hours later for the same issue; ours stood and nothing was posted |
| Checks at merge | three integration jobs red (`windows 3.9` x2, `vue3 ubuntu 3.9`) |
| Upstream | maartenbreddels merged with no change to the patch |

## What the maintainer changed

Nothing. Provenance records no maintainer commits.

## Lessons

- The merger called the three red jobs flaky and already fixed on master, and
  merged over them. The watch's inherited-failure check only compares open
  sibling PRs, so it could not have said that. Reading the thread did.
- Filing first on an issue another contributor was about to take was enough;
  the maintainer picked the earlier PR with the test.
