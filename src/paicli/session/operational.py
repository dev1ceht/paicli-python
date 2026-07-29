from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any
from uuid import uuid4

from paicli.clock import east_eight_now, format_timestamp, normalize_timestamp
from paicli.session.errors import SessionLeaseConflictError
from paicli.session.integrity import canonical_json
from paicli.session.models import PendingAction, SessionLease, ToolActionSpec

_PENDING_COLUMNS = """
session_id, tool_call_id, turn_id, tool_name, arguments_json,
raw_call_json, status, is_read_only, is_idempotent, model_turn,
batch_index, approval_status, created_at, updated_at
"""


class SessionLeaseStore:
    """Own the short-lived, single-writer lease state machine."""

    @staticmethod
    def acquire(
        connection: sqlite3.Connection,
        session_id: str,
        *,
        owner_id: str,
        ttl_seconds: int,
    ) -> SessionLease:
        now = east_eight_now()
        refreshed_at = format_timestamp(now)
        expires_at = format_timestamp(now + timedelta(seconds=ttl_seconds))
        row = connection.execute(
            """
            select session_id, owner_id, token, acquired_at, refreshed_at, expires_at
            from session_leases
            where session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is not None and str(row["expires_at"]) > refreshed_at:
            if str(row["owner_id"]) != owner_id:
                raise SessionLeaseConflictError(
                    f"session is already open by another owner: {session_id}"
                )
            connection.execute(
                """
                update session_leases
                set refreshed_at = ?, expires_at = ?
                where session_id = ?
                """,
                (refreshed_at, expires_at, session_id),
            )
            return SessionLease(
                session_id=session_id,
                owner_id=owner_id,
                token=str(row["token"]),
                acquired_at=normalize_timestamp(str(row["acquired_at"])),
                refreshed_at=refreshed_at,
                expires_at=expires_at,
            )
        token = f"lease_{uuid4().hex}"
        connection.execute(
            """
            insert into session_leases(
                session_id, owner_id, token, acquired_at, refreshed_at, expires_at
            ) values (?, ?, ?, ?, ?, ?)
            on conflict(session_id) do update set
                owner_id = excluded.owner_id,
                token = excluded.token,
                acquired_at = excluded.acquired_at,
                refreshed_at = excluded.refreshed_at,
                expires_at = excluded.expires_at
            """,
            (session_id, owner_id, token, refreshed_at, refreshed_at, expires_at),
        )
        return SessionLease(
            session_id=session_id,
            owner_id=owner_id,
            token=token,
            acquired_at=refreshed_at,
            refreshed_at=refreshed_at,
            expires_at=expires_at,
        )

    @staticmethod
    def refresh(
        connection: sqlite3.Connection,
        session_id: str,
        token: str,
        *,
        ttl_seconds: int,
    ) -> SessionLease:
        now = east_eight_now()
        refreshed_at = format_timestamp(now)
        expires_at = format_timestamp(now + timedelta(seconds=ttl_seconds))
        row = connection.execute(
            """
            select owner_id, acquired_at
            from session_leases
            where session_id = ? and token = ? and expires_at > ?
            """,
            (session_id, token, refreshed_at),
        ).fetchone()
        if row is None:
            raise SessionLeaseConflictError(f"session lease is no longer owned: {session_id}")
        connection.execute(
            """
            update session_leases
            set refreshed_at = ?, expires_at = ?
            where session_id = ? and token = ?
            """,
            (refreshed_at, expires_at, session_id, token),
        )
        return SessionLease(
            session_id=session_id,
            owner_id=str(row["owner_id"]),
            token=token,
            acquired_at=normalize_timestamp(str(row["acquired_at"])),
            refreshed_at=refreshed_at,
            expires_at=expires_at,
        )

    @staticmethod
    def release(connection: sqlite3.Connection, session_id: str, token: str) -> bool:
        deleted = connection.execute(
            "delete from session_leases where session_id = ? and token = ?",
            (session_id, token),
        )
        return deleted.rowcount == 1

    @staticmethod
    def require_active_token(
        connection: sqlite3.Connection,
        session_id: str,
        token: str | None,
        *,
        now: str,
    ) -> None:
        lease = connection.execute(
            "select token, expires_at from session_leases where session_id = ?",
            (session_id,),
        ).fetchone()
        if lease is not None and (str(lease["token"]) != token or str(lease["expires_at"]) <= now):
            raise SessionLeaseConflictError(
                f"session write requires its active lease token: {session_id}"
            )


class PendingActionStore:
    """Own the normalized pending-action projection and its transitions."""

    @staticmethod
    def get(
        connection: sqlite3.Connection,
        session_id: str,
        tool_call_id: str,
    ) -> PendingAction | None:
        row = connection.execute(
            f"""
            select {_PENDING_COLUMNS}
            from pending_actions
            where session_id = ? and tool_call_id = ?
            """,
            (session_id, tool_call_id),
        ).fetchone()
        return _from_row(row) if row is not None else None

    @staticmethod
    def list(
        connection: sqlite3.Connection,
        session_id: str,
        *,
        include_settled: bool,
        turn_id: str | None = None,
    ) -> list[PendingAction]:
        conditions = ["session_id = ?"]
        values: list[Any] = [session_id]
        if not include_settled:
            conditions.append("status not in ('completed', 'abandoned')")
        if turn_id is not None:
            conditions.append("turn_id = ?")
            values.append(turn_id)
        rows = connection.execute(
            f"""
            select {_PENDING_COLUMNS}
            from pending_actions
            where {" and ".join(conditions)}
            order by model_turn, batch_index
            """,
            tuple(values),
        ).fetchall()
        return [_from_row(row) for row in rows]

    @staticmethod
    def insert_prepared(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        turn_id: str,
        model_turn: int,
        batch_index: int,
        action: ToolActionSpec,
        now: str,
    ) -> None:
        connection.execute(
            """
            insert into pending_actions(
                session_id, tool_call_id, turn_id, tool_name, arguments_json,
                raw_call_json, status, is_read_only, is_idempotent, model_turn,
                batch_index, approval_status, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, null, ?, ?)
            """,
            (
                session_id,
                action.tool_call_id,
                turn_id,
                action.tool_name,
                canonical_json(action.arguments),
                canonical_json(action.raw_call),
                int(action.is_read_only),
                int(action.is_idempotent),
                model_turn,
                batch_index,
                now,
                now,
            ),
        )

    @staticmethod
    def transition(
        connection: sqlite3.Connection,
        session_id: str,
        tool_call_id: str,
        *,
        status: str,
        approval_status: str | None,
        now: str,
        expected_statuses: tuple[str, ...],
        expected_approval: str | None | object = ...,
    ) -> bool:
        placeholders = ", ".join("?" for _ in expected_statuses)
        approval_clause = ""
        values: list[Any] = [status, approval_status, now, session_id, tool_call_id]
        if expected_approval is not ...:
            approval_clause = (
                " and approval_status is ?"
                if expected_approval is None
                else (" and approval_status = ?")
            )
            values.append(expected_approval)
        values.extend(expected_statuses)
        updated = connection.execute(
            f"""
            update pending_actions
            set status = ?, approval_status = ?, updated_at = ?
            where session_id = ? and tool_call_id = ?
              {approval_clause}
              and status in ({placeholders})
            """,
            tuple(values),
        )
        return updated.rowcount == 1

    @staticmethod
    def set_status(
        connection: sqlite3.Connection,
        session_id: str,
        tool_call_id: str,
        *,
        status: str,
        now: str,
        expected_statuses: tuple[str, ...],
    ) -> bool:
        placeholders = ", ".join("?" for _ in expected_statuses)
        updated = connection.execute(
            f"""
            update pending_actions
            set status = ?, updated_at = ?
            where session_id = ? and tool_call_id = ?
              and status in ({placeholders})
            """,
            (status, now, session_id, tool_call_id, *expected_statuses),
        )
        return updated.rowcount == 1


def _from_row(row: sqlite3.Row) -> PendingAction:
    return PendingAction(
        session_id=str(row["session_id"]),
        turn_id=str(row["turn_id"]),
        tool_call_id=str(row["tool_call_id"]),
        tool_name=str(row["tool_name"]),
        arguments=json.loads(row["arguments_json"]),
        raw_call=json.loads(row["raw_call_json"]),
        status=str(row["status"]),
        is_read_only=bool(row["is_read_only"]),
        is_idempotent=bool(row["is_idempotent"]),
        model_turn=int(row["model_turn"]),
        batch_index=int(row["batch_index"]),
        approval_status=(
            str(row["approval_status"]) if row["approval_status"] is not None else None
        ),
        created_at=normalize_timestamp(str(row["created_at"])),
        updated_at=normalize_timestamp(str(row["updated_at"])),
    )
