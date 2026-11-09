"""Routing of Win-Test commands that the K3 emulation does not handle itself (Lua scripts).

Rules (CLAUDE.md 4.5), in this order:
  translate table -> '@' prefix (always remote) -> ignore -> block -> to_both -> default target.
"""

from __future__ import annotations

import logging
from typing import Callable

from ..common import elecraft
from ..common.log import TrafficLog

log = logging.getLogger("router")

Sender = Callable[[str], None]


class Router:
    def __init__(self, cfg: dict, to_kx2: Sender, to_remote: Sender | None = None,
                 traffic: TrafficLog | None = None):
        self.cfg = cfg
        self.to_kx2 = to_kx2
        self.to_remote = to_remote
        self.traffic = traffic
        self.default = cfg.get("default", "remote")
        self.ignore = set(cfg.get("ignore") or [])
        self.to_both = set(cfg.get("to_both") or [])
        self.block = list(cfg.get("block") or [])
        self.prefix = cfg.get("prefix_remote") or ""
        self.translate = {self._norm(k): self._norm(v) for k, v in (cfg.get("translate") or {}).items()}

    @staticmethod
    def _norm(cmd: str) -> str:
        return cmd.strip().rstrip(";")

    def route(self, frame: str) -> str:
        """Route one frame (without ';'). Returns the decision for logging/tests:
        'kx2', 'remote', 'both', 'ignored', 'blocked', 'dropped'."""
        frame = self._norm(frame)
        target: str | None = None
        if frame in self.translate:
            frame = self.translate[frame]
            target = "remote"
        elif self.prefix and frame.startswith(self.prefix):
            frame = frame[len(self.prefix):]
            target = "remote"
        key = elecraft.frame_key(frame)
        if target is None and key in self.ignore:
            return self._log(frame, "ignored")
        if any(frame.startswith(b) for b in self.block):
            return self._log(frame, "blocked")
        if target is None:
            target = "both" if key in self.to_both else self.default
        if target in ("kx2", "both"):
            self.to_kx2(frame + ";")
        if target in ("remote", "both"):
            if self.to_remote is None:
                if target == "remote":
                    return self._log(frame, "dropped", "no remote connection")
            else:
                self.to_remote(frame + ";")
        return self._log(frame, target)

    def _log(self, frame: str, decision: str, note: str = "") -> str:
        if self.traffic:
            self.traffic.write("ROUTE", frame, decision + (" (" + note + ")" if note else ""))
        if decision in ("blocked", "dropped"):
            log.warning("%s: %s %s", decision, frame, note)
        else:
            log.debug("%s -> %s", frame, decision)
        return decision
