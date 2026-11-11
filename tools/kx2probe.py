#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kx2probe.py - CAT test tool for the Elecraft KX2 (and the TS-590SG)

Shows every queried setting of the rig in a text UI and records for each one whether
changes were reported by the rig itself (auto information, AI) or only found by polling.

Python standard library only (curses, termios) - nothing to install.

Usage:
    python3 kx2probe.py                         # /dev/ttyUSB0, 38400 Bd, AI2
    python3 kx2probe.py --port /dev/ttyUSB1 --ai 1
    python3 kx2probe.py --init "K31;"           # send commands at start
    python3 kx2probe.py --rig ts590             # TS-590SG on /dev/ttyUSB1, 115200 Bd
    python3 kx2probe.py --af-to-pc              # AF knob sets the PC volume
    python3 kx2probe.py --prio BW=fast,KS=normal   # change the priority of single values

Keys:
    q        quit (restores the original AI mode)
    p        polling on/off  -> with "PAUSE" you only see what AI reports by itself
    a        cycle AI mode 0 -> 1 -> 2
    c        reset counters and the list of changes
    m        set a marker (running number in the list and the log, for test steps)
    r        retry commands marked "not supported"
    arrows / PgUp/PgDn   scroll the table

All raw data (sent/received, with time stamp and classification) also goes to kx2probe.log.

