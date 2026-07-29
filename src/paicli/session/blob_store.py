from __future__ import annotations

import hashlib
import zlib
from pathlib import Path

from paicli.clock import normalize_timestamp, now_timestamp
from paicli.session.integrity import stored_blob_is_valid
from paicli.session.models import BlobReference, StoredBlob
from paicli.session.schema import connect


class BlobStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def put(self, data: bytes, *, content_type: str) -> StoredBlob:
        content_hash = hashlib.sha256(data).hexdigest()
        existing = self.get(content_hash)
        if existing is not None:
            return existing
        compressed = zlib.compress(data)
        if len(compressed) < len(data):
            compression = "zlib"
            stored_data = compressed
        else:
            compression = "none"
            stored_data = data
        now = now_timestamp()
        with connect(self.db_path) as connection:
            connection.execute(
                """
                insert or ignore into blobs(
                    content_hash, content_type, compression, original_size,
                    stored_size, data, created_at
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    content_hash,
                    content_type,
                    compression,
                    len(data),
                    len(stored_data),
                    stored_data,
                    now,
                ),
            )
        stored = self.get(content_hash)
        if stored is None:  # pragma: no cover - an insert or racing insert must win
            raise RuntimeError(f"blob disappeared after insert: {content_hash}")
        return stored

    def get(self, content_hash: str) -> StoredBlob | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                """
                select content_hash, content_type, compression, original_size,
                       stored_size, data, created_at
                from blobs
                where content_hash = ?
                """,
                (content_hash,),
            ).fetchone()
        if row is None:
            return None
        if not stored_blob_is_valid(row, content_hash):
            raise ValueError(f"blob content failed integrity validation: {content_hash}")
        raw = bytes(row["data"])
        compression = str(row["compression"])
        data = zlib.decompress(raw) if compression == "zlib" else raw
        return StoredBlob(
            content_hash=str(row["content_hash"]),
            content_type=str(row["content_type"]),
            compression=compression,
            original_size=int(row["original_size"]),
            stored_size=int(row["stored_size"]),
            data=data,
            created_at=normalize_timestamp(str(row["created_at"])),
        )

    def load_bytes(self, content_hash: str) -> bytes:
        blob = self.get(content_hash)
        if blob is None:
            raise KeyError(f"blob not found: {content_hash}")
        return blob.data

    def list_event_refs(self, event_id: str) -> tuple[BlobReference, ...]:
        with connect(self.db_path) as connection:
            rows = connection.execute(
                """
                select content_hash, role
                from event_blob_refs
                where event_id = ?
                order by ordinal
                """,
                (event_id,),
            ).fetchall()
        return tuple(
            BlobReference(content_hash=str(row["content_hash"]), role=str(row["role"]))
            for row in rows
        )

    def collect_orphans(self) -> int:
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                delete from blobs
                where not exists (
                    select 1
                    from event_blob_refs
                    where event_blob_refs.content_hash = blobs.content_hash
                )
                """
            )
            return cursor.rowcount
