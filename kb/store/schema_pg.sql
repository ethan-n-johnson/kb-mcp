-- PostgreSQL schema for kb-mcp. Applied at startup and idempotent: every
-- statement is IF NOT EXISTS. Same tables and columns as the SQLite schema in
-- kb/db.py; the generated `tsv` column replaces the FTS5 table and triggers.
--
-- Timestamps are TIMESTAMPTZ; the store returns them as the same ISO 8601 UTC
-- strings the SQLite schema stores (2026-10-31T12:00:00Z). `content_hash` is the
-- SHA-256 hex digest as TEXT (see "Phase 2: PostgreSQL backend" in
-- docs/PROGRESS.md).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS docs (
    id            BIGSERIAL PRIMARY KEY,
    title         TEXT NOT NULL,
    source_url    TEXT,
    source_type   TEXT NOT NULL DEFAULT 'markdown',
    content_md    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    s3_key        TEXT,
    created_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL,
    verified_at   TIMESTAMPTZ,
    verified_by   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_content_hash ON docs(content_hash);

CREATE TABLE IF NOT EXISTS chunks (
    id            BIGSERIAL PRIMARY KEY,
    doc_id        BIGINT NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    ord           INTEGER NOT NULL,
    heading_path  TEXT NOT NULL DEFAULT '',
    text          TEXT NOT NULL,
    embedding     vector(384),
    embed_model   TEXT,
    tsv           tsvector GENERATED ALWAYS AS (
                      setweight(to_tsvector('english', coalesce(heading_path, '')), 'A')
                      || setweight(to_tsvector('english', text), 'B')
                  ) STORED
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING GIN (tsv);

CREATE TABLE IF NOT EXISTS doc_tags (
    doc_id  BIGINT NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    PRIMARY KEY (doc_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_doc_tags_tag ON doc_tags(tag);

CREATE TABLE IF NOT EXISTS gaps (
    id               BIGSERIAL PRIMARY KEY,
    query            TEXT NOT NULL,
    query_norm       TEXT NOT NULL,
    top_score        DOUBLE PRECISION,
    mode             TEXT,
    created_at       TIMESTAMPTZ NOT NULL,
    resolved_doc_id  BIGINT REFERENCES docs(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_gaps_query_norm ON gaps(query_norm);

CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    token_name  TEXT,
    action      TEXT NOT NULL,
    doc_id      BIGINT,
    at          TIMESTAMPTZ NOT NULL,
    note        TEXT
);
