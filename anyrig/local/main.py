"""kx2-local: the laptop side (KX2 + Win-Test).

Milestone M1: KX2 link and Win-Test K3 emulation. Without a remote connection run with
``--local-only``: Win-Test then behaves exactly as with a directly connected KX2, and every
frame is logged to the traffic log.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from .. import __version__
from ..common import config, log as logsetup
from ..common.log import TrafficLog
from ..common.state import State
from .k3emu import K3Emulator
from .kx2 import KX2Link
from .router import Router

log = logging.getLogger("kx2-local")


async def run(cfg: dict, stop: asyncio.Event | None = None) -> None:
    state = State()
    traffic = TrafficLog(cfg["wintest"].get("traffic_log") or None)
    kx2 = KX2Link(cfg["kx2"], state, traffic)
    router = Router(cfg["router"], to_kx2=kx2.write, to_remote=None, traffic=traffic)
    emu = K3Emulator(cfg["wintest"]["cat_pty_link"], state, kx2, router, traffic)

    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await kx2.start()
    except OSError as exc:
        log.error("cannot open KX2 port %s: %s", cfg["kx2"]["port"], exc)
        traffic.close()
        raise SystemExit(1)
    emu.start()
    check_wine_com(cfg["wintest"]["cat_pty_link"], "COM1")
    log.info("kx2-local %s running (router default: %s). Ctrl+C to stop.", __version__, router.default)
    _warn_kx2(state)
    try:
        await stop.wait()
    finally:
        await emu.stop()
        await kx2.stop()
        traffic.close()
        log.info("stopped")


def wine_com_path(com: str) -> str:
    prefix = os.environ.get("WINEPREFIX") or os.path.expanduser("~/.wine")
    return os.path.join(prefix, "dosdevices", com.lower())


def check_wine_com(link: str, com: str) -> bool:
    """Warn if Wine's COM port does not lead to our pty. Returns True if it does."""
    path = wine_com_path(com)
    if os.path.realpath(path) == os.path.realpath(link):
        log.info("Wine %s -> %s: OK", com, link)
        return True
    target = os.readlink(path) if os.path.islink(path) else "(missing)"
    log.warning("Wine %s points to %s, not to %s. Win-Test will not find the K3. Fix once with:\n"
                "  wine reg add 'HKLM\\Software\\Wine\\Ports' /v %s /t REG_SZ /d %s /f && wineserver -k\n"
                "then start Win-Test again.", com, target, link, com, link)
    return False


def _warn_kx2(state: State) -> None:
    def check(key, value, source):
        if key == "kx2_power" and value:
            log.warning("KX2 output power is %s – set it to 0 / TX TEST, the KX2 must not transmit", value)
        if key == "kx2_cwt" and value:
            log.warning("CWT (auto spot) is on at the KX2 – switch it off for remote operation")
    state.subscribe(check)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="kx2-local", description="KX2 control head for a remote rig")
    ap.add_argument("-c", "--config", help="YAML config (default: built-in defaults)")
    ap.add_argument("--local-only", action="store_true",
                    help="no remote: route all Win-Test commands to the KX2 (test/M1 mode)")
    ap.add_argument("--kx2-port", help="override kx2.port")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)

    cfg = config.load(args.config, config.LOCAL_DEFAULTS)
    if args.local_only:
        cfg["router"]["default"] = "kx2"
    if args.kx2_port:
        cfg["kx2"]["port"] = args.kx2_port
    logsetup.setup("DEBUG" if args.debug else cfg["log"]["level"], cfg["log"].get("file"))

    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
