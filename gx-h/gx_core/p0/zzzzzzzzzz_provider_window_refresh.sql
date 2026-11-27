-- Coalesce reads of the same blocked quota window across Drivers and restarts.
ALTER TABLE p0_provider_limits ADD COLUMN refresh_after REAL;
