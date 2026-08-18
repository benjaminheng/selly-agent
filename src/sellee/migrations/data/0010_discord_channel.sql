-- Drop the channel singleton's enumerating adapter CHECK. SQLite has no ALTER TABLE for a CHECK
-- constraint, so a constraint that names every adapter forces this whole recreate-copy-swap dance
-- again on the day a third provider lands. Adapter validity moves to store.KNOWN_ADAPTERS, checked
-- in arm_bind — the one write path, on the single write connection — leaving the column with only
-- the constraint that never needs revisiting. This is the same posture as the settings registry:
-- the code is the source of validity, not the schema.
--
-- Every other column is unchanged: a Discord DM channel id fits chat_id's INTEGER (a snowflake is
-- well within signed 64-bit range), and bot_username/commands_hash are generic enough to reuse
-- as-is (Discord leaves commands_hash NULL — no slash-command registration in this provider).

CREATE TABLE channel_new (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    adapter       TEXT NOT NULL DEFAULT 'telegram' CHECK (adapter <> ''),
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
