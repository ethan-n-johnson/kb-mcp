"""HTML, PDF and Markdown to clean Markdown."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import unquote, urlsplit

import yaml

from kb.ingest.errors import IngestError
from kb.ingest.mdutil import FENCE_RE, closes_fence

Format = Literal["html", "pdf", "markdown"]

MAX_PDF_PAGES = 300
MIN_HTML_WORDS = 5
MIN_PDF_CHARS = 100
MIN_PDF_CHARS_PER_PAGE = 20

# Whole-line boilerplate patterns, matched case-insensitively against a line
# after markdown decoration (links, emphasis, list markers, heading hashes) is
# removed. Phase 11 extends this tuple when the demo corpus shows new noise.
BOILERPLATE_PATTERNS: tuple[str, ...] = (
    r"skip to (main )?content",
    r"edit this page( on github)?",
    r"was this page helpful\??.*",
    r"table of contents",
    r"(we|this (web)?site) uses? cookies\b.*",
    r"(accept|reject|manage|decline)( all)? cookies?",
    r"cookie (settings|preferences|policy|notice)",
)
_BOILERPLATE_RE = re.compile(
    "|".join(f"(?:{p})" for p in BOILERPLATE_PATTERNS), re.IGNORECASE
)

_HEADING_EMPHASIS_RE = re.compile(r"^( {0,3}#{1,6}[ \t]+)(\*\*|__)(.+?)\2[ \t]*$")
_EMPTY_HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s*#*\s*$")
_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_DECORATION_RE = re.compile(r"^[\s#>*_\-+`]+|[\s*_`]+$")
_FRONT_MATTER_RE = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_H1_RE = re.compile(r"^ {0,3}#[ \t]+(.+?)[ \t]*#*[ \t]*$")

# mkdocs syntax found in the FastAPI docs (phase 11): `{* ../../docs_src/x.py hl[1] *}`
# include lines, heading anchors `## Title { #title }`, and `/// tip` or
# `//// tab | Python 3.10+` admonition fences closed by a line of slashes.
_MKDOCS_INCLUDE_RE = re.compile(r"^\s*\{\*.*\*\}\s*$")
_HEADING_ANCHOR_RE = re.compile(r"^( {0,3}#{1,6}[ \t]+.*?)[ \t]*\{[ \t]*#[\w.:-]+[ \t]*\}[ \t]*$")
_ADMONITION_RE = re.compile(r"^ {0,3}/{3,}(?:[ \t]*([A-Za-z][\w-]*)(?:[ \t]*\|[ \t]*(.*?))?)?[ \t]*$")

_EXT_FORMATS: dict[str, Format] = {
    ".html": "html",
    ".htm": "html",
    ".xhtml": "html",
    ".pdf": "pdf",
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdown": "markdown",
    ".txt": "markdown",
}
_TYPE_FORMATS: dict[str, Format] = {
    "text/html": "html",
    "application/xhtml+xml": "html",
    "application/pdf": "pdf",
    "text/markdown": "markdown",
    "text/x-markdown": "markdown",
    "text/plain": "markdown",
}
_DECLARED_ALIASES: dict[str, Format] = {
    "html": "html",
    "htm": "html",
    "pdf": "pdf",
    "markdown": "markdown",
    "md": "markdown",
}


@dataclass(frozen=True)
class Converted:
    """Result of converting one input to Markdown."""

    title: str
    markdown: str
    tags: list[str]
    source_type: str


def _format_from_extension(path: str | None) -> Format | None:
    if not path:
        return None
    return _EXT_FORMATS.get(PurePosixPath(path.replace("\\", "/")).suffix.lower())


def detect_format(
    *,
    declared: str | None = None,
    filename: str | None = None,
    content_type: str | None = None,
    url: str | None = None,
) -> Format:
    """Decide whether input is `html`, `pdf` or `markdown`.

    Order: the declared format, then the filename extension, then the URL path
    extension, then the Content-Type. Falls back to `html` when nothing matches.
    Raises `IngestError("bad_format")` for an unknown declared format.
    """
    if declared:
        fmt = _DECLARED_ALIASES.get(declared.strip().lower())
        if fmt is None:
            raise IngestError("bad_format", f"unknown format {declared!r}")
        return fmt
    fmt = _format_from_extension(filename)
    if fmt:
        return fmt
    if url:
        fmt = _format_from_extension(unquote(urlsplit(url).path))
        if fmt:
            return fmt
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    return _TYPE_FORMATS.get(media_type, "html")


def _decorated_text(line: str) -> str:
    """Line with markdown links, emphasis, list markers and hashes removed."""
    return _DECORATION_RE.sub("", _LINK_RE.sub(r"\1", line.strip()))


def _admonition_line(line: str) -> str | None:
    """Replacement for an mkdocs admonition fence line, or None if `line` is not one.

    A closing fence (only slashes) and a tab marker become an empty string (the
    line is dropped). An opening fence becomes a bold label, the title when it
    has one (`/// note | Technical Details`), else the type (`/// tip`).
    """
    match = _ADMONITION_RE.match(line)
    if match is None:
        return None
    kind, title = match.group(1), (match.group(2) or "").strip()
    if kind is None or kind.lower() == "tab":
        return ""
    return f"**{title or kind.capitalize()}:**"


def clean_markdown(md: str) -> str:
    """Strip boilerplate and tidy whitespace.

    Drops empty headings and lines matching `BOILERPLATE_PATTERNS`, trims
    trailing spaces and collapses runs of blank lines to one. mkdocs syntax
    (include lines, heading anchors, admonition fences) is removed or turned
    into plain text. Fenced code blocks are left untouched.
    """
    out: list[str] = []
    fence: str | None = None
    for raw in md.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = FENCE_RE.match(raw)
        if fence is not None:
            out.append(raw)
            if closes_fence(fence, raw):
                fence = None
            continue
        if match:
            fence = match.group(1)
            out.append(raw.rstrip())
            continue
        line = raw.rstrip()
        if _MKDOCS_INCLUDE_RE.match(line):
            continue
        admonition = _admonition_line(line)
        if admonition is not None:
            if not admonition:
                continue
            line = admonition
        line = _HEADING_ANCHOR_RE.sub(r"\1", line)
        line = _HEADING_EMPHASIS_RE.sub(r"\1\3", line)
        if _EMPTY_HEADING_RE.match(line):
            continue
        decorated = _decorated_text(line)
        if decorated and _BOILERPLATE_RE.fullmatch(decorated):
            continue
        if not line and (not out or not out[-1]):
            continue  # collapse a run of blank lines to one
        out.append(line)
    return "\n".join(out).strip()


def _title_from_url(url: str | None) -> str | None:
    """Readable title from the last URL path segment, if there is one."""
    if not url:
        return None
    segment = PurePosixPath(unquote(urlsplit(url).path)).name
    stem = PurePosixPath(segment).stem if segment else ""
    words = re.sub(r"[-_+]+", " ", stem).strip()
    return words or None


def _first_h1(md: str) -> str | None:
    """Text of the first H1 outside fenced code blocks."""
    fence: str | None = None
    for line in md.split("\n"):
        match = FENCE_RE.match(line)
        if fence is not None:
            if closes_fence(fence, line):
                fence = None
            continue
        if match:
            fence = match.group(1)
            continue
        h1 = _H1_RE.match(line)
        if h1:
            return h1.group(1).strip()
    return None


def _first_heading(md: str) -> str | None:
    """Text of the first ATX heading of any level, outside code fences."""
    fence: str | None = None
    for line in md.split("\n"):
        match = FENCE_RE.match(line)
        if fence is not None:
            if closes_fence(fence, line):
                fence = None
            continue
        if match:
            fence = match.group(1)
            continue
        heading = re.match(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", line)
        if heading:
            return heading.group(1).strip()
    return None


def _strip_inline(text: str) -> str:
    """Plain text of a heading: markdown emphasis and links removed."""
    return re.sub(r"[*_`]+", "", _LINK_RE.sub(r"\1", text)).strip()


def _html_to_markdown(data: bytes | str, url: str | None) -> Converted:
    import trafilatura

    markdown = trafilatura.extract(
        data,
        output_format="markdown",
        include_tables=True,
        include_links=True,
        url=url,
    )
    if not markdown or not markdown.strip():
        raise IngestError("empty_content", "no readable content found in the HTML")
    title: str | None = None
    try:
        meta = trafilatura.extract_metadata(data, default_url=url)
        title = (meta.title or "").strip() if meta is not None else None
    except Exception:  # metadata is best effort; fall through to other sources
        title = None
    cleaned = clean_markdown(markdown)
    # trafilatura falls back to navigation links on pages with no article, so a
    # handful of words counts as empty.
    if len(_LINK_RE.sub(r"\1", cleaned).split()) < MIN_HTML_WORDS:
        raise IngestError("empty_content", "no readable content found in the HTML")
    h1 = _first_h1(cleaned)
    title = title or (_strip_inline(h1) if h1 else None) or _title_from_url(url) or "Untitled"
    return Converted(title=title, markdown=cleaned, tags=[], source_type="html")


def _pdf_to_markdown(data: bytes | str, url: str | None) -> Converted:
    import pymupdf
    import pymupdf4llm

    if isinstance(data, str):
        raise IngestError("bad_pdf", "PDF content must be bytes")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise IngestError("bad_pdf", "could not open the PDF") from exc
    with doc:
        if doc.needs_pass:
            raise IngestError("bad_pdf", "PDF is password protected")
        pages = doc.page_count
        if pages > MAX_PDF_PAGES:
            raise IngestError(
                "too_many_pages", f"PDF has {pages} pages; the limit is {MAX_PDF_PAGES}"
            )
        # Check the text layer directly so image-only PDFs never reach the
        # converter (which could otherwise try OCR when it is installed).
        chars = sum(len("".join(page.get_text().split())) for page in doc)
        if pages == 0 or chars < MIN_PDF_CHARS or chars < MIN_PDF_CHARS_PER_PAGE * pages:
            raise IngestError("no_text_layer", "PDF has no text layer; OCR is not supported")
        # OCR is explicitly off: scanned PDFs are rejected, not silently processed.
        markdown = pymupdf4llm.to_markdown(doc, use_ocr=False)
        meta_title = ((doc.metadata or {}).get("title") or "").strip()
    cleaned = clean_markdown(markdown)
    if not cleaned:
        raise IngestError("no_text_layer", "PDF has no text layer; OCR is not supported")
    heading = _first_heading(cleaned)
    title = meta_title or (_strip_inline(heading) if heading else None) or _title_from_url(url) or "Untitled"
    return Converted(title=title, markdown=cleaned, tags=[], source_type="pdf")


def _parse_tags(value: object) -> list[str]:
    """Normalize a front matter `tags` value (list or comma string) to strings."""
    if isinstance(value, str):
        items: list[object] = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    tags: list[str] = []
    for item in items:
        tag = str(item).strip()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def _markdown_to_markdown(data: bytes | str, url: str | None) -> Converted:
    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise IngestError("bad_encoding", "Markdown must be UTF-8") from exc
    else:
        text = data
    text = text.lstrip("﻿")
    title: str | None = None
    tags: list[str] = []
    match = _FRONT_MATTER_RE.match(text)
    if match:
        try:
            meta = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            meta = None
        if isinstance(meta, dict):
            raw_title = meta.get("title")
            title = str(raw_title).strip() if raw_title not in (None, "") else None
            tags = _parse_tags(meta.get("tags"))
            text = text[match.end():]
    cleaned = clean_markdown(text)
    if not cleaned:
        raise IngestError("empty_content", "the Markdown document is empty")
    h1 = _first_h1(cleaned)
    title = title or (_strip_inline(h1) if h1 else None) or _title_from_url(url) or "Untitled"
    return Converted(title=title, markdown=cleaned, tags=tags, source_type="markdown")


def to_markdown(data: bytes | str, fmt: str, *, url: str | None = None) -> Converted:
    """Convert `data` of format `fmt` into cleaned Markdown plus metadata.

    `fmt` is `html`, `pdf` or `markdown`. `url` is a hint for trafilatura and a
    last-resort title source. Raises `IngestError` (`empty_content`,
    `too_many_pages`, `no_text_layer`, `bad_pdf`, `bad_encoding`, `bad_format`).
    """
    if fmt == "html":
        return _html_to_markdown(data, url)
    if fmt == "pdf":
        return _pdf_to_markdown(data, url)
    if fmt == "markdown":
        return _markdown_to_markdown(data, url)
    raise IngestError("bad_format", f"unknown format {fmt!r}")
