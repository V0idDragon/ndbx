CREATE KEYSPACE IF NOT EXISTS testkeyspace
WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};

USE testkeyspace;

CREATE TABLE IF NOT EXISTS event_reactions (
    event_id text,
    created_by text,
    like_value tinyint,
    created_at timestamp,
    PRIMARY KEY (event_id, created_by)
);