"""Search quality evaluation for tuning (`python -m kb.cli eval`, phase 11).

Runs every question of a YAML file through the real search pipeline and
reports, per question, the rank of the expected document, the top score, the
confidence and the mode; then hit@1 and hit@5 over the answerable questions,
the top-score ranges of answerable and out-of-corpus questions, and suggested
`KB_CONF_HIGH` / `KB_CONF_LOW` values. With a planted near-duplicate file it
also reports that file's similarity to its original and the highest similarity
between two distinct stored docs, with a suggested `KB_DUP_THRESHOLD`.

The searches run against a temporary copy of the database, so the gaps that
low-confidence questions record never reach the real one. The search timeout
is raised to 60 seconds so a slow laptop CPU cannot turn a question into a
keyword search and spoil the numbers.

Questions file:

    questions:
      - q: how does an Auto Scaling group replace an unhealthy instance
        kind: answerable        # answerable, paraphrase or outside
        expect: https://...     # source_url of the doc that answers it, or a list of
                                # them when several docs answer it equally well
        demo: true              # optional: must be high with the doc at rank 1
    near_duplicate:             # optional
      path: ../../seeds/planted/fastapi-near-duplicate.md   # relative to this file
      original: https://...     # source_url of the doc it was copied from
"""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import numpy as np
import yaml

from kb import db, service
from kb.config import Settings
from kb.schemas import UploadRequest
from kb.search import dupes, indexing, pipeline
from kb.search.state import EmbedderLike, RerankerLike, SearchState

KINDS = ("answerable", "paraphrase", "outside")
EVAL_TIMEOUT = 60.0
TOP_K = 5


class QuestionsError(ValueError):
    """The questions file is missing, not YAML, or has an invalid question."""


@dataclass(frozen=True)
class Question:
    q: str
    kind: str
    expect: tuple[str, ...] = ()  # source_urls that count as the right answer

    def __post_init__(self) -> None:
        if isinstance(self.expect, str):  # a single URL is accepted for convenience
            object.__setattr__(self, "expect", (self.expect,))
    demo: bool = False


@dataclass(frozen=True)
class QuestionSet:
    questions: list[Question]
    planted: Path | None = None
    planted_original: str | None = None


@dataclass(frozen=True)
class QuestionResult:
    question: Question
    rank: int | None  # 1-based rank of the expected doc in the top 5, None if absent
    top_score: float | None
    top_url: str | None
    confidence: str
    mode: str
    latency_ms: float = 0.0


@dataclass(frozen=True)
class DupReport:
    """Near-duplicate numbers: the planted file against the stored docs."""

    planted_title: str
    original_similarity: float | None  # None when the original is not stored
    top_candidate_url: str | None
    top_candidate_similarity: float | None
    max_distinct: float | None  # highest similarity between two different stored docs
    max_distinct_pair: tuple[str, str] | None


