CREATE TABLE IF NOT EXISTS p0_roots (
  root_key TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  task_id TEXT REFERENCES tasks(task_id),
  attempt INTEGER,
  lease_id TEXT UNIQUE REFERENCES leases(lease_id),
  thread_id TEXT UNIQUE,
  cwd TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL,
  model TEXT NOT NULL,
  effort TEXT NOT NULL,
  brief TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'prepared',
  wait_job_id TEXT REFERENCES jobs(job_id),
  goal_desired TEXT,
  UNIQUE(task_id,attempt)
);
CREATE UNIQUE INDEX IF NOT EXISTS p0_main_root ON p0_roots(run_id) WHERE task_id IS NULL;
CREATE TABLE IF NOT EXISTS p0_root_calls (
  thread_id TEXT NOT NULL REFERENCES p0_roots(thread_id),
  call_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  request_sha TEXT NOT NULL,
  result_json TEXT NOT NULL,
  PRIMARY KEY(thread_id,call_id)
);
CREATE TABLE IF NOT EXISTS p0_root_artifact_reads (
  root_key TEXT NOT NULL REFERENCES p0_roots(root_key),
  artifact_ref TEXT NOT NULL REFERENCES artifacts(artifact_ref),
  PRIMARY KEY(root_key,artifact_ref)
);
