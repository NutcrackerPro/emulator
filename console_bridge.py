"""Authenticated, loopback-only WebSocket bridge to one private UTM VNC socket.

WebSocket framing is handled by the pinned `websockets` library, never by a
custom implementation. Browser clients can forward binary RFB traffic only;
there is no endpoint for choosing a different upstream server or file.
"""

import asyncio
import importlib
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import sys
import threading
import urllib.parse


ROOT = Path(__file__).resolve().parent
WEBSOCKETS_VERSION = "15.0.1"
DEFAULT_PORT = 8767
RFB_GREETING = re.compile(rb"RFB 003\.[0-9]{3}\n")


def default_socket():
    # Signed UTM 4.7.5 runs QEMU in its shared app-group socket directory,
    # distinct from the main app's sandbox. The verified QEMU argument is
    # `-vnc unix:nutcracker-vnc.sock`. Use its one fixed resolved endpoint.
    # Never relax either sandbox or accept client-selected upstream paths.
    return Path.home() / "Library/Group Containers/WDNLXAD4W8.com.utmapp.UTM/nutcracker-vnc.sock"


def load_websockets():
    dependency_path = ROOT / ".runtime" / "python-deps"
    if dependency_path.is_dir():
        if dependency_path.is_symlink() or dependency_path.parent.is_symlink():
            raise ImportError("The local dependency directory cannot be a symlink.")
        if str(dependency_path) not in sys.path:
            sys.path.insert(0, str(dependency_path))
    library = importlib.import_module("websockets")
    if library.__version__ != WEBSOCKETS_VERSION:
        raise ImportError("The tested WebSocket library version is not installed.")
    server = importlib.import_module("websockets.asyncio.server")
    exceptions = importlib.import_module("websockets.exceptions")
    return server.serve, exceptions.ConnectionClosed


class ConsoleUnavailable(Exception):
    pass


