"""Lightweight compatibility layer for Tenacity."""
from __future__ import annotations

from functools import wraps
from typing import Any, Callable, Iterable, Tuple, Type

try:  # pragma: no cover - real dependency path
    from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
except ImportError:  # pragma: no cover - fallback path

    class _RetryCondition:
        def __init__(self, exception_types: Iterable[type[BaseException]]) -> None:
            self.exception_types = tuple(exception_types)

    def retry_if_exception_type(exception_types: Iterable[type[BaseException]]):  # type: ignore[misc]
        return _RetryCondition(exception_types)

    class stop_after_attempt:  # type: ignore[misc]
        def __init__(self, attempts: int) -> None:
            self.attempts = attempts

    class wait_exponential:  # type: ignore[misc]
        def __init__(self, multiplier: float = 1.0, min: float = 0.0, max: float | None = None) -> None:
            self.multiplier = multiplier
            self.min = min
            self.max = max

    def retry(  # type: ignore[misc]
        *,
        reraise: bool | None = None,
        stop: stop_after_attempt | None = None,
        wait: wait_exponential | None = None,
        retry: _RetryCondition | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                return func(*args, **kwargs)

            return wrapper

        return decorator

__all__ = ["retry", "retry_if_exception_type", "stop_after_attempt", "wait_exponential"]
