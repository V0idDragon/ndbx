CREATE KEYSPACE IF NOT EXISTS testkeyspace
WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};

USE testkeyspace;

CREATE TABLE IF NOT EXISTS event_reactions (
    event_id text,
    like_value tinyint,
    created_by text,
    created_at timestamp,
    PRIMARY KEY (event_id, created_by)
);

CREATE TABLE IF NOT EXISTS event_reviews (
    event_id text,
    created_by text,
    id uuid,
    rating tinyint,
    comment text,
    created_at timestamp,
    updated_at timestamp,
    PRIMARY KEY ((event_id), created_by)
);

CREATE INDEX IF NOT EXISTS event_reactions_like_value_idx ON event_reactions (like_value);
CREATE INDEX IF NOT EXISTS event_reactions_created_by_idx ON event_reactions (created_by);