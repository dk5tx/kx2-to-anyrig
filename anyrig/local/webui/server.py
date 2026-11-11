"""Local web UI (CLAUDE.md 4.11) – standard library only, no aiohttp.

- ``GET /``         the page (``index.html`` next to this file, plain HTML/JS, no build step)
- ``GET /events``   Server-Sent Events: a JSON snapshot whenever something changed (max 10/s)
- ``POST /api/set`` ``{"k": key, "v": value}`` -> remote rig (only the keys in ``UI_KEYS``)
- ``POST /api/raw`` ``{"d": "CA1;"}`` -> remote rig (only commands matching ``RAW_ALLOWED``)

Everything shown is the rig's real state as reported by the remote, never the last command.
Binds to 127.0.0.1 by default.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("webui")

PAGE = Path(__file__).with_name("index.html")

# settable from the UI (normalized keys of the remote rig)
UI_KEYS = {
    "power", "nr", "nr_level", "nb", "notch", "notch_freq", "beat_cancel", "agc", "break_in_delay",
    "rf_gain", "att", "preamp", "keyer_wpm", "vox",
}
# raw commands the UI buttons may send (ATU tune transmits – only from its own button)
RAW_ALLOWED = re.compile(r"^(AC111|AC110|CA1|CA0|RC|AN[129][019][019]);$")


class WebUI:
    def __init__(self, cfg: dict, state: Any, sync: Any = None, link: Any = None,
                 audio: Any = None, winkeyer: Any = None):
        self.cfg = cfg
        self.state = state
        self.sync = sync
        self.link = link
        self.audio = audio
        self.winkeyer = winkeyer
        self.server: asyncio.base_events.Server | None = None
        self.port = 0
        self.clients = 0
        self._writers: set[asyncio.StreamWriter] = set()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        host, port = self.cfg.get("host", "127.0.0.1"), int(self.cfg.get("port", 8073))
        self.server = await asyncio.start_server(self._handle, host, port)
        self.port = self.server.sockets[0].getsockname()[1]
        log.info("web UI on http://%s:%s/", host, self.port)

    async def stop(self) -> None:
        # open event streams (browser tabs) would keep wait_closed() waiting forever
        # (11 Nov 2026: Ctrl+C did not end kx2-local) -> close them first, never wait long
        if self.server:
            self.server.close()
        for w in list(self._writers):
            try:
                w.close()
            except Exception:
                pass
        if self.server:
            try:
                await asyncio.wait_for(self.server.wait_closed(), 1.0)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ snapshot
    def snapshot(self) -> dict[str, Any]:
        sync, link, audio = self.sync, self.link, self.audio
        info = sync.remote_info if sync else {}
        conn = getattr(link, "conn", None) if link else None
        return {
            "link": {"connected": bool(conn is not None and not getattr(conn, "closed", True)),
                     "rtt_ms": getattr(link, "rtt_ms", None) if link else None,
                     "ready": bool(sync and sync.ready)},
            "rig": info.get("rig"),
            "cw": info.get("cw"),
            "max_power": info.get("max_power", self.cfg.get("max_power", 100)),
            "state": dict(sync.remote_state) if sync else {},
            "meters": {k: v for k, v in (sync.meters if sync else {}).items() if k != "t"},
            "audio": {"state": getattr(audio, "state", "off") if audio else "off",
                      "level_db": getattr(audio, "level_db", None) if audio else None,
                      "volume": getattr(audio, "volume", None) if audio else None,
                      "live": bool(audio and getattr(audio, "live_volume", False))},
            "kx2": {"power": self.state.get("kx2_power"), "cwt": self.state.get("kx2_cwt"),
                    "af": self.state.get("af_gain")},
            "keyer_busy": bool(self.winkeyer and getattr(self.winkeyer, "busy", False)),
        }

    # ------------------------------------------------------------------ commands
    def ui_set(self, key: str, value: Any) -> tuple[bool, str]:
        if key not in UI_KEYS:
            return False, "key not allowed"
        if not self.link or not self.link.send({"t": "set", "k": key, "v": value}):
            return False, "no connection"
        return True, ""

    def ui_raw(self, cmd: str) -> tuple[bool, str]:
        cmd = cmd.strip().upper()
        if not cmd.endswith(";"):
            cmd += ";"
        if not RAW_ALLOWED.match(cmd):
            return False, "command not allowed"
        if not self.link or not self.link.send({"t": "raw", "d": cmd}):
            return False, "no connection"
        log.info("UI: %s", cmd)
        return True, ""

    # ------------------------------------------------------------------ HTTP
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            parts = line.decode("latin-1").split()
            if len(parts) < 2:
                return
            method, path = parts[0], parts[1].split("?", 1)[0]
            headers: dict[str, str] = {}
            while True:
                h = await asyncio.wait_for(reader.readline(), 10)
                if h in (b"\r\n", b"\n", b""):
                    break
                k, _, v = h.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            body = b""
            n = int(headers.get("content-length", "0") or 0)
            if n:
                body = await asyncio.wait_for(reader.readexactly(min(n, 65536)), 10)
            if method == "GET" and path in ("/", "/index.html"):
                await self._send(writer, 200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif method == "GET" and path == "/events":
                await self._events(writer)
            elif method == "GET" and path == "/api/state":
                await self._json(writer, 200, self.snapshot())
            elif method == "POST" and path in ("/api/set", "/api/raw"):
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    await self._json(writer, 400, {"ok": False, "error": "bad json"})
                    return
                if path == "/api/set":
                    ok, err = self.ui_set(str(data.get("k")), data.get("v"))
                else:
                    ok, err = self.ui_raw(str(data.get("d", "")))
                await self._json(writer, 200 if ok else 400, {"ok": ok, "error": err})
            else:
                await self._send(writer, 404, b"not found", "text/plain")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._writers.discard(writer)
            try:
                writer.close()
            except Exception:
                pass

    async def _send(self, writer: asyncio.StreamWriter, code: int, data: bytes, ctype: str) -> None:
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(code, "OK")
        writer.write(("HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
                      "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
                      % (code, reason, ctype, len(data))).encode() + data)
        await writer.drain()

    async def _json(self, writer: asyncio.StreamWriter, code: int, obj: Any) -> None:
        await self._send(writer, code, json.dumps(obj).encode(), "application/json")

    async def _events(self, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Cache-Control: no-store\r\nConnection: keep-alive\r\n\r\n")
        self.clients += 1
        last, last_sent = "", 0.0
        try:
            while True:
                snap = json.dumps(self.snapshot(), separators=(",", ":"))
                now = time.monotonic()
                if snap != last:
                    writer.write(b"data: " + snap.encode() + b"\n\n")
                    last, last_sent = snap, now
                elif now - last_sent > 5:
                    writer.write(b": keep-alive\n\n")
                    last_sent = now
                await writer.drain()
                await asyncio.sleep(0.1)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.clients -= 1
