-- Trusted operator ingress. A row is durably keyed before Codex sees the text.
CREATE TABLE IF NOT EXISTS p0_operator_messages (
    message_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    action_json TEXT NOT NULL,
    message_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    result_json TEXT,
    rejected TEXT
);
CREATE INDEX IF NOT EXISTS p0_messages_run ON p0_operator_messages(run_id,created_at);

CREATE TABLE IF NOT EXISTS p0_main_replies (
    message_id TEXT PRIMARY KEY REFERENCES p0_operator_messages(message_id),
    turn_id TEXT NOT NULL,
    status TEXT NOT NULL,
    response TEXT NOT NULL
);
