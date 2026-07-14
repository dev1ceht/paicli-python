from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from paicli.llm.openai_compatible import OpenAICompatibleClient
from paicli.policy import AuditLog
from paicli.retry import RetryPolicy
from paicli.types import Message


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode()


def test_model_endpoint_retries_transient_status_before_streaming(tmp_path, monkeypatch):
    requests = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests < 3:
            return httpx.Response(503, json={"error": {"message": "overloaded"}})
        return httpx.Response(
            200,
            content=_sse({"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}),
        )

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("paicli.llm.openai_compatible.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("paicli.retry.random.uniform", lambda _a, b: b)
    client = OpenAICompatibleClient(
        provider_name="test",
        model="test-model",
        api_key="key",
        base_url="https://retry.example/v1",
        retry_policy=RetryPolicy(max_retries=3, base_delay=1.0, max_delay=8.0),
        transport=httpx.MockTransport(handler),
        retry_audit_path=tmp_path / "audit",
    )

    async def run() -> list[dict]:
        return [
            event
            async for event in client.chat(
                [Message(role="user", content="hello")],
                [],
                system_prompt="system",
            )
        ]

    events = asyncio.run(run())

    assert requests == 3
    assert sleeps == [1.0, 2.0]
    assert [event["attempt"] for event in events if event["type"] == "retry"] == [1, 2]
    assert [event.get("text") for event in events if event["type"] == "text_delta"] == ["done"]
    audit_events = AuditLog(tmp_path / "audit").tail(10)
    assert [event["attempt"] for event in audit_events] == [1, 2]
    assert len({event["logical_call_id"] for event in audit_events}) == 1


def test_model_endpoint_honors_retry_after_and_does_not_retry_bad_request(tmp_path, monkeypatch):
    sleeps: list[float] = []
    rate_limited_requests = 0
    bad_requests = 0

    def rate_limited_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal rate_limited_requests
        rate_limited_requests += 1
        if rate_limited_requests == 1:
            return httpx.Response(429, headers={"Retry-After": "5"})
        return httpx.Response(200, content=_sse({"choices": []}))

    def bad_request_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal bad_requests
        bad_requests += 1
        return httpx.Response(400, json={"error": {"message": "invalid parameter"}})

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("paicli.llm.openai_compatible.asyncio.sleep", fake_sleep)
    rate_limited = OpenAICompatibleClient(
        provider_name="test",
        model="rate-limited",
        api_key="key",
        base_url="https://retry-after.example/v1",
        transport=httpx.MockTransport(rate_limited_handler),
        retry_audit_path=tmp_path / "rate-limit-audit",
    )
    bad_request = OpenAICompatibleClient(
        provider_name="test",
        model="bad-request",
        api_key="key",
        base_url="https://bad-request.example/v1",
        transport=httpx.MockTransport(bad_request_handler),
    )

    async def collect(client: OpenAICompatibleClient) -> list[dict]:
        return [
            event
            async for event in client.chat(
                [Message(role="user", content="hello")], [], system_prompt="system"
            )
        ]

    events = asyncio.run(collect(rate_limited))
    assert rate_limited_requests == 2
    assert sleeps == [5.0]
    assert next(event for event in events if event["type"] == "retry")["delay"] == 5.0

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(collect(bad_request))
    assert bad_requests == 1


class _DropsAfterVisibleDelta(httpx.AsyncByteStream):
    async def __aiter__(self):
        payload = json.dumps({"choices": [{"delta": {"content": "partial"}}]})
        yield f"data: {payload}\n\n".encode()
        raise httpx.ReadError("stream dropped")


def test_model_endpoint_does_not_retry_after_visible_stream_output(tmp_path):
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, stream=_DropsAfterVisibleDelta())

    client = OpenAICompatibleClient(
        provider_name="test",
        model="streaming",
        api_key="key",
        base_url="https://stream-drop.example/v1",
        transport=httpx.MockTransport(handler),
        retry_audit_path=tmp_path / "stream-audit",
    )

    async def run() -> list[dict]:
        events = []
        with pytest.raises(httpx.ReadError):
            async for event in client.chat(
                [Message(role="user", content="hello")], [], system_prompt="system"
            ):
                events.append(event)
        return events

    events = asyncio.run(run())

    assert requests == 1
    assert [event.get("text") for event in events if event["type"] == "text_delta"] == ["partial"]


def test_model_endpoint_shares_provider_cooldown_with_concurrent_calls(tmp_path, monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("paicli.llm.openai_compatible.asyncio.sleep", fake_sleep)
    overloaded = OpenAICompatibleClient(
        provider_name="shared",
        model="same-model",
        api_key="key",
        base_url="https://shared-cooldown.example/v1",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(429, headers={"Retry-After": "5"})
        ),
        retry_audit_path=tmp_path / "shared-audit",
    )
    healthy = OpenAICompatibleClient(
        provider_name="shared",
        model="same-model",
        api_key="key",
        base_url="https://shared-cooldown.example/v1",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=_sse({"choices": []}))
        ),
        retry_audit_path=tmp_path / "shared-audit",
    )
    retry_disabled = OpenAICompatibleClient(
        provider_name="shared",
        model="same-model",
        api_key="key",
        base_url="https://shared-cooldown.example/v1",
        retry_policy=RetryPolicy(enabled=False),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=_sse({"choices": []}))
        ),
        retry_audit_path=tmp_path / "shared-audit",
    )

    async def run() -> tuple[list[dict], list[dict]]:
        first = overloaded.chat([Message(role="user", content="one")], [], system_prompt="system")
        retry_event = await anext(first)
        assert retry_event["type"] == "retry"
        await first.aclose()
        disabled_events = [
            event
            async for event in retry_disabled.chat(
                [Message(role="user", content="disabled")], [], system_prompt="system"
            )
        ]
        healthy_events = [
            event
            async for event in healthy.chat(
                [Message(role="user", content="two")], [], system_prompt="system"
            )
        ]
        return disabled_events, healthy_events

    disabled_events, events = asyncio.run(run())

    assert sleeps and sleeps[0] <= 5.0
    assert disabled_events[0]["type"] == "message_start"
    assert events[0]["type"] == "retry"
    assert events[0]["error_kind"] == "shared_cooldown"


