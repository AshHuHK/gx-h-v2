-- Waiting retains a task's attempt. Write authority still belongs to its current lease.
CREATE TABLE IF NOT EXISTS p0_attempt_waits (
  wait_id INTEGER PRIMARY KEY,
  root_key TEXT NOT NULL REFERENCES p0_roots(root_key),
  reason TEXT NOT NULL CHECK (reason IN ('budget','metering','operator_pause','job')),
  wait_key TEXT NOT NULL,
  started_at TEXT NOT NULL,
  turn_id TEXT,
  ready_at TEXT,
  delivery_started_at TEXT,
  ended_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS p0_open_attempt_wait
  ON p0_attempt_waits(root_key,reason,wait_key) WHERE ended_at IS NULL;
