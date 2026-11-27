CREATE INDEX IF NOT EXISTS p0_recent_method_jobs ON jobs(capability_id,capability_version,finished_at)
WHERE status IN ('SUCCEEDED','FAILED');
