-- Immutable compiled work plus its original execution or verified reuse provenance.
CREATE TABLE v6_job_attempts (
    node_id TEXT PRIMARY KEY REFERENCES v6_nodes(node_id),
    run_id TEXT NOT NULL REFERENCES v6_goals(run_id),
    operation_key TEXT NOT NULL UNIQUE,
    plan_key TEXT NOT NULL,
    exec_key TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    request_json TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    reused_from TEXT REFERENCES v6_nodes(node_id)
);
CREATE INDEX v6_attempts_equivalent ON v6_job_attempts(run_id,exec_key);
CREATE TABLE v6_node_waits (
    node_id TEXT PRIMARY KEY REFERENCES v6_nodes(node_id),
    wait_json TEXT NOT NULL
);