def test_model_endpoint_emits_and_audits_retry_exhaustion(tmp_path, monkeypatch):
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    async def fake_sleep(_delay: float) -> None:
        pass

    monkeypatch.setattr("paicli.llm.openai_compatible.asyncio.sleep", fake_sleep)
    client = OpenAICompatibleClient(
        provider_name="test",
        model="exhausted",
        api_key="key",
        base_url="https://exhausted.example/v1",
        retry_policy=RetryPolicy(max_retries=1),
        transport=httpx.MockTransport(handler),
        retry_audit_path=tmp_path / "audit",
    )

    async def run() -> list[dict]:
        events = []
        with pytest.raises(httpx.HTTPStatusError):
            async for event in client.chat(
                [Message(role="user", content="hello")], [], system_prompt="system"
            ):
                events.append(event)
        return events

    events = asyncio.run(run())

    assert requests == 2
    assert [event["type"] for event in events] == ["retry", "retry_exhausted"]
    audit_events = AuditLog(tmp_path / "audit").tail(10)
    assert [event["outcome"] for event in audit_events] == ["scheduled", "exhausted"]


def test_model_endpoint_does_not_retry_when_audit_write_fails(tmp_path, monkeypatch):
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    def fail_audit(*_args, **_kwargs) -> None:
        raise OSError("audit unavailable")

    monkeypatch.setattr(AuditLog, "record_retry", fail_audit)
    client = OpenAICompatibleClient(
        provider_name="test",
        model="audit-required",
        api_key="key",
        base_url="https://audit-required.example/v1",
        transport=httpx.MockTransport(handler),
        retry_audit_path=tmp_path / "audit",
    )

    async def run() -> list[dict]:
        return [
            event
            async for event in client.chat(
                [Message(role="user", content="hello")], [], system_prompt="system"
            )
        ]

    with pytest.raises(OSError, match="audit unavailable"):
        asyncio.run(run())
    assert requests == 1
