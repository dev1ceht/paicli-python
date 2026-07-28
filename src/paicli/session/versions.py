from __future__ import annotations

from collections.abc import Callable
from typing import Any

DATABASE_SCHEMA_VERSION = 2
EVENT_SCHEMA_VERSION = 1

EventUpcaster = Callable[[str, dict[str, Any]], dict[str, Any]]
_EVENT_UPCASTERS: dict[int, EventUpcaster] = {}


def upcast_event_payload(
    event_type: str,
    payload: dict[str, Any],
    schema_version: int,
) -> dict[str, Any]:
    """Return the current in-memory payload without rewriting the stored event."""

    if schema_version > EVENT_SCHEMA_VERSION:
        raise ValueError(f"unsupported future event schema version: {schema_version}")
    current = schema_version
    result = dict(payload)
    while current < EVENT_SCHEMA_VERSION:
        upcaster = _EVENT_UPCASTERS.get(current)
        if upcaster is None:
            raise ValueError(f"no event upcaster registered for schema version {current}")
        result = upcaster(event_type, result)
        current += 1
    return result
