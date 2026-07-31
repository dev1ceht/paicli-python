from paicli.render.typewriter import TypewriterBuffer


def test_typewriter_reveals_text_at_the_configured_rate() -> None:
    buffer = TypewriterBuffer(chars_per_second=10, max_chars_per_second=40)
    buffer.feed("abcdefghij")

    assert buffer.advance(0.2) == "ab"
    assert buffer.visible_text == "ab"
    assert buffer.pending_length == 8


def test_typewriter_accelerates_when_the_display_backlog_grows() -> None:
    buffer = TypewriterBuffer(chars_per_second=10, max_chars_per_second=40)
    buffer.feed("x" * 240)

    chunk = buffer.advance(0.5)

    assert len(chunk) == 20
    assert buffer.pending_length == 220


def test_typewriter_flush_reveals_every_pending_character() -> None:
    buffer = TypewriterBuffer(chars_per_second=10, max_chars_per_second=40)
    buffer.feed("complete response")
    buffer.advance(0.1)

    assert buffer.flush() == "omplete response"
    assert buffer.visible_text == "complete response"
    assert buffer.pending_length == 0


def test_typewriter_finish_drains_the_backlog_within_the_requested_time() -> None:
    buffer = TypewriterBuffer(chars_per_second=10, max_chars_per_second=40)
    buffer.feed("x" * 100)

    buffer.finish(within_seconds=0.25)

    assert buffer.advance(0.125) == "x" * 50
    assert buffer.advance(0.125) == "x" * 50
    assert buffer.pending_length == 0


def test_disabled_typewriter_reveals_input_immediately() -> None:
    buffer = TypewriterBuffer(
        enabled=False,
        chars_per_second=10,
        max_chars_per_second=40,
    )

    assert buffer.feed("instant") == "instant"
    assert buffer.visible_text == "instant"
    assert buffer.pending_length == 0