The command list follows the Elecraft programmer's reference (K3/KX3/KX2). Not every command
is supported by the KX2 - that is exactly what the tool shows: commands answered three times
with "?" or not at all are marked "not supported" and no longer queried.
"""

import argparse
import curses
import fcntl
import locale
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import time
from collections import deque

# ---------------------------------------------------------------------------
# Parameter list - extend as you like.
#   kind : freq | int | bool | enum | raw | bits
#   min/max : range of the bar display (widened automatically;
#             if missing, taken from the values seen)
#   scale : factor raw value -> displayed value
#   once  : query only once at start
#   quiet : count changes but keep them out of the list of changes
#           (for values that change all the time)
# ---------------------------------------------------------------------------
PARAMS = [
    dict(cmd="FA", name="VFO A", kind="freq"),
    dict(cmd="FB", name="VFO B", kind="freq"),
    dict(cmd="MD", name="Mode", kind="enum",
         enum={"1": "LSB", "2": "USB", "3": "CW", "4": "FM", "5": "AM",
               "6": "DATA", "7": "CW-REV", "9": "DATA-REV"}),
    dict(cmd="BN", name="Band", kind="int"),
    dict(cmd="BW", name="Filter bandwidth", kind="int", scale=10, unit="Hz", min=50, max=4000),
    dict(cmd="IS", name="IF shift / centre", kind="int", unit="Hz"),
    dict(cmd="RT", name="RIT", kind="bool"),
    dict(cmd="XT", name="XIT", kind="bool"),
    dict(cmd="RO", name="RIT/XIT offset", kind="int", unit="Hz", min=-1000, max=1000),
    dict(cmd="RA", name="Attenuator (ATT)", kind="bool"),
    dict(cmd="PA", name="Preamp", kind="bool"),
    dict(cmd="AG", name="AF gain (volume)", kind="int", min=0, max=60),
    dict(cmd="RG", name="RF gain", kind="int"),
    dict(cmd="SQ", name="Squelch", kind="int"),
    dict(cmd="NB", name="Noise Blanker", kind="bool"),
    dict(cmd="NL", name="NB level", kind="raw"),
    dict(cmd="AP", name="Audio peak filter", kind="bool"),
    dict(cmd="KS", name="Keyer speed", kind="int", unit="WPM", min=8, max=50),
    dict(cmd="CW", name="CW-Pitch", kind="int", scale=10, unit="Hz", min=250, max=950),
    dict(cmd="ML", name="Sidetone level", kind="int"),
    dict(cmd="MG", name="Mic-Gain", kind="int", min=0, max=80),
    dict(cmd="CP", name="Compression", kind="int", min=0, max=40),
    dict(cmd="PC", name="Power", kind="raw"),
    dict(cmd="VX", name="VOX", kind="bool"),
    dict(cmd="FT", name="Split (TX on VFO B)", kind="bool"),
    dict(cmd="LK", name="VFO lock", kind="bool"),
    dict(cmd="TQ", name="Transmitting", kind="bool"),
    dict(cmd="SM", name="S-Meter", kind="int", quiet=True),
    dict(cmd="IC", name="Icon/status bits", kind="bits"),
    dict(cmd="DS", name="Display content", kind="raw", quiet=True),
    dict(cmd="IF", name="IF status", kind="raw", quiet=True),
    dict(cmd="OM", name="Options / model", kind="raw", once=True),
    # reported by the KX2 by auto information (found in the test of 8 Nov)
    dict(cmd="GT", name="AGC", kind="enum", enum={"002": "fast", "004": "slow"}),
    dict(cmd="AN", name="Antenna", kind="int"),
    dict(cmd="DT", name="DATA sub-mode", kind="enum",
         enum={"0": "DATA A", "1": "AFSK A", "2": "FSK D", "3": "PSK D"}),
    dict(cmd="FR", name="RX-VFO", kind="enum", enum={"0": "A", "1": "B"}),
    dict(cmd="FW", name="Filter (K2 format)", kind="raw"),
]

# ---------------------------------------------------------------------------
# TS-590SG (Kenwood PC Control Command Reference B5A-0316-00).
#   query   : what is sent to query it (default: cmd + ";")
#   answers : reply prefixes that belong to this query (RM returns 3 frames)
#   poll    : False = only filled by the reply to another query
# ---------------------------------------------------------------------------
TS590_PARAMS = [
    dict(cmd="FA", name="VFO A", kind="freq"),
    dict(cmd="FB", name="VFO B", kind="freq"),
    dict(cmd="FR", name="RX-VFO", kind="enum", enum={"0": "A", "1": "B", "2": "memory"}),
    dict(cmd="FT", name="TX-VFO", kind="enum", enum={"0": "A", "1": "B", "2": "memory"}),
    dict(cmd="MD", name="Mode", kind="enum",
         enum={"1": "LSB", "2": "USB", "3": "CW", "4": "FM", "5": "AM",
               "6": "FSK", "7": "CW-R", "9": "FSK-R"}),
    dict(cmd="DA", name="DATA mode", kind="bool"),
    dict(cmd="FW", name="Filter CW/FSK", kind="int", unit="Hz", min=50, max=2500),
    dict(cmd="SH", name="Filter high cut (index)", kind="int", min=0, max=13),
    dict(cmd="SL", name="Filter low cut (index)", kind="int", min=0, max=11),
    dict(cmd="IS", name="CW filter centre", kind="int", unit="Hz", min=300, max=1000),
    dict(cmd="RT", name="RIT", kind="bool"),
    dict(cmd="XT", name="XIT", kind="bool"),
    dict(cmd="IF", name="IF status", kind="raw", quiet=True),
    dict(cmd="RA", name="Attenuator (ATT)", kind="raw"),
    dict(cmd="PA", name="Preamp", kind="raw"),
    dict(cmd="NB", name="Noise Blanker", kind="enum", enum={"0": "off", "1": "NB1", "2": "NB2"}),
    dict(cmd="NL", name="NB level", kind="int", min=1, max=10),
    dict(cmd="NR", name="Noise Reduction", kind="enum", enum={"0": "off", "1": "NR1", "2": "NR2"}),
    dict(cmd="RL", name="NR level", kind="int"),
    dict(cmd="NT", name="Notch", kind="raw"),
    dict(cmd="BP", name="Notch frequency", kind="int"),
    dict(cmd="BC", name="Beat Cancel", kind="enum", enum={"0": "off", "1": "BC1", "2": "BC2"}),
    dict(cmd="GC", name="AGC", kind="enum", enum={"0": "off", "1": "slow", "2": "fast"}),
    dict(cmd="GT", name="AGC time constant", kind="int", min=1, max=20),
    dict(cmd="AG", name="AF gain", kind="int", query="AG0;", min=0, max=255),
    dict(cmd="RG", name="RF gain", kind="int", min=0, max=255),
    dict(cmd="SQ", name="Squelch", kind="int", query="SQ0;"),
    dict(cmd="KS", name="Keyer speed", kind="int", unit="WPM", min=4, max=60),
    dict(cmd="SD", name="Break-in delay", kind="int", unit="ms", min=0, max=1000),
    dict(cmd="VX", name="VOX / Break-in", kind="bool"),
    dict(cmd="PC", name="Power", kind="int", unit="W", min=5, max=100),
    dict(cmd="TP", name="Tune power", kind="int", unit="W"),
    dict(cmd="AC", name="Antenna tuner", kind="raw"),
    dict(cmd="CA", name="CW tune running", kind="bool"),
    dict(cmd="AN", name="Antenna", kind="raw"),
    dict(cmd="LK", name="VFO lock", kind="raw"),
    dict(cmd="BY", name="Busy (squelch open)", kind="raw"),
    dict(cmd="SM", name="S-Meter / PO (TX)", kind="int", query="SM0;", min=0, max=30),
    dict(cmd="RM1", name="Meter SWR", kind="int", query="RM;",
         answers=("RM1", "RM2", "RM3"), min=0, max=30),
    dict(cmd="RM2", name="Meter COMP", kind="int", poll=False, min=0, max=30),
    dict(cmd="RM3", name="Meter ALC", kind="int", poll=False, min=0, max=30),
    dict(cmd="KY", name="KY buffer full", kind="bool"),
    dict(cmd="FV", name="Firmware", kind="raw", once=True),
    dict(cmd="ID", name="Rig ID", kind="raw", once=True),
]

TS590_PRIO = {
    "FA": "fast", "SM": "fast", "RM1": "fast",
    "MD": "normal", "FW": "normal", "SH": "normal", "SL": "normal", "IS": "normal",
    "RT": "normal", "XT": "normal", "IF": "normal", "CA": "normal", "KY": "normal",
    "RA": "ai", "PA": "ai", "NB": "ai", "NR": "ai", "NT": "ai", "BC": "ai", "GC": "ai",
}

# length of the reply prefix if longer than 2 characters
KEYLEN = {"RM": 3}

RIGS = {
    "kx2": dict(name="KX2", params=PARAMS, prio=None, port="/dev/ttyUSB0", baud=38400),
    "ts590": dict(name="TS-590SG", params=TS590_PARAMS, prio=TS590_PRIO,
                  port="/dev/ttyUSB1", baud=115200),
}

# priority -> query interval in seconds.
#   fast   : fast controls (VFO, AF)
#   normal : changes now and then
#   slow   : rarely
#   ai     : should come by auto information, polling only as a safety net
INTERVALS = {"fast": 0.10, "normal": 0.50, "slow": 2.0, "ai": 5.0}
DEFAULT_PRIO = {
    "FA": "fast", "AG": "fast",
    "RO": "normal", "BW": "normal", "MD": "normal", "RT": "normal", "XT": "normal", "IS": "normal",
    "TQ": "normal", "SM": "normal",
    "RA": "ai", "PA": "ai", "NB": "ai", "AP": "ai", "BN": "ai",
}                                     # everything else: slow

BAUDRATES = {
    4800: termios.B4800, 9600: termios.B9600, 19200: termios.B19200,
    38400: termios.B38400, 57600: termios.B57600, 115200: termios.B115200,
}

POLL_TIMEOUT = 0.30      # s to wait for a reply
POLL_GAP = 0.002         # s between a reply and the next query (do not overload the KX2)
MAX_FAILS = 3           # this many "?" / no reply -> not supported
HIGHLIGHT_S = 2.0        # how long a changed value is highlighted


def printable(s):
    return "".join(c if 32 <= ord(c) < 127 else "\u00b7" for c in s)


def escaped(s):
    """For the log: non-printable bytes as \\xNN, so status bits are kept."""
    return "".join(c if 32 <= ord(c) < 127 and c != "\\" else "\\x%02X" % ord(c) for c in s)


def wallclock():
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d" % int((t % 1) * 1000)


# ---------------------------------------------------------------------------
# Serial port (termios, no pyserial)
# ---------------------------------------------------------------------------
class Serial:
    def __init__(self, port, baud):
        if baud not in BAUDRATES:
            raise ValueError("baud rate %d not supported" % baud)
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self.fd, termios.TIOCEXCL)   # no second program on the port
        except OSError:
            pass
        a = termios.tcgetattr(self.fd)
        a[0] = termios.IGNPAR                                  # iflag
        a[1] = 0                                               # oflag
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL    # cflag, 8N1, no HUPCL
        a[3] = 0                                               # lflag (raw)
        a[4] = a[5] = BAUDRATES[baud]
        a[6][termios.VMIN] = 0
        a[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, a)
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self.buf = b""

    def write(self, s):
        os.write(self.fd, s.encode("ascii"))

    def read_frames(self):
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            data = b""
        self.buf += data
        frames = []
        while b";" in self.buf:
            f, self.buf = self.buf.split(b";", 1)
            frames.append(f.decode("latin-1"))
        if len(self.buf) > 2048:          # drop garbage without a semicolon
            self.buf = b""
        return frames

    def close(self):
        os.close(self.fd)


# ---------------------------------------------------------------------------
# PC volume from the KX2 AF gain
# ---------------------------------------------------------------------------
class AfVolume:
    def __init__(self):
        if shutil.which("wpctl"):
            self.mk = lambda v: ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "%.2f" % v]
        elif shutil.which("pactl"):
            self.mk = lambda v: ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "%d%%" % round(v * 100)]
        else:
            self.mk = None
        self.last = None

    @property
    def available(self):
        return self.mk is not None

    def set(self, ag):
        if self.mk is None or ag is None:
            return
        v = max(0, min(60, ag)) / 60.0
        if v == self.last:
            return
        self.last = v
        try:
            subprocess.run(self.mk(v), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=0.5)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# One parameter
# ---------------------------------------------------------------------------
class Param:
    def __init__(self, d, dynamic=False, prio_table=None):
        self.cmd = d["cmd"]
        self.name = d["name"]
        self.kind = d["kind"]
        self.enum = d.get("enum", {})
        self.scale = d.get("scale", 1)
        self.unit = d.get("unit", "")
        self.lo = d.get("min")
        self.hi = d.get("max")
        self.range_auto = self.lo is None or self.hi is None
        self.once = d.get("once", False)
        self.query = d.get("query", self.cmd + ";")
        self.answers = tuple(d.get("answers", (self.cmd,)))
        self.pollable = d.get("poll", True)
        self.prio = d.get("prio") or (prio_table or DEFAULT_PRIO).get(self.cmd, "slow")
        self.last_poll = -1e9
        self.quiet = d.get("quiet", False)
        self.dynamic = dynamic
        self.reset_state()

    def reset_state(self):
        self.raw = None
        self.value = None
        self.src = None
        self.n_ai = 0          # changes reported by AI
        self.n_poll = 0        # changes found only by polling
        self.changed_at = 0.0
        self.bit_t = {}
        self.ai_times = deque(maxlen=200)    # times of AI reports
        self.poll_times = deque(maxlen=200)  # times of poll replies
        self.fail = 0
        self.unsupported = False
        self.polled_once = False
        self.status = ""

    def reset_counters(self):
        self.n_ai = 0
        self.n_poll = 0

    def parse(self, p):
        k = self.kind
        try:
            if k == "freq":
                return int(p)
            if k == "bool":
                return int(p.strip() or "0") != 0
            if k == "int":
                m = re.match(r"^\s*([+-])?\s*(\d+)", p)
                if not m:
                    return None
                v = int(m.group(2))
                if m.group(1) == "-":
                    v = -v
                return v * self.scale
            if k == "enum":
                return self.enum.get(p.strip(), p.strip())
        except ValueError:
            return None
        return p

    def rate(self, times, now, window=3.0):
        n = sum(1 for t in times if now - t <= window)
        return n / window

    def update(self, payload, src, now):
        """Returns (changed, list of changed bits)."""
        (self.ai_times if src == "ai" else self.poll_times).append(now)
        if payload == self.raw:
            return False, []
        old = self.raw
        self.raw = payload
        self.value = self.parse(payload)
        flips = []
        if self.kind == "bits" and old is not None:
            for i, (a, b) in enumerate(zip(old, payload)):
                x = ord(a) ^ ord(b)
                for bit in range(8):
                    if x & (1 << bit):
                        self.bit_t[(i, bit)] = now
                        flips.append((i, bit, (ord(b) >> bit) & 1))
        if self.kind == "int" and isinstance(self.value, int):
            v = self.value
            if self.lo is None or self.hi is None:
                self.lo = v if self.lo is None else self.lo
                self.hi = v if self.hi is None else self.hi
            if v < self.lo:
                self.lo, self.range_auto = v, True
            if v > self.hi:
                self.hi, self.range_auto = v, True
        self.src = src
        if old is not None:
            if src == "ai":
                self.n_ai += 1
            else:
                self.n_poll += 1
            self.changed_at = now
        return True, flips

    def display(self):
        if self.raw is None:
            return ""
        v, k = self.value, self.kind
        if k == "freq" and isinstance(v, int):
            return "{:,}".format(v).replace(",", ".") + " Hz"
        if k == "bool" and v is not None:
            return "ON" if v else "off"
        if k == "int" and v is not None:
            return ("%d %s" % (v, self.unit)).strip()
        if k == "enum":
            return str(v)
        if k == "bits":
            return " ".join("%02X" % ord(c) for c in self.raw)
        return printable(self.raw)


# ---------------------------------------------------------------------------
# Query logic
# ---------------------------------------------------------------------------
class Probe:
    def __init__(self, ser, ai_mode, logf, af, prio_overrides=(), rig=None):
        self.ser = ser
        rig = rig or RIGS["kx2"]
        self.params = [Param(d, prio_table=rig["prio"]) for d in rig["params"]]
        self.by_cmd = {p.cmd: p for p in self.params}
        for cmd, prio in dict(prio_overrides).items():
            if cmd in self.by_cmd:
                self.by_cmd[cmd].prio = prio
        if af:
            self.by_cmd["AG"].prio = "fast"  # AF gain fast for the PC volume
        self.marker = 0
        self.poll_times = deque(maxlen=500)
        self.logf = logf
        self.af = af
        self.polling = True
        self.ai_target = ai_mode
        self.ai_reported = None
        self.ai_original = None
        self.pending = None
        self.pending_t = 0.0
        self.last_answer = 0.0
        self.group = ()          # reply prefixes of the last query (for multi-frame replies)
        self.group_t = 0.0
        self.idx = 0
        self.changes = deque(maxlen=300)
        self.n_rx = 0

    # --- raw log ---
    def log(self, direction, kind, frame):
        if self.logf:
            self.logf.write("%s %s %-5s %s;\n" % (wallclock(), direction, kind, escaped(frame)))

    def send(self, s, kind="CMD"):
        self.ser.write(s)
        self.log("TX", kind, s.rstrip(";"))

    # --- start / end ---
    def start(self):
        self.send("AI;", "QUERY")
        t_end = time.monotonic() + 0.6
        while time.monotonic() < t_end and self.ai_original is None:
            r, _, _ = select.select([self.ser.fd], [], [], 0.05)
            if r:
                for f in self.ser.read_frames():
                    f = f.lstrip("\r\n")
                    self.log("RX", "INIT", f)
                    if f.startswith("AI"):
                        self.ai_original = f[2:].strip()
        self.set_ai(self.ai_target)

    def stop(self):
        try:
            restore = self.ai_original if self.ai_original in ("0", "1", "2") else "0"
            self.send("AI%s;" % restore, "SET")
        except OSError:
            pass

    def set_ai(self, mode):
        self.ai_target = mode
        self.send("AI%d;" % mode, "SET")
        self.send("AI;", "QUERY")

    # --- Polling ---
    def next_param(self, now):
        """Returns the next due parameter or None.
        The winner is the one most overdue relative to its interval. Under load all values
        get slower evenly, the fast ones stay faster in the same ratio - nobody starves."""
        best, best_key = None, None
        for p in self.params:
            if p.dynamic or p.unsupported or not p.pollable or (p.once and p.polled_once):
                continue
            iv = INTERVALS.get(p.prio, 2.0)
            due = p.last_poll + iv
            if due > now:
                continue
            key = -(now - p.last_poll) / iv
            if best is None or key < best_key:
                best, best_key = p, key
        return best

    def polls_per_s(self, now, window=3.0):
        return sum(1 for t in self.poll_times if now - t <= window) / window

    def mark(self):
        self.marker += 1
        self.log("--", "MARK", "### marker %d ###" % self.marker)
        self.changes.appendleft((wallclock(), "##", "", "marker %d" % self.marker, ""))

    def fail(self, p, why):
        p.fail += 1
        p.status = why
        if p.fail >= MAX_FAILS and not p.unsupported:
            p.unsupported = True
            self.changes.appendleft((wallclock(), "--", p.cmd, p.name,
                                     "not supported (%s)" % why))

    def step(self, now):
        if self.pending:
            if now - self.pending_t > POLL_TIMEOUT:
                self.log("--", "TMO", self.pending.cmd)
                self.fail(self.pending, "no reply")
                self.pending = None
            else:
                return
        if not self.polling or now - self.last_answer < POLL_GAP:
            return
        p = self.next_param(now)
        if p is None:
            return
        p.last_poll = now
        self.poll_times.append(now)
        p.polled_once = True
        self.pending = p
        self.pending_t = now
        self.send(p.query, "POLL")

    # --- receiving ---
    def handle(self, f, now):
        f = f.lstrip("\r\n")
        if not f:
            return
        self.n_rx += 1
        if f == "?":
            self.log("RX", "ERR", f)
            if self.pending:
                self.fail(self.pending, "reply ?")
                self.pending = None
                self.last_answer = now
            return
        kl = KEYLEN.get(f[:2], 2)
        cmd, payload = f[:kl], f[kl:]
        if cmd == "AI":
            self.ai_reported = payload.strip()
            self.log("RX", "AIQRY", f)
            return
        if self.pending and cmd in self.pending.answers:
            src = "poll"
            p = self.by_cmd.get(cmd, self.pending)
            self.group, self.group_t = self.pending.answers, now
            self.pending.fail, self.pending.status = 0, ""
            self.pending = None
            self.last_answer = now
        elif cmd in self.group and now - self.group_t < 0.15 and cmd in self.by_cmd:
            src, p = "poll", self.by_cmd[cmd]      # further frame of a multi-frame reply
        else:
            src = "ai"
            p = self.by_cmd.get(cmd)
            if p is None:
                p = Param(dict(cmd=cmd, name="(not in list)", kind="raw"), dynamic=True)
                self.params.append(p)
                self.by_cmd[cmd] = p
        self.log("RX", "POLL" if src == "poll" else "AUTO", f)
        old = p.display() if p.raw is not None else None
        changed, flips = p.update(payload, src, now)
        if changed and old is not None and not p.quiet:
            if flips:
                txt = ", ".join("Byte %d Bit %d: %d\u2192%d" % (i, b, 1 - n, n) for i, b, n in flips)
            else:
                txt = "%s \u2192 %s" % (old, p.display())
            self.changes.appendleft((wallclock(), "AI" if src == "ai" else "Poll", p.cmd, p.name, txt))
        if changed and p.cmd == "AG" and self.af:
            self.af.set(p.value)

    def reset(self):
        for p in self.params:
            p.reset_counters()
        self.changes.clear()

    def retry_unsupported(self):
        for p in self.params:
            if p.unsupported:
                p.unsupported, p.fail, p.status = False, 0, ""


# ---------------------------------------------------------------------------
# Text UI
# ---------------------------------------------------------------------------
C_GREEN, C_YELLOW, C_RED, C_CYAN, C_HL = 1, 2, 3, 4, 5

COLS = [("Cmd", 4), ("Function", 22), ("Value", 16), ("Display", 26),
        ("Source", 6), ("AI#", 5), ("Poll#", 6), ("AI/s", 5), ("Poll/s", 6), ("Prio", 6), ("Status", 22)]


def col_x(i):
    return 1 + sum(w + 1 for _, w in COLS[:i])


def put(scr, y, x, s, attr=0):
    h, w = scr.getmaxyx()
    if y < 0 or y >= h or x >= w - 1:
        return
    try:
        scr.addnstr(y, x, s, max(0, w - 1 - x), attr)
    except curses.error:
        pass


def bar(p, width):
    if p.lo is None or p.hi is None or p.hi <= p.lo or not isinstance(p.value, int):
        return ""
    frac = (p.value - p.lo) / float(p.hi - p.lo)
    frac = max(0.0, min(1.0, frac))
    n = int(round(frac * width))
    rng = "%s%d\u2026%d" % ("~" if p.range_auto else "", p.lo, p.hi)
    return "\u2588" * n + "\u00b7" * (width - n) + " " + rng


def draw_row(scr, y, p, now):
    cp = curses.color_pair
    put(scr, y, col_x(0), p.cmd, curses.A_BOLD)
    put(scr, y, col_x(1), p.name[:COLS[1][1]], curses.A_DIM if p.dynamic else 0)

    hl = (now - p.changed_at) < HIGHLIGHT_S
    vattr = (cp(C_HL) | curses.A_BOLD) if hl else 0
    span = COLS[2][1] + 1 + COLS[3][1]

    if p.unsupported:
        put(scr, y, col_x(2), "\u2014", curses.A_DIM)
    elif p.kind == "bits" and p.raw is not None:
        x = col_x(2)
        for i, c in enumerate(p.raw):
            for bit in range(7, -1, -1):
                if x >= col_x(2) + span:
                    break
                on = (ord(c) >> bit) & 1
                recent = (now - p.bit_t.get((i, bit), -99)) < HIGHLIGHT_S * 1.5
                a = cp(C_HL) | curses.A_BOLD if recent else (0 if i % 2 == 0 else cp(C_CYAN))
                put(scr, y, x, "1" if on else "0", a)
                x += 1
    elif p.kind in ("raw",):
        put(scr, y, col_x(2), p.display()[:span], vattr)
    else:
        put(scr, y, col_x(2), p.display()[:COLS[2][1]], vattr)
        if p.kind == "bool" and p.value is not None:
            if p.value:
                put(scr, y, col_x(3), "\u25cf active", cp(C_GREEN) | curses.A_BOLD)
            else:
                put(scr, y, col_x(3), "\u25cb inactive", curses.A_DIM)
        elif p.kind == "int":
            put(scr, y, col_x(3), bar(p, 12)[:COLS[3][1]], cp(C_CYAN))

    if p.src:
        put(scr, y, col_x(4), "AI" if p.src == "ai" else "Poll",
            cp(C_GREEN) if p.src == "ai" else cp(C_YELLOW))
    put(scr, y, col_x(5), "%4d" % p.n_ai, cp(C_GREEN) if p.n_ai else curses.A_DIM)
    put(scr, y, col_x(6), "%5d" % p.n_poll, cp(C_YELLOW) if p.n_poll else curses.A_DIM)
    ra, rp = p.rate(p.ai_times, now), p.rate(p.poll_times, now)
    put(scr, y, col_x(7), "%5.1f" % ra if ra else "    -", cp(C_GREEN) if ra else curses.A_DIM)
    put(scr, y, col_x(8), "%6.1f" % rp if rp else "     -", cp(C_YELLOW) if rp else curses.A_DIM)
    put(scr, y, col_x(9), {"fast": "fast", "normal": "normal", "slow": "slow", "ai": "AI"}.get(p.prio, p.prio)[:6],
        curses.A_DIM if p.dynamic else 0)
    sx = col_x(10)
    if p.unsupported:
        put(scr, y, sx, "not supported", cp(C_RED))
    elif p.status:
        put(scr, y, sx, p.status, cp(C_RED))
    elif p.raw is None:
        put(scr, y, sx, "waiting \u2026", curses.A_DIM)
    elif p.changed_at:
        age = now - p.changed_at
        put(scr, y, sx, "changed %.0f s ago" % age if age >= 1 else "just changed",
            curses.A_DIM)


def draw(scr, pr, args, scroll):
    scr.erase()
    h, w = scr.getmaxyx()
    now = time.monotonic()
    cp = curses.color_pair

    ct = "%.0f queries/s" % pr.polls_per_s(now)
    hdr = (" " + args.rig_name + "-probe  %s @ %d Bd \u2502 AI wanted %d / is %s \u2502 Polling: %s \u2502 %s \u2502 RX %d"
           % (args.port, args.baud, pr.ai_target, pr.ai_reported or "?",
              "ON" if pr.polling else "PAUSE", ct, pr.n_rx))
    if pr.af:
        hdr += " \u2502 AF\u2192PC %s" % ("on" if pr.af.available else "no wpctl/pactl")
    put(scr, 0, 0, hdr.ljust(w), curses.A_REVERSE | curses.A_BOLD)
    put(scr, 1, 1, "q quit  p polling on/off  a AI mode  m marker  c counters 0  r retry unsupported  "
                   "\u2191\u2193 PgUp/PgDn scroll", curses.A_DIM)
    for i, (t, _) in enumerate(COLS):
        put(scr, 2, col_x(i), t, curses.A_UNDERLINE | curses.A_BOLD)

    logh = min(12, max(4, h // 4))
    top = 3
    rows_h = max(1, h - top - logh - 1)
    params = pr.params
    scroll = max(0, min(scroll, max(0, len(params) - rows_h)))
    for r, p in enumerate(params[scroll:scroll + rows_h]):
        draw_row(scr, top + r, p, now)
    if scroll + rows_h < len(params):
        put(scr, top + rows_h - 1, w - 12, " \u2193 more ", curses.A_REVERSE)

    ly = top + rows_h
    put(scr, ly, 0, ("\u2500 changes (newest first) " + "\u2500" * w)[:w - 1], curses.A_DIM)
    for i, (t, src, cmd, name, txt) in enumerate(list(pr.changes)[:h - ly - 1]):
        y = ly + 1 + i
        put(scr, y, 1, t, curses.A_DIM)
        put(scr, y, 14, "%-4s" % src,
            cp(C_GREEN) if src == "AI" else cp(C_YELLOW) if src == "Poll" else cp(C_RED))
        put(scr, y, 19, "%-3s %-20s %s" % (cmd, name[:20], txt))
    scr.refresh()
    return scroll


def tui(scr, ser, args, logf):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(C_GREEN, curses.COLOR_GREEN, bg)
    curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, bg)
    curses.init_pair(C_RED, curses.COLOR_RED, bg)
    curses.init_pair(C_CYAN, curses.COLOR_CYAN, bg)
    curses.init_pair(C_HL, curses.COLOR_BLACK, curses.COLOR_YELLOW)
    scr.nodelay(True)
    scr.keypad(True)

    af = AfVolume() if args.af_to_pc else None
    overrides = []
    for item in args.prio.split(","):
        if "=" in item:
            c, v = (x.strip() for x in item.split("=", 1))
            if v.lower() not in INTERVALS:
                raise SystemExit("unknown priority '%s' (allowed: %s)" % (v, ", ".join(INTERVALS)))
            overrides.append((c.upper(), v.lower()))
    pr = Probe(ser, args.ai, logf, af, overrides, RIGS[args.rig])
    pr.start()
    for c in [c.strip() for c in args.init.split(";") if c.strip()]:
        pr.send(c + ";", "INIT")
    scroll = 0
    last_draw = 0.0
    try:
        while True:
            r, _, _ = select.select([ser.fd], [], [], 0.005)
            now = time.monotonic()
            if r:
                for f in ser.read_frames():
                    pr.handle(f, now)
            pr.step(now)

            while True:
                k = scr.getch()
                if k == -1:
                    break
                if k in (ord("q"), ord("Q")):
                    return
                elif k in (ord("p"), ord("P")):
                    pr.polling = not pr.polling
                elif k in (ord("a"), ord("A")):
                    pr.set_ai((pr.ai_target + 1) % 3)
                elif k in (ord("m"), ord("M")):
                    pr.mark()
                elif k in (ord("c"), ord("C")):
                    pr.reset()
                elif k in (ord("r"), ord("R")):
                    pr.retry_unsupported()
                elif k == curses.KEY_UP:
                    scroll -= 1
                elif k == curses.KEY_DOWN:
                    scroll += 1
                elif k == curses.KEY_PPAGE:
                    scroll -= 10
                elif k == curses.KEY_NPAGE:
                    scroll += 10

            if now - last_draw > 0.05:
                scroll = draw(scr, pr, args, scroll)
                last_draw = now
    finally:
        pr.stop()


def main():
    ap = argparse.ArgumentParser(description="KX2 / TS-590SG CAT test tool (auto information vs. polling)")
    ap.add_argument("--rig", choices=sorted(RIGS), default="kx2",
                    help="rig: kx2 (default) or ts590")
    ap.add_argument("--port", default=None, help="default: kx2 /dev/ttyUSB0, ts590 /dev/ttyUSB1")
    ap.add_argument("--baud", type=int, default=None, help="default: kx2 38400, ts590 115200")
    ap.add_argument("--ai", type=int, choices=(0, 1, 2), default=2,
                    help="auto information mode at start (default 2)")
    ap.add_argument("--log", default="kx2probe.log", help="raw data log ('' = off)")
    ap.add_argument("--prio", default="",
                    help="change priorities, e.g. BW=fast,KS=normal "
                         "(levels: fast 0.1 s / normal 0.5 s / slow 2 s / ai 5 s)")
    ap.add_argument("--init", default="",
                    help="commands sent once at start, e.g. 'K31;' (KX2 extended mode)")
    ap.add_argument("--af-to-pc", action="store_true",
                    help="map the KX2 AF gain (0-60) to the PC volume")
    args = ap.parse_args()
    rig = RIGS[args.rig]
    args.port = args.port or rig["port"]
    args.baud = args.baud or rig["baud"]
    args.rig_name = rig["name"]

    try:
        ser = Serial(args.port, args.baud)
    except FileNotFoundError:
        sys.exit("port %s not found. Is the rig connected? (ls /dev/ttyUSB*)" % args.port)
    except PermissionError:
        sys.exit("no access to %s. Add the user to the group 'dialout':\n"
                 "  sudo usermod -aG dialout $USER   (then log out and in again)" % args.port)
    except OSError as e:
        sys.exit("cannot open port %s: %s" % (args.port, e))

    logf = open(args.log, "a", buffering=1, encoding="utf-8") if args.log else None
    if logf:
        logf.write("\n# --- Start %s  %s @ %d Bd ---\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                         args.port, args.baud))
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(tui, ser, args, logf)
    except KeyboardInterrupt:
        pass
    except OSError as e:
        print("lost the port: %s" % e)
    finally:
        ser.close()
        if logf:
            logf.close()


if __name__ == "__main__":
    main()
