"""Heading-based chunking: paths, fences, merge and split rules."""

from __future__ import annotations

from pathlib import Path

from kb.ingest.chunk import MAX_WORDS, MIN_WORDS, chunk_markdown
from kb.ingest.convert import to_markdown

FIXTURES = Path(__file__).parent / "fixtures"


def words(n: int, tag: str = "w") -> str:
    """A paragraph of exactly `n` distinct words."""
    return " ".join(f"{tag}{i}" for i in range(n))


def paragraphs(count: int, size: int, tag: str = "p") -> str:
    return "\n\n".join(words(size, f"{tag}{i}_") for i in range(count))


def test_empty_input_gives_no_chunks() -> None:
    assert chunk_markdown("", "Doc") == []
    assert chunk_markdown("\n\n  \n", "Doc") == []


def test_heading_paths_follow_h1_h2_h3() -> None:
    md = "\n\n".join(
        [
            "# EBS",
            words(60, "a"),
            "## Attach a volume",
            words(60, "b"),
            "### Linux",
            words(60, "c"),
            "### Windows",
            words(60, "d"),
            "## Detach a volume",
            words(60, "e"),
            "# S3",
            words(60, "f"),
        ]
    )
    chunks = chunk_markdown(md, "Storage")
    assert [c.heading_path for c in chunks] == [
        "EBS",
        "EBS > Attach a volume",
        "EBS > Attach a volume > Linux",
        "EBS > Attach a volume > Windows",
        "EBS > Detach a volume",
        "S3",
    ]
    assert [c.ord for c in chunks] == list(range(6))
    assert all(c.word_count == len(c.text.split()) for c in chunks)


def test_text_before_first_heading_uses_doc_title() -> None:
    md = f"{words(70, 'intro')}\n\n## First\n\n{words(70, 'body')}"
    chunks = chunk_markdown(md, "My Document")
    assert chunks[0].heading_path == "My Document"
    assert chunks[0].text.startswith("intro0")
    assert chunks[1].heading_path == "First"


def test_h2_without_h1_and_h4_stays_in_body() -> None:
    md = f"## Only H2\n\n{words(30, 'x')}\n\n#### Deep\n\n{words(30, 'y')}"
    chunks = chunk_markdown(md, "Doc")
    assert len(chunks) == 1
    assert chunks[0].heading_path == "Only H2"
    assert "#### Deep" in chunks[0].text


def test_hash_lines_in_fenced_code_are_not_headings() -> None:
    md = (
        f"# Real\n\n{words(60, 'a')}\n\n```bash\n# not a heading\n## also not\n```\n\n"
        f"~~~\n# tilde fence\n~~~\n\n{words(10, 'b')}"
    )
    chunks = chunk_markdown(md, "Doc")
    assert [c.heading_path for c in chunks] == ["Real"]
    assert "# not a heading" in chunks[0].text
    assert "# tilde fence" in chunks[0].text


def test_longer_closing_fence_and_unclosed_fence() -> None:
    md = f"# A\n\n{words(60)}\n\n````\n```\n# inner\n````\n\n## B\n\n{words(60, 'q')}\n\n```\n## never closed"
    chunks = chunk_markdown(md, "Doc")
    assert [c.heading_path for c in chunks] == ["A", "A > B"]
    assert "## never closed" in chunks[1].text


def test_small_section_merges_into_next() -> None:
    md = f"# Big one\n\n{words(80, 'a')}\n\n## Tiny\n\n{words(10, 't')}\n\n## Big two\n\n{words(80, 'b')}"
    chunks = chunk_markdown(md, "Doc")
    assert [c.heading_path for c in chunks] == ["Big one", "Big one > Big two"]
    merged = chunks[1].text
    assert "## Tiny" in merged and "t0" in merged  # the merged section keeps its heading text
    assert merged.index("t0") < merged.index("b0")
    assert chunks[1].word_count > 80


def test_small_sections_cascade_forward() -> None:
    md = f"# One\n\n{words(10, 'a')}\n\n# Two\n\n{words(10, 'b')}\n\n# Three\n\n{words(60, 'c')}"
    chunks = chunk_markdown(md, "Doc")
    assert [c.heading_path for c in chunks] == ["Three"]
    assert all(token in chunks[0].text for token in ("a0", "b0", "c0", "# One", "# Two"))


