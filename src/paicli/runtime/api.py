from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from paicli.agent import QueryEngine
from paicli.bootstrap import build_tool_registry
from paicli.cancellation import (
    CancellationToken,
    TaskCanceled,
    await_with_cancellation,
    raise_if_cancelled,
)
from paicli.config import PaiCliConfig
from paicli.llm import create_llm_client
from paicli.runtime.tasks import DurableTaskManager
from paicli.tools.base import ApprovalPending


class RuntimeApiServer:
    def __init__(
        self,
        *,
        cwd: str,
        config: PaiCliConfig,
        api_key: str,
        port: int = 8080,
        workers: int = 2,
    ):
        self.cwd = str(Path(cwd).resolve())
        self.config = config
        self.api_key = api_key
        self.port = port
        self.db_path = Path.home() / ".paicli" / "runtime" / "runtime.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.task_manager = DurableTaskManager(Path.home() / ".paicli" / "tasks" / "tasks.db")
        self.workers = workers
        self._stop = threading.Event()
        self._task_cancellations: dict[str, CancellationToken] = {}
        self._task_cancellations_lock = threading.Lock()
        self._ensure_schema()

    def serve_forever(self) -> None:
        self.task_manager.fail_interrupted_tasks()
        for index in range(self.workers):
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"paicli-task-{index}",
                daemon=True,
            )
            thread.start()

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                outer._handle(self)

            def do_GET(self) -> None:  # noqa: N802
                outer._handle(self)

            def log_message(self, _format: str, *args: Any) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        print(f"PaiCLI Runtime API listening on http://127.0.0.1:{self.port}", flush=True)
        try:
            server.serve_forever()
        finally:
            self._stop.set()

    def _handle(self, request: BaseHTTPRequestHandler) -> None:
        if not self._authorized(request):
            _send_json(request, 401, {"error": "unauthorized"})
            return
        method = request.command
        path = request.path.split("?", 1)[0]
        body = _read_json(request)
        try:
            if method == "POST" and path == "/v1/threads":
                thread_id = self._create_thread()
                _send_json(request, 200, {"id": thread_id})
            elif method == "POST" and path.startswith("/v1/threads/") and path.endswith("/turns"):
                thread_id = path.split("/")[3]
                message = str(body.get("message") or body.get("prompt") or "")
                if not message:
                    _send_json(request, 400, {"error": "message is required"})
                    return
                result = asyncio.run(self._run_turn(thread_id, message))
                _send_json(request, 200, result)
            elif method == "GET" and path.startswith("/v1/threads/") and path.endswith("/events"):
                thread_id = path.split("/")[3]
                self._send_events(request, thread_id)
            elif method == "POST" and path == "/v1/tasks":
                prompt = str(body.get("message") or body.get("prompt") or "")
                if not prompt:
                    _send_json(request, 400, {"error": "message is required"})
                    return
                task_id = self.task_manager.add(prompt)
                _send_json(request, 200, {"id": task_id, "status": "queued"})
            elif method == "GET" and path == "/v1/tasks":
                _send_json(
                    request,
                    200,
                    {"tasks": [task.to_dict() for task in self.task_manager.list()]},
                )
            elif method == "GET" and path.startswith("/v1/tasks/"):
                task = self.task_manager.get(path.split("/")[3])
                payload = task.to_dict() if task else {"error": "not found"}
                if task:
                    payload["approvals"] = [
                        approval.to_dict()
                        for approval in self.task_manager.list_approvals(task.id)
                    ]
                _send_json(request, 200 if task else 404, payload)
            elif method == "POST" and path.startswith("/v1/tasks/") and path.endswith("/retry"):
                task_id = path.split("/")[3]
                if not self.task_manager.get(task_id):
                    _send_json(request, 404, {"error": "not found"})
                    return
                retry_id = self.task_manager.retry(task_id)
                if not retry_id:
                    _send_json(request, 409, {"error": "only failed tasks can be retried"})
                    return
                _send_json(request, 200, {"id": retry_id, "status": "queued", "retry_of": task_id})
            elif method == "POST" and path.startswith("/v1/tasks/") and path.endswith("/approve"):
                task_id = path.split("/")[3]
                if not self.task_manager.get(task_id):
                    _send_json(request, 404, {"error": "not found"})
                    return
                approved = self.task_manager.approve(task_id, source="api")
                if not approved:
                    _send_json(request, 409, {"error": "task is not waiting for approval"})
                    return
                _send_json(request, 200, {"approved": True, "status": "queued"})
            elif method == "POST" and path.startswith("/v1/tasks/") and path.endswith("/deny"):
                task_id = path.split("/")[3]
                if not self.task_manager.get(task_id):
                    _send_json(request, 404, {"error": "not found"})
                    return
                denied = self.task_manager.deny(task_id, source="api")
                if not denied:
                    _send_json(request, 409, {"error": "task is not waiting for approval"})
                    return
                _send_json(request, 200, {"denied": True, "status": "queued"})
            elif method == "POST" and path.startswith("/v1/tasks/") and path.endswith("/cancel"):
                task_id = path.split("/")[3]
                _send_json(request, 200, {"canceled": self._cancel_task(task_id)})
            else:
                _send_json(request, 404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001 - API boundary
            _send_json(request, 500, {"error": str(exc)})

    async def _run_turn(self, thread_id: str, message: str) -> dict[str, Any]:
        self._ensure_llm_key()
        self._append_event(thread_id, "turn.started", {"message": message})
        registry, _manager = await build_tool_registry(config=self.config, cwd=self.cwd)
        engine = QueryEngine(
            llm_client=create_llm_client(self.config.llm),
            tool_registry=registry,
            config=self.config,
            cwd=self.cwd,
        )
        text = ""
        async for event in engine.ask(message):
            event_type = str(event.get("type"))
            if event_type == "text_delta":
                text += str(event.get("text") or "")
                self._append_event(thread_id, "message.delta", {"text": event.get("text") or ""})
            elif event_type in {"tool_call", "tool_result", "error", "done"}:
                self._append_event(thread_id, event_type, _jsonable(event))
        self._append_event(thread_id, "turn.completed", {"text": text})
        return {"thread_id": thread_id, "text": text}

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            task = self.task_manager.claim_next()
            if not task:
                time.sleep(0.5)
                continue
            cancellation = CancellationToken()
            with self._task_cancellations_lock:
                self._task_cancellations[task.id] = cancellation
            current = self.task_manager.get(task.id)
            if not current or current.status != "running":
                self._clear_task_cancellation(task.id, cancellation)
                continue
            try:
                result = asyncio.run(self._run_task(task.id, task.prompt, cancellation))
                self.task_manager.complete(task.id, result)
            except ApprovalPending:
                pass
            except TaskCanceled:
                pass
            except Exception as exc:  # noqa: BLE001
                self.task_manager.fail(task.id, str(exc))
            finally:
                self._clear_task_cancellation(task.id, cancellation)

    async def _run_task(
        self,
        task_id: str,
        prompt: str,
        cancellation: CancellationToken | None = None,
    ) -> str:
        cancellation_check = cancellation.is_set if cancellation else None
        raise_if_cancelled(cancellation_check)
        self._ensure_llm_key()
        registry, _manager = await build_tool_registry(config=self.config, cwd=self.cwd)
        execution_state = self.task_manager.get_checkpoint(task_id)
        runtime_identity = self._runtime_identity(registry)
        if execution_state and execution_state.get("runtime_identity") != runtime_identity:
            approvals = self.task_manager.list_approvals(task_id)
            request = execution_state.get("approval_request")
            if not isinstance(request, dict):
                request = approvals[-1].request if approvals else {}
            execution_state.pop("approval_decision", None)
            execution_state["approval_request"] = request
            execution_state["runtime_identity"] = runtime_identity
            execution_state["approval_context_stale"] = True
            approval = self.task_manager.wait_for_approval(
                task_id,
                checkpoint=execution_state,
                request=request,
                invalidation_reason="runtime_identity_changed",
            )
            if not approval:
                raise TaskCanceled()
            raise ApprovalPending()

        def checkpoint_callback(
            state: dict[str, Any], request: dict[str, Any]
        ) -> None:
            state["runtime_identity"] = runtime_identity
            approval = self.task_manager.wait_for_approval(
                task_id,
                checkpoint=state,
                request=request,
            )
            if not approval:
                raise TaskCanceled()

        engine = QueryEngine(
            llm_client=create_llm_client(self.config.llm),
            tool_registry=registry,
            config=self.config,
            cwd=self.cwd,
            cancellation_check=cancellation_check,
        )
        if execution_state:
            operation = asyncio.create_task(
                engine.ask_complete_async(
                    prompt,
                    execution_state=execution_state,
                    checkpoint_callback=checkpoint_callback,
                )
            )
        else:
            operation = asyncio.create_task(
                engine.ask_complete_async(prompt, checkpoint_callback=checkpoint_callback)
            )
        if cancellation:
            return (await await_with_cancellation(operation, cancellation)).text
        return (await operation).text

    def _runtime_identity(self, registry: Any) -> dict[str, Any]:
        return {
            "cwd": self.cwd,
            "model": self.config.llm.model,
            "hitl_mode": self.config.policy.hitl_mode,
            "tools": registry.list_names(),
        }

    def _cancel_task(self, task_id: str) -> bool:
        canceled = self.task_manager.cancel(task_id)
        if not canceled:
            return False
        with self._task_cancellations_lock:
            cancellation = self._task_cancellations.get(task_id)
        if cancellation:
            cancellation.cancel()
        return True

    def _clear_task_cancellation(self, task_id: str, cancellation: CancellationToken) -> None:
        with self._task_cancellations_lock:
            if self._task_cancellations.get(task_id) is cancellation:
                self._task_cancellations.pop(task_id, None)

    def _ensure_llm_key(self) -> None:
        if not self.config.llm.api_key:
            raise ValueError(
                "PAICLI_API_KEY is not configured. Runtime turns/tasks need a working LLM key."
            )

    def _authorized(self, request: BaseHTTPRequestHandler) -> bool:
        auth = request.headers.get("authorization", "")
        token = request.headers.get("x-api-key", "")
        return auth == f"Bearer {self.api_key}" or token == self.api_key

    def _create_thread(self) -> str:
        thread_id = f"thread_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
        with self._connect() as conn:
            conn.execute(
                "insert into threads(id, created_at) values (?, ?)",
                (thread_id, datetime.now(UTC).isoformat()),
            )
        self._append_event(thread_id, "thread.created", {"id": thread_id})
        return thread_id

    def _append_event(self, thread_id: str, event_type: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into events(thread_id, type, payload, created_at)
                values (?, ?, ?, ?)
                """,
                (
                    thread_id,
                    event_type,
                    json.dumps(payload, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def _send_events(self, request: BaseHTTPRequestHandler, thread_id: str) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select type, payload, created_at
                from events
                where thread_id = ?
                order by id
                """,
                (thread_id,),
            ).fetchall()
        body = "".join(
            f"event: {event_type}\ndata: {payload}\n\n" for event_type, payload, _created_at in rows
        ).encode("utf-8")
        request.send_response(200)
        request.send_header("content-type", "text/event-stream")
        request.send_header("content-length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists threads (
                    id text primary key,
                    created_at text not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists events (
                    id integer primary key autoincrement,
                    thread_id text not null,
                    type text not null,
                    payload text not null,
                    created_at text not null
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)


def _read_json(request: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(request.headers.get("content-length") or 0)
    if length == 0:
        return {}
    try:
        value = json.loads(request.rfile.read(length).decode("utf-8"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _send_json(request: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request.send_response(status)
    request.send_header("content-type", "application/json")
    request.send_header("content-length", str(len(body)))
    request.end_headers()
    request.wfile.write(body)


def _jsonable(event: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key, value in event.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
        else:
            result[key] = str(value)
    return result


def runtime_api_key(explicit: str | None = None) -> str:
    key = explicit or os.environ.get("PAICLI_RUNTIME_API_KEY")
    if not key:
        raise ValueError("PAICLI_RUNTIME_API_KEY is required for Runtime API")
    return key
