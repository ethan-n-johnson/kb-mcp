"""Safe HTML for the web UI: sanitized Markdown and FTS5 snippets.

Everything stored in the knowledge base is untrusted (anyone with a write token,
or a page fetched from the internet, can put text there), so nothing reaches a
template as raw HTML unless it went through this module.
"""

from __future__ import annotations

import html

import nh3
from markdown_it import MarkdownIt
from markupsafe import Markup

ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "a", "ul", "ol", "li", "code", "pre", "blockquote",
    "table", "thead", "tbody", "tr", "th", "td",
    "em", "strong", "hr", "br", "img",
}
ALLOWED_ATTRIBUTES = {"a": {"href"}, "img": {"src", "alt"}}
ALLOWED_SCHEMES = {"http", "https", "mailto"}

# CommonMark plus tables, raw HTML off: `<script>` in the source is shown as text.
_markdown = MarkdownIt("commonmark", {"html": False}).enable("table")


def render_markdown(source: str) -> Markup:
    """Render Markdown to sanitized HTML.

    markdown-it escapes raw HTML (disabled), then nh3 keeps only the tags and
    attributes above and the `http`, `https` and `mailto` URL schemes. Links get
    `rel="noopener noreferrer"`. Anything else (scripts, event handlers,
    `javascript:` and `data:` URLs, styles) is dropped.
    """
    cleaned = nh3.clean(
        _markdown.render(source),
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=ALLOWED_SCHEMES,
        link_rel="noopener noreferrer",
    )
    return Markup(cleaned)  # noqa: S704 - sanitized by nh3 above


def render_snippet(snippet: str) -> Markup:
    """Make a search snippet safe: escape everything, then allow only `<b>`.

    Keyword-mode snippets carry `<b>...</b>` highlights from FTS5 around text
    that may itself contain markup. All of it is escaped first; only the exact
    escaped forms of `<b>` and `</b>` are turned back into tags, and a final
    nh3 pass (allowing just `b`, no attributes) balances any unpaired tag.
    """
    escaped = html.escape(snippet, quote=True)
    restored = escaped.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
    cleaned = nh3.clean(restored, tags={"b"}, attributes={})
    return Markup(cleaned)  # noqa: S704 - escaped, then only <b> allowed
