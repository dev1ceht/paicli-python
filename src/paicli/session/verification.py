from __future__ import annotations

import json
from pathlib import Path

from paicli.session.integrity import (
    EventHashMaterial,
    blob_refs_from_json,
    stored_blob_is_valid,
)
from paicli.session.models import BlobReference
from paicli.session.schema import connect
from paicli.session.validation import validate_event_payload
from paicli.session.versions import EVENT_SCHEMA_VERSION


class SessionIntegrityVerifier:
    """Verify one session's event chain and referenced content without mutating it."""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def find_issue(self, session_id: str) -> str | None:
        rows, reference_rows, blob_rows = self._load_material(session_id)
        refs_by_event: dict[str, list[BlobReference]] = {}
        for row in reference_rows:
            refs_by_event.setdefault(str(row["event_id"]), []).append(
                BlobReference(
                    content_hash=str(row["content_hash"]),
                    role=str(row["role"]),
                )
            )
        blobs_by_hash = {str(row["content_hash"]): row for row in blob_rows}
        previous_hash: str | None = None

        for expected_sequence, row in enumerate(rows, start=1):
            issue = self._check_event(
                row,
                expected_sequence=expected_sequence,
                previous_hash=previous_hash,
                actual_refs=tuple(refs_by_event.get(str(row["id"]), [])),
                blobs_by_hash=blobs_by_hash,
            )
            if issue is not None:
                return issue
            previous_hash = str(row["event_hash"])
        return None

    def _load_material(self, session_id: str) -> tuple[list, list, list]:
        with connect(self.db_path) as connection:
            session = connection.execute(
                "select id from sessions where id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise KeyError(f"session not found: {session_id}")
            rows = connection.execute(
                """
                select id, session_id, sequence, turn_id, type, schema_version,
                       payload_json, idempotency_key, previous_event_hash, event_hash,
                       created_at, source_session_id, source_event_id, blob_refs_json
                from session_events
                where session_id = ?
                order by sequence
                """,
                (session_id,),
            ).fetchall()
            reference_rows = connection.execute(
                """
                select refs.event_id, refs.content_hash, refs.role, refs.ordinal
                from event_blob_refs as refs
                join session_events as events on events.id = refs.event_id
                where events.session_id = ?
                order by refs.event_id, refs.ordinal
                """,
                (session_id,),
            ).fetchall()
            blob_rows = connection.execute(
                """
                select distinct blobs.content_hash, blobs.compression, blobs.original_size,
                                blobs.stored_size, blobs.data
                from blobs
                join event_blob_refs as refs
                  on refs.content_hash = blobs.content_hash
                join session_events as events
                  on events.id = refs.event_id
                where events.session_id = ?
                """,
                (session_id,),
            ).fetchall()
        return rows, reference_rows, blob_rows

    @staticmethod
    def _check_event(
        row,
        *,
        expected_sequence: int,
        previous_hash: str | None,
        actual_refs: tuple[BlobReference, ...],
        blobs_by_hash: dict,
    ) -> str | None:
        if int(row["sequence"]) != expected_sequence:
            return f"event sequence gap: expected {expected_sequence}, got {row['sequence']}"
        schema_version = int(row["schema_version"])
        if not 1 <= schema_version <= EVENT_SCHEMA_VERSION:
            return f"unsupported event schema version: {schema_version}"
        try:
            event_payload = json.loads(str(row["payload_json"]))
            if not isinstance(event_payload, dict):
                raise TypeError("event payload must be a JSON object")
            validate_event_payload(str(row["type"]), event_payload)
            expected_refs = blob_refs_from_json(str(row["blob_refs_json"]))
        except (KeyError, TypeError, ValueError) as error:
            return f"invalid event payload at sequence {expected_sequence}: {error}"
        if row["previous_event_hash"] != previous_hash:
            return f"previous event hash mismatch at sequence {expected_sequence}"
        if actual_refs != expected_refs:
            return f"blob reference mismatch at sequence {expected_sequence}"
        invalid_blob = next(
            (
                reference.content_hash
                for reference in expected_refs
                if not stored_blob_is_valid(
                    blobs_by_hash.get(reference.content_hash),
                    reference.content_hash,
                )
            ),
            None,
        )
        if invalid_blob is not None:
            return f"missing or corrupt blob {invalid_blob} at sequence {expected_sequence}"
        expected_hash = EventHashMaterial(
            event_id=str(row["id"]),
            session_id=str(row["session_id"]),
            sequence=int(row["sequence"]),
            event_type=str(row["type"]),
            payload_json=str(row["payload_json"]),
            schema_version=schema_version,
            created_at=str(row["created_at"]),
            previous_event_hash=previous_hash,
            turn_id=row["turn_id"],
            idempotency_key=row["idempotency_key"],
            source_session_id=row["source_session_id"],
            source_event_id=row["source_event_id"],
            blob_refs_json=str(row["blob_refs_json"]),
        ).digest()
        if str(row["event_hash"]) != expected_hash:
            return f"event hash mismatch at sequence {expected_sequence}"
        return None
