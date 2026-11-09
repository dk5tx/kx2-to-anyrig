"""Two-way state synchronisation KX2/Win-Test <-> remote rig (CLAUDE.md 4.8).

Forward:  changes on the KX2 or from Win-Test -> remote rig.
Reverse:  changes reported by the remote rig -> KX2 (and Win-Test via the shared state).

Rules
- On (re)connect the rig is master: the remote sends a snapshot, then ``snapshot_done``; only after
  that the KX2 is set to the rig's values and forwarding starts.
- Hold-off: after forwarding key K, rig reports for K are ignored for a short time (they are
  echoes, possibly of older values while the knob is still turning). When the hold-off ends, the
  final rig value is compared once and applied if it differs (e.g. filter rounded to a rig step).
- While the rig transmits, tunes the ATU or runs CW tune, mode/frequency/filter reports are not
  mirrored (the TS-590SG jumps to CW during ATU tuning); afterwards one resync runs.
- After the rig changes band or mode, the KX2 recalls its own band/mode memories. Those KX2
  changes are not forwarded for a quiet period; the rig's values are pushed to the KX2 instead.
"""

from __future__ import annotations

import asyncio
import logging
import time
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

# keys mirrored back rig -> KX2 (the KX2 can set these via CAT)
REVERSE_KEYS = [
    "mode", "freq_a", "freq_b", "split", "filter_bw", "filter_center", "rit", "xit", "rit_offset",
    "att", "preamp", "nb", "nr", "agc", "keyer_wpm", "vox",
]
ON_OFF_KEYS = {"att", "preamp", "nb", "nr"}          # rig levels 0/1/2 -> KX2 0/1
CW_MODES = {"CW", "CW-R"}

HOLD = {"freq_a": 0.5, "freq_b": 0.5, "rit_offset": 0.5, "filter_bw": 1.5, "filter_center": 1.5}
HOLD_DEFAULT = 0.4
# not mirrored while the rig is busy (TX, ATU tune, CW tune)
BUSY_KEYS = {"mode", "freq_a", "freq_b", "filter_bw", "filter_center", "split", "rit_offset"}
BUSY_FLAGS = ("tx", "atu_tuning", "cw_tune")
# after a rig band/mode change: KX2 memory recalls are not forwarded for this long [s]
QUIET = 1.5
BAND_JUMP_HZ = 100_000


