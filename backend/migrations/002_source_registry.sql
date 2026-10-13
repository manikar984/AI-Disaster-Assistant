-- 002: source registry, geographic precision, syndication dedupe and worker state.

ALTER TABLE reports ADD COLUMN dedupe_key TEXT;
ALTER TABLE reports ADD COLUMN geo_precision TEXT NOT NULL DEFAULT 'point';
ALTER TABLE incidents ADD COLUMN geo_precision TEXT NOT NULL DEFAULT 'point';
ALTER TABLE incidents ADD COLUMN corroboration_note TEXT;

ALTER TABLE sources ADD COLUMN kind TEXT NOT NULL DEFAULT 'official';
ALTER TABLE sources ADD COLUMN description TEXT NOT NULL DEFAULT '';
ALTER TABLE sources ADD COLUMN attribution TEXT NOT NULL DEFAULT '';
ALTER TABLE sources ADD COLUMN scope TEXT NOT NULL DEFAULT '';
ALTER TABLE sources ADD COLUMN interval_seconds INTEGER NOT NULL DEFAULT 300;
ALTER TABLE sources ADD COLUMN last_attempt TEXT;
ALTER TABLE sources ADD COLUMN records_total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sources ADD COLUMN records_rejected INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sources ADD COLUMN next_poll TEXT;

ALTER TABLE ingestion_runs ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ingestion_runs ADD COLUMN duration_ms INTEGER;

-- One stored copy per syndicated headline/day; NULL keys (structured sources)
-- are excluded so official records keep their own identity.
CREATE UNIQUE INDEX IF NOT EXISTS reports_syndication ON reports(dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS incidents_type_mode_time ON incidents(type, mode, published_at);
CREATE INDEX IF NOT EXISTS reports_source_time ON reports(source, published_at);

-- Worker liveness and service readiness state.
CREATE TABLE IF NOT EXISTS runtime_state(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
