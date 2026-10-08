"""Safety and real-state contracts for the loopback UTM companion.

Run: python3 -m unittest -v test_nutcracker.py
These tests never start, stop, install, or open a real VM or application.
"""

import http.client
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import nutcracker


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


class LiveHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        (cls.root / "index.html").write_text('<meta name="nutcracker-token" content="__NUTCRACKER_TOKEN__">', encoding="utf-8")
        (cls.root / "styles.css").write_text("body { color: white; }", encoding="utf-8")
        (cls.root / "app.js").write_text("'use strict';", encoding="utf-8")
        (cls.root / "steam-setup.ps1").write_text("Write-Host 'Setup'", encoding="utf-8")
        (cls.root / "secret.txt").write_text("not served", encoding="utf-8")
        cls.fake = FakeCompanion()
        cls.server = nutcracker.LocalServer(port=0, root=cls.root, companion=cls.fake)
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
                self.assertNotIn(b"not served", body)
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
            self.assertNotIn(b"not served", body)
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


if __name__ == "__main__":
    unittest.main()
