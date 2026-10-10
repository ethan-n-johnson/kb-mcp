"""Format detection and HTML, PDF and Markdown conversion."""

from __future__ import annotations

from pathlib import Path

import pytest

from kb.ingest.convert import BOILERPLATE_PATTERNS, clean_markdown, detect_format, to_markdown
from kb.ingest.errors import IngestError
from tests.pdfs import make_blank_pdf, make_image_only_pdf, make_text_pdf

FIXTURES = Path(__file__).parent / "fixtures"


# --- detect_format -----------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"declared": "pdf", "filename": "a.html", "content_type": "text/html"}, "pdf"),
        ({"declared": "MD"}, "markdown"),
        ({"declared": "html", "url": "https://x.example/a.md"}, "html"),
        ({"filename": "notes.md", "content_type": "application/pdf"}, "markdown"),
        ({"filename": "guide.PDF"}, "pdf"),
        ({"filename": "page.htm"}, "html"),
        ({"url": "https://raw.example/readme.md"}, "markdown"),
        ({"url": "https://raw.example/readme.md?raw=1"}, "markdown"),
        ({"url": "https://x.example/guide.pdf", "content_type": "text/html"}, "pdf"),
        ({"content_type": "text/markdown; charset=utf-8"}, "markdown"),
        ({"content_type": "text/plain"}, "markdown"),
        ({"content_type": "application/pdf"}, "pdf"),
        ({"content_type": "text/html; charset=UTF-8"}, "html"),
        ({"url": "https://docs.example/ec2/latest/ug/ebs.html"}, "html"),
        ({"url": "https://docs.example/ec2/"}, "html"),
        ({}, "html"),
    ],
)
def test_detect_format(kwargs: dict, expected: str) -> None:
    assert detect_format(**kwargs) == expected


def test_detect_format_rejects_unknown_declared() -> None:
    with pytest.raises(IngestError) as err:
        detect_format(declared="docx")
    assert err.value.code == "bad_format"


# --- clean_markdown ------------------------------------------------------------


def test_clean_collapses_blank_runs_and_trims() -> None:
    assert clean_markdown("\n\nA\n\n\n\n\nB   \n\n\n") == "A\n\nB"


def test_clean_drops_empty_headings_only() -> None:
    cleaned = clean_markdown("# Real\n\n##\n\n### \n\ntext\n\n#### Kept\n")
    assert cleaned == "# Real\n\ntext\n\n#### Kept"


@pytest.mark.parametrize(
    "line",
    [
        "Skip to content",
        "Skip to main content",
        "[Skip to main content](#main)",
        "Edit this page",
        "[Edit this page on GitHub](https://example.com/edit)",
        "Was this page helpful?",
        "## Table of contents",
        "**Table of Contents**",
        "We use cookies to improve your experience.",
        "Accept all cookies",
        "Cookie preferences",
    ],
)
def test_clean_drops_boilerplate_lines(line: str) -> None:
    assert clean_markdown(f"intro\n\n{line}\n\nbody") == "intro\n\nbody"


def test_clean_keeps_prose_that_merely_mentions_boilerplate_words() -> None:
    text = "Use the table of contents in the console to skip to content you need."
    assert clean_markdown(text) == text


def test_clean_leaves_code_fences_untouched() -> None:
    md = "intro\n\n```text\nSkip to content\n\n\n\nEdit this page\n##\n```\n\nafter"
    assert clean_markdown(md) == md


def test_clean_unwraps_bold_headings() -> None:
    assert clean_markdown("## **Health checks**\n\ntext") == "## Health checks\n\ntext"


def test_boilerplate_patterns_are_one_constant() -> None:
    assert isinstance(BOILERPLATE_PATTERNS, tuple) and len(BOILERPLATE_PATTERNS) >= 5


# --- HTML ----------------------------------------------------------------------


def test_html_fixture_drops_boilerplate_and_keeps_table() -> None:
    html = (FIXTURES / "aws_like.html").read_bytes()
    result = to_markdown(html, "html", url="https://docs.example.com/ec2/latest/ug/ebs-attaching-volume.html")
    md = result.markdown

    assert result.source_type == "html"
    assert result.tags == []
    assert "Attach an Amazon EBS volume" in result.title

    for noise in ("cookies", "Cookie", "Sidebar link", "Sign in to the console", "Pricing", "Footer", "Legal notices"):
        assert noise not in md, noise

    assert "## Attach a volume using the console" in md
    assert "## Volume types and limits" in md
    assert "| gp3 | 16000 | 16 TiB |" in md
    assert "| io2 | 64000 | 64 TiB |" in md
    assert "Multi-Attach" in md


def test_html_accepts_str_input() -> None:
    html = (FIXTURES / "aws_like.html").read_text(encoding="utf-8")
    assert "## Volume types and limits" in to_markdown(html, "html").markdown


@pytest.mark.parametrize(
    "html",
    [
        "<html><body></body></html>",
        "<html><body><nav><a href='/x'>x</a></nav></body></html>",
        "<html><body><nav><a href='/x'>Home</a></nav><footer>Contact us</footer></body></html>",
    ],
)
def test_html_without_content_raises_empty_content(html: str) -> None:
    with pytest.raises(IngestError) as err:
        to_markdown(html, "html")
    assert err.value.code == "empty_content"


def test_html_title_falls_back_to_url_path() -> None:
    para = "Security groups act as a virtual firewall for instances and control inbound and outbound traffic. " * 6
    html = f"<html><body><article><p>{para}</p></article></body></html>"
    result = to_markdown(html, "html", url="https://docs.example.com/vpc/security-groups.html")
    assert result.title == "security groups"


# --- PDF -----------------------------------------------------------------------


