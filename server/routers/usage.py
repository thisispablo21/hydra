"""Token usage ingestion and reporting.

Ingest is idempotent by construction: `usage_messages.message_id` is the primary
key and every insert is `INSERT OR IGNORE`. That is what makes the client's
Stop-hook reporting safe to retry, makes `usage backfill` re-runnable, and stops
a resumed session (which copies prior history into a new transcript file, under
a new session_id) from counting the same API message twice.

Cost is never stored. `server/pricing.py` prices grouped rows on the way out, so
a rate correction retroactively fixes every figure.
"""

from datetime import UTC, datetime
from typing import Any, Literal

import aiosqlite
from fastapi import APIRouter, Depends, Header, Query

from server import pricing
from server.auth import require_auth
from server.db import get_db
from server.models import CodexReconcileBatch, CodexReconcileMessage, UsageBatch

router = APIRouter(
    prefix="/api/usage", tags=["usage"], dependencies=[Depends(require_auth)]
)

GroupBy = Literal["day", "model", "project", "instance", "harness", "agent"]

# The counter columns, in one place: summed in SQL, echoed in the response, and
# fed to the pricer. Adding a counter means touching only this tuple + schema.
_COUNTERS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "web_search_requests",
    "web_fetch_requests",
)

# Group-key SQL per `group_by`. Project resolution uses scalar subqueries so
# duplicate matching paths cannot fan out usage rows.
_PROJECT_SQL = (
    "COALESCE("
    " (SELECT pe.slug FROM project_paths pe"
    "  WHERE pe.path = u.cwd ORDER BY pe.slug LIMIT 1),"
    " (SELECT pp.slug FROM project_paths pp"
    "  JOIN projects pr ON pr.slug = pp.slug"
    "  WHERE pr.auto_registered_at IS NULL"
    "   AND substr(u.cwd, 1, length(pp.path) + 1)"
    "       IN (pp.path || '/', pp.path || '\\')"
    "  ORDER BY length(pp.path) DESC, pp.slug LIMIT 1),"
    " 'unregistered')"
)

_GROUP_SQL: dict[str, str] = {
    "day": "substr(u.ts, 1, 10)",
    "model": "u.model",
    "instance": "u.instance_id",
    "harness": "u.harness",
    "agent": (
        "CASE WHEN u.is_subagent = 0 THEN 'main'"
        " ELSE COALESCE(u.agent_type, 'subagent') END"
    ),
    "project": _PROJECT_SQL,
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


@router.post("/messages")
async def ingest_usage(
    batch: UsageBatch,
    x_instance_id: str = Header(default="unknown"),
):
    """Store a batch of per-message usage rows. Already-known message ids are
    ignored, so the caller can resend freely."""
    if not batch.messages:
        return {"inserted": 0, "ignored": 0}

    db = await get_db()
    ids = list({m.message_id for m in batch.messages})
    placeholders = ",".join("?" * len(ids))
    known = {
        row["message_id"]
        for row in await db.execute_fetchall(
            f"SELECT message_id FROM usage_messages WHERE message_id IN ({placeholders})",
            ids,
        )
    }

    received_at = _now()
    await db.executemany(
        "INSERT OR IGNORE INTO usage_messages ("
        " message_id, session_id, instance_id, harness, ts, cwd, model, effort,"
        " is_subagent, agent_type, service_tier, speed,"
        " input_tokens, output_tokens, cache_read_tokens,"
        " cache_write_5m_tokens, cache_write_1h_tokens,"
        " web_search_requests, web_fetch_requests, received_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                m.message_id, batch.session_id, x_instance_id, m.harness, m.ts, m.cwd,
                m.model, m.effort, int(m.is_subagent), m.agent_type,
                m.service_tier, m.speed,
                m.input_tokens, m.output_tokens, m.cache_read_tokens,
                m.cache_write_5m_tokens, m.cache_write_1h_tokens,
                m.web_search_requests, m.web_fetch_requests, received_at,
            )
            for m in batch.messages
        ],
    )
    # A row already ingested keeps its counters (INSERT OR IGNORE), so a later
    # sweep that learned the thread's tier can only reach it through an UPDATE.
    # Scoped to service_tier IS NULL: it fills a gap, never revises a tier, and
    # never touches a token count - row identity and spend stay immutable.
    backfill = [
        (m.service_tier, m.message_id)
        for m in batch.messages
        if m.message_id in known and m.service_tier
    ]
    if backfill:
        await db.executemany(
            "UPDATE usage_messages SET service_tier = ?"
            " WHERE message_id = ? AND service_tier IS NULL",
            backfill,
        )
    await db.commit()

    inserted = len(ids) - len(known)
    return {"inserted": inserted, "ignored": len(batch.messages) - inserted}


