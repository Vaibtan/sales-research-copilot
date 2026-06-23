-- Slice 9: the semantic layer (PRD §7, §10) — pgvector chunk tables that chat
-- (slice 10) and cross-session memory (slice 11) retrieve over. Written inside
-- the same `persist` transaction as the relational rows, and subject to the same
-- full-materialization replacement (no orphans after a shrinking regenerate).
-- Embeddings are text-embedding-3-small → vector(1536). The pgvector extension is
-- already created in 0001.

CREATE TABLE IF NOT EXISTS report_chunks (
    id         TEXT PRIMARY KEY,
    session_id TEXT         NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    section    TEXT         NOT NULL,
    chunk_ix   INTEGER      NOT NULL,
    text       TEXT         NOT NULL,
    embedding  vector(1536) NOT NULL,
    UNIQUE (session_id, section, chunk_ix)
);

CREATE INDEX IF NOT EXISTS report_chunks_session_idx ON report_chunks (session_id);
CREATE INDEX IF NOT EXISTS report_chunks_embedding_idx
    ON report_chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS source_chunks (
    id         TEXT PRIMARY KEY,
    session_id TEXT         NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    source_id  TEXT         NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    chunk_ix   INTEGER      NOT NULL,
    text       TEXT         NOT NULL,
    embedding  vector(1536) NOT NULL,
    UNIQUE (source_id, chunk_ix)
);

CREATE INDEX IF NOT EXISTS source_chunks_session_idx ON source_chunks (session_id);
CREATE INDEX IF NOT EXISTS source_chunks_embedding_idx
    ON source_chunks USING hnsw (embedding vector_cosine_ops);
