import json
from datetime import UTC, datetime

import aiosqlite


def instant(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


async def append_revision(
    db: aiosqlite.Connection,
    common: str,
    variants: dict[str, dict[str, str]],
    *,
    published_at: str | None,
    source: str,
    author_instance_id: str | None = None,
    author_session_id: str | None = None,
) -> int:
    cursor = await db.execute(
        """INSERT INTO document_versions
               (document_name, snapshot, published_at, recorded_at, source,
                author_instance_id, author_session_id)
           VALUES ('instructions', ?, ?, ?, ?, ?, ?)""",
        (
            json.dumps({"common": common, "variants": variants}),
            published_at,
            now(),
            source,
            author_instance_id,
            author_session_id,
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def seed_instructions(db: aiosqlite.Connection) -> None:
    if await db.execute_fetchall(
        "SELECT 1 FROM document_versions WHERE document_name = 'instructions' LIMIT 1"
    ):
        return
    rows = await db.execute_fetchall(
        "SELECT variant, body FROM skill_variants WHERE name = 'instructions'"
    )
    common = next((row[1] for row in rows if row[0] == "common"), None)
    if common is None:
        return
    variants = {row[0]: json.loads(row[1]) for row in rows if row[0] != "common"}
    await append_revision(db, common, variants, published_at=None, source="seed")
