"""Remote gateway contracts against dummy loopback companions, never a real VM."""

import asyncio
import unittest

import remote_gateway


REMOTE_ORIGIN = "https://test-machine.example.ts.net"
REMOTE_HOST = "test-machine.example.ts.net"
DUMMY_COOKIE = "nutcracker_session=dummy-session-for-tests"
DUMMY_TOKEN = "dummy-token-for-tests"


class OriginTests(unittest.TestCase):
    def test_https_origin_is_canonical(self):
        self.assertEqual(remote_gateway.normalize_remote_origin(REMOTE_ORIGIN + "/"), REMOTE_ORIGIN)
        self.assertEqual(remote_gateway.normalize_remote_origin(REMOTE_ORIGIN + ":443"), REMOTE_ORIGIN)
        self.assertEqual(remote_gateway.normalize_remote_origin(REMOTE_ORIGIN + ":8443"), REMOTE_ORIGIN + ":8443")

    def test_invalid_configuration_is_rejected(self):
        for value in (None, "", "http://example.ts.net", "https://localhost", "https://127.0.0.1",
                      "https://example.ts.net/path", "https://user@example.ts.net", "https://example.ts.net?a=b",
                      "https://example.ts.net#fragment", "https://example.ts.net:99999", "https://example..ts.net",
                      " https://example.ts.net", "https://exam\nple.ts.net"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                remote_gateway.normalize_remote_origin(value)

    def test_invalid_ports_are_rejected(self):
        for values in ((0, 8767, 8768), (8765, -1, 8768), (8765, 8767, 65536), (True, 8767, 8768)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                remote_gateway.RemoteGateway(values[0], values[1], REMOTE_ORIGIN, port=values[2])


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.aiohttp, self.web, self.multidict = remote_gateway.load_aiohttp()
        self.page_requests = []
        self.websocket_requests = []

        async def page(request):
            body = await request.read()
            self.page_requests.append({"method": request.method, "path": request.raw_path,
                                       "headers": dict(request.headers), "body": body})
            if request.path == "/redirect":
                return self.web.Response(status=302, headers={"Location": "/login.html"})
            if request.path == "/asset":
                return self.web.Response(body=b"large-fixed-asset" * 40000, content_type="application/javascript")
            return self.web.Response(body=b"dummy-companion-response", headers={
                "Set-Cookie": "nutcracker_session=dummy; HttpOnly; Secure; SameSite=Strict",
                "Content-Security-Policy": "default-src 'self'", "Cache-Control": "no-store",
            })

        async def websocket(request):
            self.websocket_requests.append({"path": request.raw_path, "headers": dict(request.headers)})
            if (request.headers.get("Host") != REMOTE_HOST or request.headers.get("Origin") != REMOTE_ORIGIN
                    or request.headers.get("Cookie") != DUMMY_COOKIE or request.query.get("token") != DUMMY_TOKEN):
                return self.web.Response(status=401, text="Dummy sign-in required.")
            ws = self.web.WebSocketResponse(protocols=("binary",), compress=False)
            await ws.prepare(request)
            await ws.send_bytes(b"RFB 003.008\n")
            async for message in ws:
                if message.type == self.aiohttp.WSMsgType.BINARY:
                    if message.data == b"revoke":
                        await ws.close(code=1008, message=b"Dummy sign-in ended.")
                        break
                    await ws.send_bytes(message.data)
            return ws

        page_app = self.web.Application()
        page_app.router.add_route("*", "/{path:.*}", page)
        console_app = self.web.Application()
        console_app.router.add_get("/websockify", websocket)
        self.runners = []
        ports = []
        for app in (page_app, console_app):
            runner = self.web.AppRunner(app, access_log=None)
            self.runners.append(runner)
            await runner.setup()
            site = self.web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            ports.append(site._server.sockets[0].getsockname()[1])
        self.gateway = remote_gateway.RemoteGateway(ports[0], ports[1], REMOTE_ORIGIN, port=0)
        self.gateway.start()
        self.assertTrue(self.gateway.running, self.gateway.error)
        self.base = "http://127.0.0.1:" + str(self.gateway.port)
        self.session = self.aiohttp.ClientSession(cookie_jar=self.aiohttp.DummyCookieJar(), trust_env=False,
                                                  timeout=self.aiohttp.ClientTimeout(total=5))

    async def asyncTearDown(self):
        await self.session.close()
        await asyncio.to_thread(self.gateway.close)
        for runner in self.runners:
            await runner.cleanup()

    def headers(self, **extra):
        values = {"Host": REMOTE_HOST, "Origin": REMOTE_ORIGIN, "Cookie": DUMMY_COOKIE}
        values.update(extra)
        return values

    async def request(self, method="GET", path="/", headers=None, **kwargs):
        async with self.session.request(method, self.base + path,
                                        headers=self.headers() if headers is None else headers,
                                        allow_redirects=False, **kwargs) as response:
            return response.status, response.headers, await response.read()

    async def test_loopback_binding(self):
        self.assertEqual(self.gateway._bind_address, "127.0.0.1")
        self.assertFalse(self.gateway._logger.propagate)

    async def test_http_preserves_public_boundary_and_session(self):
        status, headers, body = await self.request(headers=self.headers(**{
            "X-Forwarded-Host": "attacker.example", "X-Forwarded-Proto": "http", "Forwarded": "host=evil",
            "X-Nutcracker-Token": DUMMY_TOKEN, "Sec-Fetch-Site": "same-origin",
        }))
        self.assertEqual(status, 200)
        self.assertEqual(body, b"dummy-companion-response")
        forwarded = self.page_requests[-1]["headers"]
        self.assertEqual(forwarded["Host"], REMOTE_HOST)
        self.assertEqual(forwarded["Origin"], REMOTE_ORIGIN)
        self.assertEqual(forwarded["Cookie"], DUMMY_COOKIE)
        self.assertEqual(forwarded["X-Nutcracker-Token"], DUMMY_TOKEN)
        self.assertNotIn("X-Forwarded-Host", forwarded)
        self.assertNotIn("X-Forwarded-Proto", forwarded)
        self.assertNotIn("Forwarded", forwarded)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertEqual(headers["Content-Security-Policy"], "default-src 'self'")

    async def test_top_level_navigation_without_origin_is_forwarded_for_backend_checks(self):
        status, _, _ = await self.request(headers={"Host": REMOTE_HOST, "Sec-Fetch-Site": "cross-site",
                                                  "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"})
        self.assertEqual(status, 200)
        self.assertEqual(self.page_requests[-1]["headers"]["Sec-Fetch-Site"], "cross-site")
        self.assertNotIn("Origin", self.page_requests[-1]["headers"])

    async def test_wrong_host_and_origin_are_blocked(self):
        for headers in ({"Host": "localhost"}, self.headers(Host="attacker.example"),
                        self.headers(Origin="https://attacker.example"), self.headers(Origin="null")):
            status, _, _ = await self.request(headers=headers)
            self.assertEqual(status, 403)
        self.assertEqual(self.page_requests, [])

    async def test_duplicate_critical_headers_are_blocked(self):
        # A normal HTTP client repairs duplicate Host headers. Use a raw dummy
        # request to exercise the server's boundary rather than client cleanup.
        for name, value in (("Host", REMOTE_HOST), ("Origin", REMOTE_ORIGIN), ("Cookie", DUMMY_COOKIE),
                            ("Content-Length", "0")):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.gateway.port)
            try:
                headers = list(self.headers().items())
                if name == "Content-Length":
                    headers.append((name, value))
                headers.append((name, value))
                data = "GET / HTTP/1.1\r\n" + "".join(key + ": " + item + "\r\n" for key, item in headers)
                writer.write((data + "Connection: close\r\n\r\n").encode("ascii"))
                await writer.drain()
                status_line = await asyncio.wait_for(reader.readline(), timeout=3)
                self.assertEqual(status_line.split()[1], b"400")
            finally:
                writer.close()
                await writer.wait_closed()
        self.assertEqual(self.page_requests, [])

    async def test_post_requires_origin(self):
        status, _, _ = await self.request("POST", headers={"Host": REMOTE_HOST}, json={})
        self.assertEqual(status, 403)
        self.assertEqual(self.page_requests, [])

    async def test_bounded_json_post_is_forwarded(self):
        status, _, _ = await self.request("POST", path="/api/login", json={"username": "Dummy"})
        self.assertEqual(status, 200)
        self.assertEqual(self.page_requests[-1]["method"], "POST")
        self.assertEqual(self.page_requests[-1]["path"], "/api/login")
        self.assertEqual(self.page_requests[-1]["body"], b'{"username": "Dummy"}')

    async def test_non_json_or_invalid_json_is_rejected(self):
        for body, content_type, expected in ((b"{}", "text/plain", 415), (b"broken", "application/json", 400),
                                               (b"[]", "application/json", 400)):
            status, _, _ = await self.request("POST", headers=self.headers(**{"Content-Type": content_type}), data=body)
            self.assertEqual(status, expected)
        self.assertEqual(self.page_requests, [])

    async def test_oversized_post_is_rejected(self):
        status, _, _ = await self.request("POST", headers=self.headers(**{"Content-Type": "application/json"}),
                                          data=b" " * (remote_gateway.MAX_JSON_BYTES + 1))
        self.assertEqual(status, 413)
        self.assertEqual(self.page_requests, [])

    async def test_chunked_oversized_post_is_rejected(self):
        async def chunks():
            yield b" " * 10000
            yield b" " * 10000
        status, _, _ = await self.request("POST", headers=self.headers(**{"Content-Type": "application/json"}), data=chunks())
        self.assertEqual(status, 413)
        self.assertEqual(self.page_requests, [])

    async def test_compressed_post_is_rejected(self):
        status, _, _ = await self.request("POST", headers=self.headers(**{
            "Content-Type": "application/json", "Content-Encoding": "gzip"}), data=b"{}")
        self.assertEqual(status, 415)
        self.assertEqual(self.page_requests, [])

    async def test_other_methods_and_get_bodies_are_rejected(self):
        status, _, _ = await self.request("PUT", data=b"{}")
        self.assertEqual(status, 405)
        status, _, _ = await self.request("GET", data=b"{}")
        self.assertEqual(status, 400)
        self.assertEqual(self.page_requests, [])

    async def test_assets_stream_and_head_preserves_length(self):
        status, headers, body = await self.request(path="/asset")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"large-fixed-asset" * 40000)
        status, head_headers, body = await self.request("HEAD", path="/asset")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(head_headers["Content-Length"], headers["Content-Length"])

    async def test_redirect_is_not_followed(self):
        status, headers, _ = await self.request(path="/redirect")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/login.html")
        self.assertEqual(len(self.page_requests), 1)

    async def test_client_query_cannot_choose_an_upstream(self):
        status, _, _ = await self.request(path="/?url=https://attacker.example/private")
        self.assertEqual(status, 200)
        self.assertEqual(self.page_requests[-1]["path"], "/?url=https://attacker.example/private")

    async def connect_websocket(self, headers=None, token=DUMMY_TOKEN, path="/websockify"):
        return await self.session.ws_connect(self.base + path + "?token=" + token,
                                             headers=self.headers() if headers is None else headers,
                                             protocols=("binary",), compress=0)

    async def test_websocket_binary_relay_preserves_auth_and_query(self):
        async with await self.connect_websocket(headers=self.headers(**{"X-Forwarded-Host": "attacker.example"})) as ws:
            message = await ws.receive(timeout=3)
            self.assertEqual(message.type, self.aiohttp.WSMsgType.BINARY)
            self.assertEqual(message.data, b"RFB 003.008\n")
            await ws.send_bytes(b"\x00\x01\x02\x03")
            self.assertEqual((await ws.receive(timeout=3)).data, b"\x00\x01\x02\x03")
        forwarded = self.websocket_requests[-1]
        self.assertEqual(forwarded["path"], "/websockify?token=" + DUMMY_TOKEN)
        self.assertEqual(forwarded["headers"]["Host"], REMOTE_HOST)
        self.assertEqual(forwarded["headers"]["Origin"], REMOTE_ORIGIN)
        self.assertEqual(forwarded["headers"]["Cookie"], DUMMY_COOKIE)
        self.assertNotIn("X-Forwarded-Host", forwarded["headers"])

    async def test_websocket_backend_authentication_is_required(self):
        for headers, token in (({"Host": REMOTE_HOST, "Origin": REMOTE_ORIGIN}, DUMMY_TOKEN),
                               (self.headers(), "invalid-dummy-token")):
            with self.assertRaises(self.aiohttp.WSServerHandshakeError) as failure:
                await self.connect_websocket(headers=headers, token=token)
            self.assertEqual(failure.exception.status, 401)

    async def test_websocket_requires_exact_origin_and_path(self):
        for headers, path, expected in (({"Host": REMOTE_HOST}, "/websockify", 403),
                                        (self.headers(Origin="https://attacker.example"), "/websockify", 403),
                                        (self.headers(), "/different", 404)):
            with self.assertRaises(self.aiohttp.WSServerHandshakeError) as failure:
                await self.connect_websocket(headers=headers, path=path)
            self.assertEqual(failure.exception.status, expected)

    async def test_websocket_text_is_closed(self):
        async with await self.connect_websocket() as ws:
            await ws.receive(timeout=3)
            await ws.send_str("text-is-not-rfb")
            message = await ws.receive(timeout=3)
            self.assertEqual(message.type, self.aiohttp.WSMsgType.CLOSE)
            self.assertEqual(message.data, 1003)

    async def test_backend_revocation_closes_browser_connection(self):
        async with await self.connect_websocket() as ws:
            await ws.receive(timeout=3)
            await ws.send_bytes(b"revoke")
            message = await ws.receive(timeout=3)
            self.assertEqual(message.type, self.aiohttp.WSMsgType.CLOSE)
            self.assertEqual(message.data, 1008)

    async def test_shutdown_closes_live_viewer_and_releases_port(self):
        async with await self.connect_websocket() as ws:
            await ws.receive(timeout=3)
            await asyncio.to_thread(self.gateway.close)
            message = await ws.receive(timeout=3)
            self.assertEqual(message.type, self.aiohttp.WSMsgType.CLOSE)
            self.assertEqual(message.data, 1001)
        self.assertFalse(self.gateway.running)
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", self.gateway.port)


if __name__ == "__main__":
    unittest.main()
