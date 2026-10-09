"""Markdown building blocks of `merge`: heading demotion and section layout."""

from __future__ import annotations

from kb.ingest.mdmerge import MergeSource, demote_headings, merge_markdown


def test_every_heading_moves_one_level_deeper() -> None:
    assert demote_headings("# One\n\ntext\n\n## Two\n\n### Three\n") == "## One\n\ntext\n\n### Two\n\n#### Three\n"


def test_demotion_is_capped_at_h6() -> None:
    assert demote_headings("##### Five\n###### Six\n") == "###### Five\n###### Six\n"


def test_fenced_code_is_left_alone() -> None:
    md = "# Title\n\n```bash\n# a comment, not a heading\n## also a comment\n```\n\n~~~\n# tilde fence\n~~~\n\n## After\n"
    out = demote_headings(md)
    assert "```bash\n# a comment, not a heading\n## also a comment\n```" in out
    assert "~~~\n# tilde fence\n~~~" in out
    assert out.startswith("## Title") and out.rstrip().endswith("### After")


def test_only_real_atx_headings_change() -> None:
    md = "#hashtag stays\n    # indented code stays\nSetext\n======\n#\n####### seven is not a heading\n  ## indented two spaces\n"
    out = demote_headings(md).split("\n")
    assert out[0] == "#hashtag stays"
    assert out[1] == "    # indented code stays"
    assert out[2:4] == ["Setext", "======"]
    assert out[4] == "##"  # an empty ATX heading still demotes
    assert out[5] == "####### seven is not a heading"
    assert out[6] == "  ### indented two spaces"


def test_merge_layout_with_source_urls() -> None:
    out = merge_markdown(
        "# Target\n\nTarget body.\n",
        [
            MergeSource("Source A", "https://a.example/doc", "# Source A\n\nA body.\n\n## Part\n\nPart body.\n"),
            MergeSource("Source B", None, "B body without any heading.\n"),
        ],
    )
    assert out == (
        "# Target\n\nTarget body.\n\n"
        "## Source A\n\nSource: https://a.example/doc\n\nA body.\n\n### Part\n\nPart body.\n\n"
        "## Source B\n\nB body without any heading.\n"
    )


def test_a_leading_h1_that_repeats_the_title_is_dropped_but_other_h1s_demote() -> None:
    out = merge_markdown(
        "# T\n\nbody\n",
        [MergeSource("Guide", None, "# Guide\n\nintro\n\n# Appendix\n\nmore\n")],
    )
    assert out.count("Guide") == 1  # the section heading carries the title once
    assert "## Appendix" in out  # a second H1 is demoted like any heading
    other = merge_markdown("# T\n", [MergeSource("Guide", None, "# Different heading\n\ntext\n")])
    assert "## Different heading" in other  # an H1 that is not the title is kept, demoted


def test_title_is_kept_on_one_line_and_empty_bodies_are_fine() -> None:
    out = merge_markdown("# T\n", [MergeSource("Two\nlines", "https://x.example", "")])
    assert out == "# T\n\n## Two lines\n\nSource: https://x.example\n"


def test_sources_keep_their_order() -> None:
    out = merge_markdown("# T\n", [MergeSource(n, None, "x\n") for n in ("one", "two", "three")])
    assert out.index("## one") < out.index("## two") < out.index("## three")