def test_last_small_section_merges_into_previous() -> None:
    md = f"# Main\n\n{words(80, 'a')}\n\n## Footnote\n\n{words(8, 'z')}"
    chunks = chunk_markdown(md, "Doc")
    assert [c.heading_path for c in chunks] == ["Main"]
    assert "## Footnote" in chunks[0].text and "z0" in chunks[0].text


def test_document_made_only_of_small_sections_is_one_chunk() -> None:
    md = f"# A\n\n{words(10, 'a')}\n\n## B\n\n{words(10, 'b')}"
    chunks = chunk_markdown(md, "Doc")
    assert len(chunks) == 1 and chunks[0].ord == 0
    assert chunks[0].heading_path == "A"
    assert "a0" in chunks[0].text and "b0" in chunks[0].text


def test_heading_only_document_keeps_its_heading_text() -> None:
    chunks = chunk_markdown("# Lonely heading", "Doc")
    assert len(chunks) == 1
    assert chunks[0].text == "# Lonely heading"


def test_section_at_exact_thresholds() -> None:
    at_min = f"# A\n\n{words(MIN_WORDS, 'a')}\n\n# B\n\n{words(MIN_WORDS, 'b')}"
    assert len(chunk_markdown(at_min, "Doc")) == 2  # exactly 50 words is not "under 50"
    at_max = f"# A\n\n{words(MAX_WORDS, 'a')}"
    assert len(chunk_markdown(at_max, "Doc")) == 1  # exactly 350 words is not "over 350"


def test_long_section_splits_at_paragraph_breaks_with_same_path() -> None:
    md = f"# Long\n\n{paragraphs(8, 100)}"  # 800 words
    chunks = chunk_markdown(md, "Doc")
    assert len(chunks) == 3
    assert {c.heading_path for c in chunks} == {"Long"}
    assert all(c.word_count <= MAX_WORDS for c in chunks)
    assert sum(c.word_count for c in chunks) == 800
    assert [c.ord for c in chunks] == [0, 1, 2]
    for chunk in chunks:  # every piece starts and ends on a paragraph boundary
        first = chunk.text.split("\n\n")[0]
        assert len(first.split()) == 100


def test_long_section_never_splits_inside_a_code_fence() -> None:
    code_lines = [f"line_{i} = {i}" for i in range(60)]
    code_lines[20] = ""  # blank line inside the fence
    code_lines[40] = ""
    fence = "```python\n" + "\n".join(code_lines) + "\n```"
    md = f"# Code\n\n{paragraphs(2, 150, 'a')}\n\n{fence}\n\n{paragraphs(2, 150, 'b')}"
    chunks = chunk_markdown(md, "Doc")
    assert len(chunks) >= 2
    holders = [c for c in chunks if "line_0 = 0" in c.text]
    assert len(holders) == 1
    assert "line_59 = 59" in holders[0].text
    for chunk in chunks:
        assert chunk.text.count("```") % 2 == 0


def test_oversized_single_paragraph_is_kept_whole() -> None:
    md = f"# Wall\n\n{words(500)}"
    chunks = chunk_markdown(md, "Doc")
    assert len(chunks) == 1 and chunks[0].word_count == 500


def test_merging_happens_before_splitting() -> None:
    md = f"# Tiny\n\n{words(20, 't')}\n\n# Big\n\n{paragraphs(4, 100)}"  # 20 + 400 words
    chunks = chunk_markdown(md, "Doc")
    assert {c.heading_path for c in chunks} == {"Big"}
    assert all(c.word_count <= MAX_WORDS for c in chunks)


def test_frontmatter_fixture_ignores_fenced_hash() -> None:
    converted = to_markdown((FIXTURES / "frontmatter.md").read_bytes(), "markdown")
    chunks = chunk_markdown(converted.markdown, converted.title)
    assert chunks
    assert not any("not a heading" in c.heading_path for c in chunks)
    assert any("# not a heading" in c.text for c in chunks)
    assert [c.ord for c in chunks] == list(range(len(chunks)))


def test_html_fixture_chunks_have_real_heading_paths() -> None:
    converted = to_markdown((FIXTURES / "aws_like.html").read_bytes(), "html")
    chunks = chunk_markdown(converted.markdown, converted.title)
    paths = [c.heading_path for c in chunks]
    assert paths[1].endswith("> Attach a volume using the console")
    assert paths[2].endswith("> Volume types and limits")
    assert any("| gp3 |" in c.text for c in chunks)
