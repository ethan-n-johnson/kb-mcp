"""Command line entry point: `python -m kb.cli <command>`.

Commands are argparse subcommands; `seed`, `eval` and `snapshot-push` came in phase 11.
Heavy imports (fastembed, trafilatura, pymupdf, boto3) happen inside the
command functions so `--help` stays fast.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from kb.config import Settings
    from kb.search.state import SearchState

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
EMBED_DIM = 384


def _is_url(target: str) -> bool:
    return re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", target) is not None


def cmd_convert(args: argparse.Namespace) -> int:
    """Convert a file or URL to Markdown and print the title, tags and chunks."""
    from kb.ingest.chunk import chunk_markdown
    from kb.ingest.convert import detect_format, to_markdown
    from kb.ingest.fetch import fetch_url
    from kb.ingest.errors import IngestError

    target: str = args.target
    try:
        if _is_url(target):
            fetched = fetch_url(target)
            fmt = detect_format(
                declared=args.format, content_type=fetched.content_type, url=fetched.final_url
            )
            converted = to_markdown(fetched.body, fmt, url=fetched.final_url)
        else:
            path = Path(target)
            try:
                data = path.read_bytes()
            except OSError as exc:
                print(f"error: cannot read {target}: {exc.strerror}", file=sys.stderr)
                return 1
            fmt = detect_format(declared=args.format, filename=path.name)
            converted = to_markdown(data, fmt)
    except IngestError as exc:
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1

    chunks = chunk_markdown(converted.markdown, converted.title)
    print(f"title:  {converted.title}")
    print(f"format: {fmt}")
    print(f"tags:   {', '.join(converted.tags) if converted.tags else '(none)'}")
    print(f"chunks: {len(chunks)}")
    for chunk in chunks:
        preview = " ".join(chunk.text.split())[:80]
        print(f"[{chunk.ord}] {chunk.heading_path} ({chunk.word_count} words)")
        print(f"    {preview}")
    return 0


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def cmd_check_models(args: argparse.Namespace) -> int:
    """Load both fastembed models from `KB_MODEL_DIR` and sanity check them.

    Prints the embedding dimension and norm, then reranks a relevant and an
    irrelevant passage (raw score and sigmoid). Exits non-zero on any failure.
    """
    from kb.config import get_settings

    model_dir = get_settings().model_dir
    print(f"model dir: {model_dir}")
    try:
        import numpy as np
        from fastembed import TextEmbedding
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        embedder = TextEmbedding(EMBED_MODEL, cache_dir=model_dir)
        vector = np.asarray(
            next(iter(embedder.embed(["How do I attach an EBS volume to an EC2 instance?"]))),
            dtype=np.float32,
        )
        dim = int(vector.shape[0])
        norm = float(np.linalg.norm(vector))
        print(f"embedding model: {EMBED_MODEL}")
        print(f"  dim:  {dim}")
        print(f"  norm: {norm:.4f}")
        if dim != EMBED_DIM:
            print(f"FAIL: expected dimension {EMBED_DIM}, got {dim}", file=sys.stderr)
            return 1

        reranker = TextCrossEncoder(RERANK_MODEL, cache_dir=model_dir)
        query = "How do I attach an EBS volume to an EC2 instance?"
        relevant = (
            "To attach an EBS volume, choose the volume in the console, then Actions, "
            "Attach volume, and select the instance in the same Availability Zone."
        )
        irrelevant = "Amazon S3 lifecycle rules move objects to Glacier after a set number of days."
        raw = [float(s) for s in reranker.rerank(query, [relevant, irrelevant])]
        if len(raw) != 2:
            print(f"FAIL: expected 2 rerank scores, got {len(raw)}", file=sys.stderr)
            return 1
        print(f"reranker model: {RERANK_MODEL}")
        print(f"  relevant:   raw={raw[0]:.4f} sigmoid={_sigmoid(raw[0]):.4f}")
        print(f"  irrelevant: raw={raw[1]:.4f} sigmoid={_sigmoid(raw[1]):.4f}")
        if not raw[0] > raw[1]:
            print("FAIL: relevant passage did not outscore the irrelevant one", file=sys.stderr)
            return 1
    except Exception as exc:  # any load or inference failure must exit non-zero
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("OK")
    return 0


def cmd_mcp_stdio(args: argparse.Namespace) -> int:
    """Serve the MCP tools over stdio against the local database (`KB_DB`).

    Stdout carries the protocol, so logging goes to stderr and nothing else is
    printed. Startup mirrors the web app: open the database, load the models
    (unless `KB_MODELS=off`), backfill missing vectors and build the index, then
    run the same 11 tools. Write tools read the token from `KB_TOKEN`.
    """
    import asyncio

    from kb import db
    from kb.config import get_settings
    from kb.logging import setup_logging
    from kb.mcp_tools import build_mcp
    from kb.search import indexing

    setup_logging(sys.stderr)
    settings = get_settings()
    conn = db.connect(settings.db_path)
    try:
        db.init_schema(conn)
    finally:
        conn.close()
    state = indexing.build_state(settings)
    asyncio.run(indexing.start(state))
    try:
        build_mcp(state, stdio=True).run(transport="stdio")
    except KeyboardInterrupt:
        pass
    return 0


def _stamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_backup(args: argparse.Namespace) -> int:
    """Back up `KB_DB` to `s3://$KB_BUCKET/backups/` and print the S3 key.

    The key is the only thing written to stdout. Failures go to stderr with a
    UTC timestamp (the nightly cron appends both to a log) and exit non-zero.
    """
    from kb.backup import BackupError, create_backup
    from kb.config import get_settings

    settings = get_settings()
    if not settings.bucket:
        print(f"{_stamp()} backup failed: KB_BUCKET is not set", file=sys.stderr)
        return 1
    try:
        result = create_backup(settings.db_path, settings.bucket, region=settings.aws_region)
    except BackupError as exc:
        print(f"{_stamp()} backup failed: {exc}", file=sys.stderr)
        return 1
    print(result.key)
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """Replace the database with a snapshot or backup from S3 (the app must be stopped).

    Needs `--force`; without it nothing is read or written and the exit code
    is 2. The old database is kept as `<target>.pre-restore-<timestamp>`.

    `--latest` takes `snapshots/kb-latest.db.gz` first, else the newest object
    under `backups/`, else does nothing and exits 0 (a brand new deployment
    starts with an empty database; the instance boot script relies on this).
    Any other failure, including S3 being unreachable, exits 1 so the boot
    script retries instead of starting empty and overwriting the snapshot.

    When the restored file is the live database (`--target` is `KB_DB`) and it
    came from a nightly backup, the restored database is also uploaded as the
    new snapshot (unless `KB_SNAPSHOTS=off`). Otherwise the snapshot would still
    hold the state the restore was meant to undo, and a replacement instance
    would restore that instead.
    """
    from kb.backup import SNAPSHOT_KEY, BackupError, NothingToRestore, create_snapshot, restore_backup
    from kb.config import get_settings

    settings = get_settings()
    if not args.force:
        print(
            "error: restore replaces the database file; stop the app first, then pass --force",
            file=sys.stderr,
        )
        return 2
    if not settings.bucket:
        print("error: KB_BUCKET is not set", file=sys.stderr)
        return 1
    target = args.target or settings.db_path
    try:
        result = restore_backup(
            settings.bucket,
            target,
            key=args.key,
            latest=args.latest,
            force=True,
            region=settings.aws_region,
        )
    except NothingToRestore as exc:
        print(f"nothing to restore ({exc}); starting with an empty database")
        return 0
    except BackupError as exc:
        print(f"error: restore failed: {exc}", file=sys.stderr)
        return 1
    print(f"restored: s3://{settings.bucket}/{result.key}")
    print(f"target:   {result.target}")
    print(f"previous: {result.previous if result.previous else '(none)'}")
    print(f"docs:     {result.docs}")
    if (
        result.key != SNAPSHOT_KEY
        and settings.snapshots
        and Path(target).resolve() == Path(settings.db_path).resolve()
    ):
        try:
            create_snapshot(result.target, settings.bucket, region=settings.aws_region)
        except BackupError as exc:
            print(f"error: restored, but the snapshot upload failed: {exc}", file=sys.stderr)
            return 1
        print(f"snapshot: s3://{settings.bucket}/{SNAPSHOT_KEY} now holds the restored database")
    return 0


def _open_state(settings: "Settings", *, models: bool) -> "SearchState | None":
    """Build and start a `SearchState` (models loaded, index built), or None without models."""
    import asyncio

    from kb.search import indexing

    if not models or not settings.models_on:
        return None
    state = indexing.build_state(settings)
    asyncio.run(indexing.start(state))
    return state


def cmd_seed(args: argparse.Namespace) -> int:
    """Seed `KB_DB` from a manifest (`seeds/seeds.yaml`) and print one line per entry.

    Exits 1 when any entry failed (every other entry is still processed), 2 for
    a bad manifest. `--dry-run` fetches and converts each entry without writing.
    """
    from kb import db, seed
    from kb.config import get_settings

    try:
        manifest = seed.load_manifest(args.manifest)
    except seed.ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    settings = get_settings()
    db_path = Path(settings.db_path)
    if args.dry_run:
        print(f"dry run: {len(manifest.entries)} entries, nothing is written")
        state = None
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"database: {db_path}")
        conn = db.connect(db_path)
        try:
            db.init_schema(conn)
        finally:
            conn.close()
        state = _open_state(settings, models=True)
        if state is None:
            print("models off: chunks are stored without vectors (the server backfills them)")
        elif not state.semantic_ready():
            print("warning: the models did not load; chunks are stored without vectors")

    def show(outcome: "seed.Outcome") -> None:
        doc = "" if outcome.doc_id is None else f" doc {outcome.doc_id}"
        print(f"{outcome.status:<7}{doc:>9}  {outcome.label}", flush=True)
        if outcome.detail:
            print(f"{'':>18}{outcome.detail}", flush=True)

    conn = db.connect(":memory:" if args.dry_run else db_path)
    try:
        outcomes = seed.seed(conn, manifest, search_state=state, dry_run=args.dry_run, progress=show)
    finally:
        conn.close()
    counts = seed.summary(outcomes)
    print("summary: " + ", ".join(f"{n} {status}" for status, n in sorted(counts.items())))
    if not args.dry_run:
        conn = db.connect(db_path)
        try:
            docs = int(conn.execute("SELECT count(*) FROM docs").fetchone()[0])
        finally:
            conn.close()
        print(f"documents in the database: {docs}")
    return 1 if counts.get("failed") else 0


def cmd_eval(args: argparse.Namespace) -> int:
    """Run the evaluation questions against `KB_DB` and print the tuning report.

    Exits 1 when an acceptance check of phase 11 fails, 2 for a bad questions
    file or when the models cannot load.
    """
    from kb import evaluate
    from kb.config import get_settings

    try:
        qset = evaluate.load_questions(args.questions)
    except evaluate.QuestionsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        report = evaluate.evaluate(
            get_settings(),
            qset.questions,
            planted=None if args.no_duplicate else qset.planted,
            planted_original=qset.planted_original,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(evaluate.render(report))
    return 1 if report.failures() else 0


def cmd_search(args: argparse.Namespace) -> int:
    """Show the top hits for one or more queries against a temporary copy of `KB_DB`."""
    from kb import evaluate
    from kb.config import get_settings

    try:
        print(evaluate.inspect(get_settings(), args.queries, args.k))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_snapshot_push(args: argparse.Namespace) -> int:
    """Upload `KB_DB` as the snapshot the instances restore at boot.

    Uses the same copy, integrity check and gzip as the app's own snapshots.
    Needs `KB_BUCKET` and AWS credentials allowed to write to the bucket.
    """
    from kb.backup import SNAPSHOT_KEY, BackupError, create_snapshot
    from kb.config import get_settings

    settings = get_settings()
    if not settings.bucket:
        print("error: KB_BUCKET is not set", file=sys.stderr)
        return 1
    if not Path(settings.db_path).is_file():
        print(f"error: database {settings.db_path} does not exist", file=sys.stderr)
        return 1
    try:
        result = create_snapshot(settings.db_path, settings.bucket, region=settings.aws_region)
    except BackupError as exc:
        print(f"error: snapshot upload failed: {exc}", file=sys.stderr)
        return 1
    print(f"uploaded: s3://{settings.bucket}/{SNAPSHOT_KEY}")
    print(f"docs:     {result.docs}")
    print(f"bytes:    {result.bytes_uploaded}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser with one subparser per command."""
    parser = argparse.ArgumentParser(prog="python -m kb.cli", description="kb-mcp command line tools")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    convert = sub.add_parser(
        "convert", help="convert a file or URL to Markdown and show its chunks (debug)"
    )
    convert.add_argument("target", help="path to a local file, or an http(s) URL")
    convert.add_argument(
        "--format", "-f", choices=["html", "pdf", "markdown"], default=None,
        help="override format detection",
    )
    convert.set_defaults(func=cmd_convert)

    check = sub.add_parser(
        "check-models", help="load both ONNX models from KB_MODEL_DIR and sanity check them"
    )
    check.set_defaults(func=cmd_check_models)

    stdio = sub.add_parser(
        "mcp-stdio",
        help="serve the MCP tools over stdio against KB_DB (token from KB_TOKEN; logs go to stderr)",
    )
    stdio.set_defaults(func=cmd_mcp_stdio)

    backup = sub.add_parser(
        "backup",
        help="copy KB_DB to s3://$KB_BUCKET/backups/kb-YYYYMMDD-HHMMSS.db.gz and print the key",
    )
    backup.set_defaults(func=cmd_backup)

    restore = sub.add_parser(
        "restore",
        help="replace the database with a snapshot or backup from S3 (stop the app first; needs --force)",
    )
    which = restore.add_mutually_exclusive_group(required=True)
    which.add_argument(
        "--latest",
        action="store_true",
        help="restore the snapshot (snapshots/kb-latest.db.gz), else the newest backup, else do nothing",
    )
    which.add_argument(
        "--key", metavar="K", help="restore this S3 key (backups/kb-....db.gz or a snapshot key)"
    )
    restore.add_argument(
        "--target", metavar="PATH", default=None, help="database file to replace (default KB_DB)"
    )
    restore.add_argument(
        "--force", action="store_true", help="confirm that the app is stopped and the file may be replaced"
    )
    restore.set_defaults(func=cmd_restore)

    seed_p = sub.add_parser(
        "seed", help="seed KB_DB from a manifest such as seeds/seeds.yaml (idempotent)"
    )
    seed_p.add_argument("manifest", help="path to the YAML manifest")
    seed_p.add_argument(
        "--dry-run", action="store_true", help="fetch and convert every entry, write nothing"
    )
    seed_p.set_defaults(func=cmd_seed)

    eval_p = sub.add_parser(
        "eval", help="run the evaluation questions against KB_DB and suggest tuned cutoffs"
    )
    eval_p.add_argument("questions", help="path to the questions YAML (tests/eval/questions.yaml)")
    eval_p.add_argument(
        "--no-duplicate", action="store_true", help="skip the near-duplicate measurement"
    )
    eval_p.set_defaults(func=cmd_eval)

    search_p = sub.add_parser(
        "search",
        help="show the top hits for queries against a temporary copy of KB_DB (records no gaps)",
    )
    search_p.add_argument("queries", nargs="+", help="one or more queries (quote each)")
    search_p.add_argument("-k", type=int, default=5, help="hits per query (default 5)")
    search_p.set_defaults(func=cmd_search)

    push = sub.add_parser(
        "snapshot-push",
        help="upload KB_DB as s3://$KB_BUCKET/snapshots/kb-latest.db.gz (restored at instance boot)",
    )
    push.set_defaults(func=cmd_snapshot_push)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse `argv` and run the chosen command; returns the exit code."""
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
