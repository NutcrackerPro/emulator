"""Safety and real-state contracts for the loopback UTM companion.

Run: python3 -m unittest -v test_nutcracker.py
These tests never start, stop, install, or open a real VM or application.
"""

import http.client
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import nutcracker
import console_bridge


VM_ID = "11111111-2222-4333-a444-555555555555"
OTHER_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
HEADER = "UUID                                 Status   Name\n"
VM_LIST = HEADER + VM_ID + " started  Windows 11 ARM\n"
APP = Path("/Applications/UTM.app")
CTL = str(APP / "Contents" / "MacOS" / "utmctl")
HOST = {"os": "macOS", "architecture": "arm64", "memory_gb": 24.0, "disk_free_gb": 100.0}


class VMParsingTests(unittest.TestCase):
    def test_official_table_preserves_name_and_all_native_statuses(self):
        for status in nutcracker.VM_STATUSES:
            with self.subTest(status=status):
                output = HEADER + VM_ID.upper() + " " + status.ljust(8) + " Windows 11  ARM edition\n"
                self.assertEqual(nutcracker.parse_vm_list(output), [{
                    "id": VM_ID, "name": "Windows 11  ARM edition", "status": status,
                }])

    def test_header_only_means_no_registered_vm(self):
        self.assertEqual(nutcracker.parse_vm_list(HEADER), [])

    def test_unreadable_list_is_error_not_empty_state(self):
        for output in ("", "Permission denied", "UUID Status Name\nnot-a-uuid started Windows\n",
                       HEADER + VM_ID + " running Windows\n", VM_LIST + VM_LIST.splitlines()[1]):
            with self.subTest(output=output):
                with self.assertRaises(nutcracker.CompanionError):
                    nutcracker.parse_vm_list(output)

    def test_vm_ids_cannot_be_names_flags_paths_or_shell_text(self):
        for value in (None, 1, "Windows 11", "--force", "../VM", VM_ID + "; whoami",
                      "{" + VM_ID + "}", VM_ID + "\n"):
            with self.subTest(value=value):
                with self.assertRaises(nutcracker.CompanionError) as caught:
                    nutcracker.validate_vm_id(value)
                self.assertEqual(caught.exception.status, 400)


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.companion = nutcracker.UTMCompanion()
        self.system = patch("nutcracker.platform.system", return_value="Darwin")
        self.system.start()
        self.addCleanup(self.system.stop)
        self.application = patch.object(self.companion, "application", return_value=APP)
        self.application.start()
        self.addCleanup(self.application.stop)

    def test_status_is_actual_command_output(self):
        with patch("nutcracker.host_details", return_value=HOST), patch(
            "nutcracker.subprocess.run", return_value=subprocess.CompletedProcess([CTL, "list"], 0, VM_LIST, "")
        ) as run:
            status = self.companion.status()
        self.assertEqual(status, {
            "host": HOST, "utm_installed": True,
            "vms": [{"id": VM_ID, "name": "Windows 11 ARM", "status": "started"}],
            "error": None,
        })
        self.assertEqual(run.call_args.args[0], [CTL, "list"])
        self.assertEqual(run.call_args.kwargs["timeout"], 15)
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_missing_utm_reports_no_vm_and_does_not_run_commands(self):
        with patch.object(self.companion, "application", return_value=None), patch(
            "nutcracker.host_details", return_value=HOST
        ), patch("nutcracker.subprocess.run") as run:
            status = self.companion.status()
        self.assertFalse(status["utm_installed"])
        self.assertEqual(status["vms"], [])
        self.assertIn("UTM is not installed", status["error"])
        run.assert_not_called()

    def test_bad_list_does_not_claim_a_successful_status(self):
        with patch("nutcracker.host_details", return_value=HOST), patch(
            "nutcracker.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "nonsense", "")
        ):
            status = self.companion.status()
        self.assertEqual(status["vms"], [])
        self.assertIsNotNone(status["error"])

    def test_start_validates_against_registered_vms_and_uses_fixed_command(self):
        results = [subprocess.CompletedProcess([], 0, VM_LIST, ""), subprocess.CompletedProcess([], 0, "", "")]
        with patch("nutcracker.subprocess.run", side_effect=results) as run:
            result = self.companion.control("start", VM_ID.upper())
        self.assertTrue(result["ok"])
        self.assertEqual(result["vm_id"], VM_ID)
        self.assertEqual([item.args[0] for item in run.call_args_list], [[CTL, "list"], [CTL, "start", VM_ID]])

    def test_shutdown_requests_guest_shutdown_never_default_force(self):
        results = [subprocess.CompletedProcess([], 0, VM_LIST, ""), subprocess.CompletedProcess([], 0, "", "")]
        with patch("nutcracker.subprocess.run", side_effect=results) as run:
            result = self.companion.control("shutdown", VM_ID)
        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args.args[0], [CTL, "stop", "--request", VM_ID])
        self.assertNotIn("--force", run.call_args.args[0])
        self.assertNotIn("--kill", run.call_args.args[0])

    def test_unknown_vm_is_not_controlled(self):
        with patch("nutcracker.subprocess.run", return_value=subprocess.CompletedProcess([], 0, VM_LIST, "")) as run:
            with self.assertRaises(nutcracker.CompanionError) as caught:
                self.companion.control("start", OTHER_ID)
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(run.call_count, 1)

    def test_invalid_action_or_id_never_runs_a_command(self):
        with patch("nutcracker.subprocess.run") as run:
            for action, identifier in (("delete", VM_ID), ("start", "--kill")):
                with self.assertRaises(nutcracker.CompanionError):
                    self.companion.control(action, identifier)
        run.assert_not_called()

    def test_apple_event_stderr_is_failure_even_when_utm_exits_zero(self):
        with patch("nutcracker.subprocess.run", return_value=subprocess.CompletedProcess(
            [], 0, HEADER, "Error from event: Not authorized to send Apple events."
        )):
            with self.assertRaises(nutcracker.CompanionError) as caught:
                self.companion._run([CTL, "list"])
        self.assertIn("Not authorized", str(caught.exception))

    def test_nonzero_command_and_timeouts_are_errors(self):
        with patch("nutcracker.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "Application not found.")):
            with self.assertRaises(nutcracker.CompanionError):
                self.companion._run([CTL, "list"])
        with patch("nutcracker.subprocess.run", side_effect=subprocess.TimeoutExpired([CTL, "list"], 15)):
            with self.assertRaises(nutcracker.CompanionError) as caught:
                self.companion._run([CTL, "list"])
        self.assertEqual(caught.exception.status, 504)

    def test_open_utm_uses_only_known_application_path(self):
        with patch("nutcracker.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertTrue(self.companion.open_utm()["ok"])
        self.assertEqual(run.call_args.args[0], ["/usr/bin/open", str(APP)])

    def test_only_fixed_installation_locations_are_searched(self):
        companion = nutcracker.UTMCompanion(Path("/tmp/nutcracker-test"))
        target = companion.root / ".runtime" / "UTM.app"
        with patch.object(Path, "is_file", autospec=True, side_effect=lambda path: path == target / "Contents/MacOS/utmctl"), patch(
            "nutcracker.os.access", return_value=True
        ):
            self.assertEqual(companion.application(), target)

    def test_non_macos_does_not_run_utm(self):
        with patch("nutcracker.platform.system", return_value="Linux"), patch("nutcracker.subprocess.run") as run:
            with self.assertRaises(nutcracker.CompanionError) as caught:
                self.companion.control("start", VM_ID)
        self.assertEqual(caught.exception.status, 503)
        run.assert_not_called()


class HostTests(unittest.TestCase):
    def test_host_memory_is_read_from_macos_not_hardcoded(self):
        with patch("nutcracker.platform.system", return_value="Darwin"), patch(
            "nutcracker.platform.mac_ver", return_value=("26.0", (), "")
        ), patch("nutcracker.platform.machine", return_value="arm64"), patch(
            "nutcracker.subprocess.run", return_value=subprocess.CompletedProcess([], 0, str(24 * 1024 ** 3), "")
        ) as run, patch("nutcracker.shutil.disk_usage", return_value=SimpleNamespace(free=100 * 1024 ** 3)):
            self.assertEqual(nutcracker.host_details(), {**HOST, "os": "macOS 26.0"})
        self.assertEqual(run.call_args.args[0], ["/usr/sbin/sysctl", "-n", "hw.memsize"])

    def test_system_profiler_fallback_returns_memory_without_serial_numbers(self):
        report = {"SPHardwareDataType": [{"physical_memory": "24 GB", "serial_number": "private"}]}
        with patch("nutcracker.platform.system", return_value="Darwin"), patch(
            "nutcracker.subprocess.run", side_effect=[
                OSError("sandbox denies sysctl"),
                subprocess.CompletedProcess([], 0, json.dumps(report), ""),
            ]
        ) as run:
            status = nutcracker.host_details()
        self.assertEqual(status["memory_gb"], 24.0)
        self.assertNotIn("private", json.dumps(status))
        self.assertEqual(run.call_args.args[0], ["/usr/sbin/system_profiler", "-json", "SPHardwareDataType"])

    def test_unavailable_measurements_are_null(self):
        with patch("nutcracker.platform.system", return_value="Darwin"), patch(
            "nutcracker.subprocess.run", side_effect=OSError("missing")
        ), patch("nutcracker.shutil.disk_usage", side_effect=OSError("unavailable")):
            status = nutcracker.host_details()
        self.assertIsNone(status["memory_gb"])
        self.assertIsNone(status["disk_free_gb"])


class FakeCompanion:
    def __init__(self):
        self.status_reads = 0
        self.controls = []
        self.open_requests = 0

    def status(self):
        self.status_reads += 1
        return {"host": HOST, "utm_installed": True, "vms": [{
            "id": VM_ID, "name": "Windows", "status": "started",
        }], "error": None}

    def control(self, action, identifier):
        self.controls.append((action, identifier))
        return {"ok": True, "message": "Request sent", "vm_id": identifier}

    def open_utm(self):
        self.open_requests += 1
        return {"ok": True, "message": "UTM opened"}


class FakeConsole:
    port = 8767

    def start(self):
        pass

    def close(self):
        pass

    def info(self, hostname):
        return {"available": False, "url": None, "error": "Test display unavailable", "transport": "vnc"}


class LiveHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        (cls.root / "index.html").write_text('<meta name="nutcracker-token" content="__NUTCRACKER_TOKEN__">', encoding="utf-8")
        (cls.root / "styles.css").write_text("body { color: white; }", encoding="utf-8")
        (cls.root / "app.js").write_text("'use strict';", encoding="utf-8")
        (cls.root / "steam-setup.ps1").write_text("Write-Host 'Setup'", encoding="utf-8")
        (cls.root / "console.html").write_text('<meta name="nutcracker-token" content="__NUTCRACKER_TOKEN__">', encoding="utf-8")
        (cls.root / "console.js").write_text("'use strict';", encoding="utf-8")
        (cls.root / "vendor/novnc/core").mkdir(parents=True)
        (cls.root / "vendor/novnc/core/rfb.js").write_text("export default class RFB {}", encoding="utf-8")
        (cls.root / "vendor/novnc/LICENSE.txt").write_text("Public vendor license", encoding="utf-8")
        (cls.root / "secret.txt").write_text("private-fixture-content-321", encoding="utf-8")
        cls.fake = FakeCompanion()
        cls.server = nutcracker.LocalServer(port=0, root=cls.root, companion=cls.fake, console=FakeConsole())
        cls.host = "127.0.0.1:" + str(cls.server.server_port)
        cls.origin = "http://" + cls.host
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def setUp(self):
        self.fake.status_reads = 0
        self.fake.controls.clear()
        self.fake.open_requests = 0

    def request(self, method="GET", path="/api/status", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def action_headers(self, **overrides):
        headers = {
            "Origin": self.origin,
            "X-Nutcracker-Token": self.server.token,
            "Content-Type": "application/json",
            "Sec-Fetch-Site": "same-origin",
        }
        headers.update(overrides)
        return headers

    def test_server_binds_only_loopback(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_same_origin_status_has_no_cors_and_no_cache(self):
        status, headers, body = self.request(headers={"Origin": self.origin, "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["vms"][0]["id"], VM_ID)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")

    def test_index_injects_session_token_only_in_fixed_asset(self):
        status, headers, body = self.request(path="/")
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode("ascii"), body)
        self.assertNotIn(b"__NUTCRACKER_TOKEN__", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.fake.status_reads, 0)

    def test_external_host_and_origin_cannot_read_vm_details(self):
        for headers in ({"Host": "evil.example:" + str(self.server.server_port)}, {"Origin": "https://evil.example"},
                        {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            with self.subTest(headers=headers):
                status, _, body = self.request(headers=headers)
                self.assertEqual(status, 403)
                self.assertNotIn(VM_ID.encode("ascii"), body)
        self.assertEqual(self.fake.status_reads, 0)

    def test_localhost_alias_is_allowed_with_matching_origin(self):
        host = "localhost:" + str(self.server.server_port)
        status, _, _ = self.request(headers={"Host": host, "Origin": "http://" + host})
        self.assertEqual(status, 200)

    def test_post_requires_origin_and_current_token(self):
        body = json.dumps({"id": VM_ID})
        for headers in ({"Content-Type": "application/json"}, {"Origin": self.origin, "Content-Type": "application/json"},
                        self.action_headers(**{"Origin": "https://evil.example"}),
                        self.action_headers(**{"X-Nutcracker-Token": "wrong"}),
                        self.action_headers(**{"X-Nutcracker-Token": "caf\u00e9"}),
                        self.action_headers(**{"Sec-Fetch-Site": "cross-site"})):
            with self.subTest(headers=headers):
                status, _, _ = self.request("POST", "/api/vm/start", body, headers)
                self.assertEqual(status, 403)
        self.assertEqual(self.fake.controls, [])

    def test_valid_post_reaches_companion_with_uuid_only(self):
        for action in ("start", "shutdown"):
            status, _, body = self.request("POST", "/api/vm/" + action, json.dumps({"id": VM_ID.upper()}), self.action_headers())
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(self.fake.controls, [("start", VM_ID), ("shutdown", VM_ID)])

    def test_invalid_json_schema_and_ids_are_rejected_before_control(self):
        for body in ('{"id": "--force"}', json.dumps({"id": VM_ID, "command": "delete"}), '[]', '{', '{}'):
            with self.subTest(body=body):
                status, _, _ = self.request("POST", "/api/vm/start", body, self.action_headers())
                self.assertEqual(status, 400)
        self.assertEqual(self.fake.controls, [])

    def test_form_and_oversized_bodies_are_rejected(self):
        status, _, _ = self.request("POST", "/api/vm/start", "id=" + VM_ID, self.action_headers(**{"Content-Type": "text/plain"}))
        self.assertEqual(status, 415)
        status, _, _ = self.request("POST", "/api/vm/start", "x" * 4097, self.action_headers())
        self.assertEqual(status, 413)
        self.assertEqual(self.fake.controls, [])

    def test_arbitrary_files_traversal_and_absolute_request_targets_are_blocked(self):
        for path in ("/secret.txt", "/nutcracker.py", "/../secret.txt", "/%2e%2e/secret.txt", "/.git/config", "/.runtime/UTM.app"):
            with self.subTest(path=path):
                status, _, body = self.request(path=path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"private-fixture-content-321", body)
        status, _, _ = self.request(path="http://evil.example/api/status", headers={"Host": self.host})
        self.assertEqual(status, 400)

    def test_whitelisted_asset_symlink_cannot_expose_another_file(self):
        script = self.root / "app.js"
        original = script.read_bytes()
        script.unlink()
        script.symlink_to(self.root / "secret.txt")
        try:
            status, _, body = self.request(path="/app.js")
            self.assertEqual(status, 404)
            self.assertNotIn(b"private-fixture-content-321", body)
        finally:
            script.unlink()
            script.write_bytes(original)

    def test_open_utm_rejects_arbitrary_options(self):
        status, _, _ = self.request("POST", "/api/open-utm", '{"path": "/tmp/other.app"}', self.action_headers())
        self.assertEqual(status, 400)
        self.assertEqual(self.fake.open_requests, 0)
        status, _, _ = self.request("POST", "/api/open-utm", '{}', self.action_headers())
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.open_requests, 1)

    def test_guest_setup_script_is_downloadable(self):
        status, headers, body = self.request(path="/steam-setup.ps1")
        self.assertEqual(status, 200)
        self.assertIn('filename="steam-setup.ps1"', headers["Content-Disposition"])
        self.assertIn(b"Write-Host", body)

    def test_console_page_injects_token_and_allows_only_its_fixed_websocket_port(self):
        status, headers, body = self.request(path="/console.html")
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode("ascii"), body)
        self.assertIn("ws://localhost:8767", headers["Content-Security-Policy"])
        self.assertIn("ws://127.0.0.1:8767", headers["Content-Security-Policy"])
        self.assertNotIn("ws://*", headers["Content-Security-Policy"])

    def test_console_api_does_not_infer_display_availability_from_vm_status(self):
        status, _, body = self.request(path="/api/console")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {
            "available": False, "url": None, "error": "Test display unavailable", "transport": "vnc",
        })
        status, _, _ = self.request(path="/api/console", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_vendor_modules_and_licenses_are_served_but_escape_paths_are_not(self):
        for path in ("/vendor/novnc/core/rfb.js", "/vendor/novnc/LICENSE.txt"):
            with self.subTest(path=path):
                status, _, _ = self.request(path=path)
                self.assertEqual(status, 200)
        for path in ("/vendor/novnc/../../secret.txt", "/vendor/novnc/core/%2e%2e/secret.txt", "/vendor/novnc//core/rfb.js", "/vendor/novnc/core/rfb.py"):
            with self.subTest(path=path):
                status, _, _ = self.request(path=path)
                self.assertEqual(status, 404)

    def test_vendor_directory_symlink_cannot_expose_other_files(self):
        target = self.root / "vendor/novnc/core"
        held = self.root / "vendor/novnc/held"
        target.rename(held)
        target.symlink_to(self.root, target_is_directory=True)
        try:
            status, _, body = self.request(path="/vendor/novnc/core/secret.txt")
            self.assertEqual(status, 404)
            self.assertNotIn(b"private-fixture-content-321", body)
        finally:
            target.unlink()
            held.rename(target)

    def test_unsupported_methods_do_not_trigger_actions(self):
        status, _, _ = self.request("OPTIONS", "/api/vm/start")
        self.assertEqual(status, 405)
        self.assertEqual(self.fake.controls, [])

    def test_duplicate_host_is_rejected(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.putrequest("GET", "/api/status", skip_host=True)
            connection.putheader("Host", self.host)
            connection.putheader("Host", self.host)
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
        finally:
            connection.close()
        self.assertEqual(self.fake.status_reads, 0)


class MockRFBServer:
    """A tiny upstream RFB handshake fixture, never a simulated user desktop."""

    def __init__(self, path, greeting=b"RFB 003.008\n"):
        self.path = path
        self.greeting = greeting
        self.stopped = threading.Event()
        self.connections = 0
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(path))
        path.chmod(0o600)
        self.socket.listen(8)
        self.socket.settimeout(0.1)
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    @staticmethod
    def _read(connection, length):
        data = bytearray()
        while len(data) < length:
            chunk = connection.recv(length - len(data))
            if not chunk:
                return None
            data.extend(chunk)
        return bytes(data)

    def _handle(self, connection):
        with connection:
            try:
                connection.settimeout(2)
                connection.sendall(self.greeting)
                if self._read(connection, 12) != b"RFB 003.008\n":
                    return  # Availability probes close after the version greeting.
                connection.sendall(b"\x01\x01")  # One security type: None.
                if self._read(connection, 1) != b"\x01":
                    return
                connection.sendall(b"\x00\x00\x00\x00")
                if self._read(connection, 1) is None:
                    return
                name = b"RFB protocol test fixture"
                pixel_format = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
                connection.sendall(struct.pack(">HH", 1, 1) + pixel_format + struct.pack(">I", len(name)) + name)
                while not self.stopped.is_set():
                    message = connection.recv(65536)
                    if not message:
                        return
                    connection.sendall(message)
            except OSError:
                return

    def close(self):
        self.stopped.set()
        self.socket.close()
        self.thread.join(timeout=1)


class RFBClientBuffer:
    def __init__(self, websocket):
        self.websocket = websocket
        self.buffer = bytearray()

    def read(self, length):
        while len(self.buffer) < length:
            self.buffer.extend(self.websocket.recv(timeout=2))
        value = bytes(self.buffer[:length])
        del self.buffer[:length]
        return value


class LiveConsoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            console_bridge.load_websockets()
        except ImportError:
            raise unittest.SkipTest("Install requirements.txt to run live browser-console tests.")
        from websockets.sync.client import connect
        from websockets.exceptions import ConnectionClosed
        cls.connect = staticmethod(connect)
        cls.connection_closed = ConnectionClosed
        # Keep this Unix socket path below macOS's ~104-byte length limit.
        temporary_base = "/private/tmp" if Path("/private/tmp").is_dir() else tempfile.gettempdir()
        cls.temp = tempfile.TemporaryDirectory(prefix="nutcracker-test-", dir=temporary_base)
        cls.root = Path(cls.temp.name)
        cls.root.chmod(0o700)
        cls.target = cls.root / "rfb.sock"
        cls.rfb = MockRFBServer(cls.target)
        cls.server = nutcracker.LocalServer(
            port=0, root=cls.root, companion=FakeCompanion(), console_port=0, console_socket=cls.target,
        )
        cls.bridge = cls.server.console
        if not cls.bridge.running:
            raise RuntimeError(cls.bridge.error)
        cls.origin = "http://127.0.0.1:" + str(cls.server.server_port)
        cls.ws_host = "127.0.0.1:" + str(cls.bridge.port)
        cls.url = "ws://" + cls.ws_host + "/websockify?token=" + cls.server.token
        cls.http_thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        cls.http_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.http_thread.join(timeout=2)
        cls.rfb.close()
        cls.temp.cleanup()

    def handshake(self, path=None, **overrides):
        headers = {
            "Origin": self.origin,
            "Host": self.ws_host,
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
        }
        headers.update(overrides)
        connection = http.client.HTTPConnection("127.0.0.1", self.bridge.port, timeout=3)
        try:
            connection.request("GET", path or "/websockify?token=" + self.server.token, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_browser_bridge_listens_only_on_loopback(self):
        self.assertTrue(self.bridge.running)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(self.bridge._bind_address, "127.0.0.1")
        self.assertEqual(self.bridge.origins, [
            "http://localhost:" + str(self.server.server_port), self.origin,
        ])

    def test_complete_binary_rfb_handshake_and_keyboard_pointer_forwarding(self):
        with self.connect(self.url, origin=self.origin, proxy=None, subprotocols=["binary"], open_timeout=2) as websocket:
            self.assertEqual(websocket.subprotocol, "binary")
            client = RFBClientBuffer(websocket)
            self.assertEqual(client.read(12), b"RFB 003.008\n")
            websocket.send(b"RFB 003.008\n")
            self.assertEqual(client.read(2), b"\x01\x01")
            websocket.send(b"\x01")
            self.assertEqual(client.read(4), b"\x00\x00\x00\x00")
            websocket.send(b"\x01")
            server_init = client.read(24)
            self.assertEqual(struct.unpack(">HH", server_init[:4]), (1, 1))
            name_length = struct.unpack(">I", server_init[20:24])[0]
            self.assertEqual(client.read(name_length), b"RFB protocol test fixture")
            keyboard_and_pointer = struct.pack(">BBHI", 4, 1, 0, 65) + struct.pack(">BBHH", 5, 1, 18, 27)
            websocket.send(keyboard_and_pointer)
            self.assertEqual(client.read(len(keyboard_and_pointer)), keyboard_and_pointer)

    def test_remote_missing_and_wrong_port_origins_are_rejected(self):
        for origin in ("https://evil.example", "null", "", "http://localhost:1"):
            with self.subTest(origin=origin):
                status, body = self.handshake(Origin=origin)
                self.assertEqual(status, 403)
                self.assertNotIn(self.server.token.encode(), body)

    def test_wrong_host_and_cross_site_metadata_are_rejected(self):
        for headers in ({"Host": "evil.example:" + str(self.bridge.port)}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                status, _ = self.handshake(**headers)
                self.assertEqual(status, 403)

    def test_wrong_missing_duplicate_and_unicode_tokens_are_rejected(self):
        for path in ("/websockify", "/websockify?token=wrong", "/websockify?token=caf%C3%A9",
                     "/websockify?token=" + self.server.token + "&token=" + self.server.token,
                     "/websockify?token=" + self.server.token + "&target=evil.example"):
            with self.subTest(path=path):
                status, body = self.handshake(path)
                self.assertEqual(status, 403)
                self.assertNotIn(self.server.token.encode(), body)

    def test_ws_route_does_not_accept_arbitrary_files_or_upstream_targets(self):
        for path in ("/private/file?token=" + self.server.token, "http://evil.example/websockify?token=" + self.server.token):
            with self.subTest(path=path):
                status, _ = self.handshake(path)
                self.assertEqual(status, 404)

    def test_text_frames_are_rejected(self):
        with self.connect(self.url, origin=self.origin, proxy=None, open_timeout=2) as websocket:
            self.assertEqual(websocket.recv(timeout=2), b"RFB 003.008\n")
            websocket.send("text is not VNC")
            with self.assertRaises(self.connection_closed) as caught:
                websocket.recv(timeout=2)
            self.assertEqual(caught.exception.rcvd.code, 1003)

    def test_console_api_requires_actual_rfb_greeting_and_signs_local_url(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("GET", "/api/console")
            response = connection.getresponse()
            value = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(value, {"available": True, "url": self.url, "error": None, "transport": "vnc"})
        finally:
            connection.close()

    def test_missing_stale_or_non_socket_target_is_unavailable(self):
        for target in (self.root / "missing.sock", self.root / "file.sock", self.root / "stale.sock"):
            with self.subTest(target=target):
                if target.name == "file.sock":
                    target.write_text("not a socket")
                if target.name == "stale.sock":
                    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    stale.bind(str(target))
                    stale.close()
                bridge = console_bridge.ConsoleBridge(self.server.server_port, "test", unix_socket=target)
                bridge.running = True
                value = bridge.info("127.0.0.1")
                self.assertFalse(value["available"])
                self.assertIsNone(value["url"])
                self.assertIsNotNone(value["error"])

    def test_socket_and_parent_permissions_are_private(self):
        self.target.chmod(0o777)
        self.assertTrue(self.bridge.info("127.0.0.1")["available"])
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.root.chmod(0o755)
        try:
            self.assertFalse(self.bridge.info("127.0.0.1")["available"])
        finally:
            self.root.chmod(0o700)

    def test_live_non_rfb_server_is_unavailable(self):
        target = self.root / "invalid.sock"
        upstream = MockRFBServer(target, greeting=b"HTTP/1.1 200")
        try:
            bridge = console_bridge.ConsoleBridge(self.server.server_port, "test", unix_socket=target)
            bridge.running = True
            value = bridge.info()
            self.assertFalse(value["available"])
            self.assertIsNone(value["url"])
            self.assertIn("VNC greeting", value["error"])
        finally:
            upstream.close()

    def test_socket_symlink_and_wrong_ownership_are_rejected(self):
        link = self.root / "link.sock"
        link.symlink_to(self.target)
        bridge = console_bridge.ConsoleBridge(self.server.server_port, "test", unix_socket=link)
        bridge.running = True
        self.assertFalse(bridge.info()["available"])
        with patch("console_bridge.os.getuid", return_value=os.getuid() + 1):
            self.assertFalse(self.bridge.info()["available"])

    def test_tokens_cannot_be_written_by_library_logging(self):
        self.assertFalse(self.bridge._logger.isEnabledFor(50))
        self.assertFalse(self.bridge._logger.propagate)


if __name__ == "__main__":
    unittest.main()
