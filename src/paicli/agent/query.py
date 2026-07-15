from __future__ import annotations

import inspect
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from paicli.cancellation import CancellationCheck, TaskCanceled, raise_if_cancelled
from paicli.config import PaiCliConfig
from paicli.context import ContextManager, ContextWindowExceededError
from paicli.context.telemetry import current_context_scope
from paicli.image import parse_image_references
from paicli.llm.base import LlmClient
from paicli.prompt import PromptSections
from paicli.tools.base import ApprovalPending, ToolContext
from paicli.tools.executor import ToolExecutor
from paicli.tools.registry import ToolRegistry
from paicli.types import Message


async def query(
    *,
    llm_client: LlmClient,
    tool_registry: ToolRegistry,
    system_prompt: str,
    prompt_sections: PromptSections | None = None,
    user_message: str,
    history: list[Message] | None,
    cwd: str,
    config: PaiCliConfig,
    approval_callback=None,
    session_allowed_tools: set[str] | None = None,
    max_turns: int | None = None,
    context_manager: ContextManager | None = None,
    cancellation_check: CancellationCheck | None = None,
    execution_state: dict[str, Any] | None = None,
    checkpoint_callback=None,
) -> AsyncIterator[dict[str, Any]]:
    restored_state = dict(execution_state or {})
    if restored_state:
        messages = [_message_from_dict(item) for item in restored_state["messages"]]
        pending_tool_calls = list(restored_state.get("pending_tool_calls") or [])
        next_tool_index = int(restored_state.get("next_tool_index") or 0)
    else:
        messages = [
            *(history or []),
            Message(role="user", content=parse_image_references(user_message, cwd)),
        ]
        pending_tool_calls: list[dict[str, Any]] = []
        next_tool_index = 0
    tool_definitions = tool_registry.definitions()
    executor = ToolExecutor(tool_registry)
    tool_retry_events: list[dict[str, Any]] = []

    total_tokens = int(restored_state.get("total_tokens") or 0)
    turn = int(restored_state.get("turn") or 0)
    tool_call_count = int(restored_state.get("tool_call_count") or 0)
    started_at = time.monotonic()
    finalizing = bool(restored_state.get("finalizing", False))
    limit_reason = str(restored_state.get("limit_reason") or "")
    last_signature = str(restored_state.get("last_signature") or "")
    repeated_batches = int(restored_state.get("repeated_batches") or 0)
    last_actual_usage: dict[str, int] | None = restored_state.get("last_actual_usage")
    resumed_approval_request = restored_state.get("approval_request")
    resumed_approval_decision = restored_state.get("approval_decision")

    def checkpoint_state() -> dict[str, Any]:
        return {
            "messages": [_message_to_dict(message) for message in messages],
            "pending_tool_calls": pending_tool_calls,
            "next_tool_index": next_tool_index,
            "total_tokens": total_tokens,
            "turn": turn,
            "tool_call_count": tool_call_count,
            "finalizing": finalizing,
            "limit_reason": limit_reason,
            "last_signature": last_signature,
            "repeated_batches": repeated_batches,
            "last_actual_usage": last_actual_usage,
        }

    async def background_approval_callback(request: dict[str, Any]) -> str:
        nonlocal resumed_approval_decision
        if resumed_approval_decision:
            if request != resumed_approval_request:
                raise RuntimeError("approval checkpoint does not match the pending tool call")
            decision = str(resumed_approval_decision)
            resumed_approval_decision = None
            return "approve" if decision == "approved" else "deny"
        if checkpoint_callback:
            state = checkpoint_state()
            state["approval_request"] = request
            result = checkpoint_callback(state, request)
            if inspect.isawaitable(result):
                await result
            raise ApprovalPending()
        if not approval_callback:
            return "deny"
        result = approval_callback(request)
        if inspect.isawaitable(result):
            result = await result
        return str(result)

    context = ToolContext(
        cwd=cwd,
        config=config,
        llm_client=llm_client,
        approval_callback=background_approval_callback,
        session_allowed_tools=session_allowed_tools if session_allowed_tools is not None else set(),
        cancellation_check=cancellation_check,
        event_sink=tool_retry_events.append,
    )

    turn_limit = max_turns if max_turns is not None else config.agent.max_turns
    while turn < turn_limit or finalizing or pending_tool_calls:
        raise_if_cancelled(cancellation_check)
        if pending_tool_calls:
            for index in range(next_tool_index, len(pending_tool_calls)):
                next_tool_index = index
                call = pending_tool_calls[index]
                name = call.get("function", {}).get("name", "unknown")
                yield {"type": "tool_call", "name": name, "input": _tool_input(call)}
                results = await executor.execute_all([call], context)
                raise_if_cancelled(cancellation_check)
                for retry_event in tool_retry_events:
                    yield retry_event
                tool_retry_events.clear()
                for result in results:
                    yield {
                        "type": "tool_result",
                        "name": _tool_name_by_id([call], result.tool_use_id or ""),
                        "result": result.content,
                        "is_error": result.is_error,
                        "error_kind": result.error_kind,
                        "retryable": result.retryable,
                        "retry_after": result.retry_after,
                    }
                    messages.append(
                        Message(
                            role="tool",
                            content=result.content,
                            tool_call_id=result.tool_use_id,
                        )
                    )
                next_tool_index = index + 1
            pending_tool_calls = []
            next_tool_index = 0
            continue
        if not finalizing:
            if time.monotonic() - started_at >= config.agent.max_elapsed_seconds:
                limit_reason = "elapsed time limit reached"
            elif total_tokens >= config.agent.max_total_tokens:
                limit_reason = "token limit reached"
            if limit_reason:
                finalizing = True
                messages.append(Message(role="user", content=_finalization_prompt(limit_reason)))
                yield {"type": "guarded_finalization", "reason": limit_reason}
                continue
        turn += 1
        text = ""
        thinking = ""
        stop_reason = "end_turn"
        usage_input = 0
        usage_output = 0
        tool_states: dict[int, dict[str, Any]] = {}

        prepared = None
        if context_manager:
            try:
                context_result = await context_manager.build_turn_context(
                    prompt_sections=prompt_sections or PromptSections(prefix=system_prompt),
                    messages=messages,
                    tools=[] if finalizing else tool_definitions,
                    actual_usage=last_actual_usage,
                )
            except ContextWindowExceededError as exc:
                yield {"type": "context_pending_clear", "scope": current_context_scope()}
                yield {"type": "error", "error": exc}
                return
            final_system_prompt = context_result.system_prompt
            messages = context_result.messages
            prepared = context_result.prepared
            yield {
                "type": "context_status",
                "pressure_tier": context_result.pressure_tier,
                "pressure_ratio": (
                    context_result.pressure_after.pressure_ratio
                    if context_result.pressure_after
                    else None
                ),
                "estimated": True,
            }
            if context_result.reductions and context_result.pressure_before:
                yield {
                    "type": "context_reduced",
                    "before_ratio": context_result.pressure_before.pressure_ratio,
                    "after_ratio": context_result.pressure_after.pressure_ratio,
                    "actions": list(context_result.reductions),
                }
        else:
            final_system_prompt = system_prompt

        try:
            raise_if_cancelled(cancellation_check)
            stream = (
                llm_client.send_prepared(prepared)
                if prepared is not None and callable(getattr(llm_client, "send_prepared", None))
                else llm_client.chat(
                    messages,
                    [] if finalizing else tool_definitions,
                    system_prompt=final_system_prompt,
                )
            )
            async for event in stream:
                raise_if_cancelled(cancellation_check)
                event_type = event.get("type")
                if event_type == "text_delta":
                    delta = str(event.get("text") or "")
                    text += delta
                    yield {"type": "text_delta", "text": delta}
                elif event_type == "thinking_delta":
                    delta = str(event.get("thinking") or "")
                    thinking += delta
                    yield {"type": "thinking_delta", "thinking": delta}
                elif event_type == "tool_call_delta":
                    _merge_tool_delta(tool_states, event["tool_call"])
                elif event_type == "message_end":
                    stop_reason = str(event.get("stop_reason") or "end_turn")
                elif event_type == "usage":
                    usage = event.get("usage") or {}
                    usage_input += int(usage.get("input_tokens") or 0)
                    usage_output += int(usage.get("output_tokens") or 0)
                    last_actual_usage = usage
                    yield {"type": "usage", "usage": usage}
                elif event_type in {
                    "retry",
                    "retry_exhausted",
                    "context_usage",
                    "context_request_finished",
                    "context_scope_clear",
                }:
                    yield dict(event)
                elif event_type == "error":
                    yield {
                        "type": "context_pending_clear",
                        "scope": current_context_scope(),
                    }
                    yield {"type": "error", "error": event["error"]}
                    return
        except TaskCanceled:
            yield {
                "type": "context_scope_clear",
                "scope": current_context_scope() or "agent",
            }
            raise
        except Exception as exc:  # noqa: BLE001 - keep REPL alive on model/network failures
            exc_type = type(exc).__name__
            exc_detail = str(exc) or "(无详细信息)"
            # 对 httpx 状态码错误提取响应体，便于排查
            exc_body = ""
            if hasattr(exc, "response") and hasattr(exc.response, "text"):
                try:
                    body_text = exc.response.text
                    if body_text:
                        exc_body = f"\n响应体: {body_text[:500]}"
                except Exception:  # noqa: BLE001
                    pass
            yield {
                "type": "context_pending_clear",
                "scope": current_context_scope(),
            }
            yield {
                "type": "error",
                "error": RuntimeError(f"调用 LLM 失败 [{exc_type}]: {exc_detail}{exc_body}"),
            }
            return

        total_tokens += usage_input + usage_output
        tool_calls = _finalize_tool_calls(tool_states)
        assistant_message = Message(
            role="assistant",
            content=text,
            tool_calls=tool_calls,
            reasoning_content=thinking or None,
        )
        messages.append(assistant_message)
        yield {"type": "turn_complete", "turn": turn, "stop_reason": stop_reason}

        if finalizing:
            break

        if stop_reason != "tool_use" and not tool_calls:
            break

        signature = _tool_batch_signature(tool_calls)
        repeated_batches = repeated_batches + 1 if signature and signature == last_signature else 1
        last_signature = signature
        if tool_call_count + len(tool_calls) > config.agent.max_tool_calls:
            limit_reason = "tool call limit reached"
        elif repeated_batches >= config.agent.stagnation_threshold:
            limit_reason = "repeated-call stagnation detected"
        if limit_reason:
            finalizing = True
            messages.append(Message(role="user", content=_finalization_prompt(limit_reason)))
            yield {"type": "guarded_finalization", "reason": limit_reason}
            continue

        tool_call_count += len(tool_calls)
        pending_tool_calls = tool_calls
        next_tool_index = 0

    yield {
        "type": "done",
        "total_turns": turn,
        "total_tokens": total_tokens,
        "messages": messages,
    }


