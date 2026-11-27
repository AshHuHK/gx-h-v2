-- An unadmitted proposal has no Job row or charge. Outbox carries it to the existing JobService.
CREATE TABLE IF NOT EXISTS p0_job_proposals (
    job_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    root_key TEXT NOT NULL REFERENCES p0_roots(root_key),
    operation_key TEXT NOT NULL,
    request_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('PROPOSED','ADMITTED','REJECTED','CANCELLED')),
    error_json TEXT,
    wait_turn TEXT,
    wait_started TEXT,
    wait_ended TEXT,
    UNIQUE(root_key,operation_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS p0_one_waiting_proposal ON p0_job_proposals(root_key)
    WHERE wait_turn IS NOT NULL AND wait_ended IS NULL;
