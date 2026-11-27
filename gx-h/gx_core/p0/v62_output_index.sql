CREATE INDEX IF NOT EXISTS v62_artifact_publication_digest
ON events(json_extract(payload_json,'$.sha256'),event_id)
WHERE kind='artifact.published';
