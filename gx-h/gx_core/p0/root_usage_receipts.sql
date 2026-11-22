-- An aggregate notification can repeat an earlier value if the last paid
-- response omitted usage. Match its delta with measured response receipts.
-- NULL preserves the ambiguity of existing open reservations on upgrade.
ALTER TABLE p0_root_turn_usage ADD COLUMN measured_tokens INTEGER;
ALTER TABLE p0_root_turn_usage ADD COLUMN children_known INTEGER NOT NULL DEFAULT 0;
ALTER TABLE p0_root_turn_usage ADD COLUMN metering_reason TEXT;
ALTER TABLE p0_native_child_usage ADD COLUMN measured_tokens INTEGER;
ALTER TABLE p0_native_child_usage ADD COLUMN children_known INTEGER NOT NULL DEFAULT 0;
