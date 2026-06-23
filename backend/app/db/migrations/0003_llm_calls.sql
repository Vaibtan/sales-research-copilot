-- Slice 6: the durable LLM-call ledger (PRD §6.5, §10) — token idempotency on
-- resume. Every LLM / embedding / search call reserves a row here before paying
-- the provider, so a committed call is never re-paid and concurrent resumes /
-- double-submits single-flight on one provider call.
--
-- The PK is the *call identity* `(session_id, node, attempt, input_hash)`: same
-- inputs → same key → same reservation. `locked_by` + `lease_expires_at` are the
-- cross-process lease (the durable version of slice-5's in-process `_active`
-- guard): a waiter takes over a row whose owner crashed (lease expired) via CAS.
-- No FK on session_id — the ledger is keyed independently of the relational
-- materialization and may outlive a pruned session row.

CREATE TABLE IF NOT EXISTS llm_calls (
    session_id       TEXT        NOT NULL,
    node             TEXT        NOT NULL,
    attempt          INTEGER     NOT NULL,
    input_hash       TEXT        NOT NULL,
    status           TEXT        NOT NULL CHECK (status IN ('in_progress', 'done', 'failed')),
    idempotency_key  TEXT        NOT NULL,
    locked_by        TEXT,
    lease_expires_at TIMESTAMPTZ,
    response_jsonb   JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, node, attempt, input_hash)
);

CREATE INDEX IF NOT EXISTS llm_calls_session_idx ON llm_calls (session_id);
