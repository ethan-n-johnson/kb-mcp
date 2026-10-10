"""`kb.evaluate` and `python -m kb.cli eval`, with the fake models.

The fakes say nothing about bge's real scores, so these tests check the
mechanics (ranks, summary numbers, suggestions, acceptance checks, that the
real database is left alone), not tuned values.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kb import cli, db, service
from kb.config import Settings
from kb.evaluate import (
    DupReport,
    EvalReport,
    Question,
    QuestionResult,
    QuestionsError,
    evaluate,
    load_questions,
    render,
)
from kb.schemas import UploadRequest
from tests.corpus import EBS, ELB, S3, VPC
from tests.fakes import FakeEmbedder, FakeReranker

URLS = {
    "ebs": "https://docs.example.com/ebs.html",
    "s3": "https://docs.example.com/s3.html",
    "elb": "https://docs.example.com/elb.html",
    "vpc": "https://docs.example.com/vpc.html",
}
EBS_COPY = EBS.replace(
    "Amazon Elastic Block Store provides block storage volumes for use with EC2 instances.",
    "Our notes: EBS gives EC2 instances block storage volumes.",
)


@pytest.fixture
def seeded(db_path: Path) -> Path:
    conn = db.connect(db_path)
    db.init_schema(conn)
    try:
        for key, md in (("ebs", EBS), ("s3", S3), ("elb", ELB), ("vpc", VPC)):
            service.upload(conn, UploadRequest(content=md, source_url=URLS[key]), "test")
    finally:
        conn.close()
    return db_path


def _settings(path: Path, **extra: object) -> Settings:
    return Settings(db_path=str(path), conf_high=0.5, conf_low=0.2, dup_threshold=0.9, **extra)  # type: ignore[arg-type]


QUESTIONS = [
    Question("attach an EBS volume to a Linux instance mount", "answerable", URLS["ebs"], demo=True),
    Question("S3 lifecycle rules storage classes Glacier", "answerable", URLS["s3"]),
    Question("sourdough bread recipe", "outside"),
]


def test_eval_ranks_scores_and_leaves_the_database_alone(seeded: Path) -> None:
    report = evaluate(_settings(seeded), QUESTIONS, embedder=FakeEmbedder(), reranker=FakeReranker())
    ranks = [r.rank for r in report.results]
    assert ranks[:2] == [1, 1] and ranks[2] is None
    assert report.hit_at(1) == 1.0 and report.hit_at(5) == 1.0
    assert all(r.mode == "semantic" for r in report.results)
    conn = db.connect(seeded)
    try:
        # The out-of-corpus question logged its gap in the temporary copy only.
        assert conn.execute("SELECT count(*) FROM gaps").fetchone()[0] == 0
    finally:
        conn.close()
    text = render(report)
    assert "hit@1: 100%" in text and "suggested KB_CONF_HIGH=" in text


def test_eval_measures_the_planted_near_duplicate(seeded: Path, tmp_path: Path) -> None:
    planted = tmp_path / "copy.md"
    planted.write_text(EBS_COPY, encoding="utf-8")
    report = evaluate(
        _settings(seeded), QUESTIONS, planted=planted, planted_original=URLS["ebs"],
        embedder=FakeEmbedder(), reranker=FakeReranker(),
    )
    d = report.dup
    assert d is not None and d.original_similarity is not None and d.max_distinct is not None
    assert d.top_candidate_url == URLS["ebs"]
    assert d.original_similarity > d.max_distinct
    suggested = report.suggested_dup()
    assert suggested is not None and d.max_distinct < suggested < d.original_similarity
    assert "similarity to its original" in render(report)


def test_eval_without_a_database_or_models_is_an_error(tmp_path: Path, seeded: Path) -> None:
    with pytest.raises(RuntimeError, match="does not exist"):
        evaluate(_settings(tmp_path / "none.db"), QUESTIONS, embedder=FakeEmbedder())
    unhealthy = FakeEmbedder()
    unhealthy.fail = True
    with pytest.raises(RuntimeError, match="keyword only"):
        evaluate(_settings(seeded), QUESTIONS, embedder=unhealthy)


def _result(q: Question, rank: int | None, score: float, conf: str) -> QuestionResult:
    return QuestionResult(q, rank, score, None, conf, "semantic")


def test_suggestions_sit_between_out_of_corpus_and_answerable_scores() -> None:
    a = Question("a", "answerable", "u", demo=True)
    b = Question("b", "answerable", "u")
    o = Question("o", "outside")
    report = EvalReport(
        results=[_result(a, 1, 0.80, "high"), _result(b, 1, 0.95, "high"), _result(o, None, 0.10, "low")],
        settings=Settings(),
    )
    low, high = report.suggested_low(), report.suggested_high()
    assert low == 0.45  # midway between 0.10 and 0.80
    assert high is not None and low < high <= 0.80  # never above a demo question
    assert report.failures() == []


def test_overlapping_scores_give_no_low_suggestion_and_failures_are_listed() -> None:
    a = Question("a", "answerable", "u", demo=True)
    o = Question("o", "outside")
    report = EvalReport(
        results=[_result(a, 2, 0.30, "medium"), _result(o, None, 0.60, "medium")],
        settings=Settings(dup_threshold=0.9),
        dup=DupReport("t", 0.85, "u", 0.85, 0.93, ("x", "y")),
    )
    assert report.suggested_low() is None and report.suggested_dup() is None
    problems = "\n".join(report.failures())
    assert "hit@5 is 0%" not in problems  # rank 2 is a hit at 5
    assert "out-of-corpus question is medium" in problems
    assert "demo question is medium with the doc at rank 2" in problems
    assert "below KB_DUP_THRESHOLD" in problems and "two distinct docs" in problems
    assert "acceptance: FAIL" in render(report)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("questions: [{kind: answerable, expect: u}]", "non-empty 'q'"),
        ("questions: [{q: x, kind: other}]", "kind must be"),
        ("questions: [{q: x, kind: answerable}]", "needs 'expect'"),
        ("questions: [{q: x, kind: outside, expect: u}]", "no 'expect'"),
        ("questions: [{q: x, kind: outside}]\nnear_duplicate: {path: missing.md, original: u}", "does not exist"),
        ("questions: [{q: x, kind: outside}]\nnear_duplicate: {path: a.md}", "needs 'path' and 'original'"),
        ("nothing: 1", "'questions' list"),
    ],
)
def test_bad_question_files_name_the_problem(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "q.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(QuestionsError, match=message):
        load_questions(path)


def test_cli_eval_prints_the_report_and_exits_by_acceptance(
    seeded: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    questions = tmp_path / "q.yaml"
    questions.write_text(
        "questions:\n"
        f"  - {{q: attach an EBS volume to a Linux instance mount, kind: answerable, expect: '{URLS['ebs']}'}}\n"
        "  - {q: sourdough bread recipe, kind: outside}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KB_DB", str(seeded))
    monkeypatch.setenv("KB_FAKE_MODELS", "1")
    monkeypatch.setenv("KB_CONF_HIGH", "0.5")
    monkeypatch.setenv("KB_CONF_LOW", "0.2")
    code = cli.main(["eval", str(questions)])
    out = capsys.readouterr().out
    assert "hit@1:" in out and "acceptance:" in out
    assert code == (0 if "acceptance: PASS" in out else 1)


def test_cli_eval_bad_file_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "q.yaml"
    path.write_text("nope: 1", encoding="utf-8")
    assert cli.main(["eval", str(path)]) == 2
    assert "questions" in capsys.readouterr().err


def test_inspect_lists_hits_without_touching_the_database(seeded: Path) -> None:
    from kb.evaluate import inspect

    text = inspect(_settings(seeded), ["attach an EBS volume", "sourdough bread recipe"], 3,
                   embedder=FakeEmbedder(), reranker=FakeReranker())
    assert "attach an EBS volume  [semantic," in text and URLS["ebs"] in text
    conn = db.connect(seeded)
    try:
        assert conn.execute("SELECT count(*) FROM gaps").fetchone()[0] == 0
    finally:
        conn.close()


def test_cli_search_needs_a_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("KB_DB", str(tmp_path / "none.db"))
    assert cli.main(["search", "anything"]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_a_question_can_accept_several_right_documents(seeded: Path, tmp_path: Path) -> None:
    path = tmp_path / "q.yaml"
    path.write_text(
        "questions:\n"
        "  - q: attach an EBS volume to a Linux instance mount\n"
        "    kind: answerable\n"
        f"    expect: ['{URLS['vpc']}', '{URLS['ebs']}']\n",
        encoding="utf-8",
    )
    qset = load_questions(path)
    assert qset.questions[0].expect == (URLS["vpc"], URLS["ebs"])
    report = evaluate(_settings(seeded), qset.questions, embedder=FakeEmbedder(), reranker=FakeReranker())
    assert report.results[0].rank == 1
