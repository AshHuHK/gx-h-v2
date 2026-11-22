-- Runs with paid native history when the v2 meter is first upgraded retain
-- their optional diagnostic path. Fresh projects use only the public protocol.
CREATE TABLE p0_meter_legacy_runs (run_id TEXT PRIMARY KEY REFERENCES runs);
INSERT INTO p0_meter_legacy_runs
SELECT DISTINCT run_id FROM p0_root_deliveries WHERE method='turn/start'
AND NOT EXISTS (SELECT 1 FROM schema_meta WHERE key='p0.migration.z_v21_metering.sql');
