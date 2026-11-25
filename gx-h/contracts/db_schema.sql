-- gxdesign-harness authoritative schema, v2 (2026-11-12). SQLite >= 3.35 (WAL).
-- The ONLY authoritative store for run/path/task/job/artifact/evaluation/evidence/episode/capability/asset state.
-- Workflow checkpoints, scientific events, long-term memory, external jobs and client sessions are SEPARATE
-- tables on purpose: one checkpoint is never the whole system state.
--
-- What v2 changes, in one sentence each:
--   * The backend owns bytes: sources, their parsed segments, read receipts and searches are rows.
--   * A gate is met by rows the backend wrote (check_runs, reviews, approvals bound to a content sha256).
--   * One registry and one lifecycle for skills, workflows, adapters, evaluators, benchmarks and strategies.
--   * Identity comes from the registry: method cards, resource needs and equivalences are stored, not typed in.
--   * Who did the work, on which model and at what effort, is recorded with the level it was established at.
--   * A run pins the contract, the workflow definition and the agent policy it started under, by sha256.
-- A v1 store is not migrated: v1 never created its default store, and its three slice stores are kept as they are.
-- schema_meta holds contract_version and contract_sha256. A store written under another major version refuses
-- to open without a named migration; v1 restamped any store with the current digest on open.
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ---------- runs and objectives ----------
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  project_ref TEXT NOT NULL,
  objective TEXT NOT NULL,
  objective_id TEXT,
  status TEXT NOT NULL,
  -- for a stage workflow, the name of the open stage
  phase TEXT,
  revision INTEGER NOT NULL DEFAULT 0,
  control_epoch INTEGER NOT NULL DEFAULT 0,
  workflow_id TEXT,
  workflow_version TEXT,
  budget_ref TEXT,
  budget_json TEXT NOT NULL DEFAULT '{}',
  constraints_json TEXT NOT NULL DEFAULT '{}',
  agent_policy_ref TEXT,
  agent_policy_json TEXT NOT NULL DEFAULT '{}',
  interrupt_ref TEXT,
  -- the pins: resume compares each with what is installed and refuses on a difference
  contract_version INTEGER,
  contract_sha256 TEXT,
  workflow_sha256 TEXT,
  agent_policy_sha256 TEXT,
  inputs_json TEXT NOT NULL DEFAULT '{}',
  -- the host capability record taken when the run started: kind, version, delegation, hooks, what was probed
  host_json TEXT NOT NULL DEFAULT '{}',
  -- why the run is PAUSED and the point it continues from; NULL unless paused
  paused_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- A pause followed by a resume still invalidates work prepared before the pause.
CREATE TRIGGER IF NOT EXISTS advance_run_control_epoch
AFTER UPDATE OF status ON runs
WHEN NEW.status != OLD.status AND (NEW.status IN ('PAUSED','CANCELLED') OR OLD.status='PAUSED')
BEGIN
  UPDATE runs SET control_epoch=control_epoch+1 WHERE run_id=NEW.run_id;
END;

-- The structured objective, its screen and its confirmation. Expensive work is not derived from
-- an unconfirmed objective.
CREATE TABLE IF NOT EXISTS objectives (
  objective_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  screen_json TEXT NOT NULL DEFAULT '{}',
  approval_id TEXT,
  -- the evaluation specification the objective froze first: only evaluations under it (or its
  -- STOP_RANKING successor after a full re-score) support a layer-3 claim about the objective
  eval_spec_ref TEXT,
  eval_spec_sha256 TEXT,
  -- what a contrast objective compares: [{capability_version?, parameters?}]; [] when it names none
  variants_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Exactly one effective coordinator lease per run (cross-host hand-off).
CREATE TABLE IF NOT EXISTS coordinator_leases (
  run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
  lease_id TEXT NOT NULL,
  host TEXT NOT NULL,
  owner TEXT NOT NULL,
  session_ref TEXT,
  issued_at TEXT NOT NULL,
  renewed_at TEXT,
  expires_at TEXT NOT NULL,
  released_at TEXT,
  handoff_reason TEXT
);

-- Append-only. Never updated, never deleted.
CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  subject_kind TEXT,
  subject_id TEXT,
  prev_state TEXT,
  new_state TEXT,
  prev_revision INTEGER,
  new_revision INTEGER,
  reason TEXT,
  evidence_level TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, event_id);
CREATE INDEX IF NOT EXISTS idx_events_subject ON events(subject_kind, subject_id);

-- ---------- candidate paths ----------
-- identity_json is what the backend computed from method cards; claims_json is what the planner wrote.
-- The fingerprint is computed from capability versions, resolved parameters, the input manifest's content
-- sha256 and the eval spec's frozen sha256, and from no free text.
CREATE TABLE IF NOT EXISTS paths (
  path_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  version INTEGER NOT NULL DEFAULT 1,
  parent_path_id TEXT,
  status TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  hypothesis TEXT NOT NULL,
  method_family TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  identity_json TEXT NOT NULL DEFAULT '{}',
  claims_json TEXT NOT NULL DEFAULT '{}',
  relationship_json TEXT NOT NULL DEFAULT '{}',
  capability_refs_json TEXT NOT NULL DEFAULT '[]',
  skill_refs_json TEXT NOT NULL DEFAULT '[]',
  evidence_refs_json TEXT NOT NULL DEFAULT '[]',
  input_manifest_sha256 TEXT,
  eval_spec_ref TEXT,
  eval_spec_sha256 TEXT,
  budget_json TEXT NOT NULL DEFAULT '{}',
  reservation_ref TEXT,
  artifact_namespace TEXT NOT NULL,
  selection_reason TEXT,
  -- the registered prediction (bundles.json Prediction) and its outcome (labels.prediction_outcome),
  -- computed by the backend under the frozen specification; NULL when none was registered or computed
  prediction_json TEXT,
  prediction_outcome TEXT,
  -- funding at the selection wave: the checked estimate, and the reservation or the refusal reason
  funding_json TEXT NOT NULL DEFAULT '{}',
  -- what blocks the path, as its route recorded it: an environment, a repair or onboarding task, or
  -- an input; NULL when nothing does. Selection refuses the path while the blocker is open
  blocker_json TEXT,
  -- the execution chains of the path that ended: [{class, count}]
  ended_chains_json TEXT NOT NULL DEFAULT '[]',
  -- what a path at reduced scale leaves out (bundles.json ScopeReduction); NULL for a full-scale path
  scope_reduction_json TEXT,
  -- the outcome under the successor of a specification that had an evaluator_bug:
  -- {eval_spec_ref, sha256, verdict, eval_ids}; NULL when the path was not re-scored
  rescored_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id, fingerprint, version)
);
CREATE INDEX IF NOT EXISTS idx_paths_run ON paths(run_id, status);

-- Written only by the backend, from identity-control evaluations. A planner cannot pass one in.
CREATE TABLE IF NOT EXISTS method_equivalences (
  capability_a TEXT NOT NULL,
  capability_b TEXT NOT NULL,
  relation TEXT NOT NULL,
  basis_eval_ids_json TEXT NOT NULL DEFAULT '[]',
  conditions TEXT,
  measured_at TEXT NOT NULL,
  PRIMARY KEY (capability_a, capability_b)
);

