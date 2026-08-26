-- Sign-in requests the seller made from chat, waiting for the browser lane to serve them.
--
-- A row exists only while a request is outstanding; the lane deletes it the moment it has an
-- answer for the seller (including "still signed out" — that is an answer). So this is a handoff
-- between two threads, not a history: the durable record of what happened is the notice queued
-- back to the seller plus the browser.login event.
--
-- The market is the primary key on purpose. Opening a marketplace navigates the daemon's one
-- shared tab, so two requests for the same market must never both run; a seller double-tapping
-- the button upserts the row they already have instead of queueing a second navigation. Requests
-- for *different* markets are still separate rows — the lane serves them one at a time.
--
-- requested_ts is what lets a request that the browser could never get to (a publish pass holding
-- the tab for the whole window) be dropped with a notice rather than retried forever.
CREATE TABLE market_connect_requests (
    market       TEXT PRIMARY KEY,
    mode         TEXT NOT NULL CHECK (mode IN ('open', 'probe')),
    requested_ts REAL NOT NULL
);
