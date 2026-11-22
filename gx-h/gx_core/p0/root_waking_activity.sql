-- A native "interacted" item covers both send_message and followup_task.
-- Retain its call ID until the child's turn or an exact send-only receipt arrives.
ALTER TABLE p0_native_child_meters ADD COLUMN awaiting_call_id TEXT;
