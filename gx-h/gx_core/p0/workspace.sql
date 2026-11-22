-- A state directory contains one project. Existing v2 single runs can be read in place.
CREATE TABLE IF NOT EXISTS p0_workspace (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
    workspace_id TEXT NOT NULL UNIQUE,
    project_ref TEXT NOT NULL UNIQUE,
    budget_ref TEXT NOT NULL UNIQUE REFERENCES budgets(budget_ref),
    intent_run_id TEXT NOT NULL REFERENCES runs(run_id),
    max_parallel_roots INTEGER NOT NULL DEFAULT 10 CHECK(max_parallel_roots BETWEEN 1 AND 10)
);
CREATE TABLE IF NOT EXISTS p0_workspace_runs (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    run_key TEXT UNIQUE,
    workspace_id TEXT NOT NULL REFERENCES p0_workspace(workspace_id)
);
