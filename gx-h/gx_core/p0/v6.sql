-- A Goal selects its capabilities before the Lead selects a scientific workflow.
CREATE TABLE v6_goals (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    goal TEXT NOT NULL,
    goal_version INTEGER NOT NULL DEFAULT 1,
    capabilities_json TEXT NOT NULL,
    materials_json TEXT NOT NULL,
    limits_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    stop_decision_ref TEXT REFERENCES artifacts(artifact_ref),
    stop_op_digest TEXT
);
CREATE TABLE v6_decision_tasks (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    run_id TEXT NOT NULL REFERENCES v6_goals(run_id),
    scope TEXT NOT NULL,
    context_ref TEXT NOT NULL REFERENCES research_contexts(context_ref),
    goal_version INTEGER NOT NULL,
    base_json TEXT,
    visible_refs_json TEXT NOT NULL,
    event_key TEXT NOT NULL,
    UNIQUE(run_id,scope,event_key)
);
CREATE TABLE v6_graph_versions (
    run_id TEXT NOT NULL REFERENCES v6_goals(run_id),
    version INTEGER NOT NULL CHECK(version>=1),
    sha256 TEXT NOT NULL,
    parent_sha TEXT,
    graph_json TEXT NOT NULL,
    decision_ref TEXT NOT NULL REFERENCES artifacts(artifact_ref),
    PRIMARY KEY(run_id,version)
);
CREATE TABLE v6_decisions (
    decision_ref TEXT PRIMARY KEY REFERENCES artifacts(artifact_ref),
    run_id TEXT NOT NULL REFERENCES v6_goals(run_id),
    task_id TEXT NOT NULL UNIQUE REFERENCES v6_decision_tasks(task_id),
    context_ref TEXT NOT NULL REFERENCES research_contexts(context_ref),
    lease_id TEXT NOT NULL REFERENCES leases(lease_id),
    thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    UNIQUE(thread_id,call_id)
);
CREATE INDEX v6_decisions_run ON v6_decisions(run_id);
CREATE TABLE v6_branches (
    branch_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES v6_goals(run_id),
    decision_ref TEXT NOT NULL REFERENCES artifacts(artifact_ref),
    spec_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    stop_decision_ref TEXT REFERENCES artifacts(artifact_ref),
    stop_op_digest TEXT
);
CREATE INDEX v6_branches_run ON v6_branches(run_id);
CREATE TABLE v6_nodes (
    node_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES v6_goals(run_id),
    branch_id TEXT NOT NULL REFERENCES v6_branches(branch_id),
    version INTEGER NOT NULL,
    decision_ref TEXT NOT NULL REFERENCES artifacts(artifact_ref),
    spec_json TEXT NOT NULL,
    operation_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'PENDING',
    job_id TEXT REFERENCES jobs(job_id),
    result_ref TEXT REFERENCES artifacts(artifact_ref),
    exec_key TEXT,
    FOREIGN KEY(run_id,version) REFERENCES v6_graph_versions(run_id,version)
);
CREATE INDEX v6_nodes_dispatch ON v6_nodes(run_id,status);
CREATE TRIGGER v6_graph_no_update BEFORE UPDATE ON v6_graph_versions
BEGIN SELECT RAISE(ABORT,'graph revisions are immutable'); END;
CREATE TRIGGER v6_graph_no_delete BEFORE DELETE ON v6_graph_versions
BEGIN SELECT RAISE(ABORT,'graph revisions are immutable'); END;
CREATE TRIGGER v6_decision_no_update BEFORE UPDATE ON v6_decisions
BEGIN SELECT RAISE(ABORT,'decision receipts are immutable'); END;
CREATE TRIGGER v6_decision_no_delete BEFORE DELETE ON v6_decisions
BEGIN SELECT RAISE(ABORT,'decision receipts are immutable'); END;
