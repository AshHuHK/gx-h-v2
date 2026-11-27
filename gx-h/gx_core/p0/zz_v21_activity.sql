-- Wait ids are referenced by audit events. Preserve them when widening the reason check.
CREATE TABLE p0_v21_attempt_waits (
  wait_id INTEGER PRIMARY KEY,
  root_key TEXT NOT NULL REFERENCES p0_roots(root_key),
  reason TEXT NOT NULL CHECK (reason IN ('budget','metering','operator_pause','job','provider_backoff','team_capacity','team_paused')),
  wait_key TEXT NOT NULL,
  started_at TEXT NOT NULL,
  turn_id TEXT,
  ready_at TEXT,
  delivery_started_at TEXT,
  ended_at TEXT
);
INSERT INTO p0_v21_attempt_waits SELECT * FROM p0_attempt_waits;
DROP TABLE p0_attempt_waits;
ALTER TABLE p0_v21_attempt_waits RENAME TO p0_attempt_waits;
CREATE UNIQUE INDEX p0_open_attempt_wait
  ON p0_attempt_waits(root_key,reason,wait_key) WHERE ended_at IS NULL;

-- Codex supplies sparse rolling windows. Preserve independent windows and their resets.
CREATE TABLE p0_provider_limits (
  provider TEXT NOT NULL,
  bucket TEXT NOT NULL,
  model TEXT,
  primary_json TEXT,
  secondary_json TEXT,
  blocked_until REAL,
  source TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  PRIMARY KEY(provider,bucket)
);
