-- The operator client stages the exact action it submits to this run's main thread.
CREATE TABLE IF NOT EXISTS p0_intent_main (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    thread_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS p0_human_inputs (
    message_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES p0_intent_main(run_id),
    action_json TEXT NOT NULL,
    staged_at TEXT NOT NULL,
    receipt_turn_id TEXT,
    received_at TEXT,
    consumed_at TEXT,
    result_json TEXT
);
CREATE TABLE IF NOT EXISTS p0_intent_changes (
    change_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES p0_intent_main(run_id),
    source_message_id TEXT NOT NULL UNIQUE REFERENCES p0_human_inputs(message_id),
    confirmation_message_id TEXT REFERENCES p0_human_inputs(message_id),
    status TEXT NOT NULL CHECK(status IN ('awaiting_confirmation','committed')),
    preview_json TEXT NOT NULL,
    preview_sha256 TEXT NOT NULL,
    impact_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    committed_at TEXT
);
-- Each version is an immutable change product. Its predecessor remains addressable.
CREATE TABLE IF NOT EXISTS p0_intent_versions (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    item_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version>0),
    item_type TEXT NOT NULL,
    value_json TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','removed')),
    change_id TEXT NOT NULL REFERENCES p0_intent_changes(change_id),
    source_message_id TEXT NOT NULL REFERENCES p0_human_inputs(message_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id,item_id,version)
);
CREATE TABLE IF NOT EXISTS p0_intent_dependencies (
    run_id TEXT NOT NULL,
    object_type TEXT NOT NULL CHECK(object_type IN ('task','job','evaluation','workflow')),
    object_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    item_version INTEGER NOT NULL,
    item_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('execution','evaluation','context')),
    PRIMARY KEY(run_id,object_type,object_id,item_id),
    FOREIGN KEY(run_id,item_id,item_version) REFERENCES p0_intent_versions(run_id,item_id,version)
);
CREATE INDEX IF NOT EXISTS p0_intent_dependents ON p0_intent_dependencies(run_id,item_id);
