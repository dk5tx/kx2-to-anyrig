#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ky_test.py - misst die CW-Ausgabe des TS-590SG per CAT-Befehl KY

ACHTUNG: Dieses Skript SENDET. Nur mit Dummy Load und kleiner Leistung benutzen!
(Leistung vorher am Geraet auf 5 W stellen.)

Drei Tests:
  1. Kurzer Text: Zeit vom Befehl bis Sendebeginn (TX0), Gesamtdauer
  2. Langer Text (> 24 Zeichen): Stueckelung mit Pufferabfrage (KY;), Luecken?
  3. Abbruch: langer Text, nach 1,5 s "KY0;" -> Zeit bis Sendeende (RX)

Nur Python-Standardbibliothek. kx2probe.py darf nicht gleichzeitig laufen.
Aufruf:  python3 ky_test.py                 (/dev/ttyUSB1, 115200 Bd)
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
        """Liefert (Zeit_ms, Frame) bis timeout s ohne neue Daten."""
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
        """Sendet z. B. 'KS;' und liefert die Antwort ohne Kennung, andere Frames werden gesammelt."""
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
    """Wartet auf einen Frame, der mit want beginnt. Sammelt alle Frames in events."""
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
    # grob: PARIS = 50 Dits; 1 Zeichen ~ 10 Dits inkl. Zwischenraum
    dit = 1200.0 / wpm
    return len(text) * 10 * dit


def send_text(rig, text, events):
    """Sendet Text in 24er-Stuecken mit Pufferabfrage. Liefert Zeitpunkt der ersten Sendung."""
    t_first = None
    for part in chunks(text):
        # warten, bis Puffer Platz hat
        for _ in range(200):
            ans, seen = rig.query("KY;")
            events.extend(seen)
            if ans == "0":
                break
            time.sleep(0.05)
        else:
            print("  Puffer wurde nicht frei - Abbruch")
            return t_first
        if t_first is None:
            t_first = rig.t()
        rig.send(ky(part))
        events.extend(rig.frames(0.02))
    return t_first


def report_edges(events, t_start):
    edges = tx_edges(events)
    for tm, f in edges:
        print("    %+8.0f ms  %s" % (tm - t_start, "Senden EIN" if f.startswith("TX") else "Senden AUS"))
    return edges


def main():
    ap = argparse.ArgumentParser(description="TS-590SG KY-Test (SENDET!)")
    ap.add_argument("--port", default="/dev/ttyUSB1")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--wpm", type=int, default=None, help="Keyer-Tempo fuer den Test setzen (sonst wie eingestellt)")
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
    print("TS-590SG: Mode %s, Leistung %s W, Tempo %s WPM, AI %s" % (md, pc, ks_old, ai_old))
    if md not in ("3", "7"):
        sys.exit("Bitte zuerst auf CW schalten.")
    if pc is None or int(pc) > 10:
        print("WARNUNG: Leistung %s W. Nur mit Dummy Load fortfahren!" % pc)
    if input("Dummy Load angeschlossen und bereit zum Senden? (ja/nein) ").strip().lower() != "ja":
        sys.exit("Abgebrochen.")

    rig.send("AI2;")
    if args.wpm:
        rig.send("KS%03d;" % args.wpm)
    wpm = args.wpm or int(ks_old or 25)
    time.sleep(0.2)
    rig.frames(0.05)

    try:
        # --- Test 1
        print("\n1) Kurzer Text '%s' bei %d WPM (erwartet ca. %.0f ms)" % (SHORT, wpm, expected_ms(SHORT, wpm)))
        ev = []
        t_send = rig.t()
        rig.send(ky(SHORT))
        t_on = wait_for(rig, ev, "TX", 2.0)
        t_off = wait_for(rig, ev, "RX", 10.0)
        report_edges(ev, t_send)
        if t_on and t_off:
            print("  -> Befehl bis Sendebeginn: %.0f ms, Sendedauer: %.0f ms" % (t_on - t_send, t_off - t_on))
        else:
            print("  -> TX0/RX nicht gemeldet (AI aus? TX gesperrt?)")
        time.sleep(1.0)

        # --- Test 2
        print("\n2) Langer Text (%d Zeichen = %d Stuecke), erwartet ca. %.0f ms"
              % (len(LONG), len(chunks(LONG)), expected_ms(LONG, wpm)))
        ev = []
        t_send = send_text(rig, LONG, ev)
        wait_for(rig, ev, "RX", 30.0)
        edges = report_edges(ev, t_send)
        n_on = sum(1 for _, f in edges if f.startswith("TX"))
        if n_on > 1:
            print("  -> %d Sendephasen: Luecken zwischen den Stuecken (oder Full Break-in)" % n_on)
        elif n_on == 1:
            print("  -> durchgehend gesendet, keine Luecke beim Nachladen")
        time.sleep(1.0)

        # --- Test 3
        print("\n3) Abbruch: langer Text, nach 1,5 s 'KY0;'")
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
            print("  -> Abbruchbefehl bis Sendeende: %.0f ms" % (t_off - t_abort))
        else:
            print("  -> kein RX nach Abbruch gemeldet!")
    except KeyboardInterrupt:
        print("\nUnterbrochen.")
    finally:
        rig.send("KY0;")
        rig.send("RX;")
        if args.wpm and ks_old:
            rig.send("KS%s;" % ks_old)
        if ai_old is not None:
            rig.send("AI%s;" % ai_old)
        time.sleep(0.1)
        log.close()
    print("\nRohdaten in %s" % args.log)


if __name__ == "__main__":
    main()
