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
from ..common.serialport import PtyPort
from .audio_rx import AudioReceiver
from .k3emu import K3Emulator
from .kx2 import KX2Link
from .link import RemoteLink
from .ports import default_socket
from .router import Router
from .sync import Sync
from .webui.server import WebUI
from .winkeyer import WinKeyerEmulator

log = logging.getLogger("kx2-local")


async def run(cfg: dict, stop: asyncio.Event | None = None) -> None:
    if cfg["wintest"].get("port_holder", True):
        PtyPort.holder_socket = cfg["wintest"].get("port_socket") or default_socket()
    state = State()
    traffic = TrafficLog(cfg["wintest"].get("traffic_log") or None)
    kx2 = KX2Link(cfg["kx2"], state, traffic)
    link: RemoteLink | None = None
    sync: Sync | None = None
    if cfg["router"]["default"] != "kx2" and cfg["remote"].get("host"):
        link = RemoteLink(cfg["remote"]["host"], cfg["remote"]["port"])
        sync = Sync(state, link, cfg.get("kx2_keys"), kx2=kx2)
        link.on_message = sync.on_message
        def _lost():
            traffic.write("LINK", "lost", "last RTT %s ms" % link.rtt_ms)
            sync.on_disconnect()
        link.on_disconnect = _lost
    elif cfg["router"]["default"] != "kx2":
        log.warning("no remote.host configured – commands for the remote rig are dropped "
                    "(use --local-only or set remote.host)")
    router = Router(cfg["router"], to_kx2=kx2.write, to_remote=sync.send_raw if sync else None,
                    traffic=traffic)
    emu = K3Emulator(cfg["wintest"]["cat_pty_link"], state, kx2, router, traffic)
    wk: WinKeyerEmulator | None = None
    if cfg["wintest"].get("winkeyer_pty_link"):
        if sync:
            wk = WinKeyerEmulator(cfg["wintest"]["winkeyer_pty_link"], on_text=sync.cw_text,
                                  on_abort=sync.cw_abort, on_speed=sync.cw_speed, traffic=traffic,
                                  on_end=sync.cw_end,
                                  cq_repeat_s=cfg["wintest"].get("cq_repeat_s"),
                                  on_clear=sync.cw_clear, on_wpm=sync.cw_wpm, on_ptt=sync.cw_ptt)
            sync.winkeyer = wk
        else:
            wk = WinKeyerEmulator(cfg["wintest"]["winkeyer_pty_link"],
                                  on_text=lambda t: (log.warning("local-only: CW %r not keyed", t),
                                                     wk.done()),
                                  on_abort=lambda: None,
                                  on_speed=lambda w: kx2.write_state("keyer_wpm", w), traffic=traffic)

    audio: AudioReceiver | None = None
    if link and cfg["audio"].get("enabled", True):
        audio = AudioReceiver(cfg["audio"])
        link.on_connect = lambda: link.send({"t": "audio", "port": audio.port})
    if link:
        _connect = link.on_connect

        def _connected():
            traffic.write("LINK", "connected", "%s:%s" % (cfg["remote"]["host"], cfg["remote"]["port"]))
            if _connect:
                _connect()
        link.on_connect = _connected

        def _af(key, value, source):
            if key == "af_gain" and source == "kx2" and isinstance(value, (int, float)):
                audio.set_af(value)                  # KX2 AF knob = laptop volume
        state.subscribe(_af)

    webui: WebUI | None = None
    if cfg["webui"].get("enabled", True):
        webui = WebUI(cfg["webui"], state, sync=sync, link=link, audio=audio, winkeyer=wk)

    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop_or_force, stop)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await kx2.start()
    except OSError as exc:
        log.error("cannot open KX2 port %s: %s", cfg["kx2"]["port"], exc)
        traffic.close()
        raise SystemExit(1)
    emu.start()
    if wk:
        wk.start()
        check_wine_com(cfg["wintest"]["winkeyer_pty_link"], "COM2")
    if audio:
        audio.start()
    if webui:
        try:
            await webui.start()
        except OSError as exc:
            log.error("web UI not started: %s", exc)
            webui = None
    if link:
        link.start()
        log.info("connecting to remote %s:%s", cfg["remote"]["host"], cfg["remote"]["port"])
    check_wine_com(cfg["wintest"]["cat_pty_link"], "COM1")
    if emu.port.held:
        log.info("virtual COM ports from anyrig-ports: Win-Test keeps them across restarts")
    else:
        log.info("own virtual COM ports – after a restart of kx2-local press REOPEN in Win-Test "
                 "(or run anyrig-ports, see README)")
    log.info("kx2-local %s running (router default: %s). Ctrl+C to stop.", __version__, router.default)
    _warn_kx2(state)
    try:
        await stop.wait()
    finally:
        log.info("stopping …")
        for part in (link, audio, webui, wk):
            if part:
                await _bounded(part.stop(), type(part).__name__)
        await _bounded(emu.stop(), "K3Emulator")
        await _bounded(kx2.stop(), "KX2Link")
        traffic.close()
        log.info("stopped")


def _stop_or_force(stop: asyncio.Event) -> None:
    """First Ctrl+C: clean stop. Second Ctrl+C: leave at once (never hang in a terminal)."""
    if stop.is_set():
        log.warning("second Ctrl+C – exiting immediately")
        os._exit(1)
    stop.set()


async def _bounded(coro, name: str, timeout: float = 3.0) -> None:
    try:
        await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        log.warning("%s did not stop within %.0f s – skipped", name, timeout)
    except Exception as exc:
        log.warning("%s: error while stopping: %s", name, exc)


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
    ap.add_argument("--remote", help="remote host[:port], overrides remote.host/port")
    ap.add_argument("--no-audio", action="store_true", help="do not receive RX audio")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)

    cfg = config.load(args.config, config.LOCAL_DEFAULTS)
    if args.local_only:
        cfg["router"]["default"] = "kx2"
    if args.no_audio:
        cfg["audio"]["enabled"] = False
    if args.kx2_port:
        cfg["kx2"]["port"] = args.kx2_port
    if args.remote:
        host, _, port = args.remote.partition(":")
        cfg["remote"]["host"] = host
        if port:
            cfg["remote"]["port"] = int(port)
    logsetup.setup("DEBUG" if args.debug else cfg["log"]["level"], cfg["log"].get("file"))

    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
