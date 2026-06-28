"""Python < 3.11 compatibility shims."""
try:
    from datetime import UTC
except ImportError:  # Python 3.10
    from datetime import timezone

    UTC = timezone.utc  # type: ignore[assignment]  # noqa: UP017

__all__ = ["UTC"]
