CREATE TABLE IF NOT EXISTS p0_teams (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    team_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active','paused')),
    budget_ref TEXT NOT NULL UNIQUE REFERENCES budgets(budget_ref),
    max_parallel_roots INTEGER NOT NULL CHECK(max_parallel_roots>0),
    roles_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id,team_id)
);
CREATE TABLE IF NOT EXISTS p0_task_teams (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    team_id TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    assignment_source TEXT NOT NULL,
    FOREIGN KEY (run_id, team_id) REFERENCES p0_teams(run_id,team_id)
);
CREATE INDEX IF NOT EXISTS p0_tasks_by_team ON p0_task_teams(run_id,team_id);