class ConsoleBridge:
    def __init__(self, page_port, token, port=DEFAULT_PORT, unix_socket=None, auth=None, remote_origin=None):
        self.page_port = page_port
        self.token = token
        self.auth = auth
        self.remote_origin = remote_origin
        self.remote_host = urllib.parse.urlsplit(remote_origin).netloc if remote_origin else None
        self.port = port
        self.unix_socket = Path(unix_socket) if unix_socket is not None else default_socket()
        self.error = None
        self._loop = None
        self._stop = None
        self._thread = None
        self._ready = threading.Event()
        self.running = False
        self._connection_closed = ()
        self._clients = {}
        if auth is not None:
            auth.add_revocation_listener(self.revoke_session)
        # Library exception logs can include request URLs. Suppress those logs
        # completely so query-string session tokens never reach a log file.
        self._logger = logging.Logger("nutcracker.private-console", level=logging.CRITICAL + 1)
        self._logger.addHandler(logging.NullHandler())
        self._logger.propagate = False

    @property
    def origins(self):
        return ["http://localhost:" + str(self.page_port), "http://127.0.0.1:" + str(self.page_port)] + ([self.remote_origin] if self.remote_origin else [])

    def start(self):
        try:
            self._prepare_directory()
        except (OSError, ConsoleUnavailable):
            self.error = "The private VM display directory could not be prepared. Check its ownership and retry."
            return
        try:
            self._serve, self._connection_closed = load_websockets()
        except (ImportError, AttributeError):
            self.error = "Browser console dependency missing. Run python3 setup_console.py, then restart Nutcracker."
            return
        self._thread = threading.Thread(target=self._run, name="nutcracker-console", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            self.error = "The private browser console did not start. Restart Nutcracker."

    def _run(self):
        try:
            asyncio.run(self._listen())
        except OSError:
            self.error = "The private browser console port is unavailable. Close another Nutcracker session and retry."
        except Exception:
            self.error = "The private browser console could not start. Restart Nutcracker."
        finally:
            self.running = False
            self._ready.set()

    async def _listen(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with self._serve(
            self._forward,
            "127.0.0.1",
            self.port,
            origins=self.origins,
            process_request=self._authorize,
            subprotocols=["binary"],
            select_subprotocol=lambda connection, offered: "binary" if "binary" in offered else None,
            compression=None,
            server_header=None,
            max_size=4 * 1024 * 1024,
            max_queue=8,
            open_timeout=5,
            close_timeout=1,
            logger=self._logger,
        ) as server:
            self._bind_address = server.sockets[0].getsockname()[0]
            self.port = server.sockets[0].getsockname()[1]
            self.running = True
            self._ready.set()
            await self._stop.wait()

    def close(self):
        if self._loop is not None and self._stop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        self.running = False

    def revoke_session(self, session):
        if self._loop is None or not self._loop.is_running():
            return

        async def close_clients():
            clients = list(self._clients.get(session, ()))
            if clients:
                await asyncio.gather(*(client.close(code=1008, reason="Local sign-in ended.") for client in clients), return_exceptions=True)

        try:
            self._loop.call_soon_threadsafe(lambda: asyncio.create_task(close_clients()))
        except RuntimeError:
            pass  # A companion that is already closing has no live session.

    @staticmethod
    def _single_header(headers, name):
        values = headers.get_all(name)
        if len(values) != 1:
            return None
        return values[0]

    def _authorize(self, connection, request):
        host = self._single_header(request.headers, "Host")
        origin = self._single_header(request.headers, "Origin")
        allowed_hosts = {"localhost:" + str(self.port), "127.0.0.1:" + str(self.port)}
        if self.remote_host:
            allowed_hosts.add(self.remote_host)
        if host not in allowed_hosts or origin not in self.origins:
            return connection.respond(403, "Open the private console from the Nutcracker page on this Mac.\n")
        if (host == self.remote_host) != (origin == self.remote_origin):
            return connection.respond(403, "The console origin does not match its address.\n")
        fetch_values = request.headers.get_all("Sec-Fetch-Site")
        # A separate local port makes browser WebSocket requests same-site.
        # Exact Origin checking above is authoritative; remote origins cannot
        # connect, even if they forge or omit Fetch Metadata headers.
        if len(fetch_values) > 1 or (fetch_values and fetch_values[0] not in {"same-origin", "same-site", "none"}):
            return connection.respond(403, "Requests from other sites are blocked.\n")
        parsed = urllib.parse.urlsplit(request.path)
        if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != "/websockify":
            return connection.respond(404, "This private console endpoint does not exist.\n")
        try:
            query = urllib.parse.parse_qs(parsed.query, strict_parsing=True, max_num_fields=2)
        except ValueError:
            return connection.respond(403, "Reload the Nutcracker page to renew this session.\n")
        values = query.get("token", [])
        if set(query) != {"token"} or len(values) != 1 or not secrets.compare_digest(
            values[0].encode("utf-8"), self.token.encode("ascii")
        ):
            return connection.respond(403, "Reload the Nutcracker page to renew this session.\n")
        if self.auth is None or self.auth.session_from_cookie(self._single_header(request.headers, "Cookie")) is None:
            return connection.respond(401, "Sign in to your local Nutcracker account.\n")
        try:
            self._check_socket()
        except ConsoleUnavailable:
            return connection.respond(503, "The Windows VM display is not ready. Start the configured VM and retry.\n")
        return None

    def _prepare_directory(self):
        parent = self.unix_socket.parent
        if parent.is_symlink() or parent.resolve() != parent:
            raise ConsoleUnavailable("The VM display directory cannot be a symlink.")
        info = parent.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ConsoleUnavailable("The VM display directory must already be private to your macOS user.")

    def _check_socket(self):
        target = self.unix_socket
        try:
            parent = target.parent
            info = parent.stat()
            if parent.is_symlink() or parent.resolve() != parent or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ConsoleUnavailable("The VM display directory must be private to your macOS user.")
            info = target.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ConsoleUnavailable("The VM display endpoint is unavailable.")
            # QEMU inherits its parent's umask when creating this socket. Tighten
            # an owned socket before connecting; never touch links or other users' files.
            if stat.S_IMODE(info.st_mode) != 0o600:
                target.chmod(0o600)
                info = target.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ConsoleUnavailable("The VM display socket must be private to your macOS user.")
        except OSError:
            raise ConsoleUnavailable("The Windows VM display is not ready. Start the configured VM and retry.") from None

    def probe(self):
        """Require a live VNC/RFB greeting; socket existence alone isn't enough."""
        self._check_socket()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1)
                client.connect(str(self.unix_socket))
                greeting = bytearray()
                while len(greeting) < 12:
                    part = client.recv(12 - len(greeting))
                    if not part:
                        break
                    greeting.extend(part)
                if not RFB_GREETING.fullmatch(greeting):
                    raise ConsoleUnavailable("The VM display did not return a valid VNC greeting.")
        except OSError:
            raise ConsoleUnavailable("The Windows VM display is not responding. Check UTM and retry.") from None

    def info(self, hostname="localhost"):
        result = {"available": False, "url": None, "error": None, "transport": "vnc"}
        if not self.running:
            result["error"] = self.error or "The private browser console is not running. Restart Nutcracker."
            return result
        if hostname not in {"localhost", "127.0.0.1", self.remote_host}:
            result["error"] = "The private browser console accepts local requests only."
            return result
        try:
            self.probe()
        except ConsoleUnavailable as error:
            result["error"] = str(error)
            return result
        result["available"] = True
        result["url"] = (("wss://" + self.remote_host) if hostname == self.remote_host else "ws://" + hostname + ":" + str(self.port)) + "/websockify?token=" + self.token
        return result

    async def _forward(self, websocket):
        writer = None
        tasks = []
        session = None
        try:
            # Recheck after the HTTP upgrade. Logout can happen between the
            # handshake guard and registering the actual live connection.
            if self.auth is not None:
                session = self.auth.session_from_cookie(self._single_header(websocket.request.headers, "Cookie"))
            if session is None:
                await websocket.close(code=1008, reason="Sign in to your local Nutcracker account.")
                return
            self._clients.setdefault(session, set()).add(websocket)
            if not self.auth.is_valid(session):
                await websocket.close(code=1008, reason="Local sign-in ended.")
                return
            self._check_socket()
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(self.unix_socket)), timeout=3)
            greeting = await asyncio.wait_for(reader.readexactly(12), timeout=3)
            if not RFB_GREETING.fullmatch(greeting):
                await websocket.close(code=1011, reason="The VM display did not respond correctly.")
                return
            if not self.auth.is_valid(session):
                await websocket.close(code=1008, reason="Local sign-in ended.")
                return
            await websocket.send(greeting)

            async def from_vm():
                while True:
                    data = await reader.read(65536)
                    if not data:
                        return
                    if not self.auth.is_valid(session):
                        await websocket.close(code=1008, reason="Local sign-in ended.")
                        return
                    await websocket.send(data)

            async def to_vm():
                async for message in websocket:
                    if not isinstance(message, bytes):
                        await websocket.close(code=1003, reason="The private console requires binary RFB messages.")
                        return
                    if not self.auth.run_if_valid(session, lambda: writer.write(message)):
                        await websocket.close(code=1008, reason="Local sign-in ended.")
                        return
                    await writer.drain()

            async def session_expiry():
                while self.auth.is_valid(session):
                    await asyncio.sleep(0.5)
                await websocket.close(code=1008, reason="Local sign-in ended.")

            tasks = [asyncio.create_task(from_vm()), asyncio.create_task(to_vm()), asyncio.create_task(session_expiry())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (ConsoleUnavailable, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            await websocket.close(code=1011, reason="The Windows VM display is unavailable. Check UTM and retry.")
        except self._connection_closed:
            pass
        finally:
            if session is not None:
                clients = self._clients.get(session)
                if clients is not None:
                    clients.discard(websocket)
                    if not clients:
                        self._clients.pop(session, None)
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
