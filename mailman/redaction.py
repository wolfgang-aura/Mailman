from __future__ import annotations

import re


_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), "[REDACTED_ANTHROPIC_KEY]"),
    (
        re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"),
        r"\1[REDACTED_BEARER_TOKEN]",
    ),
    (
        re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|secret)\s*[=:]\s*)[^\s,;]+"),
        r"\1[REDACTED_SECRET]",
    ),
)


def redact(text: str) -> str:
    """Redact common credential formats from text on a best-effort basis."""
    result = text
    for pattern, replacement in _REDACTIONS:
        result = pattern.sub(replacement, result)
    return result


def changed_lines(diff: str) -> list[str]:
    """Return the added and removed lines of a unified diff, without their marker.

    File headers (`diff --git`, `---`, `+++`, `index`) are excluded by tracking
    hunk boundaries, so a genuine added line that itself starts with `+++` is
    still returned.
    """
    lines: list[str] = []
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith(("+", "-")):
            lines.append(line[1:])
    return lines


def diff_reveals_credential(diff: str) -> bool:
    """Report whether a unified diff adds or removes a line that looks like a secret.

    Context lines are excluded deliberately. A context line is already in the
    upstream repository, so it cannot carry a secret the patch introduces, and
    scanning it can only refuse a clean run. Removed lines stay in scope: a
    patch that deletes a committed secret still reproduces it in the diff.
    """
    return any(redact(line) != line for line in changed_lines(diff))