_RECONCILE_FIELDS = (
    "ts",
    "cwd",
    "model",
    "effort",
    "is_subagent",
    "agent_type",
    "service_tier",
    "speed",
    *_COUNTERS,
)


def _canonical_values(
    message: CodexReconcileMessage, stored: Any | None = None
) -> dict[str, Any]:
    values = {field: getattr(message, field) for field in _RECONCILE_FIELDS}
    values["is_subagent"] = int(values["is_subagent"])
    if stored is not None and values["service_tier"] is None:
        values["service_tier"] = stored["service_tier"]
    return values


async def _classify_reconciliation(db, batch: CodexReconcileBatch, instance_id: str):
    messages = {message.message_id: message for message in batch.messages}
    stored = {}
    if messages:
        placeholders = ",".join("?" * len(messages))
        rows = await db.execute_fetchall(
            f"SELECT * FROM usage_messages WHERE message_id IN ({placeholders})",
            list(messages),
        )
        stored = {row["message_id"]: row for row in rows}

    result = {
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "foreign_unchanged": 0,
        "conflicts": 0,
        "conflict_ids": [],
    }
    inserts = []
    updates = []
    for message_id, message in messages.items():
        row = stored.get(message_id)
        if row is None:
            result["inserted"] += 1
            inserts.append(message)
            continue
        if row["harness"] != "codex-cli":
            result["conflicts"] += 1
            result["conflict_ids"].append(message_id)
            continue

        values = _canonical_values(message, row)
        matches = all(row[field] == values[field] for field in _RECONCILE_FIELDS)
        if row["instance_id"] != instance_id:
            key = "foreign_unchanged" if matches else "conflicts"
            result[key] += 1
            if not matches:
                result["conflict_ids"].append(message_id)
        elif matches:
            result["unchanged"] += 1
        else:
            result["updated"] += 1
            updates.append((message_id, values))
    return result, inserts, updates


async def _open_reconcile_db():
    shared = await get_db()
    databases = await shared.execute_fetchall("PRAGMA database_list")
    path = next((row[2] for row in databases if row[1] == "main"), "")
    if not path:
        return shared, False
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    return db, True


