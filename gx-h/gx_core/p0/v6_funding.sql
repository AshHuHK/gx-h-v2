-- Only financial changes invalidate a prepared branch allocation, including other
-- Runs spending under the same workspace cap. Ordinary heartbeats do not.
CREATE INDEX v6_funding_event_cursor ON events(event_id DESC)
WHERE subject_kind IN ('budget','project_budget','change');
