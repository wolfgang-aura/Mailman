"""Size limits for recorded evidence.

An orchestration record once reached 124,628,965 bytes because every `target`
step embedded a full target assessment and `resume-review` replayed all prior
steps. Nothing read those bytes. The limits here keep a record readable and
turn silent growth into a visible marker.

See https://github.com/wolfgang-aura/Mailman/issues/66.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Characters kept from one captured stream before eliding the middle.
STREAM_CHARACTER_LIMIT = 200_000

#: Bytes of JSON one orchestration step may carry inline.
STEP_BYTE_LIMIT = 65_536

ELISION = "\n\n... [mailman elided {dropped} characters] ...\n\n"


def truncate_stream(text: str, limit: int = STREAM_CHARACTER_LIMIT) -> str:
    """Keep the head and tail of a captured stream, elide the middle.

    A failure's cause is usually near the start and its verdict near the end.
    Dropping the middle keeps both.
    """
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + ELISION.format(dropped=len(text) - limit) + text[-half:]


def offload(data: dict[str, Any], destination: Path, *, keep: tuple[str, ...],
            limit: int = STEP_BYTE_LIMIT) -> dict[str, Any]:
    """Write oversized step data to its own file and keep a pointer inline.

    `keep` names the small fields worth reading inline. Everything else stays
    in the file, so the evidence is not lost, only moved out of a record that
    is read whole on every load.
    """
    encoded = json.dumps(data)
    if len(encoded.encode("utf-8")) <= limit:
        return data
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    summary: dict[str, Any] = {key: data[key] for key in keep if key in data}
    summary["offloaded_to"] = str(destination)
    summary["offloaded_bytes"] = len(encoded.encode("utf-8"))
    return summary
