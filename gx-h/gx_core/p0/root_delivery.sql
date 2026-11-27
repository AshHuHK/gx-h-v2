-- A committed send without a receipt is ambiguous until Codex confirms its input.
CREATE TABLE IF NOT EXISTS p0_root_deliveries (
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  event_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,
  human INTEGER NOT NULL,
  interrupt INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('ready','sending','acknowledged')),
  method TEXT,
  target_turn TEXT,
  turn_id TEXT,
  reservation_ref TEXT REFERENCES budget_reservations(reservation_ref),
  token_budget INTEGER,
  prior_tokens INTEGER,
  PRIMARY KEY(run_id,event_id),
  UNIQUE(thread_id,client_id)
);
CREATE INDEX IF NOT EXISTS idx_p0_thread_deliveries ON p0_root_deliveries(thread_id,state);

-- Only official notifications/history can populate a turn's observed state.
CREATE TABLE IF NOT EXISTS p0_root_turn_usage (
  thread_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  terminal_status TEXT,
  total_tokens INTEGER,
  PRIMARY KEY(thread_id,turn_id)
);
