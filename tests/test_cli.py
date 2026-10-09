"""The `kb.cli` entry point: argparse wiring, `convert` and `check-models`."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from kb import cli
from tests.pdfs import make_text_pdf

FIXTURES = Path(__file__).parent / "fixtures"


def test_help_lists_both_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "convert" in out and "check-models" in out


@pytest.mark.parametrize("command", ["convert", "check-models"])
def test_subcommand_help(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main([command, "--help"])
    assert exit_info.value.code == 0
    assert command in capsys.readouterr().out


def test_module_runs_with_dash_m() -> None:
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "kb.cli", "--help"], cwd=root, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0
    assert "check-models" in result.stdout


def test_missing_command_is_an_error() -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])
    assert exit_info.value.code == 2


def test_convert_prints_title_tags_and_chunks(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["convert", str(FIXTURES / "frontmatter.md")])
    out = capsys.readouterr().out
    assert code == 0
    assert "title:  VPC Peering Notes" in out
    assert "tags:   vpc, networking" in out
    assert "(" in out and "words)" in out
    assert "[0] " in out


def test_convert_html_file_shows_heading_paths(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["convert", str(FIXTURES / "aws_like.html")]) == 0
    out = capsys.readouterr().out
    assert "> Volume types and limits" in out
    assert "Sidebar link" not in out


def test_convert_format_override_and_preview_length(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "notes.dat"
    path.write_text("# Title\n\n" + "word " * 100, encoding="utf-8")
    assert cli.main(["convert", str(path), "--format", "markdown"]) == 0
    preview_lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("    ")]
    assert preview_lines and all(len(ln.strip()) <= 80 for ln in preview_lines)


def test_convert_pdf_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "elb.pdf"
    path.write_bytes(make_text_pdf())
    assert cli.main(["convert", str(path)]) == 0
    assert "Elastic Load Balancing" in capsys.readouterr().out


def test_convert_blocked_address_exits_nonzero_before_any_network(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["convert", "http://169.254.169.254/latest/meta-data/"])
    captured = capsys.readouterr()
    assert code == 1
    assert "blocked_address" in captured.err
    assert captured.out == ""


def test_convert_reports_ingest_errors_and_missing_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["convert", str(tmp_path / "missing.md")]) == 1
    assert "cannot read" in capsys.readouterr().err

    empty = tmp_path / "empty.md"
    empty.write_text("\n", encoding="utf-8")
    assert cli.main(["convert", str(empty)]) == 1
    assert "empty_content" in capsys.readouterr().err

    assert cli.main(["convert", "ftp://example.com/x"]) == 1
    assert "bad_scheme" in capsys.readouterr().err


# --- check-models, with stand-ins for fastembed -------------------------------


class _StubEmbedding:
    dim = 384

    def __init__(self, name: str, cache_dir: str | None = None) -> None:
        assert name == cli.EMBED_MODEL
        self.cache_dir = cache_dir

    def embed(self, texts):
        vec = np.zeros(self.dim, dtype=np.float32)
        vec[0] = 1.0
        return iter([vec for _ in texts])


class _StubCrossEncoder:
    relevant_first = True

    def __init__(self, name: str, cache_dir: str | None = None) -> None:
        assert name == cli.RERANK_MODEL

    def rerank(self, query, documents):
        scores = [4.0, -6.0]
        return iter(scores if self.relevant_first else scores[::-1])


def _install_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    import fastembed
    from fastembed.rerank import cross_encoder

    monkeypatch.setattr(fastembed, "TextEmbedding", _StubEmbedding)
    monkeypatch.setattr(cross_encoder, "TextCrossEncoder", _StubCrossEncoder)


def test_check_models_success_with_stubs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_stubs(monkeypatch)
    monkeypatch.setenv("KB_MODEL_DIR", "/tmp/does-not-matter")
    assert cli.main(["check-models"]) == 0
    out = capsys.readouterr().out
    assert "dim:  384" in out and "norm: 1.0000" in out
    assert "sigmoid=0.98" in out  # sigmoid(4.0)
    assert "OK" in out


def test_check_models_fails_when_ranking_is_wrong(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_stubs(monkeypatch)
    monkeypatch.setattr(_StubCrossEncoder, "relevant_first", False)
    assert cli.main(["check-models"]) == 1
    assert "FAIL" in capsys.readouterr().err


def test_check_models_fails_on_wrong_dimension(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_stubs(monkeypatch)
    monkeypatch.setattr(_StubEmbedding, "dim", 768)
    assert cli.main(["check-models"]) == 1
    assert "384" in capsys.readouterr().err


def test_check_models_fails_when_a_model_cannot_load(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import fastembed

    def broken(*args, **kwargs):
        raise RuntimeError("model files missing")

    monkeypatch.setattr(fastembed, "TextEmbedding", broken)
    assert cli.main(["check-models"]) == 1
    assert "model files missing" in capsys.readouterr().err


@pytest.mark.models
def test_check_models_with_real_models(capsys: pytest.CaptureFixture[str]) -> None:
    """Needs the real ONNX models in KB_MODEL_DIR (run with `pytest -m models`)."""
    assert cli.main(["check-models"]) == 0
    assert "OK" in capsys.readouterr().out
