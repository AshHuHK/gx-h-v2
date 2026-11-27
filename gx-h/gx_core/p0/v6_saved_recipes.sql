CREATE TABLE IF NOT EXISTS v6_saved_recipes (
    reference TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    source_run TEXT NOT NULL REFERENCES v6_goals(run_id),
    sources_json TEXT NOT NULL
);
