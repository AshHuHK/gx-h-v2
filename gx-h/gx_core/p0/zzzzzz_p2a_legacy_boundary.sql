-- Preserve which unitemized historical settlement preceded a newly observed host Turn.
ALTER TABLE p0_meter_receipt_intervals ADD COLUMN started_after_legacy_sequence INTEGER;
