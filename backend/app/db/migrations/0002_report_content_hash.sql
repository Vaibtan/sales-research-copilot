-- Slice 5 hardening (Codex F2): make report versioning idempotent under
-- resume-replay. A crash after `persist` commits but before LangGraph checkpoints
-- END leaves `persist` pending; resume re-runs it. Without a content fingerprint
-- the upsert bumps `reports.version` for an *identical* materialization, falsely
-- staling chat/report clients (§8.1). `content_hash` lets the upsert bump the
-- version only on an actual semantic change (see app.db.repository.persist_results).
-- Nullable: pre-existing rows backfill to a real hash on their next persist.

ALTER TABLE reports ADD COLUMN IF NOT EXISTS content_hash TEXT;
