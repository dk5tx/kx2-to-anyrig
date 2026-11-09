"""State synchronisation between the KX2/Win-Test side and the remote rig (CLAUDE.md 4.8).

Milestone M2: forward direction only – changes made on the KX2 or by Win-Test are sent to the
remote rig. Rig state reported by the remote is kept in ``remote_state`` (reverse sync: M3).
"""

from __future__ import annotations

import logging
from typing import Any

from ..common.state import State
from .link import RemoteLink

log = logging.getLogger("sync")

# normalized keys mirrored KX2 -> remote rig (CLAUDE.md 4.9)
MIRROR_KEYS = {
    "freq_a", "freq_b", "mode", "mode_b", "split", "filter_bw", "filter_center",
    "rit", "xit", "rit_offset", "att", "preamp", "nb", "nr", "agc", "rf_gain", "keyer_wpm", "vox",
}
FORWARD_SOURCES = {"kx2", "wintest", "ui"}


class Sync:
    def __init__(self, state: State, link: RemoteLink, kx2_keys: dict | None = None):
        self.state = state
        self.link = link
        self.kx2_keys = kx2_keys or {}
        self.remote_state: dict[str, Any] = {}
        self.meters: dict[str, Any] = {}
        self.remote_info: dict[str, Any] = {}
        self.sent = 0
        self._apf_known = False
        state.subscribe(self._on_local_change)

    # ------------------------------------------------------------------ local -> remote
    def _on_local_change(self, key: str, value: Any, source: str) -> None:
        if source not in FORWARD_SOURCES:
            return
        if key in MIRROR_KEYS:
            if self.link.send({"t": "set", "k": key, "v": value}):
                self.sent += 1
        elif key == "apf" and source == "kx2":
            # the KX2 APF key (CW) triggers a configurable function on the remote rig.
            # The first reading after start is not a key press (measured: caused BC1 at start).
            if not self._apf_known:
                self._apf_known = True
                return
            action = self.kx2_keys.get("apf")
            if action and self.state.get("mode") in ("CW", "CW-R"):
                self.link.send({"t": "action", "a": action})

    def send_raw(self, cmd: str) -> None:
        if not self.link.send({"t": "raw", "d": cmd}):
            log.warning("no remote connection – dropped %s", cmd)

    # ------------------------------------------------------------------ remote -> local
    def on_message(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "state":
            self.remote_state[msg.get("k")] = msg.get("v")
        elif t == "meters":
            self.meters = msg
        elif t == "hello":
            self.remote_info = msg
            log.info("remote rig: %s", msg.get("rig"))
