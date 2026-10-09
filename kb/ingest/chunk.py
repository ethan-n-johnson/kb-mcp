"""Heading-boundary chunk splitter.

Only ATX headings (`#`, `##`, `###`) start a new section; deeper headings stay
in the body. Setext headings are not recognised (trafilatura and pymupdf4llm
both emit ATX).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from kb.ingest.mdutil import FENCE_RE, closes_fence

MIN_WORDS = 50
MAX_WORDS = 350

_HEADING_RE = re.compile(r"^ {0,3}(#{1,3})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_PATH_SEP = " > "


@dataclass(frozen=True)
class Chunk:
    """One search unit: a heading section (or a piece of a long one)."""

    ord: int
    heading_path: str
    text: str
    word_count: int


@dataclass
class _Section:
    """A heading section while merging and splitting are in progress."""

    path: str
    heading_line: str  # empty for the text before the first heading
    body: str


def _words(text: str) -> int:
    return len(text.split())


def _parse_sections(md: str, title: str) -> list[_Section]:
    """Split `md` at H1 to H3 headings, ignoring `#` lines inside code fences."""
    sections: list[_Section] = []
    titles: dict[int, str] = {}
    path, heading_line = title, ""
    body: list[str] = []
    fence: str | None = None

    def flush() -> None:
        text = "\n".join(body).strip("\n")
        if heading_line or text.strip():
            sections.append(_Section(path, heading_line, text.strip()))

    for line in md.split("\n"):
        if fence is not None:
            body.append(line)
            if closes_fence(fence, line):
                fence = None
            continue
        fence_match = FENCE_RE.match(line)
        if fence_match:
            fence = fence_match.group(1)
            body.append(line)
            continue
        heading = _HEADING_RE.match(line)
        if heading is None:
            body.append(line)
            continue
        flush()
        level = len(heading.group(1))
        titles = {lvl: t for lvl, t in titles.items() if lvl < level}
        titles[level] = heading.group(2).strip()
        path = _PATH_SEP.join(titles[lvl] for lvl in sorted(titles))
        heading_line = line.strip()
        body = []
    flush()
    return sections


def _join(*parts: str) -> str:
    return "\n\n".join(p for p in parts if p)


def _merge_small(sections: list[_Section]) -> list[_Section]:
    """Merge sections under `MIN_WORDS` into the next one (last into previous).

    A section that is merged away keeps its heading line in the text so no
    heading is lost; the combined chunk takes the heading path of the section
    that absorbed it.
    """
    merged: list[_Section] = []
    carry = ""
    for section in sections:
        body = _join(carry, section.body)
        if _words(body) < MIN_WORDS:
            carry = _join(carry, section.heading_line, section.body)
        else:
            merged.append(_Section(section.path, section.heading_line, body))
            carry = ""
    if carry:
        if merged:
            merged[-1].body = _join(merged[-1].body, carry)
        elif sections:
            # Everything is small: one chunk for the whole document.
            merged.append(_Section(sections[0].path, "", carry))
    return merged


def _blocks(text: str) -> list[str]:
    """Paragraph blocks of `text`; blank lines inside code fences do not split."""
    blocks: list[str] = []
    current: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        if fence is not None:
            current.append(line)
            if closes_fence(fence, line):
                fence = None
            continue
        fence_match = FENCE_RE.match(line)
        if fence_match:
            fence = fence_match.group(1)
            current.append(line)
        elif line.strip():
            current.append(line)
        elif current:
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))
    return blocks


def _split_long(text: str) -> list[str]:
    """Pack paragraph blocks into pieces of at most `MAX_WORDS` words.

    A single block longer than the limit (one huge paragraph or code fence)
    stays whole rather than being cut mid-block.
    """
    if _words(text) <= MAX_WORDS:
        return [text]
    pieces: list[str] = []
    current: list[str] = []
    count = 0
    for block in _blocks(text):
        size = _words(block)
        if current and count + size > MAX_WORDS:
            pieces.append("\n\n".join(current))
            current, count = [], 0
        current.append(block)
        count += size
    if current:
        pieces.append("\n\n".join(current))
    return pieces


def chunk_markdown(md: str, title: str) -> list[Chunk]:
    """Split Markdown into heading-based chunks.

    Sections are cut at H1, H2 and H3 (never at a `#` line inside a code fence).
    `heading_path` joins the active headings with " > "; text before the first
    heading uses `title` as its path. Sections under 50 words are merged into the
    next one (the last into the previous); sections over 350 words are split at
    paragraph breaks, never inside a code fence, and every piece keeps the same
    path. `ord` is 0-based and contiguous.
    """
    sections = _parse_sections(md.replace("\r\n", "\n"), title)
    chunks: list[Chunk] = []
    for section in _merge_small(sections):
        for piece in _split_long(section.body):
            text = piece.strip()
            if text:
                chunks.append(Chunk(len(chunks), section.path, text, _words(text)))
    return chunks
