-- Durable research delegation. Existing V6 runs and decision tasks acquire no module rows.
CREATE TABLE IF NOT EXISTS v62_modules (
    module_instance_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES v6_goals(run_id),
    scope TEXT NOT NULL UNIQUE,
    branch_id TEXT NOT NULL UNIQUE,
    module_id TEXT NOT NULL,
    module_version TEXT NOT NULL,
    goal_version INTEGER NOT NULL,
    round INTEGER NOT NULL CHECK(round >= 1),
    focus TEXT NOT NULL,
    expected_delivery TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('OPEN','COMPLETE','PARTIAL','CANCELLED','STALE')),
    parent_task_id TEXT NOT NULL REFERENCES v6_decision_tasks(task_id),
    parent_context_ref TEXT NOT NULL REFERENCES research_contexts(context_ref),
    open_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    grant_json TEXT NOT NULL,
    budget_ref TEXT NOT NULL REFERENCES budgets(budget_ref),
    budget_json TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    wake_json TEXT NOT NULL,
    UNIQUE(run_id,open_key)
);
CREATE INDEX IF NOT EXISTS v62_module_round ON v62_modules(run_id,round,status);
CREATE TABLE IF NOT EXISTS v62_module_reports (
    module_instance_id TEXT PRIMARY KEY REFERENCES v62_modules(module_instance_id),
    run_id TEXT NOT NULL REFERENCES v6_goals(run_id),
    version INTEGER NOT NULL DEFAULT 1,
    report_ref TEXT UNIQUE NOT NULL REFERENCES artifacts(artifact_ref),
    operation_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v62_module_wakes (
    run_id TEXT NOT NULL REFERENCES v6_goals(run_id),
    wake_key TEXT NOT NULL,
    round INTEGER NOT NULL,
    event_id INTEGER NOT NULL UNIQUE REFERENCES events(event_id),
    PRIMARY KEY(run_id,wake_key)
);
