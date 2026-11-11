#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ky_test.py - measures CW text output of the TS-590SG with the CAT command KY

WARNING: this script TRANSMITS. Use it only with a dummy load and low power!
(Set the power to 5 W at the rig first.)

Three tests:
  1. short text: time from the command to TX start (TX0), total time
  2. long text (> 24 characters): packets with buffer checks (KY;), gaps?
  3. abort: long text, "KY0;" after 1.5 s -> time to TX end (RX)

Python standard library only. kx2probe.py must not run at the same time.
Usage:   python3 ky_test.py                 (/dev/ttyUSB1, 115200 Bd)
         python3 ky_test.py --port /dev/ttyUSB1 --wpm 28
"""
import argparse
import os
import select
import sys
import termios
import time

BAUD = {9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400,
        57600: termios.B57600, 115200: termios.B115200}

SHORT = "TEST"
LONG = "CQ CQ TEST DL1ABC DL1ABC TEST CQ CQ TEST DL1ABC DL1ABC TEST"
ABORT = "TEST TEST TEST TEST TEST TEST TEST TEST TEST"


class Rig:
    def __init__(self, port, baud, log):
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        a = termios.tcgetattr(self.fd)
        a[0] = termios.IGNPAR
        a[1] = 0
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        a[3] = 0
        a[4] = a[5] = BAUD[baud]
        a[6][termios.VMIN] = 0
        a[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, a)
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self.buf = b""
        self.t0 = time.monotonic()
        self.log = log

    def t(self):
        return (time.monotonic() - self.t0) * 1000.0

    def send(self, s):
        os.write(self.fd, s.encode("ascii"))
        self.log.write("%9.1f TX %s\n" % (self.t(), s))

    def frames(self, timeout):
        """Returns (time_ms, frame) until timeout s without new data."""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if r:
            try:
                self.buf += os.read(self.fd, 4096)
            except BlockingIOError:
                pass
        out = []
        while b";" in self.buf:
            f, self.buf = self.buf.split(b";", 1)
            f = f.decode("latin-1").strip()
            if f:
                tm = self.t()
                self.log.write("%9.1f RX %s;\n" % (tm, f))
                out.append((tm, f))
        return out

    def query(self, cmd, timeout=0.3):
        """Sends e.g. 'KS;' and returns the reply without its prefix; other frames are collected."""
        self.send(cmd)
        key = cmd[:2]
        end = time.monotonic() + timeout
        seen = []
        while time.monotonic() < end:
            for tm, f in self.frames(0.02):
                seen.append((tm, f))
                if f.startswith(key):
                    return f[2:], seen
        return None, seen


def chunks(text, n=24):
    return [text[i:i + n] for i in range(0, len(text), n)]


def ky(text):
    return "KY " + text.ljust(24) + ";"


def wait_for(rig, events, want, timeout):
    """Waits for a frame starting with want. Collects all frames in events."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for tm, f in rig.frames(0.02):
            events.append((tm, f))
            if f.startswith(want):
                return tm
    return None


def tx_edges(events):
    return [(tm, f) for tm, f in events if f.startswith("TX") or f == "RX"]


def expected_ms(text, wpm):
    # rough: PARIS = 50 dits; 1 character ~ 10 dits incl. gap
    dit = 1200.0 / wpm
    return len(text) * 10 * dit


def send_text(rig, text, events):
    """Sends text in 24-character packets, checking the buffer. Returns the time of the first packet."""
    t_first = None
    for part in chunks(text):
        # wait until the buffer has room
        for _ in range(200):
            ans, seen = rig.query("KY;")
            events.extend(seen)
            if ans == "0":
                break
            time.sleep(0.05)
        else:
            print("  buffer did not get free - giving up")
            return t_first
        if t_first is None:
            t_first = rig.t()
        rig.send(ky(part))
        events.extend(rig.frames(0.02))
    return t_first


