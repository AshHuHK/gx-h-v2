-- Only metering attribution. Native child threads have no GX task or lease.
CREATE TABLE IF NOT EXISTS p0_native_child_meters (
  child_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  root_id TEXT NOT NULL,
  parent_id TEXT NOT NULL,
  owner_turn TEXT NOT NULL,
  metadata_verified INTEGER NOT NULL DEFAULT 0,
  awaiting_turn INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS p0_native_child_usage (
  child_id TEXT NOT NULL REFERENCES p0_native_child_meters(child_id),
  turn_id TEXT NOT NULL,
  owner_turn TEXT NOT NULL,
  prior_tokens INTEGER,
  total_tokens INTEGER,
  terminal INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(child_id,turn_id)
);
CREATE INDEX IF NOT EXISTS idx_p0_native_owner_usage ON p0_native_child_usage(owner_turn);
