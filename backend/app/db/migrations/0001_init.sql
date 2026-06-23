-- Slice 2: Layer-2 relational entities the API serves (PRD §10).
-- Vector tables (report_chunks/source_chunks/research_memory), messages, and the
-- llm_calls ledger arrive in their own slices. Ids are deterministic TEXT digests
-- of natural keys (app.domain.identity); session ids are random.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    company     TEXT        NOT NULL,
    company_key TEXT        NOT NULL,
    website     TEXT,
    objective   TEXT        NOT NULL,
    status      TEXT        NOT NULL,
    degraded    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sessions_company_key_idx ON sessions (company_key);
CREATE INDEX IF NOT EXISTS sessions_created_at_idx ON sessions (created_at DESC);

CREATE TABLE IF NOT EXISTS reports (
    id               TEXT PRIMARY KEY,
    session_id       TEXT        NOT NULL UNIQUE REFERENCES sessions (id) ON DELETE CASCADE,
    version          INTEGER     NOT NULL DEFAULT 1,
    sections_jsonb   JSONB       NOT NULL,
    quality_score    DOUBLE PRECISION,
    model_meta_jsonb JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sources (
    id           TEXT PRIMARY KEY,
    session_id   TEXT        NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    url          TEXT        NOT NULL,
    title        TEXT        NOT NULL,
    snippet      TEXT        NOT NULL,
    content_hash TEXT        NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL,
    UNIQUE (session_id, url)
);

CREATE INDEX IF NOT EXISTS sources_session_idx ON sources (session_id);

CREATE TABLE IF NOT EXISTS facts (
    id              TEXT PRIMARY KEY,
    session_id      TEXT             NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    question_id     TEXT             NOT NULL,
    section         TEXT             NOT NULL,
    freshness_class TEXT             NOT NULL,
    claim           TEXT             NOT NULL,
    claim_norm      TEXT             NOT NULL,
    source_id       TEXT             NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    confidence      DOUBLE PRECISION NOT NULL,
    attempt         INTEGER          NOT NULL,
    UNIQUE (session_id, question_id, source_id, claim_norm)
);

CREATE INDEX IF NOT EXISTS facts_session_idx ON facts (session_id);
CREATE INDEX IF NOT EXISTS facts_question_idx ON facts (session_id, question_id);