def report_edges(events, t_start):
    edges = tx_edges(events)
    for tm, f in edges:
        print("    %+8.0f ms  %s" % (tm - t_start, "TX on" if f.startswith("TX") else "TX off"))
    return edges


def main():
    ap = argparse.ArgumentParser(description="TS-590SG KY test (TRANSMITS!)")
    ap.add_argument("--port", default="/dev/ttyUSB1")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--wpm", type=int, default=None, help="keyer speed for the test (default: as set on the rig)")
    ap.add_argument("--log", default="ky_test.log")
    args = ap.parse_args()

    log = open(args.log, "a", buffering=1)
    log.write("\n# --- Start %s ---\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        rig = Rig(args.port, args.baud, log)
    except OSError as e:
        sys.exit("Port %s: %s" % (args.port, e))

    ai_old, _ = rig.query("AI;")
    pc, _ = rig.query("PC;")
    md, _ = rig.query("MD;")
    ks_old, _ = rig.query("KS;")
    print("TS-590SG: mode %s, power %s W, speed %s WPM, AI %s" % (md, pc, ks_old, ai_old))
    if md not in ("3", "7"):
        sys.exit("switch the rig to CW first.")
    if pc is None or int(pc) > 10:
        print("WARNING: power %s W. Continue only with a dummy load!" % pc)
    if input("Dummy load connected and ready to transmit? (yes/no) ").strip().lower() != "yes":
        sys.exit("cancelled.")

    rig.send("AI2;")
    if args.wpm:
        rig.send("KS%03d;" % args.wpm)
    wpm = args.wpm or int(ks_old or 25)
    time.sleep(0.2)
    rig.frames(0.05)

    try:
        # --- Test 1
        print("\n1) short text '%s' at %d WPM (expected about %.0f ms)" % (SHORT, wpm, expected_ms(SHORT, wpm)))
        ev = []
        t_send = rig.t()
        rig.send(ky(SHORT))
        t_on = wait_for(rig, ev, "TX", 2.0)
        t_off = wait_for(rig, ev, "RX", 10.0)
        report_edges(ev, t_send)
        if t_on and t_off:
            print("  -> command to TX start: %.0f ms, keying time: %.0f ms" % (t_on - t_send, t_off - t_on))
        else:
            print("  -> TX0/RX not reported (AI off? TX inhibited?)")
        time.sleep(1.0)

        # --- Test 2
        print("\n2) long text (%d characters = %d packets), expected about %.0f ms"
              % (len(LONG), len(chunks(LONG)), expected_ms(LONG, wpm)))
        ev = []
        t_send = send_text(rig, LONG, ev)
        wait_for(rig, ev, "RX", 30.0)
        edges = report_edges(ev, t_send)
        n_on = sum(1 for _, f in edges if f.startswith("TX"))
        if n_on > 1:
            print("  -> %d TX periods: gaps between the packets (or full break-in)" % n_on)
        elif n_on == 1:
            print("  -> keyed continuously, no gap when refilling")
        time.sleep(1.0)

        # --- Test 3
        print("\n3) abort: long text, 'KY0;' after 1.5 s")
        ev = []
        t_send = send_text(rig, ABORT[:24], ev)
        rig.send(ky(ABORT[24:48]))
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            ev.extend(rig.frames(0.02))
        t_abort = rig.t()
        rig.send("KY0;")
        t_off = wait_for(rig, ev, "RX", 10.0)
        report_edges(ev, t_send)
        if t_off:
            print("  -> abort command to TX end: %.0f ms" % (t_off - t_abort))
        else:
            print("  -> no RX reported after the abort!")
    except KeyboardInterrupt:
        print("\ninterrupted.")
    finally:
        rig.send("KY0;")
        rig.send("RX;")
        if args.wpm and ks_old:
            rig.send("KS%s;" % ks_old)
        if ai_old is not None:
            rig.send("AI%s;" % ai_old)
        time.sleep(0.1)
        log.close()
    print("\nraw data in %s" % args.log)


if __name__ == "__main__":
    main()
