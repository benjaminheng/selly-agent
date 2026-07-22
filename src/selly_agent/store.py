"""Typed accessors over the business database — the one writer for items, floors, and passes.

Every state change the LLM can cause lands here as an explicit function on the single write
connection, in a real transaction. Two disciplines are load-bearing:

  * The floor is confidential. It lives in its own table and is never returned by a read an
    LLM-facing tool can call — only the publish gate and (later) the engines load it. set_floor
    is the one hardened writer: it validates 0 < floor <= list_price (list price from the item
    record, never the caller), records provenance, refuses to let a `default` write clobber a
    seller value, requires force to replace one seller value with another, and never emits the
    value. The check and the write share one transaction so a race can't clobber a just-set
    seller floor with a default.

  * update_item is field-constrained: transcript-style fields don't exist here, listing_urls is
    written only by the publish path (never a hand-edit), and status moves only between draft and
    ready — the sale-state transitions belong to their owning flow, not a generic writer.

Passes are claimed single-flight: claim_queued_pass stamps running + started_ts in one
transaction, so two claimers never take the same row; a crash mid-pass is failed loudly by the
stale-running sweep, never silently re-run.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from .db import Database

# Fields a caller may set on an item. listing_urls is deliberately absent — it is written only
# after a live listing verify, by the publish path. Sale-state transitions are not here either.
_ITEM_WRITABLE = ("title", "description", "condition", "list_price", "currency", "status")
_ITEM_STATUSES = ("draft", "ready")

_FLOOR_SOURCES = ("seller", "default")

_PASS_TERMINAL = ("done", "error")


class StoreError(Exception):
    """An expected, caller-facing store failure (bad input, not found, refused overwrite).

    Tools translate this into a structured tool error; it never carries a secret value.
    """


class ItemNotFound(StoreError):
    pass


@dataclass(frozen=True)
class ClaimedPass:
    pass_id: str
    type: str
    payload: dict


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _item_from_row(row) -> dict:
    """The buyer-safe item view — never a floor."""
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "condition": row["condition"],
        "list_price": row["list_price"],
        "currency": row["currency"],
        "status": row["status"],
        "listing_urls": json.loads(row["listing_urls"]),
        "created_ts": row["created_ts"],
        "updated_ts": row["updated_ts"],
    }


class Store:
    """Typed access to selly.db, serialized behind the single write connection."""

    def __init__(self, db: Database):
        self._db = db

    # --- items: reads -----------------------------------------------------------------------

    def get_item(self, item_id: str) -> dict | None:
        rows = self._db.query("SELECT * FROM items WHERE id = ?", (item_id,))
        return _item_from_row(rows[0]) if rows else None

    def list_items(self, status: str | None = None) -> list[dict]:
        if status is None:
            rows = self._db.query("SELECT * FROM items ORDER BY created_ts DESC")
        else:
            rows = self._db.query(
                "SELECT * FROM items WHERE status = ? ORDER BY created_ts DESC", (status,)
            )
        return [_item_from_row(r) for r in rows]

    # --- items: writes ----------------------------------------------------------------------

    def create_item(
        self,
        *,
        title: str,
        list_price: float,
        currency: str | None = None,
        description: str = "",
        condition: str | None = None,
    ) -> dict:
        if not title or not title.strip():
            raise StoreError("title must be non-empty")
        item_id = _new_id("item")
        ts = _now()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO items "
                "(id, title, description, condition, list_price, currency, status, "
                " listing_urls, created_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, 'draft', '{}', ?, ?)",
                (
                    item_id,
                    title.strip(),
                    description or "",
                    condition,
                    list_price,
                    currency,
                    ts,
                    ts,
                ),
            )
        return self.get_item(item_id)  # type: ignore[return-value]

    def update_item(self, item_id: str, fields: dict) -> dict:
        if "listing_urls" in fields:
            raise StoreError(
                "listing_urls is not writable here — it is recorded by "
                "carousell_ai_publish_listing after the listing is verified live"
            )
        unknown = [k for k in fields if k not in _ITEM_WRITABLE]
        if unknown:
            raise StoreError(
                f"unknown or non-writable field(s): {', '.join(sorted(unknown))}; "
                f"writable: {', '.join(_ITEM_WRITABLE)}"
            )
        if "status" in fields and fields["status"] not in _ITEM_STATUSES:
            raise StoreError(
                f"status may only move between {_ITEM_STATUSES}; sale-state transitions are "
                "owned by their own flow"
            )
        if not fields:
            raise StoreError("no fields to update")

        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [fields[name] for name in fields]
        with self._db.transaction() as conn:
            exists = conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone()
            if not exists:
                raise ItemNotFound(f"no item with id {item_id!r}")
            conn.execute(
                f"UPDATE items SET {assignments}, updated_ts = ? WHERE id = ?",
                (*values, _now(), item_id),
            )
        return self.get_item(item_id)  # type: ignore[return-value]

    def record_listing_url(self, item_id: str, market: str, url: str) -> dict:
        """Merge one verified listing URL into the item's listing_urls map. The one writer of
        that field — a live verify has already passed before this is called."""
        with self._db.transaction() as conn:
            row = conn.execute("SELECT listing_urls FROM items WHERE id = ?", (item_id,)).fetchone()
            if not row:
                raise ItemNotFound(f"no item with id {item_id!r}")
            urls = json.loads(row["listing_urls"])
            urls[market] = url
            conn.execute(
                "UPDATE items SET listing_urls = ?, updated_ts = ? WHERE id = ?",
                (json.dumps(urls, sort_keys=True), _now(), item_id),
            )
        return self.get_item(item_id)  # type: ignore[return-value]

    # --- floors -----------------------------------------------------------------------------

    def get_floor(self, item_id: str) -> dict | None:
        """Internal only: the confidential floor record. No LLM-facing tool calls this."""
        rows = self._db.query("SELECT * FROM floors WHERE item_id = ?", (item_id,))
        if not rows:
            return None
        row = rows[0]
        return {
            "item_id": row["item_id"],
            "floor": row["floor"],
            "currency": row["currency"],
            "source": row["source"],
            "updated_ts": row["updated_ts"],
        }

    def set_floor(self, item_id: str, floor: float, source: str, force: bool = False) -> dict:
        """The one hardened floor writer. Returns an ack carrying provenance only — never the
        value. Raises StoreError on invalid input or a refused overwrite."""
        if source not in _FLOOR_SOURCES:
            raise StoreError(f"source must be one of {_FLOOR_SOURCES}, got {source!r}")
        if not isinstance(floor, (int, float)) or isinstance(floor, bool) or floor <= 0:
            raise StoreError("floor must be a positive number")
        with self._db.transaction() as conn:
            item = conn.execute(
                "SELECT list_price, currency FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if not item:
                raise ItemNotFound(f"no item with id {item_id!r}")
            list_price = item["list_price"]
            if not isinstance(list_price, (int, float)) or list_price <= 0:
                raise StoreError(f"item {item_id!r} has no valid list price to bound the floor")
            if floor > list_price:
                raise StoreError(
                    "floor is above the list price — lower the floor or raise the "
                    "listing price first"
                )
            existing = conn.execute(
                "SELECT source FROM floors WHERE item_id = ?", (item_id,)
            ).fetchone()
            replaced = existing["source"] if existing else None
            if replaced == "seller" and not (source == "seller" and force):
                raise StoreError(
                    "a seller-set floor already exists for this item — refusing to overwrite "
                    "(an explicit seller correction with force is required to change it)"
                )
            conn.execute(
                "INSERT INTO floors (item_id, floor, currency, source, updated_ts) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (item_id) DO UPDATE SET "
                "floor = excluded.floor, currency = excluded.currency, "
                "source = excluded.source, updated_ts = excluded.updated_ts",
                (item_id, floor, item["currency"], source, _now()),
            )
        return {"status": "written", "item_id": item_id, "source": source, "replaced": replaced}

    # --- passes -----------------------------------------------------------------------------

    def enqueue_pass(self, pass_type: str, payload: dict) -> str:
        pass_id = _new_id("pass")
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO passes (pass_id, type, payload, status, requested_ts) "
                "VALUES (?, ?, ?, 'queued', ?)",
                (pass_id, pass_type, json.dumps(payload, sort_keys=True), _now()),
            )
        return pass_id

    def claim_queued_pass(self) -> ClaimedPass | None:
        """Claim the oldest queued pass, stamping it running in the same transaction so two
        claimers never take the same row. Returns None when the queue is empty."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT pass_id, type, payload FROM passes WHERE status = 'queued' "
                "ORDER BY requested_ts ASC, pass_id ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE passes SET status = 'running', started_ts = ? WHERE pass_id = ?",
                (_now(), row["pass_id"]),
            )
            return ClaimedPass(
                pass_id=row["pass_id"], type=row["type"], payload=json.loads(row["payload"])
            )

    def finish_pass(
        self,
        pass_id: str,
        *,
        status: str,
        rc: int | None = None,
        cls: str | None = None,
        summary: str | None = None,
    ) -> None:
        if status not in _PASS_TERMINAL:
            raise StoreError(f"a finished pass status must be one of {_PASS_TERMINAL}")
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE passes SET status = ?, rc = ?, class = ?, summary = ?, finished_ts = ? "
                "WHERE pass_id = ?",
                (status, rc, cls, summary, _now(), pass_id),
            )

    def get_pass(self, pass_id: str) -> dict | None:
        rows = self._db.query("SELECT * FROM passes WHERE pass_id = ?", (pass_id,))
        if not rows:
            return None
        row = rows[0]
        return {
            "pass_id": row["pass_id"],
            "type": row["type"],
            "payload": json.loads(row["payload"]),
            "status": row["status"],
            "rc": row["rc"],
            "class": row["class"],
            "summary": row["summary"],
            "requested_ts": row["requested_ts"],
            "started_ts": row["started_ts"],
            "finished_ts": row["finished_ts"],
        }

    def count_queued_passes(self) -> int:
        rows = self._db.query("SELECT COUNT(*) AS n FROM passes WHERE status = 'queued'")
        return rows[0]["n"]

    def fail_stale_running(self, max_age_sec: float, now: float | None = None) -> list[str]:
        """Fail (never re-run) any pass stuck in `running` past `max_age_sec` — a crash mid-pass.
        Returns the pass_ids failed so the caller can ledger each one loudly."""
        cutoff = (now if now is not None else _now()) - max_age_sec
        with self._db.transaction() as conn:
            stale = conn.execute(
                "SELECT pass_id FROM passes WHERE status = 'running' AND started_ts < ?",
                (cutoff,),
            ).fetchall()
            pass_ids = [r["pass_id"] for r in stale]
            if pass_ids:
                placeholders = ",".join("?" for _ in pass_ids)
                conn.execute(
                    f"UPDATE passes SET status = 'error', class = 'stale', finished_ts = ? "
                    f"WHERE pass_id IN ({placeholders})",
                    (_now(), *pass_ids),
                )
        return pass_ids
