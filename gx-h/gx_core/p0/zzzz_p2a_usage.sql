-- An exact late Turn receipt supersedes the original funding estimate without changing it.
CREATE TABLE p0_meter_receipt_adjustments (
  reservation_ref TEXT PRIMARY KEY REFERENCES p0_meter_turns,
  settlement_event_id INTEGER NOT NULL REFERENCES events(event_id),
  event_id INTEGER NOT NULL UNIQUE REFERENCES events(event_id),
  confirmed_tokens INTEGER NOT NULL CHECK(confirmed_tokens >= 0),
  delta INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

-- A new keyed receipt after terminal can complete a previously incomplete interval.
CREATE TABLE p0_meter_receipt_intervals (
  thread_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  late_receipt INTEGER NOT NULL DEFAULT 0,
  disputed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(thread_id, turn_id)
);
