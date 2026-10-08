"""One loopback gateway for the authenticated page and its fixed VNC bridge.

An HTTPS tunnel forwards to this gateway. The gateway accepts only the exact
configured public Host and Origin and preserves them for the companion's own
authentication checks. No request can select a different upstream. HTTP and
WebSocket framing are handled by the pinned aiohttp library, not custom code.
"""

import asyncio
import importlib
import json
import logging
from pathlib import Path
import re
import sys
import threading
import urllib.parse


ROOT = Path(__file__).resolve().parent
AIOHTTP_VERSION = "3.14.4"
DEFAULT_PORT = 8768
MAX_JSON_BYTES = 16384
MAX_WEBSOCKET_BYTES = 4 * 1024 * 1024
_CRITICAL_HEADERS = {
    "host", "origin", "cookie", "content-length", "content-type",
    "x-nutcracker-token", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest",
    "sec-fetch-user", "sec-websocket-key", "sec-websocket-version",
}
_FORWARD_HEADERS = {
    "accept", "accept-encoding", "cache-control", "content-type", "cookie",
    "if-modified-since", "if-none-match", "origin", "sec-fetch-site",
    "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user", "x-nutcracker-token",
}
_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}


def normalize_remote_origin(value):
    """Require one explicit HTTPS origin, without paths or embedded credentials."""
    if (not isinstance(value, str) or value != value.strip()
            or re.search(r"[\x00-\x20\x7f]", value)):
        raise ValueError("The remote address must be an HTTPS origin.")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("The remote address must be an HTTPS origin.") from None
    hostname = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", hostname)
            or ".." in hostname or "." not in hostname
            or hostname in {"localhost", "127.0.0.1"}
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("The remote address must be an HTTPS origin.")
    host = hostname if port in {None, 443} else hostname + ":" + str(port)
    return "https://" + host


def load_aiohttp():
    dependency_path = ROOT / ".runtime" / "python-deps"
    if dependency_path.is_dir():
        if dependency_path.is_symlink() or dependency_path.parent.is_symlink():
            raise ImportError("The local dependency directory cannot be a symlink.")
        if str(dependency_path) not in sys.path:
            sys.path.insert(0, str(dependency_path))
    library = importlib.import_module("aiohttp")
    if library.__version__ != AIOHTTP_VERSION:
        raise ImportError("The tested remote gateway library is not installed.")
    return library, importlib.import_module("aiohttp.web"), importlib.import_module("multidict")


