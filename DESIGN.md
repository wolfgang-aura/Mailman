# Design

The visual language for anything Mailman renders for a person. Written
2026-09-02, when the run review page was built.

## What the page is for

One run, one page, one decision: does this patch go upstream or not. Everything
on it exists to make that decision or to justify it later. Nothing is there to
look impressive.

The first thing a viewer should notice is the verdict strip: the run status, the
reviewer's verdict, and whether Mailman's own gate passed. The second is the
diff. Everything else is evidence they scroll to when the first two are not
enough.

## Reference

GitHub's commit and pull request review view, captured 2026-09-02 at 1440x900
(`https://github.com/encode/starlette/commit/39fd0ff`). What was taken from it:

- a file card per changed file, path and stat in a header bar, body scrolling
  horizontally on its own
- two number gutters and a sign column before the code
- the whole row tinted for an added or removed line, not just the text
- 12px monospace for code, roughly 1.5 line height

What was deliberately not taken: the top navigation, the file tree rail, and
the review controls. This page has no accounts, no comments, and no actions.

## Viewports

- Golden viewport: 1440x900. Design and screenshot here first.
- Must also read at 390x844. The rail moves above the content and the facts
  grid becomes one column.

## Type

One family stack for prose, one for code. No third.

| Role | Size | Weight | Notes |
| --- | --- | --- | --- |
| Page title | 22px | 600 | run id, monospace |
| Section heading | 15px | 600 | sentence case, never title case |
| Body | 14px | 400 | 1.55 line height |
| Code and diff | 12.5px | 400 | 1.5 line height |
| Label | 11px | 600 | uppercase, 0.06em tracking, muted |

## Spacing

A 4px base. Use 4, 8, 12, 16, 24, 32, 48. Nothing between.

Section gap 32. Card padding 16. Grid gutter 12.

## Colour

Tokens are defined on `:root` for light and redefined under
`prefers-color-scheme: dark`. Never define a colour in only one of the two.

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--bg` | `#f6f7f9` | `#0d1117` | page |
| `--surface` | `#ffffff` | `#151b23` | cards |
| `--border` | `#d8dee4` | `#2a323c` | hairlines |
| `--ink` | `#1c2128` | `#e6edf3` | body text |
| `--muted` | `#5b6773` | `#9198a1` | labels, secondary |
| `--accent` | `#0969da` | `#4493f8` | links, focus |
| `--ok` | `#1a7f37` | `#3fb950` | passed, approved |
| `--stop` | `#cf222e` | `#f85149` | blocked, failed |
| `--warn` | `#9a6700` | `#d29922` | unverified, absent |
| `--add-bg` | `#e6ffec` | `#12261e` | added diff row |
| `--del-bg` | `#ffebe9` | `#25171c` | removed diff row |

Status colour carries meaning, so never use `--ok` or `--stop` decoratively.

## Rules

- No CSS framework and no CDN. The page must open from a local file with no
  network, because that is where run evidence lives. The cost is that every
  component here is hand-written and there is no component library to inherit
  from; keep the component count small enough that this stays true.
- No JavaScript for anything structural. Collapsing uses `<details>`.
- Every string that came from an agent, an issue, or a command is escaped. All
  of it is untrusted input.
- Absolute host paths are shortened for display. The full path stays in the
  title attribute for anyone who needs it.
