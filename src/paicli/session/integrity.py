from __future__ import annotations

import hashlib
import json
import zlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from paicli.session.models import BlobReference


@dataclass(frozen=True, slots=True)
class EventHashMaterial:
    event_id: str
    session_id: str
    sequence: int
    event_type: str
    payload_json: str
    schema_version: int
    created_at: str
    previous_event_hash: str | None
    turn_id: str | None
    idempotency_key: str | None
    source_session_id: str | None
    source_event_id: str | None
    blob_refs_json: str

    def digest(self) -> str:
        encoded = canonical_json(asdict(self)).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def blob_refs_json(references: tuple[BlobReference, ...]) -> str:
    return canonical_json(
        [
            {"content_hash": reference.content_hash, "role": reference.role}
            for reference in references
        ]
    )


def blob_refs_from_json(value: str) -> tuple[BlobReference, ...]:
    decoded = json.loads(value)
    if not isinstance(decoded, list):
        raise TypeError("blob references must be a JSON array")
    references: list[BlobReference] = []
    for item in decoded:
        if not isinstance(item, dict):
            raise TypeError("blob reference must be a JSON object")
        content_hash = item.get("content_hash")
        role = item.get("role")
        if not isinstance(content_hash, str) or not content_hash:
            raise TypeError("blob reference content_hash must be a non-empty string")
        if not isinstance(role, str) or not role:
            raise TypeError("blob reference role must be a non-empty string")
        references.append(BlobReference(content_hash=content_hash, role=role))
    return tuple(references)


def stored_blob_is_valid(
    row: Mapping[str, Any] | None,
    content_hash: str,
) -> bool:
    if row is None:
        return False
    stored = bytes(row["data"])
    compression = str(row["compression"])
    try:
        if compression == "zlib":
            raw = zlib.decompress(stored)
        elif compression == "none":
            raw = stored
        else:
            return False
    except zlib.error:
        return False
    return (
        len(stored) == int(row["stored_size"])
        and len(raw) == int(row["original_size"])
        and hashlib.sha256(raw).hexdigest() == content_hash
    )
