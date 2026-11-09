"""Normalized rig state with change notification.

Keys and units follow CLAUDE.md 4.1 (freq_a [Hz], mode 'CW', filter_bw [Hz], ...).
Every change carries a source: 'kx2', 'wintest', 'rig', 'ui'.
"""

from __future__ import annotations

import time
from typing import Any, Callable

Listener = Callable[[str, Any, str], None]


class State:
    def __init__(self) -> None:
        self._values: dict[str, Any] = {}
        self._changed_at: dict[str, float] = {}
        self._source: dict[str, str] = {}
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self._values

    def items(self):
        return self._values.items()

    def set(self, key: str, value: Any, source: str) -> bool:
        """Store a value. Returns True (and notifies) only if it changed."""
        if key in self._values and self._values[key] == value:
            return False
        self._values[key] = value
        self._changed_at[key] = time.monotonic()
        self._source[key] = source
        for listener in list(self._listeners):
            listener(key, value, source)
        return True

    def update(self, values: dict[str, Any], source: str) -> list[str]:
        return [k for k, v in values.items() if self.set(k, v, source)]

    def last_change(self, key: str) -> tuple[float, str | None]:
        return self._changed_at.get(key, 0.0), self._source.get(key)
