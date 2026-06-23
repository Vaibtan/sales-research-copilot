-- Slice 11: cross-session research memory (PRD §7.1, §10). Self-contained — each
-- row carries its own source provenance + content_hash + fetched_at + a persisted
-- `section` and `freshness_class`, and has **no** session-scoped source_id or
-- question_id (the binding is done at recall time by matching to the current
-- plan). So recall can warm-start a future run for the same company_key without
-- ever making a report cite another session's row.

CREATE TABLE IF NOT EXISTS research_memory (
    id                TEXT PRIMARY KEY,
    company_key       TEXT         NOT NULL,
    section           TEXT         NOT NULL,
    freshness_class   TEXT         NOT NULL,
    url               TEXT         NOT NULL,
    title             TEXT         NOT NULL,
    snippet           TEXT         NOT NULL,
    content_hash      TEXT         NOT NULL,
    fact              TEXT         NOT NULL,
    fact_norm         TEXT         NOT NULL,
    embedding         vector(1536) NOT NULL,
    fetched_at        TIMESTAMPTZ  NOT NULL,
    last_validated_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (company_key, content_hash, fact_norm)
);

CREATE INDEX IF NOT EXISTS research_memory_company_idx ON research_memory (company_key);
CREATE INDEX IF NOT EXISTS research_memory_embedding_idx
    ON research_memory USING hnsw (embedding vector_cosine_ops);
