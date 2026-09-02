"""Render the markdown that agents actually write, and nothing more.

An agent report is the most readable thing a run produces and it was being
shown as a wall of preformatted text, asterisks and all. A full markdown
library would be a dependency for a project that has none, so this covers the
subset the reports use: headings, lists, tables, fenced code, block quotes,
and the inline spans. Anything unrecognized falls through as a paragraph, which
is the failure a reader can still read.

Every value is escaped before any markup is added, so an agent cannot write
HTML into the page by writing HTML into its report.
"""

from __future__ import annotations

import html
import re

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BARE_LINK = re.compile(r"(?<![\"'>=])(https?://[^\s<>()\[\]]+)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_TABLE_RULE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")


def _inline(text: str) -> str:
    """Escape first, then add the few spans a report uses."""
    escaped = html.escape(text, quote=False)
    escaped = _CODE.sub(lambda match: f"<code>{match.group(1)}</code>", escaped)
    escaped = _BOLD.sub(lambda match: f"<strong>{match.group(1)}</strong>", escaped)
    escaped = _ITALIC.sub(lambda match: f"<em>{match.group(1)}</em>", escaped)
    escaped = _LINK.sub(
        lambda match: f'<a href="{match.group(2)}">{match.group(1)}</a>', escaped
    )
    return _BARE_LINK.sub(lambda match: f'<a href="{match.group(1)}">{match.group(1)}</a>', escaped)


def _split_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def render_markdown(text: str) -> str:
    """Render a report as HTML. Unknown syntax degrades to a paragraph."""
    lines = str(text).replace("\r\n", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []
    index = 0

    def flush() -> None:
        if paragraph:
            out.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush()
            index += 1
            block: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                block.append(lines[index])
                index += 1
            index += 1
            body = html.escape("\n".join(block), quote=False)
            out.append(f'<pre class="block"><code>{body}</code></pre>')
            continue

        if not stripped:
            flush()
            index += 1
            continue

        heading = _HEADING.match(stripped)
        if heading:
            flush()
            level = min(len(heading.group(1)) + 2, 6)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        if stripped.startswith(">"):
            flush()
            quoted = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quoted.append(lines[index].strip().lstrip(">").strip())
                index += 1
            out.append(f"<blockquote>{_inline(' '.join(quoted))}</blockquote>")
            continue

        if set(stripped) <= {"-", "*", "_"} and len(stripped) >= 3:
            flush()
            out.append("<hr>")
            index += 1
            continue

        if (
            "|" in stripped
            and index + 1 < len(lines)
            and _TABLE_RULE.match(lines[index + 1])
        ):
            flush()
            header = _split_row(stripped)
            index += 2
            rows = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_split_row(lines[index]))
                index += 1
            head = "".join(f"<th>{_inline(cell)}</th>" for cell in header)
            body = "".join(
                "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>"
                for row in rows
            )
            out.append(f"<table class='prose'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
            continue

        if _BULLET.match(line) or _NUMBERED.match(line):
            flush()
            ordered = bool(_NUMBERED.match(line))
            items = []
            while index < len(lines):
                current = lines[index]
                bullet = _NUMBERED.match(current) if ordered else _BULLET.match(current)
                if not bullet:
                    if current.strip() and current.startswith(("  ", "\t")) and items:
                        # A wrapped continuation belongs to the item above it.
                        items[-1] += " " + current.strip()
                        index += 1
                        continue
                    break
                items.append(bullet.group(2) if ordered else bullet.group(1))
                index += 1
            tag = "ol" if ordered else "ul"
            body = "".join(f"<li>{_inline(item)}</li>" for item in items)
            out.append(f"<{tag}>{body}</{tag}>")
            continue

        paragraph.append(stripped)
        index += 1

    flush()
    return "".join(out)
