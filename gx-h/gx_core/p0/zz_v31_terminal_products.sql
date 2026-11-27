-- A Root identifies one leased attempt. Failed calls and terminal delivery each have
-- one identity across transport replay and driver restart.
CREATE TABLE p0_product_repairs (
    thread_id TEXT NOT NULL REFERENCES p0_roots(thread_id),
    call_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    request_sha TEXT NOT NULL,
    category TEXT NOT NULL,
    contract_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (thread_id, call_id)
);
CREATE TABLE p0_root_terminals (
    thread_id TEXT PRIMARY KEY REFERENCES p0_roots(thread_id),
    turn_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('GX_ACCEPTED','GX_TERMINAL_REJECTED','GX_REPAIR_LIMIT')),
    recorded_at TEXT NOT NULL,
    goal_checked_at TEXT,
    interrupt_ack_at TEXT,
    goal_last_error TEXT,
    interrupt_last_error TEXT,
    interrupt_attempts INTEGER NOT NULL DEFAULT 0
);
