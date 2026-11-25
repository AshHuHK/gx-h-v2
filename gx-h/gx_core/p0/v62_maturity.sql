CREATE TABLE v62_maturity_cursors (
    run_id TEXT PRIMARY KEY REFERENCES v6_goals(run_id),
    event_cursor INTEGER NOT NULL
);
