-- Observational data only. Reservations and settlement use the existing meter tables.
CREATE TABLE p0_response_attribution (
    run_id TEXT NOT NULL,
    root_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    response_id TEXT NOT NULL,
    request_ordinal INTEGER,
    observed_at TEXT NOT NULL,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    total_tokens INTEGER,
    conflicting INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(thread_id,response_id)
);
CREATE INDEX p0_attribution_by_run ON p0_response_attribution(run_id);

CREATE TABLE p0_response_tools (
    run_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    source TEXT NOT NULL,
    tool TEXT NOT NULL,
    namespace TEXT,
    model_call_id TEXT,
    response_id TEXT,
    link TEXT,
    outcome TEXT,
    PRIMARY KEY(thread_id,turn_id,source,call_id)
);
CREATE INDEX p0_response_tools_by_run ON p0_response_tools(run_id);
