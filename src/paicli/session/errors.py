class SessionError(RuntimeError):
    """Base error for durable session operations."""


class SessionIdempotencyConflictError(SessionError):
    """An idempotency key was reused for a different semantic event."""


class SessionCorruptError(SessionError):
    """A session event stream failed integrity or schema validation."""


class SessionReadOnlyError(SessionError):
    """A session lifecycle state forbids new semantic events."""
