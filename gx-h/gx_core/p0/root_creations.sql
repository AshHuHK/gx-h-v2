-- A pre-turn thread may never be indexed by Codex. Preserve each original creation intent.
CREATE TABLE IF NOT EXISTS p0_root_creations (
  cwd TEXT PRIMARY KEY,
  root_key TEXT NOT NULL REFERENCES p0_roots(root_key),
  host_pid INTEGER NOT NULL,
  host_start_ticks TEXT NOT NULL,
  outcome TEXT NOT NULL,
  thread_id TEXT
);
