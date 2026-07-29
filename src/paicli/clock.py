from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

EAST_EIGHT = timezone(timedelta(hours=8))
EAST_EIGHT_OFFSET_SECONDS = 8 * 60 * 60
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
FILENAME_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"


def east_eight_now() -> datetime:
    """Return the current instant represented in UTC+8."""
    return datetime.now(UTC).astimezone(EAST_EIGHT)


def format_timestamp(value: datetime) -> str:
    """Format a datetime as timezone-free UTC+8 text with second precision."""
    localized = (
        value.replace(tzinfo=EAST_EIGHT)
        if value.tzinfo is None
        else value.astimezone(EAST_EIGHT)
    )
    return localized.strftime(TIMESTAMP_FORMAT)


def now_timestamp() -> str:
    return format_timestamp(east_eight_now())


def parse_timestamp(value: str) -> datetime:
    """Parse current UTC+8 text and legacy ISO/UTC values as an aware UTC+8 datetime."""
    normalized = value.strip()
    try:
        parsed = datetime.strptime(normalized, TIMESTAMP_FORMAT)
    except ValueError:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EAST_EIGHT)
    return parsed.astimezone(EAST_EIGHT)


def normalize_timestamp(value: str) -> str:
    """Normalize valid current or legacy timestamps, preserving unknown legacy text."""
    try:
        return format_timestamp(parse_timestamp(value))
    except ValueError:
        return value


def normalize_optional_timestamp(value: str | None) -> str | None:
    """Normalize a nullable timestamp without adding timezone or fractional fields."""
    return normalize_timestamp(value) if value is not None else None


def filename_timestamp(value: datetime | None = None) -> str:
    localized = value or east_eight_now()
    if localized.tzinfo is None:
        localized = localized.replace(tzinfo=EAST_EIGHT)
    else:
        localized = localized.astimezone(EAST_EIGHT)
    return localized.strftime(FILENAME_TIMESTAMP_FORMAT)
