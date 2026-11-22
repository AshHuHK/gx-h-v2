PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- Minimal reference schema only. Reconcile with existing GX tables before migration.
CREATE TABLE IF NOT EXISTS intent_items (
    item_id TEXT PRIMARY KEY,
    item_type TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    value_json TEXT NOT NULL,
    machine_checkable INTEGER NOT NULL DEFAULT 0,
    hard_constraint INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    source_event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS human_input_authorizations (
    authorization_id TEXT PRIMARY KEY,
    client_user_message_id TEXT NOT NULL UNIQUE,
    main_thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    raw_event_ref TEXT NOT NULL,
    verified_human_input INTEGER NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_records (
    change_id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL,
    source_thread_id TEXT NOT NULL,
    source_turn_id TEXT NOT NULL,
    classification TEXT NOT NULL,
    status TEXT NOT NULL,
    preview_json TEXT NOT NULL,
    impact_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    committed_at TEXT
);

CREATE TABLE IF NOT EXISTS change_item_versions (
    change_id TEXT NOT NULL REFERENCES change_records(change_id),
    item_id TEXT NOT NULL REFERENCES intent_items(item_id),
    old_version INTEGER,
    new_version INTEGER,
    operation TEXT NOT NULL,
    old_value_json TEXT,
    new_value_json TEXT,
    PRIMARY KEY(change_id, item_id)
);

CREATE TABLE IF NOT EXISTS object_intent_dependencies (
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    item_id TEXT NOT NULL REFERENCES intent_items(item_id),
    item_version INTEGER NOT NULL,
    item_hash TEXT NOT NULL,
    dependency_role TEXT NOT NULL,
    PRIMARY KEY(object_type, object_id, item_id)
);

CREATE TABLE IF NOT EXISTS outbox_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending
ON outbox_events(status, next_attempt_at, created_at);

CREATE TABLE IF NOT EXISTS durable_tasks (
    task_id TEXT PRIMARY KEY,
    objective_id TEXT,
    path_id TEXT,
    task_type TEXT NOT NULL,
    task_spec_json TEXT NOT NULL,
    status TEXT NOT NULL,
    acceptance_spec_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES durable_tasks(task_id),
    attempt_no INTEGER NOT NULL,
    thread_id TEXT,
    turn_id TEXT,
    lease_token TEXT NOT NULL,
    status TEXT NOT NULL,
    wake_packet_hash TEXT,
    intent_snapshot_json TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    UNIQUE(task_id, attempt_no)
);

CREATE INDEX IF NOT EXISTS idx_attempt_thread ON task_attempts(thread_id);

CREATE TABLE IF NOT EXISTS compute_jobs (
    job_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES durable_tasks(task_id),
    attempt_id TEXT NOT NULL REFERENCES task_attempts(attempt_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    backend TEXT NOT NULL,
    backend_job_id TEXT,
    status TEXT NOT NULL,
    execution_manifest_json TEXT NOT NULL,
    submitted_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    uri TEXT NOT NULL,
    media_type TEXT,
    manifest_ref TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id TEXT PRIMARY KEY,
    evaluator_id TEXT NOT NULL,
    evaluator_version TEXT NOT NULL,
    frozen_spec_hash TEXT NOT NULL,
    input_artifact_ids_json TEXT NOT NULL,
    output_artifact_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    hypothesis_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('supports','refutes','neutral')),
    evaluation_id TEXT NOT NULL REFERENCES evaluations(evaluation_id),
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    job_id TEXT NOT NULL REFERENCES compute_jobs(job_id),
    validity TEXT NOT NULL CHECK(validity = 'valid_measurement'),
    reviewer_task_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_manifests (
    manifest_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES compute_jobs(job_id),
    manifest_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    reproducibility_level TEXT NOT NULL,
    created_at TEXT NOT NULL
);
