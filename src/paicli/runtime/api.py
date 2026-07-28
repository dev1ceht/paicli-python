from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

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
from paicli.llm.base import LlmClient
from paicli.runtime.tasks import DurableTaskManager
from paicli.session import (
    InteractiveSession,
    SessionRepository,
    default_session_database_path,
)
from paicli.tools.base import ApprovalPending

SESSION_LEASE_REFRESH_SECONDS = 20.0


class RuntimeApiServer:
    def __init__(
        self,
        *,
        cwd: str,
        config: PaiCliConfig,
        api_key: str,
        port: int = 8080,
        workers: int = 2,
        session_repository: SessionRepository | None = None,
    ):
        self.cwd = str(Path(cwd).resolve())
        self.config = config
        self.api_key = api_key
        self.port = port
        self.session_repository = session_repository or SessionRepository(
            default_session_database_path()
        )
        self.runtime_root = self._resolve_runtime_root()
        self.task_manager = DurableTaskManager(
            self.session_repository,
            workspace_root=self.cwd,
            parent_session_id=self.runtime_root.id,
        )
        self.workers = workers
        self._stop = threading.Event()
        self._task_cancellations: dict[str, CancellationToken] = {}
        self._task_cancellations_lock = threading.Lock()

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
                task = self.task_manager.get(task_id)
                _send_json(
                    request,
                    200,
                    {
                        "id": task_id,
                        "status": "queued",
                        "session_id": task.session_id if task else None,
                        "parent_session_id": (task.parent_session_id if task else None),
                    },
                )
            elif method == "GET" and path == "/v1/tasks":
                _send_json(
                    request,
                    200,
                    {"tasks": [task.to_dict() for task in self.task_manager.list()]},
                )
            elif method == "GET" and path.startswith("/v1/tasks/"):
                task = self.task_manager.get(path.split("/")[3])
                payload: dict[str, Any] = task.to_dict() if task else {"error": "not found"}
                if task:
                    payload["approvals"] = [
                        approval.to_dict() for approval in self.task_manager.list_approvals(task.id)
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
                retry = self.task_manager.get(retry_id)
                _send_json(
                    request,
                    200,
                    {
                        "id": retry_id,
                        "status": "queued",
                        "retry_of": task_id,
                        "session_id": retry.session_id if retry else None,
                        "parent_session_id": (retry.parent_session_id if retry else None),
                    },
                )
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
        thread = self.session_repository.get_session(thread_id)
        if thread is None or thread.metadata.get("session_kind") != "runtime_thread":
            raise KeyError(f"runtime thread not found: {thread_id}")
        registry, _manager = await build_tool_registry(config=self.config, cwd=self.cwd)
        interactive = InteractiveSession(
            self.session_repository,
            self.cwd,
            session_id=thread_id,
        )
        engine = QueryEngine(
            llm_client=cast(
                LlmClient,
                create_llm_client(
                    self.config.llm,
                    retry_policy=self.config.retry.resolve("llm"),
                    retry_audit_path=self.config.policy.audit_log_path,
                    retry_cwd=self.cwd,
                ),
            ),
            tool_registry=registry,
            config=self.config,
            cwd=self.cwd,
        )
        try:
            recovery_state = interactive.prepare_recovery_state()
            if recovery_state is not None:
                await self._execute_session_turn(
                    interactive,
                    engine,
                    "",
                    execution_state=recovery_state,
                )
            interactive.begin_turn(message)
            text = await self._execute_session_turn(interactive, engine, message)
            return {"thread_id": thread_id, "text": text}
        finally:
            interactive.close()

    async def _execute_session_turn(
        self,
        interactive: InteractiveSession,
        engine: QueryEngine,
        message: str,
        *,
        execution_state: dict[str, Any] | None = None,
        checkpoint_callback=None,
        lease_refresh: Callable[[], None] | None = None,
    ) -> str:
        operation = asyncio.create_task(
            self._consume_session_turn(
                interactive,
                engine,
                message,
                execution_state=execution_state,
                checkpoint_callback=checkpoint_callback,
            )
        )
        lease_heartbeat = asyncio.create_task(
            self._refresh_session_lease(interactive, lease_refresh)
        )
        try:
            done, _pending = await asyncio.wait(
                {operation, lease_heartbeat},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation in done:
                return await operation
            heartbeat_error = lease_heartbeat.exception()
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            if heartbeat_error is not None:
                raise heartbeat_error
            raise RuntimeError("Runtime Session lease heartbeat stopped unexpectedly")
        finally:
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            if not lease_heartbeat.done():
                lease_heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await lease_heartbeat

    async def _consume_session_turn(
        self,
        interactive: InteractiveSession,
        engine: QueryEngine,
        message: str,
        *,
        execution_state: dict[str, Any] | None = None,
        checkpoint_callback=None,
    ) -> str:
        response_text: list[str] = []
        assistant_text: list[str] = []
        completed = False
        try:
            history = None if execution_state is not None else interactive.agent_history[:-1]
            async for event in engine.ask(
                message,
                history,
                execution_state=execution_state,
                checkpoint_callback=checkpoint_callback,
            ):
                event_type = str(event.get("type"))
                if event_type == "text_delta":
                    text = str(event.get("text") or "")
                    response_text.append(text)
                    assistant_text.append(text)
                elif event_type == "turn_complete":
                    actions = list(event.get("tool_actions") or [])
                    if actions:
                        model_message = event.get("message") or {}
                        interactive.record_tool_batch(
                            model_turn=int(event.get("turn") or 0),
                            assistant_content=str(model_message.get("content") or ""),
                            reasoning_content=(
                                str(model_message["reasoning_content"])
                                if model_message.get("reasoning_content")
                                else None
                            ),
                            actions=actions,
                        )
                        assistant_text.clear()
                elif event_type == "tool_call":
                    interactive.start_tool_action(str(event.get("tool_call_id") or ""))
                elif event_type == "tool_result":
                    interactive.complete_tool_action(
                        str(event.get("tool_call_id") or ""),
                        content=str(event.get("result") or ""),
                        is_error=bool(event.get("is_error")),
                    )
                elif event_type == "error":
                    raise event["error"]
                elif event_type == "done":
                    completed = True
            if not completed:
                raise RuntimeError("Runtime Agent stopped before completing the turn")
            interactive.complete_turn("".join(assistant_text))
            return "".join(response_text)
        except ApprovalPending:
            raise
        except asyncio.CancelledError:
            interactive.interrupt_turn(
                "".join(assistant_text),
                reason="runtime_turn_canceled",
            )
            raise
        except Exception:
            interactive.interrupt_turn(
                "".join(assistant_text),
                reason="runtime_turn_stopped",
            )
            raise

    @staticmethod
    async def _refresh_session_lease(
        interactive: InteractiveSession,
        extra_refresh: Callable[[], None] | None = None,
    ) -> None:
        while True:
            await asyncio.sleep(SESSION_LEASE_REFRESH_SECONDS)
            interactive.refresh_lease()
            if extra_refresh is not None:
                extra_refresh()

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
        operation = asyncio.create_task(
            self._run_claimed_task(task_id, prompt, cancellation)
        )
        claim_heartbeat = asyncio.create_task(self._refresh_task_claim(task_id))
        try:
            done, _pending = await asyncio.wait(
                {operation, claim_heartbeat},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation in done:
                return await operation
            heartbeat_error = claim_heartbeat.exception()
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            if heartbeat_error is not None:
                raise heartbeat_error
            raise RuntimeError("background task claim heartbeat stopped unexpectedly")
        finally:
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            if not claim_heartbeat.done():
                claim_heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await claim_heartbeat

    async def _run_claimed_task(
        self,
        task_id: str,
        prompt: str,
        cancellation: CancellationToken | None,
    ) -> str:
        cancellation_check = cancellation.is_set if cancellation else None
        raise_if_cancelled(cancellation_check)
        self._ensure_llm_key()
        registry, _manager = await build_tool_registry(config=self.config, cwd=self.cwd)
        task = self.task_manager.get(task_id)
        if task is None:
            raise KeyError(f"background task not found: {task_id}")
        interactive = InteractiveSession(
            self.task_manager.repository,
            self.cwd,
            session_id=task.session_id,
        )
        execution_state = self.task_manager.get_checkpoint(task_id)
        runtime_identity = self._runtime_identity(registry)
        try:
            if execution_state and execution_state.get("runtime_identity") != runtime_identity:
                approvals = self.task_manager.list_approvals(task_id)
                request = execution_state.get("approval_request")
                if not isinstance(request, dict):
                    request = approvals[-1].request if approvals else {}
                execution_state.pop("approval_decision", None)
                execution_state["approval_request"] = request
                execution_state["runtime_identity"] = runtime_identity
                execution_state["approval_context_stale"] = True
                active_tool_call_id = str(
                    execution_state.get("active_tool_call_id") or ""
                )
                approval = self.task_manager.wait_for_approval(
                    task_id,
                    checkpoint=execution_state,
                    request=request,
                    invalidation_reason="runtime_identity_changed",
                    session_id=(interactive.id if active_tool_call_id else None),
                    tool_call_id=active_tool_call_id or None,
                    lease_token=(interactive.lease_token if active_tool_call_id else None),
                )
                if not approval:
                    raise TaskCanceled()
                raise ApprovalPending()

            if execution_state and execution_state.get("approval_decision"):
                tool_call_id = str(execution_state.get("active_tool_call_id") or "")
                decision = str(execution_state["approval_decision"])
                interactive.resolve_tool_approval(
                    tool_call_id,
                    "approve" if decision == "approved" else "deny",
                    deferred_execution=True,
                )

            def checkpoint_callback(
                state: dict[str, Any],
                request: dict[str, Any],
            ) -> None:
                state["runtime_identity"] = runtime_identity
                tool_call_id = str(state.get("active_tool_call_id") or "")
                approval = self.task_manager.wait_for_approval(
                    task_id,
                    checkpoint=state,
                    request=request,
                    session_id=interactive.id,
                    tool_call_id=tool_call_id,
                    lease_token=interactive.lease_token,
                )
                if not approval:
                    raise TaskCanceled()

            engine = QueryEngine(
                llm_client=cast(
                    LlmClient,
                    create_llm_client(
                        self.config.llm,
                        retry_policy=self.config.retry.resolve("llm"),
                        retry_audit_path=self.config.policy.audit_log_path,
                        retry_cwd=self.cwd,
                    ),
                ),
                tool_registry=registry,
                config=self.config,
                cwd=self.cwd,
                cancellation_check=cancellation_check,
            )
            recovery_state = interactive.prepare_recovery_state()
            if recovery_state is None:
                interactive.begin_turn(prompt)
            active_state = recovery_state or execution_state
            operation = asyncio.create_task(
                self._execute_session_turn(
                    interactive,
                    engine,
                    "" if active_state is not None else prompt,
                    execution_state=active_state,
                    checkpoint_callback=checkpoint_callback,
                )
            )
            if cancellation:
                return await await_with_cancellation(operation, cancellation)
            return await operation
        finally:
            interactive.close()

    async def _refresh_task_claim(self, task_id: str) -> None:
        while True:
            await asyncio.sleep(SESSION_LEASE_REFRESH_SECONDS)
            self.task_manager.refresh_claim(task_id)

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
        else:
            task = self.task_manager.get(task_id)
            if task is not None:
                interactive = InteractiveSession(
                    self.task_manager.repository,
                    self.cwd,
                    session_id=task.session_id,
                )
                try:
                    interactive.interrupt_turn(
                        "",
                        reason="background_task_canceled",
                    )
                finally:
                    interactive.close()
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
        thread = self.session_repository.create_child_session(
            self.runtime_root.id,
            relation_type="runtime_thread",
            title="Runtime thread",
            metadata={"session_kind": "runtime_thread"},
        )
        return thread.id

    def _send_events(self, request: BaseHTTPRequestHandler, thread_id: str) -> None:
        thread = self.session_repository.get_session(thread_id)
        if (
            thread is None
            or thread.workspace_root != self.cwd
            or thread.metadata.get("session_kind") != "runtime_thread"
        ):
            _send_json(request, 404, {"error": "not found"})
            return
        events = self.session_repository.list_events(thread_id)
        body = "".join(
            f"event: {event.type}\ndata: {json.dumps(event.payload, ensure_ascii=False)}\n\n"
            for event in events
        ).encode("utf-8")
        request.send_response(200)
        request.send_header("content-type", "text/event-stream")
        request.send_header("content-length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    def _resolve_runtime_root(self):
        return self.session_repository.get_or_create_root_session(
            self.cwd,
            title="Runtime",
            root_kind="runtime_root",
        )


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


def runtime_api_key(explicit: str | None = None) -> str:
    key = explicit or os.environ.get("PAICLI_RUNTIME_API_KEY")
    if not key:
        raise ValueError("PAICLI_RUNTIME_API_KEY is required for Runtime API")
    return key
