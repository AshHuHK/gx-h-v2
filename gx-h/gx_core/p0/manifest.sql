CREATE TABLE IF NOT EXISTS p0_execution_manifests (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    admission_json TEXT NOT NULL,
    admission_sha256 TEXT NOT NULL,
    completion_json TEXT,
    completion_sha256 TEXT,
    CHECK ((completion_json IS NULL) = (completion_sha256 IS NULL))
);
