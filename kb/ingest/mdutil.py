"""Small Markdown helpers shared by conversion and chunking."""

from __future__ import annotations

import re

FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def closes_fence(fence: str, line: str) -> bool:
    """True when `line` closes a code fence opened with the marker `fence`."""
    stripped = line.strip()
    return (
        len(stripped) >= len(fence)
        and set(stripped) == {fence[0]}
        and len(line) - len(line.lstrip(" ")) <= 3
    )
