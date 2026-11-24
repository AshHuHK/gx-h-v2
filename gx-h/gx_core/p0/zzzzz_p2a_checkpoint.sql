-- A terminal aggregate may reconcile a common funding pool only after its Turn grace.
ALTER TABLE p0_meter_receipt_intervals ADD COLUMN pending_checkpoint INTEGER;
ALTER TABLE p0_meter_receipt_intervals ADD COLUMN applied_checkpoint INTEGER;
