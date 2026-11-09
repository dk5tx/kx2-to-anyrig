"""Abstract rig driver (CLAUDE.md 4.1). Drivers keep a normalized state and report changes."""

from __future__ import annotations

import abc
from typing import Any, Callable

from ..common.state import State


class Rig(abc.ABC):
    """Base class for remote rig drivers.

    Subclasses update ``self.state`` with source 'rig'; listeners subscribed via
    ``self.state.subscribe`` get every change. Meters are reported via ``on_meters``.
    """

    def __init__(self, profile: dict):
        self.profile = profile
        self.state = State()
        self.on_meters: Callable[[dict[str, Any]], None] | None = None
        self.on_raw: Callable[[str], None] | None = None

    @abc.abstractmethod
    async def open(self) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...

    @abc.abstractmethod
    def set(self, key: str, value: Any) -> bool:
        """Set a normalized value on the rig. Returns False if not supported."""

    @abc.abstractmethod
    def send_raw(self, cmd: str) -> None: ...

    def get(self, key: str, default: Any = None) -> Any:
        return self.state.get(key, default)

    def capabilities(self) -> set[str]:
        return set()

    # CW and special functions – implemented per driver (M3/M4)
    def send_cw_text(self, text: str) -> None:
        raise NotImplementedError

    def abort_cw(self) -> None:
        raise NotImplementedError

    def unkey(self) -> None:
        """Safety: stop any transmission immediately."""
        raise NotImplementedError
