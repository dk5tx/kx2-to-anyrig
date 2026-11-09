#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
catsniff.py - schneidet die serielle Kommunikation zwischen Win-Test (Wine) und dem Funkgeraet mit

Prinzip: Das Skript legt einen virtuellen Port (pty) an und verbindet ihn mit dem echten Port.
Win-Test wird auf den virtuellen Port umgebogen; alles wird 1:1 durchgereicht und mit
Zeitstempel protokolliert.

    Win-Test (COM1) -> ~/.wine/dosdevices/com1 -> /tmp/catsniff-com1 (pty) -> catsniff -> /dev/ttyUSB0 -> KX2

Einschraenkung: Ein pty hat keine Steuerleitungen. CW-Tastung und PTT ueber DTR/RTS von Win-Test
kommen waehrend des Mitschnitts NICHT am Geraet an. CAT funktioniert normal.

Nur Python-Standardbibliothek.

Aufruf (Win-Test vorher beenden):
    python3 catsniff.py                                  # KX2: /dev/ttyUSB0, 38400 Bd
    python3 catsniff.py --port /dev/ttyUSB1 --baud 115200
    python3 catsniff.py --hex                            # Binaerprotokolle (z. B. WinKeyer)

Dann in einem zweiten Terminal Win-Test umbiegen (das Skript zeigt die Befehle an),
Win-Test starten, normal bedienen, am Ende Win-Test beenden und catsniff mit Strg+C stoppen.
"""
import argparse
import os
import pty
import select
import signal
import sys
import termios
import time
import tty

BAUD = {4800: termios.B4800, 9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400,
        57600: termios.B57600, 115200: termios.B115200}


def stamp():
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d" % int((t % 1) * 1000)


def esc(b):
    return "".join(chr(c) if 32 <= c < 127 and c != 92 else "\\x%02X" % c for c in b)


def open_real(port, baud):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    a = termios.tcgetattr(fd)
    a[0] = termios.IGNPAR
    a[1] = 0
    a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    a[3] = 0
    a[4] = a[5] = BAUD[baud]
    a[6][termios.VMIN] = 0
    a[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, a)
    return fd


class Logger:
    """Protokolliert je Richtung. Textmodus: ein Eintrag pro ';'-Befehl. Hexmodus: rohe Bloecke."""

    def __init__(self, f, hexmode, label_pc, label_rig):
        self.f = f
        self.hex = hexmode
        self.buf = {label_pc: b"", label_rig: b""}
        self.t_first = {}
        self.count = {label_pc: 0, label_rig: 0}

    def feed(self, label, data):
        if self.hex:
            self.f.write("%s %s %s\n" % (stamp(), label, " ".join("%02X" % c for c in data)))
            self.count[label] += len(data)
            return
        if not self.buf[label]:
            self.t_first[label] = stamp()
        self.buf[label] += data
        while b";" in self.buf[label]:
            frame, self.buf[label] = self.buf[label].split(b";", 1)
            self.f.write("%s %s %s;\n" % (self.t_first[label], label, esc(frame)))
            self.count[label] += 1
            self.t_first[label] = stamp()

    def flush(self):
        for label, b in self.buf.items():
            if b:
                self.f.write("%s %s %s  (ohne ';')\n" % (stamp(), label, esc(b)))


def main():
    ap = argparse.ArgumentParser(description="Mitschnitt serieller CAT-Kommunikation ueber einen pty")
    ap.add_argument("--port", default="/dev/ttyUSB0", help="echter Port (Standard /dev/ttyUSB0)")
    ap.add_argument("--baud", type=int, default=38400)
    ap.add_argument("--link", default="/tmp/catsniff-com1", help="Pfad des virtuellen Ports")
    ap.add_argument("--log", default="catsniff.log")
    ap.add_argument("--hex", action="store_true", help="Hex-Ausgabe statt ';'-Befehle")
    ap.add_argument("--quiet", action="store_true", help="nichts auf dem Bildschirm ausgeben")
    args = ap.parse_args()

    try:
        rfd = open_real(args.port, args.baud)
    except OSError as e:
        sys.exit("Port %s: %s  (laeuft Win-Test noch?)" % (args.port, e))

    mfd, sfd = pty.openpty()
    tty.setraw(sfd)
    os.set_blocking(mfd, False)
    slave_name = os.ttyname(sfd)
    try:
        if os.path.islink(args.link) or os.path.exists(args.link):
            os.remove(args.link)
        os.symlink(slave_name, args.link)
    except OSError as e:
        sys.exit("Link %s: %s" % (args.link, e))

    com = os.path.expanduser("~/.wine/dosdevices/com1")
    print("Virtueller Port: %s  ->  %s" % (args.link, slave_name))
    print("Echter Port:     %s @ %d Bd" % (args.port, args.baud))
    print()
    print("Win-Test umbiegen (zweites Terminal, Win-Test beendet):")
    print("  mv %s %s.orig" % (com, com))
    print("  ln -s %s %s" % (args.link, com))
    print("Danach wiederherstellen:")
    print("  rm %s && mv %s.orig %s" % (com, com, com))
    print()
    print("Mitschnitt laeuft -> %s   (Ende mit Strg+C)" % args.log)

    logf = open(args.log, "a", buffering=1, encoding="utf-8")
    logf.write("\n# --- Start %s  %s @ %d Bd  %s ---\n"
               % (time.strftime("%Y-%m-%d %H:%M:%S"), args.port, args.baud, "hex" if args.hex else "text"))
    log = Logger(logf, args.hex, "PC>", "<RIG")

    stop = [False]
    signal.signal(signal.SIGINT, lambda *a: stop.__setitem__(0, True))
    signal.signal(signal.SIGTERM, lambda *a: stop.__setitem__(0, True))

    last_status = 0.0
    try:
        while not stop[0]:
            try:
                r, _, _ = select.select([mfd, rfd], [], [], 0.2)
            except InterruptedError:
                continue
            if mfd in r:
                try:
                    data = os.read(mfd, 4096)
                except (BlockingIOError, OSError):
                    data = b""         # EIO, solange Win-Test den Port nicht offen hat
                if data:
                    os.write(rfd, data)
                    log.feed("PC>", data)
            if rfd in r:
                try:
                    data = os.read(rfd, 4096)
                except BlockingIOError:
                    data = b""
                if data:
                    try:
                        os.write(mfd, data)
                    except OSError:
                        pass
                    log.feed("<RIG", data)
            if not args.quiet and time.time() - last_status > 1.0:
                last_status = time.time()
                sys.stdout.write("\r  PC>RIG: %6d   RIG>PC: %6d  %s"
                                 % (log.count["PC>"], log.count["<RIG"], "Bytes" if args.hex else "Befehle"))
                sys.stdout.flush()
    finally:
        log.flush()
        logf.write("# --- Ende %s ---\n" % time.strftime("%H:%M:%S"))
        logf.close()
        try:
            os.remove(args.link)
        except OSError:
            pass
        os.close(rfd)
        print("\nBeendet. Log: %s" % args.log)


if __name__ == "__main__":
    main()