@dataclass
class EvalReport:
    results: list[QuestionResult] = field(default_factory=list)
    dup: DupReport | None = None
    settings: Settings | None = None

    # --- summary numbers ------------------------------------------------------------

    def _answerable(self) -> list[QuestionResult]:
        return [r for r in self.results if r.question.kind != "outside"]

    def _outside(self) -> list[QuestionResult]:
        return [r for r in self.results if r.question.kind == "outside"]

    def hit_at(self, k: int) -> float | None:
        rows = self._answerable()
        if not rows:
            return None
        return sum(1 for r in rows if r.rank is not None and r.rank <= k) / len(rows)

    def answerable_scores(self) -> list[float]:
        """Top scores of answerable questions whose expected doc is ranked first."""
        return [r.top_score for r in self._answerable() if r.rank == 1 and r.top_score is not None]

    def outside_scores(self) -> list[float]:
        return [r.top_score for r in self._outside() if r.top_score is not None]

    def suggested_low(self) -> float | None:
        """Midway between the best out-of-corpus score and the worst rank-1 answerable one."""
        ans, out = self.answerable_scores(), self.outside_scores()
        if not ans:
            return None
        if not out:
            return round(min(ans) / 2, 2)
        return round((max(out) + min(ans)) / 2, 2) if max(out) < min(ans) else None

    def suggested_high(self) -> float | None:
        """The top score that most rank-1 answerable questions reach: their 25th percentile,
        but never above the lowest demo question (both demo questions must be high)."""
        ans = self.answerable_scores()
        if not ans:
            return None
        value = float(np.percentile(ans, 25))
        demo = [
            r.top_score
            for r in self.results
            if r.question.demo and r.rank == 1 and r.top_score is not None
        ]
        if demo:
            value = min(value, min(demo))
        low = self.suggested_low()
        if low is not None and value <= low:
            return None
        return float(np.floor(value * 100) / 100)

    def suggested_dup(self) -> float | None:
        d = self.dup
        if d is None or d.original_similarity is None or d.max_distinct is None:
            return None
        if d.original_similarity <= d.max_distinct:
            return None
        return round((d.original_similarity + d.max_distinct) / 2, 3)

    def failures(self) -> list[str]:
        """Acceptance checks of phase 11 that do not pass."""
        problems: list[str] = []
        hit5 = self.hit_at(5)
        if hit5 is not None and hit5 < 0.9:
            problems.append(f"hit@5 is {hit5:.0%}; needs at least 90%")
        for r in self._outside():
            if r.confidence != "low":
                problems.append(f"out-of-corpus question is {r.confidence}: {r.question.q}")
        for r in self.results:
            if r.question.demo and (r.rank != 1 or r.confidence != "high"):
                problems.append(
                    f"demo question is {r.confidence} with the doc at rank {r.rank}: {r.question.q}"
                )
        if self.dup is not None:
            d = self.dup
            threshold = self.settings.dup_threshold if self.settings else None
            if d.original_similarity is None:
                problems.append("the near-duplicate's original is not stored")
            elif threshold is not None and d.original_similarity < threshold:
                problems.append(
                    f"near-duplicate similarity {d.original_similarity:.4f} is below"
                    f" KB_DUP_THRESHOLD {threshold}"
                )
            if (
                threshold is not None
                and d.max_distinct is not None
                and d.max_distinct >= threshold
            ):
                problems.append(
                    f"two distinct docs reach similarity {d.max_distinct:.4f}, at or above"
                    f" KB_DUP_THRESHOLD {threshold}"
                )
        return problems


