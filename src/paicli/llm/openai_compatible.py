from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from paicli.context.telemetry import current_context_scope
from paicli.context.token_estimator import TokenEstimator
from paicli.llm.base import PreparedOutboundRequest
from paicli.policy import AuditLog
from paicli.retry import RetryPolicy, classify_transient_error, compute_retry_delay
from paicli.types import Message

_VISIBLE_STREAM_EVENTS = {"text_delta", "thinking_delta", "tool_call_delta", "usage"}
_MODEL_COOLDOWNS: dict[tuple[str, str, str], float] = {}


@dataclass(slots=True)
class OpenAICompatibleClient:
    provider_name: str
    model: str
    api_key: str
    base_url: str
    max_tokens: int = 8192
    temperature: float = 0.7
    timeout: float = 120.0
    max_context_window: int = 128_000
    context_window_known: bool = True
    prompt_cache: bool = False
    supports_reasoning_content: bool = False
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    retry_audit_path: str | Path = field(default="~/.paicli/audit", repr=False)
    retry_cwd: str = field(default="", repr=False)
    context_estimator: TokenEstimator = field(default_factory=TokenEstimator, repr=False)

    @property
    def model_name(self) -> str:
        return self.model

    @property
    def reported_context_window(self) -> int | None:
        return self.max_context_window if self.context_window_known else None

    @property
    def supports_images(self) -> bool:
        model = self.model.lower()
        provider = self.provider_name.lower()
        return any(marker in model for marker in ("vision", "image", "5v", "vl")) or (
            provider in {"glm", "zhipu"} and "5v" in model
        )

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        system_prompt: str,
    ) -> AsyncIterator[dict[str, Any]]:
        prepared = self.prepare_request(messages, tools, system_prompt=system_prompt)
        async for event in self.send_prepared(prepared):
            yield event

    def prepare_request(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        system_prompt: str,
    ) -> PreparedOutboundRequest:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._format_messages(messages, system_prompt),
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        estimated_input = self.context_estimator.estimate(
            json.dumps(
                {"messages": payload["messages"], "tools": payload.get("tools", [])},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return PreparedOutboundRequest(
            payload_json=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            estimated_input_tokens=estimated_input,
        )

    async def send_prepared(
        self,
        prepared: PreparedOutboundRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        if not self.api_key:
            yield {
                "type": "error",
                "error": RuntimeError(
                    "PAICLI_API_KEY is not configured. Set it in env, ~/.paicli/config.json, "
                    "or project .paicli/config.json."
                ),
            }
            return

        payload = prepared.payload

        headers = {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
            "user-agent": "PaiCLI-Python/0.1.0",
        }
        url = self.base_url.rstrip("/") + "/chat/completions"

        cooldown_key = (self.provider_name, self.model, self.base_url)
        logical_call_id = f"llm_{uuid4().hex}"
        context_scope = current_context_scope()
        estimated_input = 0
        streamed_output = ""
        last_context_update = time.monotonic()
        provider_usage_received = False
        if context_scope:
            estimated_input = prepared.estimated_input_tokens
            yield self._context_usage_payload(
                state="active",
                scope=context_scope,
                estimated=True,
                input_tokens=estimated_input,
                request_id=logical_call_id,
                prepared=prepared,
            )
        visible_stream_started = False
        message_started = False
        attempt = 0
        skip_shared_cooldown_once = False

        while True:
            if skip_shared_cooldown_once:
                skip_shared_cooldown_once = False
            elif self.retry_policy.enabled:
                cooldown_delay = _model_cooldown_remaining(cooldown_key)
                if cooldown_delay > 0:
                    AuditLog(self.retry_audit_path).record_retry(
                        scope="llm",
                        operation=f"{self.provider_name}/{self.model}",
                        logical_call_id=logical_call_id,
                        attempt=0,
                        error_kind="shared_cooldown",
                        retry_delay=cooldown_delay,
                        cwd=self.retry_cwd,
                    )
                    yield {
                        "type": "retry",
                        "scope": "llm",
                        "provider": self.provider_name,
                        "model": self.model,
                        "attempt": 0,
                        "max_retries": self.retry_policy.max_retries,
                        "error_kind": "shared_cooldown",
                        "delay": cooldown_delay,
                    }
                    await asyncio.sleep(cooldown_delay)
            try:
                async with (
                    httpx.AsyncClient(
                        timeout=self.timeout,
                        http2=False,
                        transport=self.transport,
                    ) as client,
                    client.stream("POST", url, headers=headers, json=payload) as response,
                ):
                    if response.is_error:
                        await response.aread()
                    response.raise_for_status()
                    if not message_started:
                        message_started = True
                        yield {"type": "message_start", "model": self.model}
                    async for event in _iter_sse(response):
                        if event == "[DONE]":
                            break
                        try:
                            chunk = json.loads(event)
                        except json.JSONDecodeError:
                            continue
                        async for parsed in self._parse_chunk(chunk):
                            if parsed.get("type") in _VISIBLE_STREAM_EVENTS:
                                visible_stream_started = True
                            fragment = _context_output_fragment(parsed)
                            if fragment:
                                streamed_output += fragment
                            yield parsed
                            if parsed.get("type") == "usage" and context_scope:
                                provider_usage_received = True
                                usage = dict(parsed.get("usage") or {})
                                actual_input = int(usage.get("input_tokens") or 0)
                                actual_output = int(usage.get("output_tokens") or 0)
                                if actual_input > 0:
                                    self.context_estimator.calibrate(
                                        estimated_input,
                                        actual_input,
                                    )
                                yield self._context_usage_payload(
                                    state="active",
                                    scope=context_scope,
                                    estimated=False,
                                    input_tokens=actual_input,
                                    output_tokens=actual_output,
                                    cached_tokens=int(usage.get("cached_tokens") or 0),
                                    request_id=logical_call_id,
                                    prepared=prepared,
                                )
                            elif (
                                context_scope
                                and not provider_usage_received
                                and fragment
                                and time.monotonic() - last_context_update >= 0.25
                            ):
                                estimated_output = self.context_estimator.estimate(
                                    streamed_output
                                )
                                yield self._context_usage_payload(
                                    state="active",
                                    scope=context_scope,
                                    estimated=True,
                                    input_tokens=estimated_input,
                                    output_tokens=estimated_output,
                                    request_id=logical_call_id,
                                    prepared=prepared,
                                )
                                last_context_update = time.monotonic()
                if context_scope:
                    yield {
                        "type": "context_request_finished",
                        "request_id": logical_call_id,
                        "scope": context_scope,
                    }
                return
            except Exception as exc:
                decision = classify_transient_error(exc)
                retry_enabled = self.retry_policy.enabled and decision.retryable
                if retry_enabled and (
                    visible_stream_started or attempt >= self.retry_policy.max_retries
                ):
                    reason = "stream_started" if visible_stream_started else "max_retries"
                    AuditLog(self.retry_audit_path).record_retry(
                        scope="llm",
                        operation=f"{self.provider_name}/{self.model}",
                        logical_call_id=logical_call_id,
                        attempt=attempt,
                        error_kind=decision.error_kind,
                        retry_delay=0.0,
                        outcome="exhausted",
                        cwd=self.retry_cwd,
                    )
                    yield {
                        "type": "retry_exhausted",
                        "scope": "llm",
                        "provider": self.provider_name,
                        "model": self.model,
                        "attempt": attempt,
                        "max_retries": self.retry_policy.max_retries,
                        "error_kind": decision.error_kind,
                        "reason": reason,
                    }
                    if context_scope:
                        yield {
                            "type": "context_request_finished",
                            "request_id": logical_call_id,
                            "scope": context_scope,
                            "outcome": "failed",
                        }
                    raise
                if not retry_enabled:
                    if context_scope:
                        yield {
                            "type": "context_request_finished",
                            "request_id": logical_call_id,
                            "scope": context_scope,
                            "outcome": "failed",
                        }
                    raise

                retry_number = attempt + 1
                delay = compute_retry_delay(
                    self.retry_policy,
                    attempt=attempt,
                    retry_after=decision.retry_after,
                )
                _MODEL_COOLDOWNS[cooldown_key] = max(
                    _MODEL_COOLDOWNS.get(cooldown_key, 0.0),
                    time.monotonic() + delay,
                )
                AuditLog(self.retry_audit_path).record_retry(
                    scope="llm",
                    operation=f"{self.provider_name}/{self.model}",
                    logical_call_id=logical_call_id,
                    attempt=retry_number,
                    error_kind=decision.error_kind,
                    retry_delay=delay,
                    cwd=self.retry_cwd,
                )
                yield {
                    "type": "retry",
                    "scope": "llm",
                    "provider": self.provider_name,
                    "model": self.model,
                    "attempt": retry_number,
                    "max_retries": self.retry_policy.max_retries,
                    "error_kind": decision.error_kind,
                    "delay": delay,
                }
                attempt = retry_number
                await asyncio.sleep(delay)
                skip_shared_cooldown_once = True

    def context_usage_event(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        system_prompt: str,
        *,
        state: str,
        scope: str = "agent",
        quality_budget_tokens: int | None = None,
        pressure_thresholds: tuple[float, float, float] | None = None,
    ) -> dict[str, Any]:
        prepared = self.prepare_request(
            messages,
            tools,
            system_prompt=system_prompt,
        )
        if quality_budget_tokens is not None:
            prepared = prepared.with_quality_budget(
                quality_budget_tokens,
                pressure_thresholds,
            )
        return self._context_usage_payload(
            state=state,
            scope=scope,
            estimated=True,
            input_tokens=prepared.estimated_input_tokens,
            prepared=prepared,
        )

    def _context_usage_payload(
        self,
        *,
        state: str,
        scope: str,
        estimated: bool,
        input_tokens: int,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        request_id: str | None = None,
        prepared: PreparedOutboundRequest | None = None,
    ) -> dict[str, Any]:
        used_tokens = input_tokens + output_tokens
        event: dict[str, Any] = {
            "type": "context_usage",
            "state": state,
            "scope": scope,
            "estimated": estimated,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "used_tokens": used_tokens,
            "cached_tokens": cached_tokens,
            "context_window": self.reported_context_window,
            "safety_context_window": self.max_context_window,
        }
        if prepared and prepared.quality_budget_tokens:
            pressure_ratio = used_tokens / prepared.quality_budget_tokens
            event.update(
                {
                    "quality_budget_tokens": prepared.quality_budget_tokens,
                    "pressure_ratio": pressure_ratio,
                    "pressure_tier": _pressure_tier(
                        pressure_ratio,
                        prepared.pressure_thresholds,
                    ),
                }
            )
        if request_id:
            event["request_id"] = request_id
        return event

    def _format_messages(
        self, messages: list[Message], system_prompt: str
    ) -> list[dict[str, Any]]:
        formatted: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for message in messages:
            if message.role == "tool":
                formatted.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id or "",
                        "content": str(message.content),
                    }
                )
            elif message.role == "assistant":
                item: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
                if self.supports_reasoning_content and message.reasoning_content:
                    item["reasoning_content"] = message.reasoning_content
                if message.tool_calls:
                    item["tool_calls"] = message.tool_calls
                formatted.append(item)
            else:
                formatted.append(
                    {"role": message.role, "content": self._format_content(message.content)}
                )
        return formatted

    def _format_content(self, content: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        if isinstance(content, str):
            return content
        if self.supports_images:
            cleaned = []
            for part in content:
                item = {key: value for key, value in part.items() if key != "metadata"}
                cleaned.append(item)
            return cleaned
        text_parts = []
        for part in content:
            if part.get("type") == "text":
                text_parts.append(str(part.get("text") or ""))
            elif part.get("type") == "image_url":
                metadata = part.get("metadata") or {}
                source = metadata.get("source", "remote image")
                width = metadata.get("width", "?")
                height = metadata.get("height", "?")
                text_parts.append(f"[Image omitted: {source}, {width}x{height}]")
        return "\n".join(text_parts)

    async def _parse_chunk(self, chunk: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        # Extract usage FIRST — many providers send usage in a chunk
        # with no choices (e.g. DeepSeek final chunk after [DONE]).
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            cached = 0
            prompt_details = usage.get("prompt_tokens_details")
            if isinstance(prompt_details, dict):
                cached = int(prompt_details.get("cached_tokens") or 0)
            yield {
                "type": "usage",
                "usage": {
                    "input_tokens": int(usage.get("prompt_tokens") or 0),
                    "output_tokens": int(usage.get("completion_tokens") or 0),
                    "cached_tokens": cached,
                },
            }

        choices = chunk.get("choices") or []
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta") or {}

        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            yield {"type": "thinking_delta", "thinking": reasoning}

        content = delta.get("content")
        if isinstance(content, str) and content:
            yield {"type": "text_delta", "text": content}

        tool_calls = delta.get("tool_calls") or []
        for tool_call in tool_calls:
            yield {"type": "tool_call_delta", "tool_call": tool_call}

        finish_reason = choice.get("finish_reason")
        if finish_reason:
            yield {"type": "message_end", "stop_reason": _map_finish_reason(str(finish_reason))}


async def _iter_sse(response: httpx.Response) -> AsyncIterator[str]:
    buffer = ""
    async for text in response.aiter_text():
        buffer += text
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            data_lines = []
            for line in event.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            if data_lines:
                yield "\n".join(data_lines)
    if buffer.strip():
        data_lines = []
        for line in buffer.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            yield "\n".join(data_lines)


def _model_cooldown_remaining(key: tuple[str, str, str]) -> float:
    deadline = _MODEL_COOLDOWNS.get(key, 0.0)
    remaining = deadline - time.monotonic()
    if remaining <= 0 and deadline:
        _MODEL_COOLDOWNS.pop(key, None)
        return 0.0
    return max(remaining, 0.0)


def _context_output_fragment(event: dict[str, Any]) -> str:
    event_type = event.get("type")
    if event_type == "text_delta":
        return str(event.get("text") or "")
    if event_type == "thinking_delta":
        return str(event.get("thinking") or "")
    if event_type == "tool_call_delta":
        return json.dumps(event.get("tool_call") or {}, ensure_ascii=False, sort_keys=True)
    return ""


def _pressure_tier(
    ratio: float,
    thresholds: tuple[float, float, float],
) -> str:
    tier1, tier2, tier3 = thresholds
    if ratio < tier1:
        return "tier0_observe"
    if ratio < tier2:
        return "tier1_snip"
    if ratio < tier3:
        return "tier2_prune"
    return "tier3_summary"


def _map_finish_reason(reason: str) -> str:
    if reason in {"tool_calls", "tool_use"}:
        return "tool_use"
    if reason == "length":
        return "max_tokens"
    if reason == "content_filter":
        return "stop_sequence"
    return "end_turn"
