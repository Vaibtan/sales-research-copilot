-- Slice 10: grounded follow-up chat (PRD §8, §8.1, §10). Each message snapshots
-- its citations (source_id + url/title/snippet at answer time) so history resolves
-- even if a source is later dropped by a shrinking regenerate, and records the
-- report_version it was grounded on; `persist` flips `stale` true on older messages
-- when it bumps the version.

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    session_id      TEXT        NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    role            TEXT        NOT NULL,
    content         TEXT        NOT NULL,
    citations_jsonb JSONB       NOT NULL DEFAULT '[]'::jsonb,
    report_version  INTEGER,
    stale           BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS messages_session_idx ON messages (session_id, created_at);