class RemoteGateway:
    def __init__(self, page_port, console_port, remote_origin, port=DEFAULT_PORT):
        for value in (page_port, console_port):
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
                raise ValueError("The gateway needs valid fixed companion ports.")
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise ValueError("The gateway port is invalid.")
        self.page_port = page_port
        self.console_port = console_port
        self.remote_origin = normalize_remote_origin(remote_origin)
        self.remote_host = urllib.parse.urlsplit(self.remote_origin).netloc
        self.port = port
        self.error = None
        self.running = False
        self._loop = None
        self._stop = None
        self._thread = None
        self._ready = threading.Event()
        self._session = None
        self._websockets = set()
        # Library access/error logs can include cookies or signed WebSocket
        # URLs. Suppress them instead of attempting to scrub arbitrary logs.
        self._logger = logging.Logger("nutcracker.remote-gateway", level=logging.CRITICAL + 1)
        self._logger.addHandler(logging.NullHandler())
        self._logger.propagate = False

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self.error = None
        try:
            self._aiohttp, self._web, self._multidict = load_aiohttp()
        except (ImportError, AttributeError):
            self.error = "Remote access dependency missing. Run the Nutcracker setup helper and restart."
            return
        self._thread = threading.Thread(target=self._run, name="nutcracker-remote-gateway", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            self.error = "The remote access gateway did not start. Restart Nutcracker."
            self.close()

    def _run(self):
        try:
            asyncio.run(self._listen())
        except OSError:
            self.error = "The remote gateway port is unavailable. Close another Nutcracker launcher and retry."
        except Exception:
            self.error = "The remote access gateway could not start. Restart Nutcracker."
        finally:
            self.running = False
            self._ready.set()

    async def _listen(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        app = self._web.Application(client_max_size=MAX_JSON_BYTES)
        app.router.add_route("*", "/{path:.*}", self._handle)
        runner = self._web.AppRunner(
            app, access_log=None, logger=self._logger, shutdown_timeout=2,
            auto_decompress=False,
        )
        try:
            await runner.setup()
            timeout = self._aiohttp.ClientTimeout(total=30, connect=3, sock_read=30)
            async with self._aiohttp.ClientSession(
                cookie_jar=self._aiohttp.DummyCookieJar(), trust_env=False,
                auto_decompress=False, timeout=timeout,
                skip_auto_headers={"Accept-Encoding", "User-Agent"},
            ) as self._session:
                site = self._web.TCPSite(runner, "127.0.0.1", self.port)
                await site.start()
                address = site._server.sockets[0].getsockname()
                self._bind_address, self.port = address[0], address[1]
                self.running = True
                self._ready.set()
                await self._stop.wait()
                if self._websockets:
                    await asyncio.gather(
                        *(client.close(code=1001, message=b"Nutcracker launcher stopped.")
                          for client in list(self._websockets)), return_exceptions=True,
                    )
        finally:
            self._session = None
            await runner.cleanup()

    def close(self):
        if self._loop is not None and self._stop is not None and self._loop.is_running():
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=4)
        self.running = False

    @staticmethod
    def _single_header(request, name):
        values = request.headers.getall(name, [])
        return values[0] if len(values) == 1 else None

    def _response(self, status, message):
        return self._web.Response(
            status=status, text=message + "\n",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    def _check_request(self, request):
        counts = {}
        for name, _ in request.raw_headers:
            lowered = name.decode("ascii", "ignore").lower()
            counts[lowered] = counts.get(lowered, 0) + 1
        if any(counts.get(name, 0) > 1 for name in _CRITICAL_HEADERS):
            return self._response(400, "Ambiguous request headers are blocked.")
        if self._single_header(request, "Host") != self.remote_host:
            return self._response(403, "Open the configured Nutcracker remote address.")
        origin = self._single_header(request, "Origin")
        if origin is not None and origin != self.remote_origin:
            return self._response(403, "Requests from other sites are blocked.")
        if request.method not in {"GET", "HEAD", "POST"}:
            return self._response(405, "This request method is unavailable.")
        if request.method == "POST" and origin != self.remote_origin:
            return self._response(403, "Requests from other sites are blocked.")
        if (request.rel_url.is_absolute() or not request.raw_path.startswith("/")
                or request.raw_path.startswith("//")):
            return self._response(400, "This request address is unavailable.")
        length = self._single_header(request, "Content-Length")
        if length is not None and (not re.fullmatch(r"[0-9]+", length) or int(length) > MAX_JSON_BYTES):
            return self._response(413, "This request is too large.")
        if request.headers.get("Content-Encoding", "identity").lower() != "identity":
            return self._response(415, "Compressed request bodies are unavailable.")
        return None

    def _request_headers(self, request, websocket=False):
        headers = self._multidict.CIMultiDict()
        # Host/Origin are the actual configured public values, not a forged
        # localhost identity. The companion checks the same explicit boundary.
        headers["Host"] = self._single_header(request, "Host")
        allowed = {"cookie", "origin", "sec-fetch-site"} if websocket else _FORWARD_HEADERS
        for name, value in request.headers.items():
            if name.lower() in allowed:
                headers.add(name, value)
        return headers

    async def _handle(self, request):
        refusal = self._check_request(request)
        if refusal is not None:
            return refusal
        websocket_request = request.headers.get("Upgrade", "").lower() == "websocket"
        if request.path == "/websockify":
            if not websocket_request or request.method != "GET":
                return self._response(400, "The Windows display requires a WebSocket connection.")
            if self._single_header(request, "Origin") != self.remote_origin:
                return self._response(403, "Requests from other sites are blocked.")
            return await self._websocket(request)
        if websocket_request:
            return self._response(404, "This WebSocket endpoint does not exist.")
        if request.method != "POST" and request.can_read_body:
            return self._response(400, "This request cannot contain a body.")
        body = None
        if request.method == "POST":
            content_type = self._single_header(request, "Content-Type") or ""
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                return self._response(415, "This request requires JSON.")
            try:
                body = await asyncio.wait_for(request.read(), timeout=10)
                if len(body) > MAX_JSON_BYTES:
                    return self._response(413, "This request is too large.")
                if not isinstance(json.loads(body.decode("utf-8")), dict):
                    return self._response(400, "This request requires a JSON object.")
            except self._web.HTTPRequestEntityTooLarge:
                return self._response(413, "This request is too large.")
            except (ValueError, UnicodeError, RecursionError):
                return self._response(400, "This request requires a JSON object.")
            except asyncio.TimeoutError:
                return self._response(408, "This request did not finish.")
        response = None
        try:
            url = self._multidict_url("http", self.page_port, request.raw_path)
            async with self._session.request(
                request.method, url, headers=self._request_headers(request),
                data=body, allow_redirects=False,
            ) as upstream:
                headers = self._multidict.CIMultiDict()
                blocked = set(_HOP_HEADERS)
                for value in upstream.headers.getall("Connection", []):
                    blocked.update(part.strip().lower() for part in value.split(","))
                for name, value in upstream.headers.items():
                    if name.lower() not in blocked:
                        headers.add(name, value)
                response = self._web.StreamResponse(status=upstream.status, headers=headers)
                await response.prepare(request)
                if request.method != "HEAD":
                    async for chunk in upstream.content.iter_chunked(65536):
                        await response.write(chunk)
                await response.write_eof()
                return response
        except (self._aiohttp.ClientError, asyncio.TimeoutError, OSError):
            if response is not None and response.prepared:
                response.force_close()
                return response
            return self._response(502, "The Nutcracker launcher is unavailable. Keep it running on your Mac.")

    @staticmethod
    def _multidict_url(scheme, port, raw_path):
        # encoded=True retains the exact signed query without re-quoting it.
        # The authority is always our fixed loopback companion, never user input.
        URL = importlib.import_module("yarl").URL
        return URL(scheme + "://127.0.0.1:" + str(port) + raw_path, encoded=True)

    @staticmethod
    def _close_code(code):
        return code if code is not None and 1000 <= code <= 4999 and code not in {1005, 1006, 1015} else 1011

    async def _websocket(self, request):
        front = self._web.WebSocketResponse(
            protocols=("binary",), compress=False, max_msg_size=MAX_WEBSOCKET_BYTES,
            heartbeat=25, timeout=2,
        )
        if not front.can_prepare(request).ok:
            return self._response(400, "The Windows display connection is invalid.")
        backend = None
        tasks = []
        try:
            backend = await self._session.ws_connect(
                self._multidict_url("ws", self.console_port, request.raw_path),
                headers=self._request_headers(request, websocket=True),
                protocols=("binary",), compress=0, max_msg_size=MAX_WEBSOCKET_BYTES,
                heartbeat=25,
                timeout=self._aiohttp.ClientWSTimeout(ws_receive=None, ws_close=2),
            )
            await front.prepare(request)
            self._websockets.add(front)

            async def relay(source, destination):
                async for message in source:
                    if message.type == self._aiohttp.WSMsgType.BINARY:
                        await destination.send_bytes(message.data)
                    elif message.type == self._aiohttp.WSMsgType.TEXT:
                        await front.close(code=1003, message=b"The Windows display requires binary messages.")
                        return
                    elif message.type in {self._aiohttp.WSMsgType.CLOSE, self._aiohttp.WSMsgType.CLOSED, self._aiohttp.WSMsgType.ERROR}:
                        break
                await destination.close(code=self._close_code(source.close_code), message=b"Windows connection ended.")

            tasks = [asyncio.create_task(relay(front, backend)), asyncio.create_task(relay(backend, front))]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            return front
        except self._aiohttp.WSServerHandshakeError as failure:
            # Preserve authentication failure without leaking the signed URL or
            # upstream exception details. The backend owns all session checks.
            status = failure.status if failure.status in {401, 403, 404, 503} else 502
            return self._response(status, "Sign in again or check the Windows connection on your Mac.")
        except (self._aiohttp.ClientError, asyncio.TimeoutError, OSError):
            if front.prepared:
                await front.close(code=1011, message=b"The Windows connection is unavailable.")
                return front
            return self._response(502, "The Windows connection is unavailable. Keep Nutcracker running on your Mac.")
        finally:
            self._websockets.discard(front)
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if backend is not None:
                await backend.close()
            if front.prepared and not front.closed:
                await front.close()
