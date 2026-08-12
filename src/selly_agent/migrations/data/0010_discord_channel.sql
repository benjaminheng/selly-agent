-- Widen the channel singleton's adapter CHECK to allow 'discord' alongside 'telegram'. SQLite has
-- no ALTER TABLE for a CHECK constraint, so this recreates the table with the new constraint,
-- copies the one existing row (if any), and swaps it in. Every other column is unchanged: a
-- Discord DM channel id fits chat_id's INTEGER (a snowflake is well within signed 64-bit range),
-- and bot_username/commands_hash are generic enough to reuse as-is (Discord leaves
-- commands_hash NULL — no slash-command registration in this provider).

CREATE TABLE channel_new (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    adapter       TEXT NOT NULL DEFAULT 'telegram' CHECK (adapter IN ('telegram', 'discord')),
    bot_username  TEXT,
    chat_id       INTEGER,
    update_offset INTEGER NOT NULL DEFAULT 0,
    bind_nonce    TEXT,
    welcomed_at   REAL,
    commands_hash TEXT,
    bound_ts      REAL,
    updated_ts    REAL NOT NULL
);

INSERT INTO channel_new
    SELECT id, adapter, bot_username, chat_id, update_offset, bind_nonce, welcomed_at,
           commands_hash, bound_ts, updated_ts
    FROM channel;

DROP TABLE channel;
ALTER TABLE channel_new RENAME TO channel;
