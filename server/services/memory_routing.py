import asyncio
import json
import re
from collections.abc import Mapping
from typing import Any

import aiosqlite
from fastapi import HTTPException

from server.models import MemoryItem

MEMORY_WRITE_LOCK = asyncio.Lock()
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def memory_item(row: Mapping[str, Any]) -> MemoryItem:
    values = dict(row)
    values["topics"] = json.loads(values["topics"]) if values.get("topics") is not None else None
    return MemoryItem(**values)


def encode_topics(topics: list[str] | None) -> str | None:
    return json.dumps(topics) if topics is not None else None


async def validate_topics(db: aiosqlite.Connection, topics: list[str] | None) -> None:
    if topics is None:
        return
    if len(set(topics)) != len(topics):
        raise HTTPException(status_code=422, detail="Duplicate topic memberships")
    if any(not _SLUG_RE.fullmatch(slug) or slug == "catalog" for slug in topics):
        raise HTTPException(status_code=422, detail="Invalid topic slug")
    known = {row[0] for row in await db.execute_fetchall("SELECT slug FROM memory_topics")}
    unknown = sorted(set(topics) - known)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown topic: {unknown[0]}")
