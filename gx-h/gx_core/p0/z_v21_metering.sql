-- Each cumulative count belongs to one official thread. A native child has no GX call charge.
CREATE TABLE p0_meter_threads (
  thread_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs,
  root_id TEXT NOT NULL,
  observed_total INTEGER,
  observed_at TEXT,
  observation_source TEXT,
  reconciled_total INTEGER,
  confidence TEXT NOT NULL DEFAULT 'OBSERVED',
  anomaly_total INTEGER,
  anomaly_at TEXT
);
CREATE INDEX p0_meter_thread_owner ON p0_meter_threads(run_id,root_id);

CREATE TABLE p0_meter_turns (
  reservation_ref TEXT PRIMARY KEY REFERENCES budget_reservations,
  terminal_at TEXT,
  confidence TEXT NOT NULL DEFAULT 'RESERVED',
  settled_tokens INTEGER,
  observed_lower_bound INTEGER NOT NULL DEFAULT 0,
  estimate_snapshot INTEGER,
  attribution TEXT,
  estimated_at TEXT
);
CREATE TABLE p0_meter_adjustments (
  adjustment_id INTEGER PRIMARY KEY,
  reservation_ref TEXT NOT NULL REFERENCES p0_meter_turns,
  through_reservation TEXT NOT NULL REFERENCES p0_meter_turns,
  root_id TEXT NOT NULL,
  reported_total INTEGER NOT NULL,
  delta INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(through_reservation,reported_total,reservation_ref)
);
CREATE TABLE p0_meter_interrupts (
  thread_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES runs,
  reason TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  sent_at TEXT,
  terminal_status TEXT,
  ended_at TEXT,
  PRIMARY KEY(thread_id,turn_id)
);
-- Keep historical charges immutable. The original settlement receipt includes descendants.
INSERT INTO p0_meter_turns(reservation_ref,terminal_at,confidence,settled_tokens,attribution)
SELECT b.reservation_ref,CASE WHEN u.terminal_status IS NOT NULL THEN b.settled_at END,
       CASE b.status WHEN 'SETTLED' THEN 'PROVIDER_CONFIRMED' ELSE 'RESERVED' END,
       CASE WHEN b.status='SETTLED' THEN json_extract(e.payload_json,'$.actual.tokens') END,
       CASE WHEN b.status='SETTLED' THEN 'HISTORICAL_TURN' END
FROM p0_root_deliveries d JOIN budget_reservations b USING(reservation_ref)
LEFT JOIN p0_root_turn_usage u USING(thread_id,turn_id)
LEFT JOIN events e ON e.kind='budget.settled' AND e.subject_id=b.budget_ref
                    AND json_extract(e.payload_json,'$.reservation_ref')=b.reservation_ref
WHERE d.method='turn/start' AND b.status IN ('HELD','SETTLED');
INSERT INTO p0_meter_threads(thread_id,run_id,root_id,observed_total,observation_source)
SELECT d.thread_id,d.run_id,d.thread_id,max(u.total_tokens),'historical cumulative'
FROM p0_root_deliveries d JOIN p0_root_turn_usage u USING(thread_id,turn_id)
WHERE d.method='turn/start' GROUP BY d.thread_id,d.run_id;
INSERT INTO p0_meter_threads(thread_id,run_id,root_id,observed_total,observation_source)
SELECT m.child_id,m.run_id,m.root_id,max(u.total_tokens),'historical child cumulative'
FROM p0_native_child_meters m JOIN p0_native_child_usage u USING(child_id)
WHERE m.metadata_verified=1 GROUP BY m.child_id,m.run_id,m.root_id;

CREATE VIEW p0_meter_native_turns AS
SELECT d.run_id,d.thread_id,d.turn_id,d.event_id,d.state,d.prior_tokens,d.token_budget,
       b.reservation_ref,b.budget_ref,b.rowid AS sequence,b.status AS budget_status,
       u.terminal_status,u.total_tokens,u.measured_tokens,u.children_known,u.metering_reason,
       m.terminal_at,m.confidence,m.settled_tokens,m.observed_lower_bound,m.estimate_snapshot,
       m.attribution,m.estimated_at
FROM p0_root_deliveries d JOIN budget_reservations b USING(reservation_ref)
LEFT JOIN p0_root_turn_usage u USING(thread_id,turn_id) LEFT JOIN p0_meter_turns m USING(reservation_ref)
WHERE d.method='turn/start' AND b.status IN ('HELD','SETTLED');
