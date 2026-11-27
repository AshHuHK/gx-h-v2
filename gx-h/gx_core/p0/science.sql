-- The claim, frozen evaluation, and immutable original bytes remain in the existing GX tables.
CREATE TABLE IF NOT EXISTS science_settlements (
    settlement_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    round_index INTEGER,
    path_id TEXT,
    hypothesis_id TEXT REFERENCES hypotheses(hypothesis_id),
    task_id TEXT REFERENCES tasks(task_id),
    attempt INTEGER,
    submission_id TEXT REFERENCES submissions(submission_id),
    job_id TEXT REFERENCES jobs(job_id),
    kind TEXT NOT NULL CHECK(kind IN ('infrastructure_failure','agent_execution_failure','measurement_failure','valid_scientific_outcome')),
    reason TEXT NOT NULL,
    eval_id TEXT REFERENCES evaluations(eval_id),
    artifact_ref TEXT REFERENCES artifacts(artifact_ref),
    manifest_hash TEXT,
    evidence_id TEXT UNIQUE REFERENCES evidence(evidence_id),
    direction TEXT CHECK(direction IN ('supports','refutes')),
    created_at TEXT NOT NULL,
    CHECK((kind='valid_scientific_outcome') = (evidence_id IS NOT NULL)),
    CHECK(evidence_id IS NULL OR
        (eval_id IS NOT NULL AND artifact_ref IS NOT NULL AND manifest_hash IS NOT NULL
         AND hypothesis_id IS NOT NULL AND submission_id IS NOT NULL AND direction IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_science_settlement_run ON science_settlements(run_id,round_index);

CREATE TABLE IF NOT EXISTS science_decisions (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    round_index INTEGER NOT NULL,
    submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(submission_id),
    reviewer_submission_id TEXT NOT NULL REFERENCES submissions(submission_id),
    decision_ref TEXT NOT NULL REFERENCES artifacts(artifact_ref),
    actions_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    stop_reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, round_index)
);

CREATE TRIGGER IF NOT EXISTS science_submission_rejected_on_insert AFTER INSERT ON submissions
WHEN NEW.result='REJECTED'
BEGIN
    INSERT OR IGNORE INTO science_settlements
        (settlement_id,run_id,task_id,attempt,submission_id,kind,reason,created_at)
    SELECT 'submission:'||NEW.submission_id,t.run_id,t.task_id,NEW.attempt,NEW.submission_id,
           'agent_execution_failure',NEW.validation_json,NEW.created_at FROM tasks t
    JOIN runs r USING(run_id) WHERE t.task_id=NEW.task_id AND json_extract(r.inputs_json,'$.inputs.p0')=1;
END;
CREATE TRIGGER IF NOT EXISTS science_submission_rejected_on_update AFTER UPDATE OF result ON submissions
WHEN NEW.result='REJECTED' AND OLD.result!='REJECTED'
BEGIN
    INSERT OR IGNORE INTO science_settlements
        (settlement_id,run_id,task_id,attempt,submission_id,kind,reason,created_at)
    SELECT 'submission:'||NEW.submission_id,t.run_id,t.task_id,NEW.attempt,NEW.submission_id,
           'agent_execution_failure',NEW.validation_json,NEW.created_at FROM tasks t
    JOIN runs r USING(run_id) WHERE t.task_id=NEW.task_id AND json_extract(r.inputs_json,'$.inputs.p0')=1;
END;
