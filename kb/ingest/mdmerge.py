"""Building the Markdown of a merged document."""

from __future__ import annotations

import re
from dataclasses import dataclass

from kb.ingest.mdutil import FENCE_RE, closes_fence

MAX_HEADING_LEVEL = 6

_ATX_RE = re.compile(r"^( {0,3})(#{1,6})([ \t].*|)$")
_H1_LINE_RE = re.compile(r"^ {0,3}#[ \t]+(.+?)[ \t]*#*[ \t]*$")


@dataclass(frozen=True)
class MergeSource:
    """A document to fold into the target."""

    title: str
    source_url: str | None
    markdown: str


def demote_headings(md: str) -> str:
    """Move every ATX heading one level deeper (`#` becomes `##`), capped at `######`.

    Lines inside fenced code blocks are left alone, so a `# comment` in a shell
    snippet stays a comment.
    """
    out: list[str] = []
    fence: str | None = None
    for line in md.split("\n"):
        if fence is not None:
            out.append(line)
            if closes_fence(fence, line):
                fence = None
            continue
        fence_match = FENCE_RE.match(line)
        if fence_match:
            fence = fence_match.group(1)
            out.append(line)
            continue
        heading = _ATX_RE.match(line)
        if heading:
            indent, hashes, rest = heading.groups()
            line = f"{indent}{'#' * min(len(hashes) + 1, MAX_HEADING_LEVEL)}{rest}"
        out.append(line)
    return "\n".join(out)


def _drop_repeated_title(md: str, title: str) -> str:
    """Remove a leading H1 equal to `title`; the merged section heading replaces it."""
    lines = md.split("\n")
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is None:
        return md
    match = _H1_LINE_RE.match(lines[first])
    if match and match.group(1).strip() == title.strip():
        return "\n".join(lines[first + 1 :]).strip("\n")
    return md


def merge_markdown(target_md: str, sources: list[MergeSource]) -> str:
    """The target's Markdown followed by one section per source.

    Each section is `## <source title>`, then `Source: <url>` when the source
    has a URL, then the source body with every heading demoted one level. A
    leading H1 that repeats the source title is dropped first, because the new
    `##` heading already carries it (otherwise the section would start with the
    same title twice and its own headings would land beside it, not under it).
    """
    parts = [target_md.strip("\n")]
    for source in sources:
        body = demote_headings(_drop_repeated_title(source.markdown, source.title)).strip("\n")
        section = [f"## {' '.join(source.title.split())}"]
        if source.source_url:
            section.append(f"Source: {source.source_url}")
        if body:
            section.append(body)
        parts.append("\n\n".join(section))
    return "\n\n".join(parts) + "\n"
