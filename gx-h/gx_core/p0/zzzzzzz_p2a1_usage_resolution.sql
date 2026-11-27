ALTER TABLE p0_meter_turns ADD COLUMN unresolved_at TEXT;
UPDATE p0_meter_turns SET unresolved_at=coalesce(terminal_at,
  (SELECT coalesce(settled_at,created_at) FROM budget_reservations b WHERE b.reservation_ref=p0_meter_turns.reservation_ref))
WHERE confidence IN ('UNRESOLVED','HISTORICAL_SETTLED');

CREATE TABLE p0_usage_releases (
  reservation_ref TEXT PRIMARY KEY REFERENCES p0_meter_turns,
  operator_event_id INTEGER UNIQUE REFERENCES events,
  known_tokens INTEGER NOT NULL,
  known_api_calls INTEGER NOT NULL,
  confirmation_event_id INTEGER UNIQUE REFERENCES events
);
CREATE TABLE p0_usage_host_histories (
  thread_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs,
  root_id TEXT NOT NULL,
  path TEXT NOT NULL
);
DROP VIEW p0_meter_native_turns;
CREATE VIEW p0_meter_native_turns AS
SELECT d.run_id,d.thread_id,d.turn_id,d.event_id,d.state,d.prior_tokens,d.token_budget,
       b.reservation_ref,b.budget_ref,b.rowid AS sequence,b.status AS budget_status,
       u.terminal_status,u.total_tokens,u.measured_tokens,u.children_known,u.metering_reason,
       m.terminal_at,m.confidence,m.settled_tokens,m.observed_lower_bound,m.estimate_snapshot,
       m.attribution,m.estimated_at
FROM p0_root_deliveries d JOIN budget_reservations b USING(reservation_ref)
LEFT JOIN p0_root_turn_usage u USING(thread_id,turn_id) LEFT JOIN p0_meter_turns m USING(reservation_ref)
WHERE d.method='turn/start' AND (b.status IN ('HELD','SETTLED') OR
      EXISTS (SELECT 1 FROM p0_usage_releases x WHERE x.reservation_ref=b.reservation_ref));
