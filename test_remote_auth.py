"""HTTPS-origin authentication checks with dummy credentials and no real VM."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

import console_bridge
import nutcracker
from test_auth import USERNAME, PASSWORD
from test_nutcracker import FakeCompanion, FakeConsole


class RemoteAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.auth = nutcracker.LocalAuth(cls.root, load=False)
        cls.auth.configure(USERNAME, PASSWORD)
        for name in ('login.html', 'console.html'):
            (cls.root / name).write_text('__NUTCRACKER_LOGIN_TOKEN__ __NUTCRACKER_TOKEN__')
        cls.origin = 'https://emulator.test.ts.net'
        cls.host = 'emulator.test.ts.net'
        cls.server = nutcracker.LocalServer(0, cls.root, companion=FakeCompanion(), console=FakeConsole(), auth=cls.auth, remote_origin=cls.origin)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)
        cls.temp.cleanup()

    def request(self, method='GET', path='/api/auth', headers=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        options = {'Host': self.host, **(headers or {})}
        if body is not None:
            body = json.dumps(body)
            options['Content-Type'] = 'application/json'
        connection.request(method, path, body, options)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_remote_sign_in_secure_cookie_and_revocation(self):
        status, headers, body = self.request(path='/console.html')
        self.assertEqual(status, 303)
        self.assertNotIn(self.server.token.encode(), body)
        status, headers, _ = self.request('POST', '/api/login', {
            'Origin': self.origin, 'X-Nutcracker-Token': self.server.login_token,
        }, {'username': USERNAME, 'password': PASSWORD})
        self.assertEqual(status, 200)
        self.assertIn('; Secure', headers['Set-Cookie'])
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        status, headers, _ = self.request(path='/console.html', headers={'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertIn('wss://' + self.host, headers['Content-Security-Policy'])
        self.assertNotIn('ws://localhost', headers['Content-Security-Policy'])
        status, headers, _ = self.request('POST', '/api/logout', {
            'Origin': self.origin, 'Cookie': cookie, 'X-Nutcracker-Token': self.server.token,
        }, {})
        self.assertEqual(status, 200)
        self.assertIn('; Secure', headers['Set-Cookie'])
        self.assertEqual(self.request(path='/api/status', headers={'Cookie': cookie})[0], 401)

    def test_cross_origin_requests_and_unconfigured_hosts_are_blocked(self):
        for headers in ({'Origin': 'https://other.test'}, {'Host': 'other.test'},
                        {'Origin': 'http://127.0.0.1:' + str(self.server.server_port)},
                        {'Host': '127.0.0.1:' + str(self.server.server_port), 'Origin': self.origin}):
            self.assertEqual(self.request(headers=headers)[0], 403)
        self.assertEqual(self.request('POST', '/api/login', {'Origin': self.origin}, {'username': USERNAME, 'password': PASSWORD})[0], 403)

    def test_public_launch_navigation_only_opens_sign_in(self):
        status, headers, _ = self.request(path='/console.html', headers={
            'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Dest': 'document', 'Sec-Fetch-User': '?1',
        })
        self.assertEqual(status, 303)
        self.assertTrue(headers['Location'].startswith('/login.html?next='))
        self.assertEqual(self.request(path='/api/auth', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_console_address_is_exact_same_https_origin(self):
        bridge = console_bridge.ConsoleBridge(8765, 'fake-token', remote_origin=self.origin)
        bridge.running = True
        bridge.probe = lambda: None
        self.assertEqual(bridge.info(self.host)['url'], 'wss://' + self.host + '/websockify?token=fake-token')
        self.assertFalse(bridge.info('other.test')['available'])


if __name__ == '__main__':
    unittest.main()
