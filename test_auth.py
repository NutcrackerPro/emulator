"""Local authentication contracts, using dummy credentials and temporary files.

These tests never launch a VM, operate UTM, or read the real local account.
Run: python3 -B -m unittest -v test_auth.py
"""

import http.client
import io
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import nutcracker
from test_nutcracker import FakeCompanion, FakeConsole, VM_ID


USERNAME = "TestAccount"
PASSWORD = "Dummy-password-for-tests-only!"


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class AuthStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Exercise the actual KDF once for the shared, explicitly fake fixture.
        with tempfile.TemporaryDirectory() as temporary:
            auth = nutcracker.LocalAuth(temporary, load=False)
            auth.configure(USERNAME, PASSWORD)
            cls.fixture_record = dict(auth.record)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.directory = self.root / ".runtime"
        self.directory.mkdir(mode=0o700)
        self.file = self.directory / "auth.json"
        self.write_record(self.fixture_record)
        self.auth = nutcracker.LocalAuth(self.root, clock=self.clock)

    def write_record(self, record):
        self.file.write_text(json.dumps(record), encoding="utf-8")
        self.file.chmod(0o600)

    def assert_error(self, status, callback):
        with self.assertRaises(nutcracker.CompanionError) as caught:
            callback()
        self.assertEqual(caught.exception.status, status)
        return str(caught.exception)

    def test_missing_record_fails_closed(self):
        self.file.unlink()
        missing = nutcracker.LocalAuth(self.root)
        self.assertFalse(missing.configured)
        self.assert_error(503, lambda: missing.login(USERNAME, PASSWORD))
        self.assertFalse(missing.is_valid(secrets.token_urlsafe(32)))

    def test_configure_stores_only_salted_hash_and_private_permissions(self):
        previous_salt = self.auth.record["salt"]
        previous_session = self.auth.login(USERNAME, PASSWORD)
        self.auth.configure(USERNAME, PASSWORD)
        raw = self.file.read_text(encoding="utf-8")
        record = json.loads(raw)
        self.assertNotIn(PASSWORD, raw)
        self.assertEqual(set(record), {"version", "username", "algorithm", "iterations", "salt", "hash"})
        self.assertEqual(record["iterations"], 600000)
        self.assertEqual(len(bytes.fromhex(record["salt"])), 32)
        self.assertEqual(len(bytes.fromhex(record["hash"])), 32)
        self.assertNotEqual(previous_salt, record["salt"])
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertFalse(self.auth.is_valid(previous_session))
        reloaded = nutcracker.LocalAuth(self.root)
        self.assertTrue(reloaded.is_valid(reloaded.login(USERNAME, PASSWORD)))

    def test_corrupt_or_unbounded_parameters_are_rejected_before_hashing(self):
        records = [None, [], 1, "unexpected"]
        for field, value in (("iterations", 1), ("iterations", 10**20),
                             ("iterations", True), ("version", True),
                             ("salt", "00"), ("hash", "0" * 63),
                             ("algorithm", "sha256"), ("username", "bad name")):
            record = dict(self.fixture_record)
            record[field] = value
            records.append(record)
        records.append({**self.fixture_record, "password": PASSWORD})
        with patch("nutcracker.hashlib.pbkdf2_hmac") as derive:
            for record in records:
                with self.subTest(record_type=type(record).__name__):
                    self.write_record(record)
                    self.assert_error(503, lambda: nutcracker.LocalAuth(self.root))
            derive.assert_not_called()

    def test_oversized_record_and_loose_permissions_are_rejected(self):
        self.file.write_bytes(b"x" * (nutcracker.MAX_BODY_BYTES + 1))
        self.assert_error(503, lambda: nutcracker.LocalAuth(self.root))
        self.write_record(self.fixture_record)
        self.file.chmod(0o644)
        self.assert_error(503, lambda: nutcracker.LocalAuth(self.root))
        self.file.chmod(0o600)
        self.directory.chmod(0o755)
        self.assert_error(503, lambda: nutcracker.LocalAuth(self.root))

    def test_auth_file_and_runtime_directory_symlinks_are_rejected(self):
        target = self.root / "other.json"
        target.write_text(json.dumps(self.fixture_record), encoding="utf-8")
        target.chmod(0o600)
        self.file.unlink()
        self.file.symlink_to(target)
        self.assert_error(503, lambda: nutcracker.LocalAuth(self.root))
        self.assert_error(503, lambda: self.auth.configure(USERNAME, PASSWORD))
        self.file.unlink()
        self.directory.rmdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        self.directory.symlink_to(elsewhere, target_is_directory=True)
        self.assert_error(503, lambda: self.auth.configure(USERNAME, PASSWORD))
        self.assertFalse((elsewhere / "auth.json").exists())

    def test_wrong_username_and_password_have_same_error_and_both_derive(self):
        original = nutcracker.hashlib.pbkdf2_hmac
        with patch("nutcracker.hashlib.pbkdf2_hmac", wraps=original) as derive:
            first = self.assert_error(401, lambda: self.auth.login("UnknownAccount", PASSWORD))
            second = self.assert_error(401, lambda: self.auth.login(USERNAME, "wrong-password"))
        self.assertEqual(first, second)
        self.assertEqual(derive.call_count, 2)
        self.assertNotIn(PASSWORD, first)

    def test_utf8_limit_and_malformed_credentials_do_not_reach_hashing(self):
        with patch("nutcracker.hashlib.pbkdf2_hmac") as derive:
            for username, password in ((USERNAME, "x" * 1025), (USERNAME, "\U0001f600" * 257),
                                       (USERNAME, "\ud800"), ("\ud800", PASSWORD),
                                       (None, PASSWORD), (USERNAME, None)):
                self.assert_error(400, lambda: self.auth.login(username, password))
            derive.assert_not_called()
        self.assertEqual(len(self.auth._password_bytes("\U0001f600" * 256)), 1024)

    def test_cookie_requires_one_well_formed_live_session(self):
        session = self.auth.login(USERNAME, PASSWORD)
        valid = nutcracker.SESSION_COOKIE + "=" + session
        self.assertEqual(self.auth.session_from_cookie("other=value; " + valid), session)
        for cookie in (None, "", valid + "; " + valid, nutcracker.SESSION_COOKIE + "=bad",
                       nutcracker.SESSION_COOKIE + "=" + secrets.token_urlsafe(32),
                       "x" * (nutcracker.MAX_BODY_BYTES + 1)):
            with self.subTest(cookie_type=type(cookie).__name__):
                self.assertIsNone(self.auth.session_from_cookie(cookie))
        self.auth.invalidate(session)
        self.assertIsNone(self.auth.session_from_cookie(valid))

    def test_session_deadline_is_absolute_and_old_cookie_dies_on_restart(self):
        session = self.auth.login(USERNAME, PASSWORD)
        cookie = nutcracker.SESSION_COOKIE + "=" + session
        self.clock.advance(nutcracker.SESSION_SECONDS - 0.01)
        self.assertEqual(self.auth.session_from_cookie(cookie), session)
        self.clock.advance(0.01)
        self.assertFalse(self.auth.is_valid(session))
        self.assertIsNone(self.auth.session_from_cookie(cookie))
        fresh_process = nutcracker.LocalAuth(self.root)
        self.assertIsNone(fresh_process.session_from_cookie(cookie))

    def test_session_limit_evicts_oldest_and_notifies_live_connections(self):
        revoked = []
        self.auth.add_revocation_listener(revoked.append)
        sessions = [self.auth.login(USERNAME, PASSWORD) for _ in range(9)]
        self.assertEqual(len(set(sessions)), 9)
        self.assertFalse(self.auth.is_valid(sessions[0]))
        self.assertEqual(revoked, sessions[:1])
        self.assertTrue(all(self.auth.is_valid(session) for session in sessions[1:]))
        self.auth.invalidate_all()
        self.assertEqual(set(revoked), set(sessions))
        self.assertTrue(all(not self.auth.is_valid(session) for session in sessions))

    def test_attempt_limit_is_global_and_recovers_at_window_boundary(self):
        for index in range(5):
            self.assert_error(401, lambda: self.auth.login("Different" + str(index), PASSWORD))
        with patch("nutcracker.hashlib.pbkdf2_hmac") as derive:
            self.assert_error(429, lambda: self.auth.login(USERNAME, PASSWORD))
            self.clock.advance(59.99)
            self.assert_error(429, lambda: self.auth.login(USERNAME, PASSWORD))
            derive.assert_not_called()
        self.clock.advance(0.01)
        self.assertTrue(self.auth.is_valid(self.auth.login(USERNAME, PASSWORD)))

    def test_only_one_password_hash_runs_concurrently(self):
        entered = threading.Event()
        release = threading.Event()
        failures = []
        actual_digest = bytes.fromhex(self.fixture_record["hash"])

        def held_hash(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("Concurrent verification test timed out")
            return actual_digest

        def worker():
            try:
                self.auth.login(USERNAME, PASSWORD)
            except Exception as error:
                failures.append(error)

        with patch("nutcracker.hashlib.pbkdf2_hmac", side_effect=held_hash) as derive:
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assert_error(429, lambda: self.auth.login(USERNAME, PASSWORD))
                self.assertEqual(derive.call_count, 1)
            finally:
                release.set()
                thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])

    def test_logout_cannot_interleave_with_an_authorized_input_write(self):
        session = self.auth.login(USERNAME, PASSWORD)
        writing = threading.Event()
        release = threading.Event()
        invalidating = threading.Event()
        invalidated = threading.Event()
        forwarded = []

        def write():
            writing.set()
            release.wait(2)
            forwarded.append("authorized input")

        def logout():
            invalidating.set()
            self.auth.invalidate(session)
            invalidated.set()

        writer = threading.Thread(target=lambda: self.auth.run_if_valid(session, write))
        writer.start()
        self.assertTrue(writing.wait(2))
        closer = threading.Thread(target=logout)
        closer.start()
        try:
            self.assertTrue(invalidating.wait(2))
            self.assertFalse(invalidated.wait(0.03))
        finally:
            release.set()
            writer.join(timeout=2)
            closer.join(timeout=2)
        self.assertTrue(invalidated.is_set())
        self.assertFalse(self.auth.run_if_valid(session, lambda: forwarded.append("unauthorized input")))
        self.assertEqual(forwarded, ["authorized input"])


class PasswordSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def invoke(self, arguments, incoming=None, prompts=None):
        output, errors = io.StringIO(), io.StringIO()
        incoming = io.TextIOWrapper(io.BytesIO(incoming or b""), encoding="utf-8")
        with patch.object(nutcracker, "ROOT", self.root), patch("nutcracker.sys.stdin", incoming), \
             patch("nutcracker.getpass.getpass", side_effect=prompts), \
             patch("nutcracker.UTMCompanion") as companion, \
             redirect_stdout(output), redirect_stderr(errors):
            result = nutcracker.main(arguments)
        companion.assert_not_called()
        self.assertNotIn(PASSWORD, output.getvalue() + errors.getvalue())
        return result, output.getvalue(), errors.getvalue()

    def test_stdin_provisioning_hashes_without_echoing_or_launching_vm(self):
        result, output, errors = self.invoke(
            ["--set-password", "--username", USERNAME, "--password-stdin"],
            incoming=(PASSWORD + "\n").encode("utf-8"),
        )
        self.assertEqual(result, 0)
        self.assertEqual(errors, "")
        self.assertIn("configured", output)
        auth = nutcracker.LocalAuth(self.root)
        self.assertTrue(auth.is_valid(auth.login(USERNAME, PASSWORD)))

    def test_confirmation_mismatch_does_not_write_a_record(self):
        result, output, errors = self.invoke(["--set-password"], prompts=[PASSWORD, "different-password"])
        self.assertEqual(result, 1)
        self.assertIn("did not match", errors)
        self.assertFalse((self.root / ".runtime/auth.json").exists())

    def test_oversized_or_invalid_utf8_stdin_is_rejected_without_record(self):
        for incoming in (b"x" * 1027, b"\xff\xff"):
            result, _, _ = self.invoke(["--set-password", "--password-stdin"], incoming=incoming)
            self.assertEqual(result, 1)
            self.assertFalse((self.root / ".runtime/auth.json").exists())


class LiveAuthHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.clock = Clock()
        cls.auth = nutcracker.LocalAuth(cls.root, clock=cls.clock, load=False)
        cls.auth.configure(USERNAME, PASSWORD)
        for name in ("index.html", "console.html"):
            (cls.root / name).write_text('protected __NUTCRACKER_TOKEN__', encoding="utf-8")
        (cls.root / "login.html").write_text('login __NUTCRACKER_LOGIN_TOKEN__ __NUTCRACKER_STYLE_NONCE__', encoding="utf-8")
        for name in ("login.js", "app.js", "console.js", "styles.css", "steam-setup.ps1"):
            (cls.root / name).write_text("test asset", encoding="utf-8")
        (cls.root / "vendor/novnc").mkdir(parents=True)
        (cls.root / "vendor/novnc/rfb.bundle.js").write_text("test vendor", encoding="utf-8")
        cls.fake = FakeCompanion()
        cls.server = nutcracker.LocalServer(port=0, root=cls.root, auth=cls.auth, companion=cls.fake, console=FakeConsole())
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
        self.auth.invalidate_all()
        self.clock.advance(61)
        self.fake.status_reads = 0
        self.fake.controls.clear()
        self.fake.open_requests = 0

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        payload = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
        all_headers = {"Host": self.host}
        if payload is not None:
            all_headers["Content-Type"] = "application/json"
        all_headers.update(headers or {})
        try:
            connection.request(method, path, body=payload, headers=all_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def login(self, username=USERNAME, password=PASSWORD, overrides=None):
        headers = {"Origin": self.origin, "Sec-Fetch-Site": "same-origin",
                   "X-Nutcracker-Token": self.server.login_token}
        headers.update(overrides or {})
        return self.request("POST", "/api/login", {"username": username, "password": password}, headers)

    def cookie(self):
        status, headers, _ = self.login()
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_public_login_does_not_reveal_vm_token_or_details(self):
        status, _, body = self.request("GET", "/login.html")
        self.assertEqual(status, 200)
        self.assertIn(self.server.login_token.encode("ascii"), body)
        self.assertNotIn(self.server.token.encode("ascii"), body)
        self.assertNotIn(PASSWORD.encode("utf-8"), body)
        status, headers, body = self.request("GET", "/api/auth")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"authenticated": False, "configured": True})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.fake.status_reads, 0)

    def test_all_real_get_and_head_routes_require_authentication(self):
        paths = ("/", "/index.html", "/console.html", "/styles.css", "/app.js", "/console.js",
                 "/steam-setup.ps1", "/vendor/novnc/rfb.bundle.js", "/api/status", "/api/console")
        for method in ("GET", "HEAD"):
            for path in paths:
                with self.subTest(method=method, path=path):
                    status, headers, body = self.request(method, path)
                    expected = 303 if path in {"/", "/index.html", "/console.html"} else 401
                    self.assertEqual(status, expected)
                    if status == 303:
                        self.assertTrue(headers["Location"].startswith("/login.html?next=%2F"))
                    self.assertNotIn(self.server.token.encode("ascii"), body)
                    if method == "HEAD":
                        self.assertEqual(body, b"")
        self.assertEqual(self.fake.status_reads, 0)

    def test_login_cookie_and_authenticated_resources(self):
        status, headers, body = self.login()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        cookie_header = headers["Set-Cookie"]
        for attribute in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=28800"):
            self.assertIn(attribute, cookie_header)
        self.assertNotIn("Domain=", cookie_header)
        cookie = cookie_header.split(";", 1)[0]
        for path in ("/console.html", "/styles.css", "/vendor/novnc/rfb.bundle.js", "/api/status", "/api/console"):
            self.assertEqual(self.request("GET", path, headers={"Cookie": cookie})[0], 200)
        status, _, body = self.request("GET", "/api/auth", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"authenticated": True, "configured": True,
                                          "username": USERNAME, "token": self.server.token})

    def test_logout_revokes_server_cookie_and_prevents_reuse(self):
        cookie = self.cookie()
        revoked = []
        self.auth.add_revocation_listener(revoked.append)
        status, headers, body = self.request("POST", "/api/logout", {}, {
            "Cookie": cookie, "Origin": self.origin, "X-Nutcracker-Token": self.server.token,
        })
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(revoked[-1], cookie.split("=", 1)[1])
        self.assertEqual(self.request("GET", "/api/status", headers={"Cookie": cookie})[0], 401)
        self.assertEqual(self.request("GET", "/console.html", headers={"Cookie": cookie})[0], 303)

    def test_local_session_token_alone_cannot_control_a_vm(self):
        status, _, _ = self.request("POST", "/api/vm/start", {"id": VM_ID}, {
            "Origin": self.origin, "X-Nutcracker-Token": self.server.token,
        })
        self.assertEqual(status, 401)
        self.assertEqual(self.fake.controls, [])

    def test_cookie_alone_cannot_mutate_or_logout(self):
        cookie = self.cookie()
        for path, body in (("/api/vm/start", {"id": VM_ID}), ("/api/logout", {})):
            status, _, _ = self.request("POST", path, body, {"Cookie": cookie, "Origin": self.origin})
            self.assertEqual(status, 403)
        self.assertEqual(self.fake.controls, [])
        self.assertEqual(self.request("GET", "/api/status", headers={"Cookie": cookie})[0], 200)

    def test_login_rejects_wrong_origin_fetch_metadata_and_csrf_token(self):
        for overrides in ({"Origin": "https://example.com"}, {"Origin": "http://localhost:9999"},
                          {"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": "cross-site"},
                          {"X-Nutcracker-Token": self.server.token}, {"Host": "attacker.example"}):
            with self.subTest(header_names=sorted(overrides)):
                self.assertEqual(self.login(overrides=overrides)[0], 403)
        status, _, _ = self.request("POST", "/api/login", {"username": USERNAME, "password": PASSWORD}, {
            "X-Nutcracker-Token": self.server.login_token,
        })
        self.assertEqual(status, 403)

    def test_duplicate_or_forged_session_cookie_is_rejected(self):
        cookie = self.cookie()
        for candidate in (cookie + "; " + cookie, nutcracker.SESSION_COOKIE + "=" + secrets.token_urlsafe(32)):
            self.assertEqual(self.request("GET", "/api/status", headers={"Cookie": candidate})[0], 401)
        self.assertEqual(self.fake.status_reads, 0)

    def test_malformed_login_and_body_limits_preserve_safe_errors(self):
        headers = {"Origin": self.origin, "X-Nutcracker-Token": self.server.login_token}
        for body in ({"username": USERNAME}, {"username": USERNAME, "password": PASSWORD, "extra": 1},
                     {"username": USERNAME, "password": "\ud800"},
                     {"username": USERNAME, "password": "x" * 1025}):
            self.assertEqual(self.request("POST", "/api/login", body, headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/login", b"x" * 4097, headers)[0], 413)
        self.assertEqual(self.request("POST", "/api/login", b"not-json", headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/login", b"{}", {
            **headers, "Content-Type": "text/plain",
        })[0], 415)

    def test_login_rate_limit_exposes_retry_after_and_recovers(self):
        for index in range(5):
            self.assertEqual(self.login(username="Wrong" + str(index))[0], 401)
        status, headers, body = self.login()
        self.assertEqual(status, 429)
        self.assertEqual(headers["Retry-After"], "60")
        self.assertNotIn(PASSWORD.encode("utf-8"), body)
        self.clock.advance(60)
        self.assertEqual(self.login()[0], 200)

    def test_expired_cookie_cannot_read_real_http_resources(self):
        cookie = self.cookie()
        self.clock.advance(nutcracker.SESSION_SECONDS)
        self.assertEqual(self.request("GET", "/api/status", headers={"Cookie": cookie})[0], 401)
        self.assertEqual(self.request("GET", "/console.html", headers={"Cookie": cookie})[0], 303)


if __name__ == "__main__":
    unittest.main()