def load_questions(path: str | Path) -> QuestionSet:
    """Read and validate a questions file. Raises `QuestionsError`."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise QuestionsError(f"cannot read {path}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise QuestionsError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("questions"), list):
        raise QuestionsError(f"{path}: expected a mapping with a 'questions' list")
    out: list[Question] = []
    for index, item in enumerate(raw["questions"], start=1):
        where = f"question {index}"
        if not isinstance(item, dict) or not str(item.get("q") or "").strip():
            raise QuestionsError(f"{where}: needs a non-empty 'q'")
        kind = str(item.get("kind") or "answerable")
        if kind not in KINDS:
            raise QuestionsError(f"{where}: kind must be one of {', '.join(KINDS)}")
        raw_expect = item.get("expect")
        if isinstance(raw_expect, list):
            expect = tuple(str(u).strip() for u in raw_expect if str(u).strip())
        elif raw_expect:
            expect = (str(raw_expect).strip(),)
        else:
            expect = ()
        if kind != "outside" and not expect:
            raise QuestionsError(f"{where}: an {kind} question needs 'expect' (a source_url)")
        if kind == "outside" and expect:
            raise QuestionsError(f"{where}: an outside question has no 'expect'")
        out.append(Question(str(item["q"]).strip(), kind, expect, bool(item.get("demo", False))))
    dup = raw.get("near_duplicate")
    if dup is None:
        return QuestionSet(out)
    if not isinstance(dup, dict) or not dup.get("path") or not dup.get("original"):
        raise QuestionsError(f"{path}: near_duplicate needs 'path' and 'original'")
    planted = path.parent / str(dup["path"])
    if not planted.is_file():
        raise QuestionsError(f"{path}: near_duplicate file {planted} does not exist")
    return QuestionSet(out, planted, str(dup["original"]))


def _copy_database(source: Path, dest: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _dup_report(conn: sqlite3.Connection, state: SearchState, planted: Path, original_url: str) -> DupReport:
    """Similarity of the planted file to every stored doc, with no write."""
    req = UploadRequest(content=planted.read_text(encoding="utf-8"), format="markdown", force=True)
    prepared = service.prepare_upload(req)
    vectors = indexing.embed_chunks(state, prepared.title, prepared.chunks)
    if vectors is None:
        raise RuntimeError("the embedder is not available; the near-duplicate check needs the models")
    doc_ids, matrix = state.index.doc_vectors()
    urls = {
        int(r["id"]): r["source_url"] for r in conn.execute("SELECT id, source_url FROM docs")
    }
    if doc_ids.shape[0] == 0:
        return DupReport(prepared.title, None, None, None, None, None)
    sims = matrix @ dupes.doc_vector(vectors)
    best = int(np.argmax(sims))
    original = [i for i, d in enumerate(doc_ids) if urls.get(int(d)) == original_url]
    pair_max: float | None = None
    pair: tuple[str, str] | None = None
    if doc_ids.shape[0] > 1:
        gram = matrix @ matrix.T
        np.fill_diagonal(gram, -np.inf)
        i, j = np.unravel_index(int(np.argmax(gram)), gram.shape)
        pair_max = float(gram[i, j])
        pair = (str(urls.get(int(doc_ids[i]))), str(urls.get(int(doc_ids[j]))))
    return DupReport(
        planted_title=prepared.title,
        original_similarity=float(sims[original[0]]) if original else None,
        top_candidate_url=urls.get(int(doc_ids[best])),
        top_candidate_similarity=float(sims[best]),
        max_distinct=pair_max,
        max_distinct_pair=pair,
    )


async def _run(state: SearchState, questions: list[Question]) -> list[QuestionResult]:
    results: list[QuestionResult] = []
    for question in questions:
        started = time.perf_counter()
        found = await pipeline.search(state, question.q, TOP_K)
        latency_ms = (time.perf_counter() - started) * 1000.0
        urls = [hit.source_url for hit in found.results]
        doc_rank: int | None = None
        seen: list[int] = []
        for hit in found.results:  # rank by document, not by chunk
            if hit.doc_id in seen:
                continue
            seen.append(hit.doc_id)
            if hit.source_url in question.expect:
                doc_rank = len(seen)
                break
        top = found.results[0] if found.results else None
        results.append(
            QuestionResult(
                question=question,
                rank=doc_rank,
                top_score=None if top is None else float(top.score),
                top_url=urls[0] if urls else None,
                confidence=found.confidence,
                mode=found.mode,
                latency_ms=latency_ms,
            )
        )
    return results


@contextmanager
def _temp_state(
    settings: Settings, embedder: EmbedderLike | None, reranker: RerankerLike | None
) -> Iterator[tuple[SearchState, Path]]:
    """A started `SearchState` over a temporary copy of `settings.db_path`.

    Raises `RuntimeError` when the database is missing or the models cannot
    load: tuning in keyword mode would measure the wrong thing.
    """
    source = Path(settings.db_path)
    if not source.is_file():
        raise RuntimeError(f"database {source} does not exist; run the seed first")
    with tempfile.TemporaryDirectory(prefix="kb-eval-") as tmp:
        copy = Path(tmp) / "kb.db"
        _copy_database(source, copy)
        local = dataclasses.replace(
            settings, db_path=str(copy), search_timeout=EVAL_TIMEOUT, bucket=None, models_on=True
        )
        state = indexing.build_state(local, embedder, reranker)
        asyncio.run(indexing.start(state))
        if not state.semantic_ready():
            raise RuntimeError("the models did not load, so search is keyword only; check KB_MODEL_DIR")
        yield state, copy


def evaluate(
    settings: Settings,
    questions: list[Question],
    *,
    planted: Path | None = None,
    planted_original: str | None = None,
    embedder: EmbedderLike | None = None,
    reranker: RerankerLike | None = None,
) -> EvalReport:
    """Evaluate `questions` against a temporary copy of `settings.db_path`.

    `embedder` and `reranker` replace the real models (tests).
    """
    with _temp_state(settings, embedder, reranker) as (state, copy):
        report = EvalReport(settings=settings)
        report.results = asyncio.run(_run(state, questions))
        if planted is not None and planted_original is not None:
            conn = db.connect(copy)
            try:
                report.dup = _dup_report(conn, state, planted, planted_original)
            finally:
                conn.close()
    return report


def inspect(
    settings: Settings,
    queries: list[str],
    k: int = TOP_K,
    *,
    embedder: EmbedderLike | None = None,
    reranker: RerankerLike | None = None,
) -> str:
    """Run `queries` on a temporary copy and show every hit (for tuning by eye).

    Each hit shows rank, score, document, heading path and the start of the
    snippet. Like `evaluate`, nothing reaches the real database (no gaps).
    """
    lines: list[str] = []
    with _temp_state(settings, embedder, reranker) as (state, _copy):
        for query in queries:
            found = asyncio.run(pipeline.search(state, query, k))
            lines.append(f"{query}  [{found.mode}, {found.confidence}]")
            for rank, hit in enumerate(found.results, start=1):
                snippet = " ".join(hit.snippet.split())[:160]
                lines.append(f"  {rank}. {hit.score:.4f}  doc {hit.doc_id}  {hit.heading_path}")
                lines.append(f"     {hit.source_url}")
                lines.append(f"     {snippet}")
            lines.append("")
    return "\n".join(lines).rstrip()


def _fmt(value: float | None, digits: int = 4) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def render(report: EvalReport) -> str:
    """The report as plain text for the terminal and for docs/PROGRESS.md."""
    lines: list[str] = []
    lines.append(f"{'rank':>4}  {'score':>6}  {'conf':<6}  {'mode':<8}  {'ms':>5}  kind        question")
    for r in report.results:
        rank = "-" if r.rank is None else str(r.rank)
        flag = " (demo)" if r.question.demo else ""
        lines.append(
            f"{rank:>4}  {_fmt(r.top_score):>6}  {r.confidence:<6}  {r.mode:<8}"
            f"  {r.latency_ms:>5.0f}  {r.question.kind:<10}  {r.question.q}{flag}"
        )
        if r.question.kind != "outside" and r.rank != 1:
            lines.append(f"{'':>37}top hit: {r.top_url}")
    ans, out = report.answerable_scores(), report.outside_scores()
    s = report.settings
    lines.append("")
    lines.append(f"hit@1: {_pct(report.hit_at(1))}   hit@5: {_pct(report.hit_at(5))}")
    times = sorted(r.latency_ms for r in report.results)
    if times:
        lines.append(
            f"search latency: median {times[len(times) // 2]:.0f} ms, max {times[-1]:.0f} ms"
            + (
                f" (KB_RERANK_CANDIDATES={s.rerank_candidates}, KB_RERANK_MAX_CHARS={s.rerank_max_chars})"
                if s is not None
                else ""
            )
        )
    lines.append(f"answerable top scores (rank 1): {_range(ans)}")
    lines.append(f"out-of-corpus top scores:       {_range(out)}")
    if s is not None:
        lines.append(f"current KB_CONF_HIGH={s.conf_high} KB_CONF_LOW={s.conf_low}")
    lines.append(
        f"suggested KB_CONF_HIGH={_fmt(report.suggested_high(), 2)}"
        f" KB_CONF_LOW={_fmt(report.suggested_low(), 2)}"
    )
    if report.dup is not None:
        d = report.dup
        lines.append("")
        lines.append(f"near-duplicate: {d.planted_title}")
        lines.append(f"  similarity to its original:   {_fmt(d.original_similarity)}")
        lines.append(
            f"  top candidate:                {_fmt(d.top_candidate_similarity)} {d.top_candidate_url}"
        )
        lines.append(f"  highest distinct-doc pair:    {_fmt(d.max_distinct)}")
        if d.max_distinct_pair:
            lines.append(f"    {d.max_distinct_pair[0]}")
            lines.append(f"    {d.max_distinct_pair[1]}")
        if s is not None:
            lines.append(f"  current KB_DUP_THRESHOLD={s.dup_threshold}")
        lines.append(f"  suggested KB_DUP_THRESHOLD={_fmt(report.suggested_dup(), 3)}")
    problems = report.failures()
    lines.append("")
    if problems:
        lines.append("acceptance: FAIL")
        lines.extend(f"  - {p}" for p in problems)
    else:
        lines.append("acceptance: PASS")
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _range(values: list[float]) -> str:
    if not values:
        return "-"
    return f"{min(values):.4f} to {max(values):.4f} (n={len(values)})"