@router.post("/reconcile/codex")
async def reconcile_codex_usage(
    batch: CodexReconcileBatch,
    x_instance_id: str = Header(min_length=1),
):
    """Preview or transactionally apply reparsed Codex usage rows."""
    if not batch.apply:
        db = await get_db()
        result, _inserts, _updates = await _classify_reconciliation(
            db, batch, x_instance_id
        )
        return {**result, "applied": False}

    db, close_db = await _open_reconcile_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        result, inserts, updates = await _classify_reconciliation(
            db, batch, x_instance_id
        )
        if result["conflicts"]:
            await db.rollback()
            return {**result, "applied": False}

        received_at = _now()
        if inserts:
            await db.executemany(
                "INSERT INTO usage_messages ("
                " message_id, session_id, instance_id, harness, ts, cwd, model, effort,"
                " is_subagent, agent_type, service_tier, speed,"
                " input_tokens, output_tokens, cache_read_tokens,"
                " cache_write_5m_tokens, cache_write_1h_tokens,"
                " web_search_requests, web_fetch_requests, received_at"
                ") VALUES (?, ?, ?, 'codex-cli', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        message.message_id,
                        message.session_id,
                        x_instance_id,
                        *(
                            _canonical_values(message)[field]
                            for field in _RECONCILE_FIELDS
                        ),
                        received_at,
                    )
                    for message in inserts
                ],
            )
        if updates:
            assignments = ", ".join(f"{field} = ?" for field in _RECONCILE_FIELDS)
            await db.executemany(
                f"UPDATE usage_messages SET {assignments}"
                " WHERE message_id = ? AND harness = 'codex-cli' AND instance_id = ?",
                [
                    (
                        *(values[field] for field in _RECONCILE_FIELDS),
                        message_id,
                        x_instance_id,
                    )
                    for message_id, values in updates
                ],
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        if close_db:
            await db.close()
    return {**result, "applied": True}


_COST_PARTS = (
    "input", "output", "cache_read", "cache_write_5m", "cache_write_1h", "web_search",
)


def _blank_row(key: str) -> dict[str, Any]:
    row: dict[str, Any] = {"key": key, "messages": 0}
    row.update({c: 0 for c in _COUNTERS})
    row["cost_usd"] = 0.0
    row["cost_components"] = {p: 0.0 for p in _COST_PARTS}
    row["unpriced_messages"] = 0
    return row


def _fold(
    target: dict[str, Any],
    src: dict[str, Any],
    parts: dict[str, float] | None,
    *,
    flag_unpriced: bool = True,
) -> None:
    """Accumulate one (key, model, service_tier) bucket into a group row.

    Cost has to be summed per model and tier, because the rate table is per
    model and the tier multiplies it - a group that mixes either cannot be
    priced from its summed counters.
    """
    target["messages"] += src["messages"]
    for c in _COUNTERS:
        target[c] += src[c]
    if parts is None:
        if flag_unpriced:
            target["unpriced_messages"] += src["messages"]
        return
    target["cost_usd"] += sum(parts.values())
    for name, value in parts.items():
        target["cost_components"][name] += value


@router.get("/summary")
async def usage_summary(
    group_by: GroupBy = "day",
    since: str | None = Query(default=None, description="ISO date/datetime, inclusive"),
    until: str | None = Query(default=None, description="ISO date/datetime, exclusive"),
    instance: str | None = Query(default=None, description="restrict to one machine"),
    harness: str | None = Query(default=None, description="restrict to one harness"),
):
    """Grouped token totals plus reconstructed cost.

    Rows are aggregated in SQL per (group key, model, service_tier) and folded
    in Python, so each model's counters are priced at its own rate before being
    summed into the group. The tier is in the key for the same reason the model
    is: it scales the bill, so a group mixing tiers cannot be priced from its
    summed counters. Messages on a model the rate table doesn't know contribute
    their tokens but no cost, and are counted in `unpriced_messages` - unless
    the model is in `pricing.KNOWN_UNPRICED`, which is reported separately.
    """
    db = await get_db()
    key_sql = _GROUP_SQL[group_by]

    where = []
    params: list[Any] = []
    if since:
        where.append("u.ts >= ?")
        params.append(since)
    if until:
        where.append("u.ts < ?")
        params.append(until)
    if instance:
        where.append("u.instance_id = ?")
        params.append(instance)
    if harness:
        where.append("u.harness = ?")
        params.append(harness)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    sums = ", ".join(f"SUM(u.{c}) AS {c}" for c in _COUNTERS)
    rows = await db.execute_fetchall(
        f"SELECT {key_sql} AS key, u.model AS model,"
        f" u.service_tier AS service_tier, COUNT(*) AS messages, {sums}"
        f" FROM usage_messages u{where_sql}"
        " GROUP BY key, u.model, u.service_tier",
        params,
    )

    grouped: dict[str, dict[str, Any]] = {}
    totals = _blank_row("total")
    unpriced_models: set[str] = set()
    known_unpriced: set[str] = set()
    for row in rows:
        bucket = dict(row)
        parts = pricing.cost_components(
            bucket["model"],
            service_tier=bucket["service_tier"],
            input_tokens=bucket["input_tokens"],
            output_tokens=bucket["output_tokens"],
            cache_read_tokens=bucket["cache_read_tokens"],
            cache_write_5m_tokens=bucket["cache_write_5m_tokens"],
            cache_write_1h_tokens=bucket["cache_write_1h_tokens"],
            web_search_requests=bucket["web_search_requests"],
        )
        flag = bucket["model"] not in pricing.KNOWN_UNPRICED
        if parts is None:
            (unpriced_models if flag else known_unpriced).add(bucket["model"])
        target = grouped.setdefault(str(bucket["key"]), _blank_row(str(bucket["key"])))
        _fold(target, bucket, parts, flag_unpriced=flag)
        _fold(totals, bucket, parts, flag_unpriced=flag)

    out = list(grouped.values())
    if group_by == "day":
        out.sort(key=lambda r: r["key"], reverse=True)
    else:
        out.sort(key=lambda r: r["cost_usd"] or 0, reverse=True)

    return {
        "group_by": group_by,
        "since": since,
        "until": until,
        "instance": instance,
        "harness": harness,
        "rows": out,
        "totals": totals,
        "unpriced_models": sorted(unpriced_models),
        "known_unpriced_models": sorted(known_unpriced),
    }