def _message_to_dict(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "name": message.name,
        "tool_call_id": message.tool_call_id,
        "tool_calls": message.tool_calls,
        "reasoning_content": message.reasoning_content,
    }


def _message_from_dict(value: dict[str, Any]) -> Message:
    return Message(
        role=str(value["role"]),
        content=value["content"],
        name=value.get("name"),
        tool_call_id=value.get("tool_call_id"),
        tool_calls=list(value.get("tool_calls") or []),
        reasoning_content=value.get("reasoning_content"),
    )


def _merge_tool_delta(tool_states: dict[int, dict[str, Any]], delta: dict[str, Any]) -> None:
    index = int(delta.get("index") or 0)
    state = tool_states.setdefault(
        index,
        {
            "id": delta.get("id") or f"tool_{index}",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        },
    )
    if delta.get("id"):
        state["id"] = delta["id"]
    function = delta.get("function") or {}
    if function.get("name"):
        state["function"]["name"] = function["name"]
    if function.get("arguments"):
        state["function"]["arguments"] += function["arguments"]


def _finalize_tool_calls(tool_states: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    calls = []
    for index in sorted(tool_states):
        state = tool_states[index]
        if state["function"]["name"]:
            calls.append(state)
    return calls


def _tool_input(call: dict[str, Any]) -> dict[str, Any]:
    raw = call.get("function", {}).get("arguments") or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _tool_name_by_id(calls: list[dict[str, Any]], tool_call_id: str) -> str:
    for call in calls:
        if call.get("id") == tool_call_id:
            return str(call.get("function", {}).get("name") or "unknown")
    return "unknown"


def _tool_batch_signature(calls: list[dict[str, Any]]) -> str:
    normalized = []
    for call in calls:
        normalized.append(
            (
                _tool_name_by_id([call], str(call.get("id") or "")),
                _tool_input(call),
            )
        )
    return json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str)


def _finalization_prompt(reason: str) -> str:
    return (
        f"Agent safety protection triggered: {reason}. Do not call tools. "
        "Provide your best final answer from the evidence already collected, including "
        "the conclusion, unresolved blockers, and a recommended next step."
    )
