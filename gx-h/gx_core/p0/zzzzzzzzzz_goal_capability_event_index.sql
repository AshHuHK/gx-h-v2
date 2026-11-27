CREATE INDEX goal_capability_events ON events(run_id,event_id DESC)
WHERE kind='research.capabilities.changed';
