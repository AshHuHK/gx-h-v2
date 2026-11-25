CREATE TABLE p0_v6_native_calls (
    thread_id TEXT NOT NULL REFERENCES p0_roots(thread_id),
    call_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    PRIMARY KEY(thread_id,call_id)
);
