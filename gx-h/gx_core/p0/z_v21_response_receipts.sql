CREATE TABLE p0_meter_responses (
  thread_id TEXT NOT NULL,
  response_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  tokens INTEGER,
  conflicting INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(thread_id,response_id)
);
CREATE INDEX p0_meter_responses_by_turn ON p0_meter_responses(thread_id,turn_id);
