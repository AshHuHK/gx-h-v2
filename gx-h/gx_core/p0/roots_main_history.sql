-- Preserve native call foreign keys for retired Main threads; one current Main per Run.
DROP INDEX p0_main_root;
CREATE UNIQUE INDEX p0_main_root ON p0_roots(run_id) WHERE task_id IS NULL AND state <> 'archived_main';
