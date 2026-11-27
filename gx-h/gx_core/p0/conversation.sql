-- The original text and official Main response stay in the existing operator journal.
CREATE TABLE p0_conversation_messages (
    message_id TEXT PRIMARY KEY REFERENCES p0_operator_messages(message_id),
    conversation_id TEXT NOT NULL,
    goal_version INTEGER,
    reply_to TEXT,
    branch_id TEXT,
    question_id TEXT,
    envelope_sha256 TEXT NOT NULL,
    answer_json TEXT,
    successor_json TEXT
);