-- ---------- team tasks ----------
-- brief_ref is the compiled brief the worker reads; bundle_kind and acceptance_json say what comes back
-- and how it is judged. A review task carries the subject sha256 it was created for and the authoring
-- task it must be independent of.
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  path_id TEXT REFERENCES paths(path_id) ON DELETE SET NULL,
  kind TEXT NOT NULL,
  role TEXT NOT NULL,
  task_class TEXT,
  status TEXT NOT NULL,
  -- attempt numbers leases, briefs and submissions and is never reused; attempts_consumed is what
  -- max_attempts bounds: a host-caused lapse or stop, a hold and a submission refused for a reason
  -- that cannot change within the lease consume none
  attempt INTEGER NOT NULL DEFAULT 0,
  attempts_consumed INTEGER NOT NULL DEFAULT 0,
  required_artifacts_json TEXT NOT NULL DEFAULT '[]',
  bundle_kind TEXT,
  acceptance_json TEXT NOT NULL DEFAULT '{}',
  input_json TEXT NOT NULL DEFAULT '{}',
  -- the brief compiled for the current attempt; recompiled, with the refusal, when the task is claimed again
  brief_ref TEXT,
  brief_sha256 TEXT,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  parallel_group TEXT,
  -- the stage (engine node) that created it; its round is derived from the workflow.reentered events
  stage TEXT,
  subject_kind TEXT,
  subject_id TEXT,
  subject_sha256 TEXT,
  independent_of_task_id TEXT,
  -- what the agent policy requires of whoever does this task
  required_model TEXT,
  required_effort_json TEXT NOT NULL DEFAULT '[]',
  -- why the task is held (BLOCKED, awaiting an approval): the reason, a labels.hold_reason, and the
  -- approval id
  hold_json TEXT,
  -- why a BLOCKED task that is not held is blocked: a labels.blocked_reason; NULL otherwise
  blocked_reason TEXT,
  budget_ref TEXT,
  owner TEXT,
  lease_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks(run_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_subject ON tasks(subject_kind, subject_id, subject_sha256);

CREATE TABLE IF NOT EXISTS task_deps (
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  depends_on TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  PRIMARY KEY (task_id, depends_on)
);

CREATE TABLE IF NOT EXISTS leases (
  lease_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  owner TEXT NOT NULL,
  principal TEXT,
  worker_role TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  released_at TEXT,
  attempt INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leases_task ON leases(task_id, released_at);

-- GX observations of a host worker. NOT a mirror of any host's native enum.
-- What the worker said about itself (reported_*) and what a host adapter read from a record the host
-- wrote (observed_*) are separate columns and are never merged: a model's statement about itself is
-- CLIENT_REPORTED, and v1 dropped even that at the schema.
CREATE TABLE IF NOT EXISTS worker_observations (
  worker_observation_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  lease_id TEXT,
  host TEXT NOT NULL,
  host_session_ref TEXT,
  observed_worker_ref TEXT,
  agent_ref TEXT,
  state TEXT NOT NULL,
  evidence_level TEXT NOT NULL,
  observation_source TEXT NOT NULL DEFAULT 'client_report',
  delegation_mode TEXT,
  reported_model TEXT,
  reported_effort TEXT,
  observed_model TEXT,
  observed_effort TEXT,
  model_evidence_level TEXT,
  turns_observed INTEGER,
  turns_off_policy INTEGER,
  transcript_sha256 TEXT,
  host_version TEXT,
  policy_verdict TEXT,
  observed_at TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_wobs_task ON worker_observations(task_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_wobs_agent ON worker_observations(host, agent_ref);

-- Events the host itself raised, as its hooks delivered them: a worker starting or stopping, a model
-- switch, a spawn about to happen. Identifiers and settings only, never message content. Append-only.
CREATE TABLE IF NOT EXISTS host_events (
  event_id TEXT PRIMARY KEY,
  run_id TEXT,
  host TEXT NOT NULL,
  hook_event TEXT NOT NULL,
  session_ref TEXT NOT NULL,
  agent_ref TEXT,
  model TEXT,
  effort TEXT,
  source TEXT NOT NULL,
  decision TEXT,
  reason TEXT,
  matched_task_id TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_host_events_session ON host_events(host, session_ref, received_at);
CREATE INDEX IF NOT EXISTS idx_host_events_agent ON host_events(host, agent_ref);

-- One row per gx_task_submit: what was handed in and how each record was judged.
CREATE TABLE IF NOT EXISTS submissions (
  submission_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  lease_id TEXT,
  attempt INTEGER NOT NULL DEFAULT 1,
  -- the brief this submission answered, and a sha256 over what was handed in: an approval that
  -- releases a held submission is bound to that sha256
  brief_sha256 TEXT,
  content_sha256 TEXT,
  worker_observation_id TEXT,
  policy_verdict TEXT,
  bundle_kind TEXT,
  artifact_refs_json TEXT NOT NULL DEFAULT '[]',
  accepted_count INTEGER NOT NULL DEFAULT 0,
  rejected_count INTEGER NOT NULL DEFAULT 0,
  result TEXT NOT NULL,
  validation_json TEXT NOT NULL DEFAULT '{}',
  records_artifact_ref TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_submissions_task ON submissions(task_id, created_at);

-- ---------- environments and capabilities ----------
CREATE TABLE IF NOT EXISTS environments (
  env_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  python_executable TEXT,
  binary_paths_json TEXT NOT NULL DEFAULT '{}',
  container_digest TEXT,
  execution_host TEXT,
  lockfile_ref TEXT,
  env_var_names_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL,
  probe_json TEXT NOT NULL DEFAULT '{}',
  probed_at TEXT,
  readiness_basis TEXT,
  dead_entrypoints_json TEXT NOT NULL DEFAULT '[]',
  limitations TEXT,
  created_at TEXT,
  updated_at TEXT
);

-- A capability version is a TOOL_ADAPTER asset: its status is the asset's, moved only by the one
-- promotion function. manifest_sha256 makes a version immutable. entry_json holds the launch recipe
-- (argv template starting from the environment's interpreter, cwd, pythonpath, fixed environment values).
-- method_card_json is the only source of method identity; resource_requirements_json is the only source
-- of GPU need, scope, memory and wall time.
CREATE TABLE IF NOT EXISTS capabilities (
  capability_id TEXT NOT NULL,
  version TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  manifest_sha256 TEXT,
  env_id TEXT REFERENCES environments(env_id),
  adapter_id TEXT,
  source_json TEXT NOT NULL DEFAULT '{}',
  weights_json TEXT NOT NULL DEFAULT '{}',
  required_data_json TEXT NOT NULL DEFAULT '[]',
  argv_template_json TEXT NOT NULL DEFAULT '[]',
  entry_json TEXT NOT NULL DEFAULT '{}',
  cwd TEXT,
  method_card_json TEXT NOT NULL DEFAULT '{}',
  resource_requirements_json TEXT NOT NULL DEFAULT '{}',
  outputs_json TEXT NOT NULL DEFAULT '[]',
  license TEXT,
  output_schema_ref TEXT,
  probe_rung TEXT,
  test_status TEXT NOT NULL DEFAULT 'STATIC_REVIEW_ONLY',
  evidence_refs_json TEXT NOT NULL DEFAULT '[]',
  limitations TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (capability_id, version)
);

-- Each rung of the probe ladder as an observation. test_status is derived from the highest rung
-- that has a job id behind it.
CREATE TABLE IF NOT EXISTS probe_records (
  probe_id TEXT PRIMARY KEY,
  subject_kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  rung TEXT NOT NULL,
  result TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  job_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_probe_subject ON probe_records(subject_kind, subject_id, created_at);

-- ---------- jobs ----------
-- A detached runner owns the tool. runner_pid with runner_started is liveness that pid reuse cannot fool.
-- measured_json is what the job is charged for. idempotency_key is derived by the backend from (run, path,
-- step_index, input-manifest sha256, job_attempt); job_attempt advances only once the step's previous job
-- is terminal and reconciled. lease_ref is the task lease the job was submitted under, and step_index
-- the step of the path's method sequence it runs.
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  path_id TEXT,
  task_id TEXT,
  lease_ref TEXT,
  step_index INTEGER,
  job_attempt INTEGER NOT NULL DEFAULT 1,
  capability_id TEXT NOT NULL,
  capability_version TEXT NOT NULL,
  env_id TEXT,
  status TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  submit_intent_json TEXT NOT NULL,
  scheduler_kind TEXT,
  scheduler_ref TEXT,
  input_manifest_ref TEXT NOT NULL,
  resources_json TEXT NOT NULL DEFAULT '{}',
  preflight_json TEXT NOT NULL DEFAULT '{}',
  side_effect_status TEXT NOT NULL DEFAULT 'NONE',
  exit_code INTEGER,
  outcome_json TEXT NOT NULL DEFAULT '{}',
  measured_json TEXT NOT NULL DEFAULT '{}',
  timeout_seconds INTEGER,
  runner_pid INTEGER,
  runner_started TEXT,
  heartbeat_at TEXT,
  staging_ref TEXT,
  reservation_ref TEXT,
  created_at TEXT NOT NULL,
  submitted_at TEXT,
  observed_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(run_id, status);

-- ---------- artifacts ----------
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_ref TEXT PRIMARY KEY,
  run_id TEXT REFERENCES runs(run_id) ON DELETE CASCADE,
  path_id TEXT,
  task_id TEXT,
  job_id TEXT,
  kind TEXT NOT NULL,
  namespace TEXT NOT NULL,
  rel_path TEXT NOT NULL,
  media_type TEXT,
  bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  schema_ref TEXT,
  origin TEXT,
  put_by TEXT,
  provenance_json TEXT NOT NULL DEFAULT '{}',
  published INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(namespace, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id, kind);
CREATE INDEX IF NOT EXISTS idx_artifacts_sha ON artifacts(sha256);

-- ---------- evaluation ----------
CREATE TABLE IF NOT EXISTS eval_specs (
  eval_spec_id TEXT NOT NULL,
  version TEXT NOT NULL,
  status TEXT NOT NULL,
  frozen_sha TEXT NOT NULL,
  objective TEXT NOT NULL,
  applicable_domain TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  implementation_ref TEXT NOT NULL,
  controls_json TEXT NOT NULL DEFAULT '[]',
  thresholds_json TEXT NOT NULL DEFAULT '{}',
  threshold_basis TEXT,
  limitations TEXT NOT NULL,
  author_principal TEXT,
  author_task_id TEXT,
  namespace TEXT,
  created_at TEXT NOT NULL,
  frozen_at TEXT,
  PRIMARY KEY (eval_spec_id, version)
);

-- Known-answer cases. A specification is not frozen until the runner has executed every one and each
-- returned the verdict it was declared to produce.
CREATE TABLE IF NOT EXISTS eval_cases (
  case_id TEXT NOT NULL,
  eval_spec_id TEXT NOT NULL,
  version TEXT NOT NULL,
  case_kind TEXT NOT NULL,
  description TEXT NOT NULL,
  artifact_refs_json TEXT NOT NULL DEFAULT '[]',
  expected_json TEXT NOT NULL,
  declared_by TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (eval_spec_id, version, case_id)
);

CREATE TABLE IF NOT EXISTS evaluations (
  eval_id TEXT PRIMARY KEY,
  run_id TEXT REFERENCES runs(run_id) ON DELETE CASCADE,
  path_id TEXT,
  eval_spec_id TEXT NOT NULL,
  eval_spec_version TEXT NOT NULL,
  frozen_sha TEXT NOT NULL,
  purpose TEXT NOT NULL DEFAULT 'CANDIDATE',
  case_id TEXT,
  dataset_id TEXT,
  record_id TEXT,
  split TEXT,
  artifact_refs_json TEXT NOT NULL,
  layer INTEGER NOT NULL,
  verdict TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  controls_json TEXT NOT NULL DEFAULT '{}',
  provenance_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evals_spec ON evaluations(eval_spec_id, eval_spec_version, purpose);
CREATE INDEX IF NOT EXISTS idx_evals_path ON evaluations(path_id);

-- Reference data with a split label on every record.
CREATE TABLE IF NOT EXISTS datasets (
  dataset_id TEXT NOT NULL,
  version TEXT NOT NULL,
  namespace TEXT NOT NULL,
  title TEXT,
  status TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  provenance_json TEXT NOT NULL DEFAULT '{}',
  license TEXT,
  limitations TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (dataset_id, version)
);

CREATE TABLE IF NOT EXISTS dataset_records (
  dataset_id TEXT NOT NULL,
  version TEXT NOT NULL,
  record_id TEXT NOT NULL,
  split TEXT NOT NULL,
  group_key TEXT,
  input_artifact_ref TEXT,
  reference_artifact_ref TEXT,
  produced_by_job_id TEXT,
  meta_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (dataset_id, version, record_id)
);

-- Every read of a held-out record or of a benchmark's final split, keyed on hashes the backend
-- computed. A name the caller chose is not part of the key.
CREATE TABLE IF NOT EXISTS heldout_reads (
  read_id TEXT PRIMARY KEY,
  subject_kind TEXT NOT NULL,
  subject_sha256 TEXT NOT NULL,
  reader_sha256 TEXT NOT NULL,
  purpose TEXT NOT NULL,
  cycle_id TEXT,
  eval_id TEXT,
  principal TEXT,
  result_artifact_ref TEXT,
  -- RESERVED when the decision to look is taken, READ once the read has happened; a reserved look
  -- counts against the budget
  state TEXT NOT NULL DEFAULT 'READ',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_heldout_subject ON heldout_reads(subject_sha256, reader_sha256);

-- ---------- knowledge: sources, text, receipts, searches ----------
-- blob_sha256 is a hash of the bytes the backend holds. text_coverage is computed from the parse.
CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY,
  namespace TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'REGISTERED',
  kind TEXT NOT NULL,
  tier TEXT,
  title TEXT,
  identifiers_json TEXT NOT NULL DEFAULT '{}',
  authors TEXT,
  year INTEGER,
  version TEXT,
  accessed_at TEXT,
  local_cache_ref TEXT,
  blob_artifact_ref TEXT,
  blob_sha256 TEXT,
  bytes INTEGER,
  media_type TEXT,
  license TEXT,
  license_basis TEXT,
  parser TEXT,
  parser_version TEXT,
  text_coverage TEXT NOT NULL,
  segments_count INTEGER NOT NULL DEFAULT 0,
  retraction_status TEXT,
  retraction_checked_at TEXT,
  sha256 TEXT,
  origin TEXT,
  provenance_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sources_ns ON sources(namespace, status);
CREATE INDEX IF NOT EXISTS idx_sources_blob ON sources(namespace, blob_sha256);

-- Strong identifiers, for deduplication before content hashing.
CREATE TABLE IF NOT EXISTS source_identifiers (
  namespace TEXT NOT NULL,
  id_kind TEXT NOT NULL,
  id_value TEXT NOT NULL,
  source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
  PRIMARY KEY (namespace, id_kind, id_value)
);

-- Addressable text: a physical page of a PDF, a section of a JATS document, a caption, a table.
CREATE TABLE IF NOT EXISTS source_segments (
  source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
  segment_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  kind TEXT NOT NULL,
  label TEXT,
  text TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,
  char_count INTEGER NOT NULL,
  PRIMARY KEY (source_id, segment_id)
);

-- A read receipt: this lease was delivered this text. Citing or reviewing a span needs one.
CREATE TABLE IF NOT EXISTS source_reads (
  receipt_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
  segment_id TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,
  rendered INTEGER NOT NULL DEFAULT 0,
  task_id TEXT,
  lease_id TEXT,
  principal TEXT,
  delivered_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reads_lease ON source_reads(lease_id, source_id, segment_id);

-- A search is a record: what was asked, what each upstream returned or how it failed, and the hash
-- of every raw response. It is what later backs a statement that the literature came back empty.
CREATE TABLE IF NOT EXISTS literature_searches (
  search_id TEXT PRIMARY KEY,
  run_id TEXT,
  namespace TEXT NOT NULL DEFAULT '',
  queries_json TEXT NOT NULL,
  per_source_json TEXT NOT NULL DEFAULT '[]',
  result_count INTEGER NOT NULL DEFAULT 0,
  results_artifact_ref TEXT,
  actor TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_searches_run ON literature_searches(run_id, created_at);

-- ---------- knowledge: evidence, reviews, gaps ----------
-- status follows the evidence machine and is written only by the backend. review_status is kept for
-- v1 readers and mirrors status.
CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY,
  namespace TEXT NOT NULL DEFAULT '',
  run_id TEXT,
  status TEXT NOT NULL DEFAULT 'EXTRACTED',
  claim TEXT NOT NULL,
  claim_grain TEXT,
  evidence_type TEXT NOT NULL,
  source_id TEXT REFERENCES sources(source_id),
  source_locator_json TEXT NOT NULL DEFAULT '{}',
  quote TEXT,
  quote_sha256 TEXT,
  anchor_status TEXT,
  anchored_segment_id TEXT,
  conditions TEXT,
  method TEXT,
  data TEXT,
  limitations TEXT,
  contradictory_refs_json TEXT NOT NULL DEFAULT '[]',
  review_status TEXT NOT NULL,
  confidence_basis TEXT NOT NULL,
  evidence_level TEXT NOT NULL,
  origin TEXT,
  import_ref TEXT,
  extracted_task_id TEXT,
  extracted_lease_id TEXT,
  extracted_principal TEXT,
  receipt_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_evidence_source ON evidence(source_id, status);
CREATE INDEX IF NOT EXISTS idx_evidence_ns ON evidence(namespace, status);

-- Append-only. A review is recorded whatever its verdict.
CREATE TABLE IF NOT EXISTS evidence_reviews (
  review_id TEXT PRIMARY KEY,
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
  task_id TEXT,
  lease_id TEXT,
  principal TEXT,
  worker_observation_id TEXT,
  independence_level TEXT NOT NULL,
  verdict TEXT NOT NULL,
  receipt_id TEXT,
  quote_sha256_seen TEXT,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evreviews_ev ON evidence_reviews(evidence_id, created_at);

-- A gap is a record: the sub-question, the searches behind it, the reviewed evidence either way.
CREATE TABLE IF NOT EXISTS gaps (
  gap_id TEXT PRIMARY KEY,
  run_id TEXT,
  namespace TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  gap_kind TEXT NOT NULL,
  question TEXT NOT NULL,
  searches_json TEXT NOT NULL DEFAULT '[]',
  evidence_for_json TEXT NOT NULL DEFAULT '[]',
  evidence_against_json TEXT NOT NULL DEFAULT '[]',
  missing TEXT,
  origin TEXT,
  failed_path_id TEXT,
  failure_class TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gaps_run ON gaps(run_id, status);

-- What rests on what. A retraction, a withdrawn record or a changed asset travels along these rows
-- to the memory documents, skills, metrics and paths that cite it.
-- Three relations are scheduling edges, not grounds: `follows` (the lineage an expansion records:
-- subject = the follow-up task, depends_on = the task or job it follows), `waits_on` (subject = the
-- waiter, depends_on = the review or product it waits for, counted only while it still waits) and
-- `independent_of` (subject = a review task, depends_on = a task it must be independent of). They
-- never gate READY, which only task_deps does, and a withdrawal does not travel along them.
CREATE TABLE IF NOT EXISTS dependencies (
  subject_kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  depends_on_kind TEXT NOT NULL,
  depends_on_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (subject_kind, subject_id, depends_on_kind, depends_on_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_deps_target ON dependencies(depends_on_kind, depends_on_id);

-- ---------- knowledge: episodes and the retrieval corpus ----------
CREATE TABLE IF NOT EXISTS episodes (
  episode_id TEXT PRIMARY KEY,
  run_id TEXT,
  path_id TEXT,
  outcome TEXT NOT NULL,
  failure_class TEXT,
  fingerprint TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  context_json TEXT NOT NULL DEFAULT '{}',
  input_hash TEXT,
  versions_json TEXT NOT NULL DEFAULT '{}',
  error_json TEXT NOT NULL DEFAULT '{}',
  evidence_refs_json TEXT NOT NULL DEFAULT '[]',
  skill_refs_json TEXT NOT NULL DEFAULT '[]',
  root_cause_hypothesis TEXT,
  tried_json TEXT NOT NULL DEFAULT '[]',
  chosen_recovery TEXT,
  expected_change TEXT,
  cost_json TEXT NOT NULL DEFAULT '{}',
  scope TEXT NOT NULL,
  promotion_status TEXT NOT NULL DEFAULT 'CANDIDATE',
  memory_doc_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodes_fp ON episodes(fingerprint);

-- Retrieval corpus. memory_class separates the five kinds that must not be conflated.
-- content_sha256 lets a served body be re-hashed against its ingest record.
CREATE TABLE IF NOT EXISTS memory_docs (
  doc_id TEXT PRIMARY KEY,
  namespace TEXT NOT NULL,
  memory_class TEXT NOT NULL,
  title TEXT,
  body TEXT NOT NULL,
  content_sha256 TEXT,
  source_refs_json TEXT NOT NULL DEFAULT '[]',
  locators_json TEXT NOT NULL DEFAULT '[]',
  meta_json TEXT NOT NULL DEFAULT '{}',
  outcome TEXT,
  origin TEXT,
  import_ref TEXT,
  created_at TEXT NOT NULL,
  superseded_by TEXT,
  retracted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_ns ON memory_docs(namespace, memory_class);
CREATE INDEX IF NOT EXISTS idx_memory_import ON memory_docs(import_ref);

-- Word index. unicode61 treats a run of CJK characters as one token, so this channel alone cannot
-- match a Chinese clause.
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  doc_id UNINDEXED, namespace UNINDEXED, title, body, tokenize='unicode61'
);

-- CJK-bigram index: title and body are stored pre-segmented into overlapping character bigrams and
-- ASCII words, so the same tokenizer indexes text it could not otherwise match.
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts_cjk USING fts5(
  doc_id UNINDEXED, namespace UNINDEXED, title, body, tokenize='unicode61'
);

-- Embeddings produced by an isolated capability, stored as float32 little-endian blobs.
CREATE TABLE IF NOT EXISTS memory_vectors (
  doc_id TEXT NOT NULL REFERENCES memory_docs(doc_id) ON DELETE CASCADE,
  model_ref TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vector BLOB NOT NULL,
  content_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (doc_id, model_ref)
);

-- Each search kept: query, the channels that ran, the strategy version, the namespaces searched and
-- the top results with their source refs.
CREATE TABLE IF NOT EXISTS memory_queries (
  query_id TEXT PRIMARY KEY,
  run_id TEXT,
  principal TEXT,
  project_scope TEXT NOT NULL,
  query TEXT NOT NULL,
  filters_json TEXT NOT NULL DEFAULT '{}',
  retrieval_mode TEXT NOT NULL,
  channels_json TEXT NOT NULL DEFAULT '[]',
  strategy_version TEXT,
  searched_namespaces_json TEXT NOT NULL DEFAULT '[]',
  top_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);

-- ---------- governed objects ----------
-- One registry for skills, workflows, adapters, evaluators, benchmarks, strategies and role intents.
-- content_sha256 addresses the immutable content of a version.
CREATE TABLE IF NOT EXISTS assets (
  asset_id TEXT NOT NULL,
  version TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  title TEXT,
  content_sha256 TEXT,
  content_artifact_ref TEXT,
  source_json TEXT NOT NULL DEFAULT '{}',
  manifest_json TEXT NOT NULL,
  origin TEXT,
  origin_ref TEXT,
  author_principal TEXT,
  author_task_id TEXT,
  author_lease_id TEXT,
  namespace TEXT,
  quarantine_path TEXT,
  approved_path TEXT,
  review_json TEXT NOT NULL DEFAULT '{}',
  test_status TEXT NOT NULL DEFAULT 'STATIC_REVIEW_ONLY',
  license TEXT,
  license_basis TEXT,
  limitations TEXT,
  superseded_by TEXT,
  created_at TEXT NOT NULL,
  promoted_at TEXT,
  PRIMARY KEY (asset_id, version)
);
CREATE INDEX IF NOT EXISTS idx_assets_kind ON assets(kind, status);

-- Append-only. Written only by backend runners; there is no route by which a caller records a result.
CREATE TABLE IF NOT EXISTS check_runs (
  check_run_id TEXT PRIMARY KEY,
  subject_kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  subject_version TEXT,
  subject_sha256 TEXT NOT NULL,
  check_id TEXT NOT NULL,
  runner TEXT NOT NULL,
  inputs_json TEXT NOT NULL DEFAULT '{}',
  result TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  diagnostics_json TEXT NOT NULL DEFAULT '[]',
  artifact_ref TEXT,
  job_id TEXT,
  superseded INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checks_subject ON check_runs(subject_kind, subject_id, subject_sha256, check_id, created_at);

-- Append-only. A review belongs to one content sha256 and records the independence the backend
-- can actually show.
CREATE TABLE IF NOT EXISTS reviews (
  review_id TEXT PRIMARY KEY,
  subject_kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  subject_version TEXT,
  subject_sha256 TEXT NOT NULL,
  task_id TEXT,
  lease_id TEXT,
  principal TEXT,
  worker_observation_id TEXT,
  independence_level TEXT NOT NULL,
  verdict TEXT NOT NULL,
  rubric_json TEXT NOT NULL DEFAULT '[]',
  bundle_artifact_ref TEXT,
  superseded INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_subject ON reviews(subject_kind, subject_id, subject_sha256, created_at);

-- A judgement on one diagnostic, bound to its fingerprint.
CREATE TABLE IF NOT EXISTS adjudications (
  adjudication_id TEXT PRIMARY KEY,
  check_run_id TEXT NOT NULL REFERENCES check_runs(check_run_id) ON DELETE CASCADE,
  diagnostic_fingerprint TEXT NOT NULL,
  decision TEXT NOT NULL,
  decided_by TEXT NOT NULL,
  independence_level TEXT,
  note TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(check_run_id, diagnostic_fingerprint)
);

-- An approval names its subject and its content sha256 and unlocks exactly that.
CREATE TABLE IF NOT EXISTS approvals (
  approval_id TEXT PRIMARY KEY,
  run_id TEXT,
  action_kind TEXT NOT NULL,
  subject_kind TEXT,
  subject_id TEXT,
  subject_sha256 TEXT,
  request_json TEXT NOT NULL,
  status TEXT NOT NULL,
  requested_by TEXT,
  requested_at TEXT NOT NULL,
  decided_at TEXT,
  decided_by TEXT,
  policy_ref TEXT,
  decision_note TEXT,
  -- what a decision carries beyond the word: for a worker_refusal, {choice, note?, omit_sections?,
  -- model?}; NULL otherwise
  decision_json TEXT,
  expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_subject ON approvals(subject_kind, subject_id, subject_sha256, status);
CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals(run_id, status);

-- ---------- self-improvement ----------
-- The pointer a service actually reads. Promotion flips it; rollback flips it back.
CREATE TABLE IF NOT EXISTS strategy_pointers (
  strategy_kind TEXT PRIMARY KEY,
  active_asset_id TEXT NOT NULL,
  active_version TEXT NOT NULL,
  active_sha256 TEXT NOT NULL,
  previous_version TEXT,
  previous_sha256 TEXT,
  set_by TEXT NOT NULL,
  set_at TEXT NOT NULL,
  cycle_id TEXT
);

CREATE TABLE IF NOT EXISTS rsi_cycles (
  cycle_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  object_kind TEXT NOT NULL,
  benchmark_asset_id TEXT NOT NULL,
  benchmark_version TEXT NOT NULL,
  benchmark_sha256 TEXT NOT NULL,
  incumbent_sha256 TEXT NOT NULL,
  candidate_asset_id TEXT,
  candidate_version TEXT,
  candidate_sha256 TEXT NOT NULL,
  rationale TEXT,
  decision_rule_json TEXT,
  decision_rule_sha256 TEXT,
  stage_results_json TEXT NOT NULL DEFAULT '{}',
  development_json TEXT NOT NULL DEFAULT '{}',
  final_json TEXT NOT NULL DEFAULT '{}',
  cost_json TEXT NOT NULL DEFAULT '{}',
  per_item_artifact_refs_json TEXT NOT NULL DEFAULT '[]',
  verdict TEXT,
  proposed_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- ---------- inheritance ----------
-- What was brought in from a tree that is only read: where from, at which sha256, how, and as what.
CREATE TABLE IF NOT EXISTS imports (
  import_id TEXT PRIMARY KEY,
  ledger_ref TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  origin_path TEXT NOT NULL,
  origin_sha256 TEXT NOT NULL,
  inherit_mode TEXT NOT NULL,
  target_kind TEXT NOT NULL,
  target_id TEXT NOT NULL,
  verified_json TEXT NOT NULL DEFAULT '{}',
  imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_imports_ledger ON imports(ledger_ref, target_kind);

-- ---------- reference projects (P1) ----------
CREATE TABLE IF NOT EXISTS references_reviewed (
  reference_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  review_state TEXT NOT NULL,
  primary_source_json TEXT NOT NULL DEFAULT '{}',
  repo_json TEXT NOT NULL DEFAULT '{}',
  pinned_commit TEXT,
  license TEXT,
  reuse_decision TEXT,
  reuse_reason TEXT,
  adapter_plan TEXT,
  limitations TEXT,
  evidence_refs_json TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL
);

-- ---------- orchestration ----------
CREATE TABLE IF NOT EXISTS checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  workflow_id TEXT NOT NULL,
  workflow_version TEXT NOT NULL,
  node TEXT NOT NULL,
  seq INTEGER NOT NULL,
  state_json TEXT NOT NULL,
  interrupt_ref TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ckpt_run ON checkpoints(run_id, seq);

-- A reaction's trigger is a row here, written in the transaction that causes it; the lazy path of a
-- run consumes that run's rows.
CREATE TABLE IF NOT EXISTS outbox (
  outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  run_id TEXT,
  topic TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  idempotency_key TEXT,
  consumed_at TEXT,
  consumer TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(consumed_at, outbox_id);
CREATE INDEX IF NOT EXISTS idx_outbox_run ON outbox(run_id, consumed_at, outbox_id);
-- One key, one row: a trigger is looked up by its idempotency key whenever one is written. A plain index,
-- not a unique one, so a store written under r4 takes it whatever it holds; the store's enqueue keeps a key
-- to one row inside the writing transaction.
CREATE INDEX IF NOT EXISTS idx_outbox_key ON outbox(idempotency_key);

CREATE TABLE IF NOT EXISTS idempotency (
  idempotency_key TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  request_sha TEXT NOT NULL,
  response_json TEXT,
  side_effect_status TEXT NOT NULL DEFAULT 'NONE',
  created_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS budgets (
  budget_ref TEXT PRIMARY KEY,
  parent_ref TEXT,
  run_id TEXT,
  limits_json TEXT NOT NULL,
  spent_json TEXT NOT NULL DEFAULT '{}',
  reserved_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- A reservation holds part of a budget for one subject until it is consumed or released, so that
-- concurrency is controlled by the backend and not by an agent keeping count in a prompt.
CREATE TABLE IF NOT EXISTS budget_reservations (
  reservation_ref TEXT PRIMARY KEY,
  budget_ref TEXT NOT NULL REFERENCES budgets(budget_ref) ON DELETE CASCADE,
  subject_kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  amounts_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  settled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reservations_budget ON budget_reservations(budget_ref, status);

-- P1 TeamManager. Lead merges this into contracts/db_schema.sql before use.
-- Task state, leases, budget balances and jobs retain their existing owners.
CREATE TABLE IF NOT EXISTS teams (
  team_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  parent_team_id TEXT REFERENCES teams(team_id),
  status TEXT NOT NULL,
  intent_version INTEGER NOT NULL CHECK(intent_version > 0),
  intent_sha256 TEXT NOT NULL,
  leader_subject TEXT,
  leader_generation INTEGER NOT NULL DEFAULT 0 CHECK(leader_generation >= 0),
  leader_expires_at TEXT,
  context_ref TEXT,
  budget_ref TEXT REFERENCES budgets(budget_ref),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_teams_run ON teams(run_id, status);
CREATE INDEX IF NOT EXISTS idx_teams_parent ON teams(parent_team_id);
CREATE TABLE IF NOT EXISTS team_intents (
  team_id TEXT NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK(version > 0),
  sha256 TEXT NOT NULL,
  intent_json TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(team_id, version)
);
CREATE TABLE IF NOT EXISTS team_grants (
  team_id TEXT NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
  subject TEXT NOT NULL,
  generation INTEGER NOT NULL,
  scopes_json TEXT NOT NULL,
  granted_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  revoked_at TEXT,
  task_ids_json TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY(team_id, subject, generation)
);
CREATE TABLE IF NOT EXISTS team_members (
  team_id TEXT NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
  subject TEXT NOT NULL,
  observed_worker_ref TEXT,
  status TEXT NOT NULL,
  joined_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(team_id, subject)
);
CREATE TABLE IF NOT EXISTS task_assignments (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
  team_id TEXT NOT NULL REFERENCES teams(team_id),
  subject TEXT NOT NULL,
  observed_worker_ref TEXT,
  context_ref TEXT,
  delegated_by TEXT,
  offered_attempt INTEGER,
  offered_control_epoch INTEGER,
  offered_leader_generation INTEGER,
  assignment_generation INTEGER NOT NULL DEFAULT 1 CHECK(assignment_generation > 0),
  status TEXT NOT NULL,
  pending_json TEXT,
  assigned_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  intent_version INTEGER NOT NULL CHECK(intent_version > 0),
  intent_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assignments_team ON task_assignments(team_id, status);
CREATE TABLE IF NOT EXISTS team_messages (
  message_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  sender_team_id TEXT NOT NULL REFERENCES teams(team_id),
  recipient_team_id TEXT NOT NULL REFERENCES teams(team_id),
  sender_subject TEXT NOT NULL,
  kind TEXT NOT NULL,
  task_id TEXT REFERENCES tasks(task_id),
  hypothesis_ref TEXT,
  context_ref TEXT,
  causal_event_id INTEGER REFERENCES events(event_id),
  correlation_key TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(run_id, sender_subject, correlation_key)
);
CREATE INDEX IF NOT EXISTS idx_team_messages_run ON team_messages(run_id, created_at, message_id);
CREATE INDEX IF NOT EXISTS idx_team_messages_recipient ON team_messages(recipient_team_id, created_at, message_id);
CREATE TABLE IF NOT EXISTS team_message_acks (
  message_id TEXT NOT NULL REFERENCES team_messages(message_id) ON DELETE CASCADE,
  subject TEXT NOT NULL,
  team_id TEXT NOT NULL REFERENCES teams(team_id),
  acknowledged_at TEXT NOT NULL,
  PRIMARY KEY(message_id, subject)
);

-- P1 scientific control plane. Proposal for lead-owned contracts/db_schema.sql.
-- No existing run, task, lease, budget or pin is changed by this additive schema.
CREATE TABLE IF NOT EXISTS hypotheses (
  hypothesis_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  head_revision INTEGER NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hypotheses_run ON hypotheses(run_id);
CREATE TABLE IF NOT EXISTS hypothesis_revisions (
  hypothesis_id TEXT NOT NULL REFERENCES hypotheses(hypothesis_id),
  revision INTEGER NOT NULL,
  parent_revision INTEGER,
  document_json TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  author_principal TEXT NOT NULL,
  author_task_id TEXT,
  proposal_id TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(hypothesis_id, revision)
);
CREATE TRIGGER IF NOT EXISTS hypothesis_revisions_no_update BEFORE UPDATE ON hypothesis_revisions
BEGIN SELECT RAISE(ABORT, 'hypothesis revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS hypothesis_revisions_no_delete BEFORE DELETE ON hypothesis_revisions
BEGIN SELECT RAISE(ABORT, 'hypothesis revisions are immutable'); END;
CREATE TABLE IF NOT EXISTS research_contexts (
  context_ref TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  version INTEGER NOT NULL,
  previous_context_ref TEXT REFERENCES research_contexts(context_ref),
  snapshot_json TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  event_cursor INTEGER NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(run_id, version)
);
CREATE TRIGGER IF NOT EXISTS research_contexts_no_update BEFORE UPDATE ON research_contexts
BEGIN SELECT RAISE(ABORT, 'research contexts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_contexts_no_delete BEFORE DELETE ON research_contexts
BEGIN SELECT RAISE(ABORT, 'research contexts are immutable'); END;
CREATE TABLE IF NOT EXISTS research_context_heads (
  run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
  context_ref TEXT NOT NULL REFERENCES research_contexts(context_ref),
  version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_rounds (
  round_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  round_index INTEGER NOT NULL,
  context_ref TEXT NOT NULL REFERENCES research_contexts(context_ref),
  expected_json TEXT NOT NULL,
  expected_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('OPEN','SEALED','COMMITTED','SUPERSEDED')),
  sealed_json TEXT,
  sealed_sha256 TEXT,
  synthesis_task_id TEXT REFERENCES tasks(task_id),
  review_task_id TEXT REFERENCES tasks(task_id),
  committed_proposal_id TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  sealed_at TEXT,
  closed_at TEXT,
  UNIQUE(run_id, round_index)
);
CREATE TRIGGER IF NOT EXISTS research_rounds_manifest_immutable BEFORE UPDATE ON research_rounds
WHEN NEW.run_id IS NOT OLD.run_id OR NEW.round_index IS NOT OLD.round_index
 OR NEW.context_ref IS NOT OLD.context_ref OR NEW.expected_json IS NOT OLD.expected_json
 OR NEW.expected_sha256 IS NOT OLD.expected_sha256
 OR (OLD.sealed_json IS NOT NULL AND (NEW.sealed_json IS NOT OLD.sealed_json
     OR NEW.sealed_sha256 IS NOT OLD.sealed_sha256))
BEGIN SELECT RAISE(ABORT, 'round result manifests are immutable'); END;
CREATE TABLE IF NOT EXISTS hypothesis_update_proposals (
  proposal_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  round_id TEXT NOT NULL REFERENCES research_rounds(round_id),
  context_ref TEXT NOT NULL REFERENCES research_contexts(context_ref),
  context_sha256 TEXT NOT NULL,
  sealed_sha256 TEXT NOT NULL,
  bundle_json TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  author_task_id TEXT NOT NULL REFERENCES tasks(task_id),
  author_lease_id TEXT NOT NULL REFERENCES leases(lease_id),
  author_principal TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PROPOSED','COMMITTED')),
  created_at TEXT NOT NULL,
  committed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_hypothesis_proposals_round ON hypothesis_update_proposals(round_id);
CREATE TRIGGER IF NOT EXISTS hypothesis_proposals_content_immutable BEFORE UPDATE ON hypothesis_update_proposals
WHEN NEW.run_id IS NOT OLD.run_id OR NEW.round_id IS NOT OLD.round_id
 OR NEW.context_ref IS NOT OLD.context_ref OR NEW.context_sha256 IS NOT OLD.context_sha256
 OR NEW.sealed_sha256 IS NOT OLD.sealed_sha256 OR NEW.bundle_json IS NOT OLD.bundle_json
 OR NEW.sha256 IS NOT OLD.sha256 OR NEW.author_task_id IS NOT OLD.author_task_id
 OR NEW.author_lease_id IS NOT OLD.author_lease_id OR NEW.author_principal IS NOT OLD.author_principal
BEGIN SELECT RAISE(ABORT, 'hypothesis proposal contents are immutable'); END;
CREATE TABLE IF NOT EXISTS hypothesis_update_reviews (
  review_id TEXT PRIMARY KEY,
  proposal_id TEXT NOT NULL REFERENCES hypothesis_update_proposals(proposal_id),
  proposal_sha256 TEXT NOT NULL,
  reviewer_task_id TEXT NOT NULL REFERENCES tasks(task_id),
  reviewer_lease_id TEXT NOT NULL REFERENCES leases(lease_id),
  reviewer_principal TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK(verdict IN ('APPROVE','REJECT')),
  note TEXT NOT NULL,
  independence_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hypothesis_reviews_proposal ON hypothesis_update_reviews(proposal_id);
CREATE TRIGGER IF NOT EXISTS hypothesis_reviews_no_update BEFORE UPDATE ON hypothesis_update_reviews
BEGIN SELECT RAISE(ABORT, 'hypothesis reviews are immutable'); END;
CREATE TRIGGER IF NOT EXISTS hypothesis_reviews_no_delete BEFORE DELETE ON hypothesis_update_reviews
BEGIN SELECT RAISE(ABORT, 'hypothesis reviews are immutable'); END;

-- Plugin descriptors index existing governed assets. Admission is independent of governance.
-- These tables contain metadata, pins and invocation receipts, never a second job queue.
CREATE TABLE IF NOT EXISTS plugin_versions (
  project_ref TEXT NOT NULL,
  plugin_id TEXT NOT NULL,
  version TEXT NOT NULL,
  family TEXT NOT NULL,
  interface_version TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  manifest_artifact_ref TEXT NOT NULL REFERENCES artifacts(artifact_ref),
  manifest_json TEXT NOT NULL,
  admission_state TEXT NOT NULL,
  registered_run_id TEXT NOT NULL REFERENCES runs(run_id),
  registered_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(project_ref, plugin_id, version)
);
CREATE TABLE IF NOT EXISTS plugin_heads (
  project_ref TEXT NOT NULL,
  plugin_id TEXT NOT NULL,
  version TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(project_ref, plugin_id),
  FOREIGN KEY(project_ref, plugin_id, version) REFERENCES plugin_versions(project_ref, plugin_id, version)
);
CREATE TABLE IF NOT EXISTS plugin_invocations (
  invocation_id TEXT PRIMARY KEY,
  project_ref TEXT NOT NULL,
  plugin_id TEXT NOT NULL,
  version TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  identity_sha256 TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  task_id TEXT,
  attempt INTEGER,
  lease_ref TEXT,
  context_ref TEXT,
  context_sha256 TEXT,
  operation TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  request_json TEXT NOT NULL,
  principal TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  status TEXT NOT NULL,
  job_id TEXT REFERENCES jobs(job_id),
  response_json TEXT,
  error_json TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE(run_id, idempotency_key),
  FOREIGN KEY(project_ref, plugin_id, version) REFERENCES plugin_versions(project_ref, plugin_id, version)
);
CREATE INDEX IF NOT EXISTS idx_plugin_invocations_version ON plugin_invocations(project_ref, plugin_id, version, status);

-- PROPOSAL for lead-owned contracts/db_schema.sql; not a product migration.
CREATE TABLE IF NOT EXISTS host_pools (
  pool_id TEXT PRIMARY KEY,
  host TEXT NOT NULL,
  session_ref TEXT NOT NULL,
  session_generation INTEGER NOT NULL DEFAULT 1,
  configured_capacity INTEGER NOT NULL,
  effective_capacity INTEGER,
  external_occupied INTEGER NOT NULL DEFAULT 0,
  max_depth INTEGER NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL,
  observed_source TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(host, session_ref)
);
CREATE TABLE IF NOT EXISTS host_pool_slots (
  slot_id TEXT PRIMARY KEY,
  pool_id TEXT NOT NULL REFERENCES host_pools(pool_id),
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  current_run_id TEXT REFERENCES runs(run_id), -- NULL on old rows means immutable origin run_id
  request_key TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  pool_generation INTEGER NOT NULL,
  generation INTEGER NOT NULL DEFAULT 1,
  state TEXT NOT NULL,
  native_agent_ref TEXT,
  native_creator_ref TEXT,
  native_creator_depth INTEGER,
  native_creator_evidence_json TEXT,
  native_parent_ref TEXT,
  native_depth INTEGER,
  logical_parent_slot_id TEXT REFERENCES host_pool_slots(slot_id),
  logical_depth INTEGER NOT NULL,
  task_id TEXT,
  lease_id TEXT,
  source TEXT NOT NULL,
  observed_source TEXT,
  observed_at TEXT,
  termination_evidence_json TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(pool_id, request_key),
  UNIQUE(pool_id, pool_generation, native_agent_ref)
);
CREATE INDEX IF NOT EXISTS idx_host_pool_slots_occupancy ON host_pool_slots(pool_id, state);
CREATE INDEX IF NOT EXISTS idx_host_pool_slots_run ON host_pool_slots(run_id, slot_id);
CREATE INDEX IF NOT EXISTS idx_host_pool_slots_parent ON host_pool_slots(logical_parent_slot_id);

-- r7: explicit current binding; run_id remains immutable origin.
CREATE INDEX IF NOT EXISTS idx_host_pool_slots_current_run ON host_pool_slots(current_run_id,slot_id);

-- Additive proposal. Root owns migration/contract; this file is not applied.
CREATE TABLE IF NOT EXISTS host_pool_inventory_receipts (
    receipt_id TEXT PRIMARY KEY,
    pool_id TEXT NOT NULL REFERENCES host_pools(pool_id),
    pool_generation INTEGER NOT NULL,
    source_native_ref TEXT NOT NULL,
    native_call_id TEXT NOT NULL,
    snapshot_at TEXT NOT NULL,
    source_evidence_json TEXT NOT NULL,
    roster_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(pool_id,native_call_id)
);
CREATE TABLE IF NOT EXISTS host_pool_external_inventory (
    pool_id TEXT NOT NULL REFERENCES host_pools(pool_id),
    native_agent_ref TEXT NOT NULL,
    native_parent_ref TEXT,
    native_depth INTEGER NOT NULL,
    agent_path TEXT NOT NULL,
    runtime_status TEXT NOT NULL CHECK(runtime_status IN ('ACTIVE','INACTIVE','UNKNOWN')),
    ownership TEXT NOT NULL CHECK(ownership IN ('EXTERNAL','OWNED')),
    slot_id TEXT REFERENCES host_pool_slots(slot_id),
    receipt_id TEXT NOT NULL REFERENCES host_pool_inventory_receipts(receipt_id),
    generation INTEGER NOT NULL DEFAULT 1,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(pool_id,native_agent_ref),
    CHECK((ownership='EXTERNAL' AND slot_id IS NULL) OR (ownership='OWNED' AND slot_id IS NOT NULL)),
    FOREIGN KEY(slot_id,pool_id,native_agent_ref) REFERENCES host_pool_slots(slot_id,pool_id,native_agent_ref),
    FOREIGN KEY(receipt_id,pool_id) REFERENCES host_pool_inventory_receipts(receipt_id,pool_id)
);
CREATE INDEX IF NOT EXISTS idx_host_pool_external_charge ON host_pool_external_inventory(pool_id,ownership,runtime_status);

-- r7 identity integrity. No migration may discard a conflicting historical row.
CREATE UNIQUE INDEX IF NOT EXISTS uidx_host_pool_live_native
  ON host_pool_slots(pool_id,native_agent_ref)
  WHERE native_agent_ref IS NOT NULL AND state<>'TERMINATED';
CREATE UNIQUE INDEX IF NOT EXISTS uidx_host_pool_slot_native_binding
  ON host_pool_slots(slot_id,pool_id,native_agent_ref);
CREATE UNIQUE INDEX IF NOT EXISTS uidx_host_pool_inventory_receipt_pool
  ON host_pool_inventory_receipts(receipt_id,pool_id);
CREATE UNIQUE INDEX IF NOT EXISTS uidx_host_pool_inventory_owned_slot
  ON host_pool_external_inventory(slot_id) WHERE slot_id IS NOT NULL;

-- Goal-first research (V6.1). The original P0 migration files and their receipts remain immutable.

-- A Goal selects its capabilities before the Lead selects a scientific workflow.
CREATE TABLE IF NOT EXISTS v6_goals (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    goal TEXT NOT NULL,
    goal_version INTEGER NOT NULL DEFAULT 1,
    capabilities_json TEXT NOT NULL,
    materials_json TEXT NOT NULL,
    limits_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    stop_decision_ref TEXT REFERENCES artifacts(artifact_ref),
    stop_op_digest TEXT
);
CREATE TABLE IF NOT EXISTS v6_decision_tasks (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    run_id TEXT NOT NULL REFERENCES v6_goals(run_id),
    scope TEXT NOT NULL,
    context_ref TEXT NOT NULL REFERENCES research_contexts(context_ref),
    goal_version INTEGER NOT NULL,
    base_json TEXT,
    visible_refs_json TEXT NOT NULL,
    event_key TEXT NOT NULL,
    UNIQUE(run_id,scope,event_key)
);
CREATE TABLE IF NOT EXISTS v6_graph_versions (
    run_id TEXT NOT NULL REFERENCES v6_goals(run_id),
    version INTEGER NOT NULL CHECK(version>=1),
    sha256 TEXT NOT NULL,
    parent_sha TEXT,
    graph_json TEXT NOT NULL,
    decision_ref TEXT NOT NULL REFERENCES artifacts(artifact_ref),
    PRIMARY KEY(run_id,version)
);
CREATE TABLE IF NOT EXISTS v6_decisions (
    decision_ref TEXT PRIMARY KEY REFERENCES artifacts(artifact_ref),
    run_id TEXT NOT NULL REFERENCES v6_goals(run_id),
    task_id TEXT NOT NULL UNIQUE REFERENCES v6_decision_tasks(task_id),
    context_ref TEXT NOT NULL REFERENCES research_contexts(context_ref),
    lease_id TEXT NOT NULL REFERENCES leases(lease_id),
    thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    UNIQUE(thread_id,call_id)
);
CREATE INDEX IF NOT EXISTS v6_decisions_run ON v6_decisions(run_id);
CREATE TABLE IF NOT EXISTS v6_branches (
    branch_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES v6_goals(run_id),
    decision_ref TEXT NOT NULL REFERENCES artifacts(artifact_ref),
    spec_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    stop_decision_ref TEXT REFERENCES artifacts(artifact_ref),
    stop_op_digest TEXT
);
CREATE INDEX IF NOT EXISTS v6_branches_run ON v6_branches(run_id);
CREATE TABLE IF NOT EXISTS v6_nodes (
    node_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES v6_goals(run_id),
    branch_id TEXT NOT NULL REFERENCES v6_branches(branch_id),
    version INTEGER NOT NULL,
    decision_ref TEXT NOT NULL REFERENCES artifacts(artifact_ref),
    spec_json TEXT NOT NULL,
    operation_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'PENDING',
    job_id TEXT REFERENCES jobs(job_id),
    result_ref TEXT REFERENCES artifacts(artifact_ref),
    exec_key TEXT,
    FOREIGN KEY(run_id,version) REFERENCES v6_graph_versions(run_id,version)
);
CREATE INDEX IF NOT EXISTS v6_nodes_dispatch ON v6_nodes(run_id,status);
CREATE TRIGGER IF NOT EXISTS v6_graph_no_update BEFORE UPDATE ON v6_graph_versions
BEGIN SELECT RAISE(ABORT,'graph revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS v6_graph_no_delete BEFORE DELETE ON v6_graph_versions
BEGIN SELECT RAISE(ABORT,'graph revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS v6_decision_no_update BEFORE UPDATE ON v6_decisions
BEGIN SELECT RAISE(ABORT,'decision receipts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS v6_decision_no_delete BEFORE DELETE ON v6_decisions
BEGIN SELECT RAISE(ABORT,'decision receipts are immutable'); END;

-- Immutable compiled work plus its original execution or verified reuse provenance.
CREATE TABLE IF NOT EXISTS v6_job_attempts (
    node_id TEXT PRIMARY KEY REFERENCES v6_nodes(node_id),
    run_id TEXT NOT NULL REFERENCES v6_goals(run_id),
    operation_key TEXT NOT NULL UNIQUE,
    plan_key TEXT NOT NULL,
    exec_key TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    request_json TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    reused_from TEXT REFERENCES v6_nodes(node_id)
);
CREATE INDEX IF NOT EXISTS v6_attempts_equivalent ON v6_job_attempts(run_id,exec_key);
CREATE TABLE IF NOT EXISTS v6_node_waits (
    node_id TEXT PRIMARY KEY REFERENCES v6_nodes(node_id),
    wait_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS p0_v6_native_calls (
    thread_id TEXT NOT NULL REFERENCES p0_roots(thread_id),
    call_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    PRIMARY KEY(thread_id,call_id)
);

-- Branch admission reads original settlement amounts while it holds the Job writer.
-- Look up just the reservations of the branch and the execution identity being admitted.
CREATE INDEX IF NOT EXISTS v6_budget_settled_ref ON events(json_extract(payload_json,'$.reservation_ref'))
    WHERE kind='budget.settled' AND subject_kind='budget';
CREATE INDEX IF NOT EXISTS v6_branch_work ON v6_nodes(branch_id,status);
CREATE INDEX IF NOT EXISTS v6_run_execution ON v6_nodes(run_id,exec_key,status);

-- Only financial changes invalidate a prepared branch allocation, including other
-- Runs spending under the same workspace cap. Ordinary heartbeats do not.
CREATE INDEX IF NOT EXISTS v6_funding_event_cursor ON events(event_id DESC)
WHERE subject_kind IN ('budget','project_budget','change');
-- Durable research delegation. Existing V6 runs and decision tasks acquire no module rows.
CREATE TABLE IF NOT EXISTS v62_modules (
    module_instance_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES v6_goals(run_id),
    scope TEXT NOT NULL UNIQUE,
    branch_id TEXT NOT NULL UNIQUE,
    module_id TEXT NOT NULL,
    module_version TEXT NOT NULL,
    goal_version INTEGER NOT NULL,
    round INTEGER NOT NULL CHECK(round >= 1),
    focus TEXT NOT NULL,
    expected_delivery TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('OPEN','COMPLETE','PARTIAL','CANCELLED','STALE')),
    parent_task_id TEXT NOT NULL REFERENCES v6_decision_tasks(task_id),
    parent_context_ref TEXT NOT NULL REFERENCES research_contexts(context_ref),
    open_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    grant_json TEXT NOT NULL,
    budget_ref TEXT NOT NULL REFERENCES budgets(budget_ref),
    budget_json TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    wake_json TEXT NOT NULL,
    UNIQUE(run_id,open_key)
);
CREATE INDEX IF NOT EXISTS v62_module_round ON v62_modules(run_id,round,status);
CREATE TABLE IF NOT EXISTS v62_module_reports (
    module_instance_id TEXT PRIMARY KEY REFERENCES v62_modules(module_instance_id),
    run_id TEXT NOT NULL REFERENCES v6_goals(run_id),
    version INTEGER NOT NULL DEFAULT 1,
    report_ref TEXT UNIQUE NOT NULL REFERENCES artifacts(artifact_ref),
    operation_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v62_module_wakes (
    run_id TEXT NOT NULL REFERENCES v6_goals(run_id),
    wake_key TEXT NOT NULL,
    round INTEGER NOT NULL,
    event_id INTEGER NOT NULL UNIQUE REFERENCES events(event_id),
    PRIMARY KEY(run_id,wake_key)
);
