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
