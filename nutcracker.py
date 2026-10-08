#!/usr/bin/env python3
"""Private, loopback-only companion for a real UTM Windows virtual machine.

This program is a launcher, not a Windows emulator or a Windows installer.
It reads real UTM state. It cannot certify Windows boot or Steam compatibility.
Python 3.9 or later is required. VM controls use the standard library; the
optional browser console uses the tested dependency in requirements.txt.
"""

import argparse
from collections import deque
import getpass
import hashlib
from http.cookies import SimpleCookie, CookieError
import http.server
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser

from console_bridge import ConsoleBridge, DEFAULT_PORT as CONSOLE_PORT


ROOT = Path(__file__).resolve().parent
COMMAND_TIMEOUT = 15
MAX_BODY_BYTES = 4096
SESSION_COOKIE = "nutcracker_session"
SESSION_SECONDS = 8 * 60 * 60
PASSWORD_ITERATIONS = 600000
NAVIGATION_PATHS = frozenset({"/", "/index.html", "/console.html", "/login", "/login.html"})
UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
VM_ROW = re.compile(r"^(" + UUID_PATTERN + r")\s+(\S+)\s+(.*)$")
VM_STATUSES = frozenset({
    "stopped", "starting", "started", "pausing", "paused", "resuming",
    "stopping", "unknown",
})
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/console.html": ("console.html", "text/html; charset=utf-8"),
    "/console.js": ("console.js", "text/javascript; charset=utf-8"),
    "/steam-setup.ps1": ("steam-setup.ps1", "application/octet-stream"),
}
VENDOR_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
}


