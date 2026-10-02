from __future__ import annotations

from typing import Any, Literal


class FakeResponse:
    """Minimal urllib response context manager used across transport tests."""

    def __init__(self, body: bytes = b"{}") -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: Any) -> Literal[False]:
        return False

    def read(self) -> bytes:
        return self.body