def test_pdf_text_layer_converts_with_headings() -> None:
    result = to_markdown(make_text_pdf(), "pdf")
    assert result.source_type == "pdf"
    assert result.title == "Elastic Load Balancing"  # first heading, no metadata title
    assert "# Elastic Load Balancing" in result.markdown
    assert "## Health checks" in result.markdown
    assert "Application Load Balancer" in result.markdown
    assert "**" not in result.markdown.split("\n\n")[0]  # heading is unwrapped


def test_pdf_metadata_title_wins() -> None:
    result = to_markdown(make_text_pdf(metadata_title="ELB Handbook"), "pdf")
    assert result.title == "ELB Handbook"


def test_image_only_pdf_has_no_text_layer() -> None:
    with pytest.raises(IngestError) as err:
        to_markdown(make_image_only_pdf(), "pdf")
    assert err.value.code == "no_text_layer"
    assert "OCR is not supported" in err.value.message


def test_pdf_over_300_pages_is_rejected() -> None:
    with pytest.raises(IngestError) as err:
        to_markdown(make_blank_pdf(301), "pdf")
    assert err.value.code == "too_many_pages"


def test_pdf_with_300_blank_pages_passes_page_check_but_has_no_text() -> None:
    with pytest.raises(IngestError) as err:
        to_markdown(make_blank_pdf(300), "pdf")
    assert err.value.code == "no_text_layer"


def test_garbage_pdf_bytes_are_rejected() -> None:
    with pytest.raises(IngestError) as err:
        to_markdown(b"this is not a pdf", "pdf")
    assert err.value.code == "bad_pdf"


# --- Markdown ------------------------------------------------------------------


def test_markdown_front_matter_fills_title_and_tags_and_is_stripped() -> None:
    result = to_markdown((FIXTURES / "frontmatter.md").read_bytes(), "markdown")
    assert result.title == "VPC Peering Notes"
    assert result.tags == ["vpc", "networking"]
    assert result.source_type == "markdown"
    assert "title: VPC Peering Notes" not in result.markdown
    assert not result.markdown.startswith("---")
    assert result.markdown.startswith("# VPC Peering Notes")


def test_markdown_keeps_fenced_hash_line() -> None:
    result = to_markdown((FIXTURES / "frontmatter.md").read_text(encoding="utf-8"), "markdown")
    assert "# not a heading" in result.markdown


def test_markdown_comma_separated_tags_and_front_matter_only_title() -> None:
    md = "---\ntitle: Notes\ntags: ec2, ebs ,  ec2\n---\nbody text here\n"
    result = to_markdown(md, "markdown")
    assert (result.title, result.tags) == ("Notes", ["ec2", "ebs"])
    assert result.markdown == "body text here"


def test_markdown_without_front_matter_uses_h1_then_url() -> None:
    assert to_markdown("# My Heading\n\ntext", "markdown").title == "My Heading"
    result = to_markdown("just text", "markdown", url="https://raw.example/dir/ebs_notes.md")
    assert result.title == "ebs notes"
    assert to_markdown("just text", "markdown").title == "Untitled"


def test_markdown_invalid_front_matter_is_left_in_place() -> None:
    md = "---\ntitle: [unclosed\n---\n# Heading\n\ntext"
    result = to_markdown(md, "markdown")
    assert result.title == "Heading"
    assert result.tags == []


def test_markdown_h1_inside_code_fence_is_not_the_title() -> None:
    md = "```\n# fake\n```\n\n# Real Title\n"
    assert to_markdown(md, "markdown").title == "Real Title"


def test_markdown_rejects_non_utf8_and_empty() -> None:
    with pytest.raises(IngestError) as err:
        to_markdown(b"\xff\xfe\x00bad", "markdown")
    assert err.value.code == "bad_encoding"
    with pytest.raises(IngestError) as err2:
        to_markdown("---\ntitle: x\n---\n\n", "markdown")
    assert err2.value.code == "empty_content"


def test_unknown_format_is_rejected() -> None:
    with pytest.raises(IngestError) as err:
        to_markdown("x", "docx")
    assert err.value.code == "bad_format"


# --- mkdocs syntax from the FastAPI docs (phase 11) -----------------------------------


def test_mkdocs_include_lines_are_dropped() -> None:
    md = "# T\n\nIntro text.\n\n{* ../../docs_src/first_steps/tutorial001_py310.py hl[1] *}\n\nMore text.\n"
    assert "docs_src" not in clean_markdown(md)
    assert "Intro text.\n\nMore text." in clean_markdown(md)


def test_heading_anchors_are_stripped() -> None:
    out = clean_markdown("# Handling Errors { #handling-errors }\n\n## Use `HTTPException` { #use-httpexception }\n")
    assert out == "# Handling Errors\n\n## Use `HTTPException`"
    assert to_markdown("# First Steps { #first-steps }\n\nBody text here.\n", "markdown").title == "First Steps"


def test_admonitions_become_labelled_paragraphs() -> None:
    md = "/// tip\n\nUse a dict.\n\n///\n\n/// note | Technical Details\n\nStarlette does it.\n\n///\n"
    assert clean_markdown(md) == "**Tip:**\n\nUse a dict.\n\n**Technical Details:**\n\nStarlette does it."


def test_tab_markers_and_four_slash_fences_are_dropped() -> None:
    md = "//// tab | Python 3.10+\n\nUse Annotated.\n\n////\n"
    assert clean_markdown(md) == "Use Annotated."


def test_mkdocs_syntax_inside_code_fences_is_left_alone() -> None:
    md = "```text\n/// tip\n{* a.py *}\n## x { #y }\n```\n"
    assert clean_markdown(md) == md.strip()