class Sync:
    def __init__(self, state: State, link: RemoteLink, kx2_keys: dict | None = None, kx2: Any = None):
        self.state = state
        self.link = link
        self.kx2 = kx2                         # KX2Link (write/write_state); None = no reverse sync
        self.kx2_keys = kx2_keys or {}
        self.remote_state: dict[str, Any] = {}
        self.meters: dict[str, Any] = {}
        self.remote_info: dict[str, Any] = {}
        self.ready = False                     # snapshot received and applied
        self.sent = 0
        self.applied = 0
        self._known: set[str] = set()      # KX2 keys read at least once
        self._last_forward: dict[str, float] = {}
        self._quiet_until = 0.0
        self._timers: dict[str, asyncio.TimerHandle] = {}
        state.subscribe(self._on_local_change)

    # ------------------------------------------------------------------ helpers
    @property
    def busy(self) -> bool:
        return any(self.remote_state.get(f) for f in BUSY_FLAGS)

    def _later(self, key: str, delay: float, func) -> None:
        old = self._timers.pop(key, None)
        if old:
            old.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timers[key] = loop.call_later(max(0.0, delay), func)

    # ------------------------------------------------------------------ local -> remote
    def _on_local_change(self, key: str, value: Any, source: str) -> None:
        if source not in FORWARD_SOURCES:
            return
        first = False
        if source == "kx2" and key not in self._known:
            self._known.add(key)
            first = True                       # first reading after start: not an operator action
        if not self.ready:
            return
        now = time.monotonic()
        if first:
            if key in REVERSE_KEYS:
                self._apply(key)               # rig is master: bring the KX2 to the rig's value
            return
        if key in MIRROR_KEYS:
            if source == "kx2" and now < self._quiet_until and key not in ("freq_a", "freq_b"):
                # KX2 recalled its own band/mode memory after a rig change: rig stays master
                self._later("quiet", self._quiet_until - now, self.resync)
                return
            if self.link.send({"t": "set", "k": key, "v": value}):
                self.sent += 1
                self._last_forward[key] = now
        elif key in self.kx2_keys and source == "kx2" and value is not None:
            self._kx2_key(key, value)

    def _kx2_key(self, key: str, value: Any) -> None:
        """KX2 function without a direct counterpart (APF, notch) -> configured rig function."""
        spec = self.kx2_keys[key]
        if not spec:                           # key mapping switched off in the config
            return
        if isinstance(spec, str):              # plain action name, e.g. "beat_cancel_toggle"
            if value:
                self.link.send({"t": "action", "a": spec})
            return
        modes = spec.get("modes")
        if modes and self.state.get("mode") not in modes:
            return
        self.link.send({"t": "set", "k": spec["key"], "v": spec["on"] if value else spec["off"]})

    def send_raw(self, cmd: str) -> None:
        if not self.link.send({"t": "raw", "d": cmd}):
            log.warning("no remote connection – dropped %s", cmd)

    # ------------------------------------------------------------------ remote -> local
    def on_message(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "state":
            k, v = msg.get("k"), msg.get("v")
            was_busy = self.busy
            self.remote_state[k] = v
            if not self.ready:
                return
            if k in BUSY_FLAGS:
                if was_busy and not self.busy:
                    self.resync()              # TX / ATU / CW tune finished
                return
            self._reverse(k)
            if k == "data":
                self._reverse("mode")
        elif t == "snapshot_done":
            log.info("rig snapshot received – rig is master, syncing KX2")
            self.resync()                      # sets a quiet period itself if band/mode change
            self.ready = True
        elif t == "meters":
            self.meters = msg
        elif t == "hello":
            self.remote_info = msg
            log.info("remote rig: %s", msg.get("rig"))

    def on_disconnect(self) -> None:
        self.ready = False
        for h in self._timers.values():
            h.cancel()
        self._timers.clear()

    def _reverse(self, key: str) -> None:
        if key not in REVERSE_KEYS or self.kx2 is None:
            return
        if self.busy and key in BUSY_KEYS:
            return                             # resync when the rig is idle again
        hold = HOLD.get(key, HOLD_DEFAULT)
        since = time.monotonic() - self._last_forward.get(key, -1e9)
        if since < hold:
            self._later(key, hold - since, lambda: self._apply(key))
            return
        self._apply(key)

    def resync(self) -> None:
        """Push every rig value that differs to the KX2 (rig is master)."""
        for key in REVERSE_KEYS:
            if not (self.busy and key in BUSY_KEYS):
                self._apply(key)

    def rig_value(self, key: str) -> Any:
        """The rig's value translated into what the KX2 can show, or None."""
        rs = self.remote_state
        if key not in rs:
            return None
        v = rs[key]
        if key == "mode":
            if rs.get("data") and v in ("USB", "LSB"):
                return "DATA" if v == "USB" else "DATA-R"
            return {"FSK": "DATA", "FSK-R": "DATA-R"}.get(v, v)
        if key in ON_OFF_KEYS:
            return 1 if v else 0
        if key == "filter_center" and rs.get("mode") not in CW_MODES:
            return None                        # rig IS means CW centre only
        if key == "agc" and v not in ("fast", "slow"):
            return None                        # KX2 has no AGC off
        return v

    def _apply(self, key: str) -> None:
        if self.kx2 is None:
            return
        rv = self.rig_value(key)
        if rv is None:
            return
        cur = self.state.get(key)
        if cur is None:
            return                             # KX2 value not read yet; synced on first reading
        if key in ON_OFF_KEYS:
            cur = 1 if cur else 0
        if cur == rv:
            return
        if key == "mode" or (key == "freq_a" and abs(int(rv) - int(cur)) > BAND_JUMP_HZ):
            # the KX2 is about to recall its band/mode memory – keep the rig as master
            self._quiet_until = time.monotonic() + QUIET
            self._later("quiet", QUIET, self.resync)
        if key == "nr":
            # the KX2 has no NR set command: toggle with a held PRE key (SWH19)
            self.kx2.write("SWH19;", {"nr": rv})
        elif not self.kx2.write_state(key, rv):
            return
        self.applied += 1
        log.debug("rig -> KX2: %s = %r", key, rv)
        self.state.set(key, rv, "rig")
