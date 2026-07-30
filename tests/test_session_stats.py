from decimal import Decimal

from paicli.session import InteractiveSession, SessionRepository
from paicli.usage import TokenUsage, UsageCost, UsageRecord, UsageSource


def _usage_record(
    usage_id: str,
    *,
    tokens: TokenUsage,
    usage_source: UsageSource = "actual",
    amount: str | None = None,
) -> UsageRecord:
    return UsageRecord(
        usage_id=usage_id,
        request_id=usage_id,
        provider="qwen",
        model="qwen-plus",
        purpose="assistant",
        tokens=tokens,
        cost=(
            UsageCost(
                amount=Decimal(amount),
                currency="CNY",
                source="catalog_estimate",
            )
            if amount is not None
            else None
        ),
        usage_source=usage_source,
    )


def test_session_usage_survives_close_and_resume(tmp_path):
    repository = SessionRepository(tmp_path / "sessions.db")
    session = InteractiveSession(repository, tmp_path)
    session_id = session.id

    session.begin_turn("hello")
    session.record_usage(
        UsageRecord(
            usage_id="request-1",
            request_id="request-1",
            provider="qwen",
            model="qwen-plus",
            purpose="assistant",
            tokens=TokenUsage(
                input_tokens=80,
                output_tokens=20,
                cache_read_tokens=40,
                cache_write_tokens=5,
            ),
            cost=UsageCost(
                amount=Decimal("0.125"),
                currency="CNY",
                source="catalog_estimate",
            ),
            usage_source="actual",
        )
    )
    session.complete_turn("hi")

    stats = session.stats
    assert stats.tokens == TokenUsage(
        input_tokens=80,
        output_tokens=20,
        cache_read_tokens=40,
        cache_write_tokens=5,
    )
    assert stats.actual_tokens == stats.tokens
    assert stats.estimated_tokens == TokenUsage()
    assert stats.total_cost == Decimal("0.125")
    assert stats.coverage == "complete"
    session.close()

    resumed = InteractiveSession(repository, tmp_path, session_id=session_id)
    try:
        assert resumed.stats == stats
    finally:
        resumed.close()


def test_session_stats_separate_actual_estimated_and_cache_hit_rate(tmp_path):
    repository = SessionRepository(tmp_path / "sessions.db")
    session = InteractiveSession(repository, tmp_path)
    try:
        session.begin_turn("hello")
        session.record_usage(
            _usage_record(
                "actual-1",
                tokens=TokenUsage(
                    input_tokens=50,
                    output_tokens=10,
                    cache_read_tokens=40,
                    cache_write_tokens=10,
                ),
                amount="0.10",
            )
        )
        session.record_usage(
            _usage_record(
                "estimated-1",
                tokens=TokenUsage(input_tokens=25, output_tokens=5),
                usage_source="estimated",
                amount="0.02",
            )
        )
        session.record_usage(
            _usage_record(
                "actual-2",
                tokens=TokenUsage(
                    input_tokens=90,
                    output_tokens=5,
                    cache_read_tokens=10,
                ),
            )
        )
        session.complete_turn("hi")

        stats = session.stats
        assert stats.actual_tokens == TokenUsage(
            input_tokens=140,
            output_tokens=15,
            cache_read_tokens=50,
            cache_write_tokens=10,
        )
        assert stats.estimated_tokens == TokenUsage(input_tokens=25, output_tokens=5)
        assert stats.tokens == stats.actual_tokens + stats.estimated_tokens
        assert stats.cache_hit_rate == 0.25
        assert stats.total_cost == Decimal("0.12")
    finally:
        session.close()


def test_fork_keeps_inherited_usage_out_of_child_totals(tmp_path):
    repository = SessionRepository(tmp_path / "sessions.db")
    session = InteractiveSession(repository, tmp_path)
    try:
        session.begin_turn("parent")
        tokens = TokenUsage(input_tokens=80, output_tokens=20, cache_read_tokens=40)
        session.record_usage(_usage_record("parent-usage", tokens=tokens, amount="0.10"))
        session.complete_turn("answer")

        session.fork_session(title="Child")

        assert session.stats.tokens == TokenUsage()
        assert session.stats.inherited_tokens == tokens
        assert session.stats.total_cost == Decimal("0")

        session.begin_turn("child")
        child_tokens = TokenUsage(input_tokens=10, output_tokens=5)
        session.record_usage(_usage_record("child-usage", tokens=child_tokens, amount="0.01"))
        session.complete_turn("child answer")

        assert session.stats.tokens == child_tokens
        assert session.stats.inherited_tokens == tokens
        assert session.stats.total_cost == Decimal("0.01")

        session.begin_turn("unmetered child turn")
        session.complete_turn("legacy child answer")
        assert session.stats.coverage == "partial"
        assert session.stats_snapshot.coverage == "partial"
    finally:
        session.close()


def test_duplicate_usage_identity_is_idempotent_within_a_turn(tmp_path):
    repository = SessionRepository(tmp_path / "sessions.db")
    session = InteractiveSession(repository, tmp_path)
    try:
        session.begin_turn("deduplicate")
        record = _usage_record(
            "same-request",
            tokens=TokenUsage(input_tokens=10, output_tokens=5),
            amount="0.01",
        )

        session.record_usage(record)
        session.record_usage(record)
        session.complete_turn("done")

        usage_events = [
            event for event in repository.list_events(session.id) if event.type == "usage.recorded"
        ]
        assert len(usage_events) == 1
        assert session.stats.tokens == record.tokens
        assert session.stats.total_cost == Decimal("0.01")
    finally:
        session.close()


def test_legacy_session_with_assistant_output_has_partial_usage_coverage(tmp_path):
    repository = SessionRepository(tmp_path / "sessions.db")
    session = InteractiveSession(repository, tmp_path)
    try:
        session.begin_turn("legacy")
        session.complete_turn("answer without a usage event")

        assert session.stats.coverage == "partial"
        assert session.stats.tokens == TokenUsage()
    finally:
        session.close()
