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
        self.winkeyer: Any = None             # WinKeyerEmulator, set by main
        self._cw_id = 0
        self._cw_open: set[int] = set()
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

    # ------------------------------------------------------------------ CW (WinKeyer -> remote)
    def cw_text(self, text: str, end: bool = False) -> None:
        self._cw_id += 1
        msg = {"t": "cw", "text": text, "id": self._cw_id}
        if end:
            msg["end"] = True                          # message complete: key the last word
        if self.link.send(msg):
            self._cw_open.add(self._cw_id)
        else:
            log.warning("no remote connection – CW %r not sent", text)
            self._cw_idle()

    def cw_clear(self) -> None:
        self.link.send({"t": "cw_clear"})

    def cw_wpm(self, wpm: int) -> None:
        self.link.send({"t": "cw_wpm", "wpm": wpm})

    def cw_ptt(self, lead_ms: int, tail_ms: int) -> None:
        self._cw_ptt = (lead_ms, tail_ms)
        self.link.send({"t": "cw_ptt", "lead_ms": lead_ms, "tail_ms": tail_ms})

    def cw_end(self, text: str) -> None:
        self.cw_text(text, end=True)

    def cw_abort(self) -> None:
        self.link.send({"t": "cw_abort"})
        self._cw_open.clear()

    def cw_speed(self, wpm: int) -> None:
        self.state.set("keyer_wpm", wpm, "wintest")        # forwarded to the rig (KS)
        if self.kx2 is not None:
            self.kx2.write_state("keyer_wpm", wpm)         # and shown on the KX2

    def _cw_idle(self) -> None:
        if self.winkeyer is not None and not self._cw_open:
            self.winkeyer.done()

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
        elif t == "cw_done":
            self._cw_open.discard(msg.get("id"))
            self._cw_idle()
        elif t == "meters":
            self.meters = msg
        elif t == "cw_echo":
            note = ""
            if isinstance(msg.get("ts"), (int, float)):
                # one-way delay relative to the fastest echo seen (clocks differ, the minimum is
                # the offset): shows network / event loop jitter in the traffic log
                d = time.monotonic() - msg["ts"]
                self._echo_min = min(getattr(self, "_echo_min", d), d)
                note = "+%.0f ms (remote loop %.0f ms)" % ((d - self._echo_min) * 1000, msg.get("q", 0))
            if self.winkeyer is not None:
                self.winkeyer.echo(str(msg.get("c", "")), note)
        elif t == "hello":
            self.remote_info = msg
            log.info("remote rig: %s, CW: %s", msg.get("rig"), msg.get("cw", "text"))
            if self.winkeyer is not None:
                self.winkeyer.set_remote_echo(msg.get("cw") == "dtr")
            if getattr(self, "_cw_ptt", None):
                self.cw_ptt(*self._cw_ptt)              # Win-Test sent it before we connected

    def on_disconnect(self) -> None:
        self.ready = False
        self._cw_open.clear()
        if self.winkeyer is not None:
            self.winkeyer.set_busy(False)
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
