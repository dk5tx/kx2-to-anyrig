"""anyrig-ports: keeps Win-Test's virtual COM ports open while kx2-local restarts.

Without it the ptys live inside kx2-local: after a restart they are new devices, Win-Test still
holds the old (dead) ones, gets an error on every read and becomes slow until REOPEN (measured
11 Nov 2026).

anyrig-ports creates the ptys and their symlinks (``/tmp/anyrig-com1`` …) and keeps them for as
long as it runs. kx2-local asks it for a port over a Unix socket and receives the pty master
file descriptor itself (SCM_RIGHTS) - no data is relayed, so there is no extra latency. While no
kx2-local holds a port, anyrig-ports reads and discards what Win-Test writes, so Win-Test never
blocks on a full buffer; its K3 polls just get no answer.

Protocol (one connection per port, kept open while the port is in use):
    client -> ``OPEN /tmp/anyrig-com1\\n``
    server -> ``OK /dev/pts/5\\n`` + the master fd   or   ``ERR <reason>\\n``
A closed connection gives the port back.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket

from ..common import config, log as logsetup
from ..common.serialport import PtyPort

log = logging.getLogger("anyrig-ports")


def default_socket() -> str:
    run = os.environ.get("XDG_RUNTIME_DIR")
    if run and os.path.isdir(run):
        return os.path.join(run, "anyrig-ports.sock")
    return "/tmp/anyrig-ports-%d.sock" % os.getuid()


# ---------------------------------------------------------------------- client (kx2-local)
def request_port(link: str, sock_path: str | None = None,
                 timeout: float = 1.0) -> tuple[int, str, socket.socket] | None:
    """Ask a running anyrig-ports for ``link``. Returns (master fd, slave name, connection) or
    None if no holder runs or it does not hold this link. Keep the connection open while the
    port is used; closing it gives the port back."""
    path = sock_path or default_socket()
    if not os.path.exists(path):
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        s.sendall(("OPEN %s\n" % link).encode())
        msg, fds, _, _ = socket.recv_fds(s, 512, 1)
    except OSError as exc:
        log.debug("anyrig-ports at %s not usable: %s", path, exc)
        s.close()
        return None
    text = msg.decode(errors="replace").strip()
    if not text.startswith("OK ") or not fds:
        for fd in fds:
            os.close(fd)
        s.close()
        log.warning("anyrig-ports: %s – %s", link, text or "no answer")
        return None
    fd = fds[0]
    os.set_blocking(fd, False)
    while True:                                     # drop what Win-Test sent while nobody listened
        try:
            if not os.read(fd, 4096):
                break
        except OSError:
            break
    s.settimeout(None)
    return fd, text[3:], s


# ---------------------------------------------------------------------- server
class PortHolder:
    def __init__(self, links: list[str], sock_path: str | None = None):
        self.links = [l for l in links if l]
        self.sock_path = sock_path or default_socket()
        self.ports: dict[str, PtyPort] = {}
        self.users: dict[str, socket.socket] = {}
        self._server: socket.socket | None = None
        self._task: asyncio.Task | None = None
        self._clients: set[asyncio.Task] = set()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        for link in self.links:
            port = PtyPort(link, use_holder=False)
            port.open()
            self.ports[link] = port
            self._drain(link, True)
            log.info("holding %s -> %s", link, port.slave_name)
        if os.path.exists(self.sock_path):
            os.remove(self.sock_path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock_path)
        os.chmod(self.sock_path, 0o600)
        srv.listen(4)
        srv.setblocking(False)
        self._server = srv
        self._task = loop.create_task(self._accept())
        log.info("waiting for kx2-local on %s", self.sock_path)

    def _drain(self, link: str, on: bool) -> None:
        port = self.ports[link]
        loop = asyncio.get_running_loop()
        if on:
            def discard() -> None:
                try:
                    os.read(port.fd, 4096)
                except OSError:
                    pass
            loop.add_reader(port.fd, discard)
        else:
            loop.remove_reader(port.fd)

    async def _accept(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            conn, _ = await loop.sock_accept(self._server)
            t = loop.create_task(self._client(conn))
            self._clients.add(t)
            t.add_done_callback(self._clients.discard)

    async def _client(self, conn: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        conn.setblocking(False)
        link = None
        try:
            line = b""
            while not line.endswith(b"\n") and len(line) < 512:
                chunk = await asyncio.wait_for(loop.sock_recv(conn, 512), 2.0)
                if not chunk:
                    return
                line += chunk
            cmd, _, link = line.decode(errors="replace").strip().partition(" ")
            port = self.ports.get(link)
            if cmd != "OPEN" or port is None:
                await loop.sock_sendall(conn, b"ERR unknown port\n")
                link = None
                return
            old = self.users.pop(link, None)
            if old is not None:
                log.warning("%s: taken over by a new kx2-local", link)
                old.close()
            else:
                self._drain(link, False)
            self.users[link] = conn
            socket.send_fds(conn, [("OK %s\n" % port.slave_name).encode()], [port.fd])
            log.info("%s handed to kx2-local", link)
            while await loop.sock_recv(conn, 64):   # until kx2-local closes / ends
                pass
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            if link and self.users.get(link) is conn:
                del self.users[link]
                self._drain(link, True)
                log.info("%s back from kx2-local – kept open for Win-Test", link)
            conn.close()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for t in list(self._clients):
            t.cancel()
        if self._server:
            self._server.close()
            try:
                os.remove(self.sock_path)
            except OSError:
                pass
        for link, port in self.ports.items():
            try:
                asyncio.get_running_loop().remove_reader(port.fd)
            except Exception:
                pass
            port.close()


async def run(links: list[str], sock_path: str | None) -> None:
    holder = PortHolder(links, sock_path)
    await holder.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await holder.stop()
        log.info("stopped – the ports are gone, Win-Test needs REOPEN after the next start")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="anyrig-ports",
                                 description="keep Win-Test's virtual COM ports open across "
                                             "kx2-local restarts")
    ap.add_argument("-c", "--config", help="kx2-local YAML config (port links are taken from it)")
    ap.add_argument("--socket", help="Unix socket (default: %s)" % default_socket())
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)
    cfg = config.load(args.config, config.LOCAL_DEFAULTS)
    logsetup.setup("DEBUG" if args.debug else cfg["log"]["level"], None)
    wt = cfg["wintest"]
    links = [wt.get("cat_pty_link"), wt.get("winkeyer_pty_link")]
    asyncio.run(run([l for l in links if l], args.socket or wt.get("port_socket")))


if __name__ == "__main__":
    main()