class CompanionError(Exception):
    """A safe, user-readable error, with a matching HTTP response status."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class LocalAuth:
    """One locally provisioned account; hashed credentials and opaque sessions.

    Passwords never enter source, command arguments, logs, or session records.
    Authentication remains mandatory even when the private record is missing.
    """

    def __init__(self, root=ROOT, clock=time.monotonic, load=True):
        self.root = Path(root).resolve()
        self.path = self.root / ".runtime" / "auth.json"
        self.clock = clock
        self.lock = threading.RLock()
        self._verification = threading.Lock()
        self._attempts = deque()
        self._sessions = {}
        self._listeners = []
        self.record = None
        if load:
            self._load()

    @property
    def configured(self):
        return self.record is not None

    def _private_directory(self, create=False):
        directory = self.path.parent
        if directory.is_symlink():
            raise CompanionError("The private sign-in directory cannot be a link.", 503)
        if create:
            directory.mkdir(mode=0o700, exist_ok=True)
        if not directory.exists():
            return False
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise CompanionError("The private sign-in directory must belong to your Mac account.", 503)
        if create:
            directory.chmod(0o700)
        elif stat.S_IMODE(info.st_mode) & 0o077:
            raise CompanionError("Run password setup to make the local sign-in directory private.", 503)
        return True

    def _load(self):
        # Runtime dependencies may exist before sign-in has been provisioned.
        if not self.path.exists() and not self.path.is_symlink():
            return
        self._private_directory()
        try:
            descriptor = os.open(str(self.path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ValueError("private file required")
                if info.st_size > MAX_BODY_BYTES:
                    raise ValueError("oversized settings")
                record = json.loads(handle.read(MAX_BODY_BYTES + 1).decode("utf-8"))
            if set(record) != {"version", "username", "algorithm", "iterations", "salt", "hash"}:
                raise ValueError("unexpected fields")
            if type(record["version"]) is not int or record["version"] != 1:
                raise ValueError("unexpected version")
            if not isinstance(record["username"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", record["username"]):
                raise ValueError("invalid username")
            if record["algorithm"] != "pbkdf2-sha256" or type(record["iterations"]) is not int or record["iterations"] != PASSWORD_ITERATIONS:
                raise ValueError("unexpected hash parameters")
            for field in ("salt", "hash"):
                if not isinstance(record[field], str) or not re.fullmatch(r"[0-9a-f]{64}", record[field]):
                    raise ValueError("invalid hash")
            self.record = record
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            raise CompanionError("Local sign-in settings are unavailable. Run password setup again.", 503) from None

    @staticmethod
    def _password_bytes(password):
        if not isinstance(password, str):
            raise CompanionError("The sign-in request is invalid.", 400)
        try:
            encoded = password.encode("utf-8")
        except UnicodeEncodeError:
            raise CompanionError("The sign-in request is invalid.", 400) from None
        if len(encoded) > 1024:
            raise CompanionError("The sign-in request is invalid.", 400)
        return encoded

    def configure(self, username, password):
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
            raise CompanionError("Use a username with letters, numbers, dots, underscores, or dashes.", 400)
        encoded = self._password_bytes(password)
        if len(password) < 8:
            raise CompanionError("Use a password with at least eight characters.", 400)
        self._private_directory(create=True)
        if self.path.is_symlink():
            raise CompanionError("The local sign-in file cannot be a link.", 503)
        if self.path.exists():
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise CompanionError("The local sign-in file must belong to your Mac account.", 503)
        salt = secrets.token_bytes(32)
        record = {
            "version": 1, "username": username, "algorithm": "pbkdf2-sha256",
            "iterations": PASSWORD_ITERATIONS, "salt": salt.hex(),
            "hash": hashlib.pbkdf2_hmac("sha256", encoded, salt, PASSWORD_ITERATIONS, dklen=32).hex(),
        }
        temporary = self.path.parent / (".auth-" + secrets.token_hex(16) + ".tmp")
        try:
            descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(record, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary), str(self.path))
        finally:
            if temporary.exists():
                temporary.unlink()
        self.record = record
        self.invalidate_all()

    def login(self, username, password):
        if not self.configured:
            raise CompanionError("Local sign-in needs password setup on this Mac.", 503)
        encoded = self._password_bytes(password)
        if not isinstance(username, str) or len(username) > 128:
            raise CompanionError("The sign-in request is invalid.", 400)
        try:
            username_bytes = username.encode("utf-8")
        except UnicodeEncodeError:
            raise CompanionError("The sign-in request is invalid.", 400) from None
        now = self.clock()
        with self.lock:
            while self._attempts and self._attempts[0] <= now - 60:
                self._attempts.popleft()
            if len(self._attempts) >= 5:
                raise CompanionError("Sign-in is temporarily unavailable. Try again in one minute.", 429)
            if not self._verification.acquire(blocking=False):
                raise CompanionError("Sign-in is temporarily unavailable. Try again in one minute.", 429)
            self._attempts.append(now)
        try:
            digest = hashlib.pbkdf2_hmac("sha256", encoded, bytes.fromhex(self.record["salt"]), PASSWORD_ITERATIONS, dklen=32)
            valid_password = secrets.compare_digest(digest, bytes.fromhex(self.record["hash"]))
            valid_username = secrets.compare_digest(username_bytes, self.record["username"].encode("ascii"))
            if not (valid_password and valid_username):
                raise CompanionError("Username or password is incorrect.", 401)
            with self.lock:
                self._attempts.clear()
                expired = [key for key, deadline in self._sessions.items() if deadline <= self.clock()]
                for key in expired:
                    self._sessions.pop(key, None)
                evicted = None
                if len(self._sessions) >= 8:
                    evicted = next(iter(self._sessions))
                    self._sessions.pop(evicted)
                session = secrets.token_urlsafe(32)
                self._sessions[session] = self.clock() + SESSION_SECONDS
            if evicted:
                self._notify(evicted)
            return session
        finally:
            self._verification.release()

    def session_from_cookie(self, value):
        if not isinstance(value, str) or len(value) > MAX_BODY_BYTES:
            return None
        if sum(part.strip().partition("=")[0] == SESSION_COOKIE for part in value.split(";")) != 1:
            return None
        try:
            cookie = SimpleCookie()
            cookie.load(value)
            session = cookie[SESSION_COOKIE].value
        except (CookieError, KeyError):
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", session) or not self.is_valid(session):
            return None
        return session

    def is_valid(self, session):
        with self.lock:
            deadline = self._sessions.get(session)
            return deadline is not None and deadline > self.clock()

    def run_if_valid(self, session, callback):
        # A logout in another HTTP thread cannot interleave with forwarding an
        # already queued key or pointer message into the VM.
        with self.lock:
            if not self.is_valid(session):
                return False
            callback()
            return True

    def add_revocation_listener(self, callback):
        with self.lock:
            self._listeners.append(callback)

    def _notify(self, session):
        with self.lock:
            callbacks = list(self._listeners)
        for callback in callbacks:
            callback(session)

    def invalidate(self, session):
        with self.lock:
            self._sessions.pop(session, None)
        self._notify(session)

    def invalidate_all(self):
        with self.lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            self._notify(session)


def validate_vm_id(value):
    """Accept UUIDs only, never VM names, paths, flags, or shell text."""
    if not isinstance(value, str) or not re.fullmatch(UUID_PATTERN, value):
        raise CompanionError("Choose a virtual machine with a valid UUID.", 400)
    return str(uuid.UUID(value))


def parse_vm_list(output):
    """Parse UTM's documented UUID / Status / Name table without inventing VMs.

    Official implementation:
    https://github.com/utmapp/UTM/blob/main/utmctl/UTMCtl.swift
    `list` prints a header, then UUID, an eight-character status field, and name.
    Unexpected output is an error, rather than a misleading empty VM list.
    """
    lines = output.splitlines()
    if not lines or not re.fullmatch(r"UUID\s+Status\s+Name", lines[0].strip()):
        raise CompanionError("UTM returned an unexpected VM list. Open UTM and retry.")
    machines = []
    seen = set()
    for line in lines[1:]:
        if not line.strip():
            continue
        match = VM_ROW.fullmatch(line)
        if not match:
            raise CompanionError("UTM returned an unreadable VM entry. Open UTM and retry.")
        identifier = validate_vm_id(match.group(1))
        status = match.group(2)
        if status not in VM_STATUSES or identifier in seen:
            raise CompanionError("UTM returned an unexpected VM status. Open UTM and retry.")
        seen.add(identifier)
        machines.append({"id": identifier, "name": match.group(3), "status": status})
    return machines


def host_details(root=ROOT):
    """Read host facts; unavailable measurements are null, never guessed."""
    system = platform.system()
    os_name = system
    memory_gb = None
    if system == "Darwin":
        version = platform.mac_ver()[0]
        os_name = "macOS" + (" " + version if version else "")
        try:
            result = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=COMMAND_TIMEOUT,
                check=True, shell=False,
            )
            memory_gb = round(int(result.stdout.strip()) / (1024 ** 3), 1)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        if memory_gb is None:
            # Some macOS sandboxes deny sysctl but permit the read-only hardware
            # report. Parse only memory; never return serial numbers or device IDs.
            try:
                result = subprocess.run(
                    ["/usr/sbin/system_profiler", "-json", "SPHardwareDataType"],
                    capture_output=True, text=True, timeout=COMMAND_TIMEOUT,
                    check=True, shell=False,
                )
                report = json.loads(result.stdout)
                physical = report["SPHardwareDataType"][0]["physical_memory"]
                match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(MB|GB|TB)", physical)
                if match:
                    factor = {"MB": 1 / 1024, "GB": 1, "TB": 1024}[match.group(2)]
                    memory_gb = round(float(match.group(1)) * factor, 1)
            except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError):
                pass
    elif system == "Linux":
        try:
            memory_gb = round(
                os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / (1024 ** 3), 1
            )
        except (OSError, ValueError):
            pass
    try:
        disk_free_gb = round(shutil.disk_usage(root).free / (1024 ** 3), 1)
    except OSError:
        disk_free_gb = None
    return {
        "os": os_name,
        "architecture": platform.machine(),
        "memory_gb": memory_gb,
        "disk_free_gb": disk_free_gb,
    }


class UTMCompanion:
    """Use only a known UTM application, with fixed commands and no shell."""

    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.lock = threading.Lock()

    def application(self):
        for app in (
            Path("/Applications/UTM.app"),
            Path.home() / "Applications" / "UTM.app",
            self.root / ".runtime" / "UTM.app",
        ):
            executable = app / "Contents" / "MacOS" / "utmctl"
            if executable.is_file() and os.access(executable, os.X_OK):
                return app
        return None

    def _require_application(self):
        if platform.system() != "Darwin":
            raise CompanionError("The UTM companion runs on macOS. Use it on your Mac.", 503)
        app = self.application()
        if app is None:
            raise CompanionError(
                "UTM is not installed. No Windows VM is available yet. Follow the setup guide.",
                503,
            )
        return app

    @staticmethod
    def _run(arguments):
        try:
            result = subprocess.run(
                arguments, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=COMMAND_TIMEOUT, check=False, shell=False,
            )
        except subprocess.TimeoutExpired:
            raise CompanionError(
                "UTM did not respond within 15 seconds. Check UTM and any macOS permission prompt.",
                504,
            ) from None
        except OSError:
            raise CompanionError("Could not run UTM. Open the UTM app and retry.", 503) from None
        # UTM's Apple-event error handler can print an error but exit with code 0.
        # Treat stderr as failure too, so a denied command cannot look successful.
        if result.returncode != 0 or result.stderr.strip():
            detail = result.stderr.strip() or "UTM exited with an error."
            detail = " ".join(detail.split())[:500]
            raise CompanionError("UTM could not complete the request: " + detail)
        return result.stdout

    def _list(self, app):
        return parse_vm_list(self._run([str(app / "Contents" / "MacOS" / "utmctl"), "list"]))

    def status(self):
        result = {
            "host": host_details(self.root),
            "utm_installed": self.application() is not None,
            "vms": [],
            "error": None,
        }
        try:
            with self.lock:
                app = self._require_application()
                result["vms"] = self._list(app)
        except CompanionError as error:
            result["error"] = str(error)
        return result

    def control(self, action, identifier):
        identifier = validate_vm_id(identifier)
        if action not in {"start", "shutdown"}:
            raise CompanionError("That VM action is unavailable.", 400)
        with self.lock:
            app = self._require_application()
            machines = self._list(app)
            if not any(vm["id"] == identifier for vm in machines):
                raise CompanionError("This virtual machine is not registered in UTM. Refresh the list.", 404)
            command = [str(app / "Contents" / "MacOS" / "utmctl")]
            if action == "start":
                command.extend(["start", identifier])
                message = "Start requested in UTM. Check the VM window for Windows boot."
            else:
                # A bare `stop` defaults to forced power-off in UTM. Always use
                # --request: ask the guest OS to shut down, never force or kill.
                command.extend(["stop", "--request", identifier])
                message = "Normal shutdown requested. Windows may take a moment to close."
            self._run(command)
        return {"ok": True, "message": message, "vm_id": identifier}

    def open_utm(self):
        app = self._require_application()
        self._run(["/usr/bin/open", str(app)])
        return {"ok": True, "message": "UTM opened. Windows and Steam must be set up inside the VM."}


class LocalServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=8765, root=ROOT, companion=None, console=None, console_port=CONSOLE_PORT, console_socket=None, auth=None):
        # The bind address is deliberately fixed and has no configurable override.
        self.root = Path(root).resolve()
        self.companion = companion or UTMCompanion(self.root)
        self.auth = auth if auth is not None else LocalAuth(self.root)
        self.token = secrets.token_urlsafe(32)
        self.login_token = secrets.token_urlsafe(32)
        self.style_nonce = secrets.token_urlsafe(24)
        super().__init__(("127.0.0.1", port), RequestHandler)
        self.console = console or ConsoleBridge(self.server_port, self.token, port=console_port, unix_socket=console_socket, auth=self.auth)
        self.console.start()

    def server_close(self):
        console = getattr(self, "console", None)
        if console is not None:
            console.close()
        super().server_close()


class RequestHandler(http.server.BaseHTTPRequestHandler):
    server_version = "Nutcracker"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # VM details and per-process tokens do not go into access logs.
        pass

    def _header(self, name):
        values = self.headers.get_all(name, [])
        if len(values) > 1:
            raise CompanionError("Duplicate request headers are not supported.", 400)
        return values[0] if values else None

    def _guard(self, mutation=False, login=False):
        host = self._header("Host")
        port = self.server.server_port
        if host not in {"127.0.0.1:" + str(port), "localhost:" + str(port)}:
            raise CompanionError("This private companion accepts local requests only.", 403)
        origin = self._header("Origin")
        if (origin is not None or mutation) and origin != "http://" + host:
            raise CompanionError("Open the companion directly in your local browser.", 403)
        fetch_site = self._header("Sec-Fetch-Site")
        if fetch_site not in {None, "none", "same-origin"}:
            # A public launch-page link may open this one local sign-in entry.
            # Never return authenticated content to its cross-site navigation,
            # even if a client happens to attach a cookie. Fetches, frames,
            # mutations, and every API/asset retain the strict origin boundary.
            if (not mutation and self.command in {"GET", "HEAD"} and fetch_site == "cross-site"
                    and origin is None and self._path() in NAVIGATION_PATHS
                    and self._header("Sec-Fetch-Mode") == "navigate"
                    and self._header("Sec-Fetch-Dest") == "document"
                    and self._header("Sec-Fetch-User") == "?1"):
                return True
            raise CompanionError("Requests from other sites are blocked.", 403)
        if mutation:
            token = self._header("X-Nutcracker-Token") or ""
            expected = self.server.login_token if login else self.server.token
            if not secrets.compare_digest(token.encode("utf-8"), expected.encode("ascii")):
                raise CompanionError("The local session expired. Reload this page and retry.", 403)
        return False

    def _session(self, required=False):
        session = self.server.auth.session_from_cookie(self._header("Cookie"))
        if required and session is None:
            raise CompanionError("Sign in to your local Nutcracker account.", 401)
        return session

    def _path(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise CompanionError("Use a relative local request path.", 400)
        return parsed.path

    def _send(self, status, body, content_type="application/json; charset=utf-8", download=False, cookie=None, location=None):
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        if location is not None:
            self.send_header("Location", location)
        if status == 429:
            self.send_header("Retry-After", "60")
        websocket_port = self.server.console.port
        self.send_header("Content-Security-Policy", (
            "default-src 'self'; script-src 'self'; style-src 'self' 'nonce-" + self.server.style_nonce + "'; "
            "img-src 'self' data:; connect-src 'self' "
            "ws://localhost:" + str(websocket_port) + " ws://127.0.0.1:" + str(websocket_port) + "; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        ))
        self.send_header("Connection", "close")
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="steam-setup.ps1"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, value, cookie=None):
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), cookie=cookie)

    def _error(self, error):
        self._json(error.status, {"ok": False, "error": str(error)})

    def _read_json(self, allow_empty=False):
        if self._header("Transfer-Encoding") is not None:
            raise CompanionError("Chunked request bodies are not supported.", 400)
        length = self._header("Content-Length")
        if length is None and allow_empty:
            return {}
        if length is None or not re.fullmatch(r"[0-9]+", length):
            raise CompanionError("A valid JSON request length is required.", 400)
        size = int(length)
        if size > MAX_BODY_BYTES:
            raise CompanionError("The request is too large.", 413)
        if size == 0 and allow_empty:
            return {}
        content_type = (self._header("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise CompanionError("Send the request as application/json.", 415)
        try:
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise CompanionError("The JSON request body was incomplete.", 400)
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TimeoutError):
            raise CompanionError("Send a valid JSON request.", 400) from None
        if not isinstance(value, dict):
            raise CompanionError("The JSON request must be an object.", 400)
        return value

    def _vendor_asset(self, path):
        prefix = "/vendor/novnc/"
        if not path.startswith(prefix):
            raise CompanionError("This file is not available from the companion.", 404)
        relative = path[len(prefix):]
        parts = relative.split("/")
        if not parts or any(part in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
            raise CompanionError("This vendor file is unavailable.", 404)
        base = self.server.root / "vendor" / "novnc"
        candidate = base.joinpath(*parts)
        content_type = VENDOR_TYPES.get(candidate.suffix.lower())
        if content_type is None:
            raise CompanionError("This vendor file type is unavailable.", 404)
        current = self.server.root
        for part in ("vendor", "novnc", *parts):
            current = current / part
            if current.is_symlink():
                raise CompanionError("Vendor links are not served by the companion.", 404)
        if not candidate.is_file():
            raise CompanionError("This vendor file is missing.", 404)
        try:
            candidate.resolve().relative_to(base.resolve())
        except ValueError:
            raise CompanionError("This vendor file is unavailable.", 404) from None
        return candidate, content_type

    def do_GET(self):
        try:
            external_navigation = self._guard()
            path = self._path()
            if external_navigation and path not in {"/login", "/login.html"}:
                destination = "/console.html" if path == "/console.html" else "/index.html"
                self._send(303, b"", location="/login.html?next=" + urllib.parse.quote(destination, safe=""))
                return
            if path == "/api/auth":
                session = self._session()
                result = {"authenticated": session is not None, "configured": self.server.auth.configured}
                if session is not None:
                    result.update({"username": self.server.auth.record["username"], "token": self.server.token})
                self._json(200, result)
                return
            if path in {"/login", "/login.html", "/login.js"}:
                filename = "login.js" if path == "/login.js" else "login.html"
                file = self.server.root / filename
                if file.is_symlink() or not file.is_file() or file.resolve().parent != self.server.root:
                    raise CompanionError("The local sign-in page is missing.", 503)
                try:
                    body = file.read_bytes()
                except OSError:
                    raise CompanionError("The local sign-in page could not be read.", 503) from None
                if filename == "login.html":
                    body = body.replace(b"__NUTCRACKER_LOGIN_TOKEN__", self.server.login_token.encode("ascii"))
                    body = body.replace(b"__NUTCRACKER_STYLE_NONCE__", self.server.style_nonce.encode("ascii"))
                self._send(200, body, "text/javascript; charset=utf-8" if filename.endswith(".js") else "text/html; charset=utf-8")
                return
            if self._session() is None:
                if path in {"/", "/index.html", "/console.html"}:
                    destination = "/console.html" if path == "/console.html" else "/index.html"
                    self._send(303, b"", location="/login.html?next=" + urllib.parse.quote(destination, safe=""))
                    return
                raise CompanionError("Sign in to your local Nutcracker account.", 401)
            if path == "/api/status":
                self._json(200, self.server.companion.status())
                return
            if path == "/api/console":
                hostname = self._header("Host").split(":", 1)[0]
                self._json(200, self.server.console.info(hostname))
                return
            if path in ASSETS:
                filename, content_type = ASSETS[path]
                file = self.server.root / filename
                # Only these fixed assets are accessible, even if a symlink is added.
                if file.is_symlink() or not file.is_file() or file.resolve().parent != self.server.root:
                    raise CompanionError("This companion file is missing.", 404)
            else:
                file, content_type = self._vendor_asset(path)
                filename = file.name
            try:
                body = file.read_bytes()
            except OSError:
                raise CompanionError("This companion file could not be read.", 500) from None
            if path in {"/", "/index.html", "/console.html"}:
                body = body.replace(b"__NUTCRACKER_TOKEN__", self.server.token.encode("ascii"))
            self._send(200, body, content_type, download=filename == "steam-setup.ps1")
        except CompanionError as error:
            self._error(error)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        try:
            path = self._path()
            self._guard(mutation=True, login=path == "/api/login")
            if path == "/api/login":
                body = self._read_json()
                if set(body) != {"username", "password"}:
                    raise CompanionError("The sign-in request is invalid.", 400)
                session = self.server.auth.login(body["username"], body["password"])
                cookie = SESSION_COOKIE + "=" + session + "; HttpOnly; SameSite=Strict; Path=/; Max-Age=" + str(SESSION_SECONDS)
                self._json(200, {"ok": True}, cookie=cookie)
                return
            session = self._session(required=True)
            if path == "/api/logout":
                if self._read_json(allow_empty=True):
                    raise CompanionError("Signing out takes no request options.", 400)
                self.server.auth.invalidate(session)
                self._json(200, {"ok": True}, cookie=SESSION_COOKIE + "=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
                return
            if path == "/api/open-utm":
                if self._read_json(allow_empty=True):
                    raise CompanionError("Opening UTM takes no request options.", 400)
                result = self.server.companion.open_utm()
            elif path in {"/api/vm/start", "/api/vm/shutdown"}:
                body = self._read_json()
                if set(body) != {"id"}:
                    raise CompanionError("Provide only the selected virtual machine's id.", 400)
                identifier = validate_vm_id(body["id"])
                result = self.server.companion.control(path.rsplit("/", 1)[1], identifier)
            else:
                raise CompanionError("This companion action does not exist.", 404)
            self._json(200, result)
        except CompanionError as error:
            self._error(error)

    def _unsupported(self):
        try:
            self._guard()
            raise CompanionError("This request method is not supported.", 405)
        except CompanionError as error:
            self._error(error)

    do_OPTIONS = _unsupported
    do_PUT = _unsupported
    do_DELETE = _unsupported
    do_PATCH = _unsupported


def main(argv=None):
    parser = argparse.ArgumentParser(description="Open your private local UTM companion.")
    parser.add_argument("--port", type=int, default=8765, help="Local browser port (default: 8765).")
    parser.add_argument("--check", action="store_true", help="Print actual host and UTM status, then exit.")
    parser.add_argument("--no-browser", action="store_true", help="Run without opening the browser.")
    parser.add_argument("--set-password", action="store_true", help="Provision or reset the private local account, then exit.")
    parser.add_argument("--username", default="Nutcracker", help="Username for local password setup (default: Nutcracker).")
    parser.add_argument("--password-stdin", action="store_true", help="Read the setup password from standard input, never command arguments.")
    args = parser.parse_args(argv)
    if args.password_stdin and not args.set_password:
        parser.error("Use --password-stdin only with --set-password.")
    if args.set_password:
        try:
            if args.password_stdin:
                raw = sys.stdin.buffer.read(1027)
                if len(raw) > 1026:
                    raise CompanionError("The setup password is too long.", 400)
                password = raw.decode("utf-8").rstrip("\r\n")
            else:
                password = getpass.getpass("New local password: ")
                confirmation = getpass.getpass("Confirm local password: ")
                if not secrets.compare_digest(password.encode("utf-8"), confirmation.encode("utf-8")):
                    raise CompanionError("The passwords did not match.", 400)
            LocalAuth(ROOT, load=False).configure(args.username, password)
        except (CompanionError, OSError, UnicodeError) as error:
            print(str(error) if isinstance(error, CompanionError) else "Local password setup could not be completed.", file=sys.stderr)
            return 1
        print("Local sign-in configured. Restart Nutcracker to use it.")
        return 0
    if not 1 <= args.port <= 65535:
        parser.error("Choose a port between 1 and 65535.")
    if args.check:
        print(json.dumps(UTMCompanion().status(), indent=2, ensure_ascii=False))
        return 0
    try:
        server = LocalServer(args.port)
    except (OSError, CompanionError) as error:
        print("The local companion could not start: " + str(error), file=sys.stderr)
        print("Close an existing companion, or choose another --port.", file=sys.stderr)
        return 1
    url = "http://localhost:" + str(server.server_port)
    print("Nutcracker is ready at " + url, flush=True)
    print("Private to this Mac. Press Control-C here to close the companion.", flush=True)
    print("UTM, Windows setup, and Steam compatibility require separate verification.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nNutcracker closed. Any running VM is still managed by UTM.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
