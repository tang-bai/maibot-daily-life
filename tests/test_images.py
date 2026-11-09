"""Offline security tests: no test may make an actual HTTP/DNS request."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from PIL import Image

from daily_life import images
from daily_life.images import ImageError, ImageGenerator, validate_image


def picture(fmt="PNG", size=(4, 3)):
    output = io.BytesIO()
    Image.new("RGB", size, "red").save(output, format=fmt)
    return output.getvalue()


def settings(**changes):
    values = dict(base_url="http://127.0.0.1:8080/v1/", api_key="super-secret-key",
                  model="example-model", size="", timeout_seconds=5,
                  max_image_bytes=8388608, max_pixels=16777216)
    values.update(changes)
    return SimpleNamespace(**values)


class Content:
    def __init__(self, chunks):
        self.chunks = chunks
        self.consumed = 0

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk


class Response:
    def __init__(self, body=b"", status=200, headers=None, content_length=None, chunks=None):
        self.status = status
        self.headers = headers or {}
        self.content_length = content_length
        self.content = Content(chunks if chunks is not None else [body])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FailingResponse:
    def __init__(self, error):
        self.error = error

    async def __aenter__(self):
        raise self.error

    async def __aexit__(self, *args):
        return False


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.close = AsyncMock()

    def _request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected request or retry")
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)


def api_document(item):
    return Response(json.dumps({"data": [item]}).encode())


class ImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # A missed transport mock fails rather than reaching the network.
        self.no_http = patch.object(aiohttp.ClientSession, "_request",
                                    side_effect=AssertionError("Real HTTP forbidden"))
        self.no_http.start()
        self.no_dns = patch("socket.getaddrinfo", side_effect=AssertionError("Real DNS forbidden"))
        self.no_dns.start()
        self.generator = ImageGenerator(settings())
        self.addAsyncCleanup(self.generator.close)
        self.addCleanup(self.no_http.stop)
        self.addCleanup(self.no_dns.stop)
        self.png = picture()

    def use_clients(self, api, download=None):
        self.generator._api_session = api
        if download is not None:
            self.generator._download_session = download

    def assert_safe(self, error):
        for secret in ("super-secret-key", "private-query", "base64-private", "https://", "127.0.0.1"):
            self.assertNotIn(secret, str(error))

    async def test_base64_success_and_exact_payload(self):
        api = Client(api_document({"b64_json": base64.b64encode(self.png).decode()}))
        self.use_clients(api)
        self.assertEqual(await self.generator.generate("a quiet day"), self.png)
        method, url, kwargs = api.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "http://127.0.0.1:8080/v1/images/generations")
        self.assertEqual(kwargs["json"], {"model": "example-model", "prompt": "a quiet day", "n": 1})
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer super-secret-key"})
        self.assertIs(kwargs["allow_redirects"], False)

    async def test_config_key_wins_and_environment_is_never_a_fallback(self):
        api = Client(api_document({"b64_json": base64.b64encode(self.png).decode()}))
        self.use_clients(api)
        with patch.dict(os.environ, {"DAILY_LIFE_IMAGE_API_KEY": "wrong-env-key", "TEST_IMAGE_KEY": "wrong-env-key"}):
            self.assertEqual(await self.generator.generate("day"), self.png)
            self.assertEqual(api.calls[0][2]["headers"]["Authorization"], "Bearer super-secret-key")
            self.generator.settings.api_key = ""
            self.assertTrue(self.generator.problems())
            with self.assertRaises(ImageError):
                await self.generator.generate("day")
        self.assertEqual(len(api.calls), 1)

    async def test_config_key_works_with_empty_environment(self):
        api = Client(api_document({"b64_json": base64.b64encode(self.png).decode()}))
        self.use_clients(api)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.generator.problems(), [])
            self.assertEqual(await self.generator.generate("day"), self.png)
        self.assertEqual(api.calls[0][2]["headers"]["Authorization"], "Bearer super-secret-key")

    async def test_size_only_if_nonempty(self):
        for size in (None, "", "   ", "1024x1024"):
            with self.subTest(size=size):
                self.generator.settings.size = size
                api = Client(api_document({"b64_json": base64.b64encode(self.png).decode()}))
                self.use_clients(api)
                await self.generator.generate("day")
                self.assertEqual("size" in api.calls[0][2]["json"], size == "1024x1024")

    async def test_url_success_keeps_bearer_out_of_download(self):
        api = Client(api_document({"url": "https://images.example/day?token=private-query"}))
        download = Client(Response(self.png, headers={"Content-Type": "image/png; charset=binary"}))
        self.use_clients(api, download)
        self.assertEqual(await self.generator.generate("day"), self.png)
        self.assertEqual(len(download.calls), 1)
        self.assertEqual(download.calls[0][2], {"allow_redirects": False})
        self.assertNotIn("super-secret-key", repr(download.calls))

    async def test_jpeg_download(self):
        jpg = picture("JPEG")
        self.use_clients(Client(api_document({"url": "https://8.8.8.8/day"})),
                         Client(Response(jpg, headers={"Content-Type": "image/jpeg"})))
        self.assertEqual(await self.generator.generate("day"), jpg)

    async def test_api_errors_and_redirects_do_not_retry_or_leak(self):
        for status in (301, 302, 307, 400, 401, 429, 500):
            with self.subTest(status=status):
                api = Client(Response(b"super-secret-key base64-private", status=status,
                                      headers={"Location": "http://127.0.0.1/?private-query"}))
                self.use_clients(api)
                with self.assertRaises(ImageError) as caught:
                    await self.generator.generate("day")
                self.assert_safe(caught.exception)
                self.assertIn(f"HTTP {status}", str(caught.exception))
                self.assertEqual(len(api.calls), 1)

    async def test_success_status_with_error_object_is_sanitized(self):
        api = Client(Response(json.dumps({"error": {"message": "super-secret-key base64-private"}}).encode()))
        self.use_clients(api)
        with self.assertRaises(ImageError) as caught:
            await self.generator.generate("day")
        self.assertIn("错误对象", str(caught.exception))
        self.assert_safe(caught.exception)
        self.assertEqual(len(api.calls), 1)

    async def test_timeout_and_network_error_are_sanitized(self):
        for error in (asyncio.TimeoutError("private-query"),
                      aiohttp.ClientConnectionError("https://127.0.0.1/?private-query")):
            with self.subTest(kind=type(error)):
                api = Client(FailingResponse(error))
                self.use_clients(api)
                with self.assertRaises(ImageError) as caught:
                    await self.generator.generate("day")
                self.assert_safe(caught.exception)
                self.assertEqual(len(api.calls), 1)

    async def test_download_timeout_is_sanitized(self):
        self.use_clients(Client(api_document({"url": "https://example.org/?private-query"})),
                         Client(FailingResponse(asyncio.TimeoutError("private-query"))))
        with self.assertRaises(ImageError) as caught:
            await self.generator.generate("day")
        self.assert_safe(caught.exception)

    async def test_api_and_download_share_one_total_deadline_without_retry(self):
        # Advance the shared deadline instead of sleeping or touching the network.
        # This exercises cancellation in the final download after API + redirects.
        real_timeout = asyncio.timeout
        deadlines = []
        stages = []

        def total_timeout(seconds):
            self.assertEqual(seconds, self.generator.settings.timeout_seconds)
            deadline = real_timeout(seconds)
            deadlines.append(deadline)
            return deadline

        class StagedResponse(Response):
            async def __aenter__(response):
                self.assertEqual(len(deadlines), 1)
                self.assertIsNotNone(deadlines[0].when())
                self.assertFalse(deadlines[0].expired())
                stages.append(response.stage)
                if response.stage == "download":
                    # API + redirect exhausted the operation's one shared budget.
                    deadlines[0].reschedule(asyncio.get_running_loop().time())
                    await asyncio.Event().wait()
                return response

        api_response = StagedResponse(json.dumps({
            "data": [{"url": "https://example.org/start?private-query"}]
        }).encode())
        api_response.stage = "api"
        redirect = StagedResponse(status=302, headers={"Location": "/final"})
        redirect.stage = "redirect"
        final = StagedResponse(self.png, headers={"Content-Type": "image/png"})
        final.stage = "download"
        api = Client(api_response)
        download = Client(redirect, final)
        self.use_clients(api, download)
        with patch.object(images.asyncio, "timeout", side_effect=total_timeout):
            with self.assertRaises(ImageError) as caught:
                await asyncio.wait_for(self.generator.generate("day"), timeout=1)
        self.assertIn("超时", str(caught.exception))
        self.assert_safe(caught.exception)
        self.assertEqual(stages, ["api", "redirect", "download"])
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(len(download.calls), 2)
        self.assertTrue(deadlines[0].expired())

    async def test_json_size_bound_by_header_and_stream(self):
        for response in (Response(content_length=32 * 1024 * 1024 + 1),
                         Response(chunks=[b"x" * (1024 * 1024)] * 33)):
            self.use_clients(Client(response))
            with self.assertRaises(ImageError):
                await self.generator.generate("day")
            self.assertLessEqual(response.content.consumed, 33)

    async def test_invalid_json_shapes(self):
        for body in (b"not json private-query", b"[]", b"null", b'{}', b'{"data": []}',
                     b'{"data": [null]}', b'{"data": {"0": {}}}', b'{"data": [{}]}'):
            with self.subTest(body=body):
                self.use_clients(Client(Response(body)))
                with self.assertRaises(ImageError) as caught:
                    await self.generator.generate("day")
                self.assert_safe(caught.exception)

    async def test_invalid_base64_is_rejected_strictly(self):
        for value in ("base64-private!", "%%%%", "é", "YWJj\n", "", None, 123):
            with self.subTest(value=value):
                self.use_clients(Client(api_document({"b64_json": value})))
                with self.assertRaises(ImageError) as caught:
                    await self.generator.generate("day")
                self.assert_safe(caught.exception)

    async def test_oversized_base64(self):
        self.generator.settings.max_image_bytes = 1
        self.use_clients(Client(api_document({"b64_json": base64.b64encode(self.png).decode()})))
        with self.assertRaises(ImageError):
            await self.generator.generate("day")

    async def test_download_size_bound_by_header_and_stream(self):
        self.generator.settings.max_image_bytes = 10
        for response in (Response(headers={"Content-Type": "image/png"}, content_length=11),
                         Response(headers={"Content-Type": "image/png"}, chunks=[b"x" * 6, b"y" * 6])):
            self.use_clients(Client(api_document({"url": "https://example.org/day"})), Client(response))
            with self.assertRaises(ImageError):
                await self.generator.generate("day")

    async def test_mime_and_real_format_must_match(self):
        for mime, data in (("text/html", self.png), ("", self.png), ("image/gif", self.png),
                           ("image/jpeg", self.png), ("image/png", b"<html>private-query</html>")):
            with self.subTest(mime=mime):
                self.use_clients(Client(api_document({"url": "https://example.org/day"})),
                                 Client(Response(data, headers={"Content-Type": mime})))
                with self.assertRaises(ImageError):
                    await self.generator.generate("day")

    async def test_non_200_download_status(self):
        self.use_clients(Client(api_document({"url": "https://example.org/day"})),
                         Client(Response(b"private-query", status=404)))
        with self.assertRaises(ImageError) as caught:
            await self.generator.generate("day")
        self.assert_safe(caught.exception)

    async def test_internal_literals_and_bad_urls_never_requested(self):
        urls = ["http://127.0.0.1/a", "http://10.1.2.3/a", "http://169.254.169.254/a",
                "http://0.0.0.0/a", "http://[::1]/a", "http://[fc00::1]/a",
                "http://[::ffff:127.0.0.1]/a", "http://[::ffff:10.0.0.1]/a",
                "http://100.64.0.1/a", "http://224.0.0.1/a", "http://[fe80::1%25eth0]/a",
                "ftp://example.org/a", "file:///a", "https://u:p@example.org/a",
                "https://example.org:444/a", "http://example.org:443/a", "//example.org/a"]
        for url in urls:
            with self.subTest(url=url):
                download = Client()
                self.use_clients(Client(api_document({"url": url})), download)
                with self.assertRaises(ImageError) as caught:
                    await self.generator.generate("day")
                self.assert_safe(caught.exception)
                self.assertEqual(download.calls, [])

    async def test_redirect_public_hops_relative_and_key_isolation(self):
        download = Client(Response(status=302, headers={"Location": "/next?private-query"}),
                          Response(status=307, headers={"Location": "https://other.example/final"}),
                          Response(self.png, headers={"Content-Type": "image/png"}))
        self.use_clients(Client(api_document({"url": "https://example.org/start"})), download)
        self.assertEqual(await self.generator.generate("day"), self.png)
        self.assertEqual(str(download.calls[1][1]), "https://example.org/next?private-query")
        self.assertEqual(str(download.calls[2][1]), "https://other.example/final")
        self.assertTrue(all(call[2] == {"allow_redirects": False} for call in download.calls))

    async def test_redirect_private_target_is_not_requested(self):
        for location in ("http://127.0.0.1/private-query", "http://[::ffff:192.168.1.2]/a",
                         "//user:password@example.org/a", "https://example.org:9000/a", "file:///etc/passwd"):
            with self.subTest(location=location):
                download = Client(Response(status=302, headers={"Location": location}))
                self.use_clients(Client(api_document({"url": "https://example.org/start"})), download)
                with self.assertRaises(ImageError):
                    await self.generator.generate("day")
                self.assertEqual(len(download.calls), 1)

    async def test_at_most_three_redirects(self):
        download = Client(*[Response(status=302, headers={"Location": "/next"}) for _ in range(4)])
        self.use_clients(Client(api_document({"url": "https://example.org/start"})), download)
        with self.assertRaises(ImageError):
            await self.generator.generate("day")
        self.assertEqual(len(download.calls), 4)

    async def test_three_redirects_then_image_allowed(self):
        download = Client(*[Response(status=302, headers={"Location": "/next"}) for _ in range(3)],
                          Response(self.png, headers={"Content-Type": "image/png"}))
        self.use_clients(Client(api_document({"url": "https://example.org/start"})), download)
        self.assertEqual(await self.generator.generate("day"), self.png)

    async def test_redirect_missing_location(self):
        self.use_clients(Client(api_document({"url": "https://example.org/start"})),
                         Client(Response(status=302)))
        with self.assertRaises(ImageError):
            await self.generator.generate("day")

    async def test_transport_separation_and_dns_binding_configuration(self):
        api = self.generator._api_client()
        download = self.generator._image_client()
        self.assertIsNot(api, download)
        self.assertFalse(api.trust_env)
        self.assertFalse(download.trust_env)
        self.assertNotIn("Authorization", download.headers)
        self.assertIsInstance(download.cookie_jar, aiohttp.DummyCookieJar)
        self.assertIsInstance(download.connector, aiohttp.TCPConnector)
        self.assertIs(download.connector._resolver, self.generator._download_resolver)
        self.assertFalse(download.connector.use_dns_cache)
        self.assertTrue(download.connector.force_close)
        if hasattr(download, "_retry_connection"):
            self.assertFalse(download._retry_connection)

    async def test_configuration_check_never_opens_client(self):
        with patch.object(aiohttp, "ClientSession", side_effect=AssertionError("network")):
            self.assertEqual(self.generator.problems(), [])
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(self.generator.problems(), [])
            self.assertIsNone(self.generator._api_session)
            self.assertIsNone(self.generator._download_session)

    async def test_invalid_settings_and_missing_key_are_safe(self):
        cases = dict(base_url="https://u:private-query@example.org", api_key="", model="",
                     size=123, timeout_seconds=float("nan"), max_image_bytes=0, max_pixels=-1)
        for field, value in cases.items():
            with self.subTest(field=field):
                generator = ImageGenerator(settings(**{field: value}))
                self.assertTrue(generator.problems())
                with self.assertRaises(ImageError) as caught:
                    await generator.generate("day")
                self.assert_safe(caught.exception)
        for key in (None, "", " ", "key with spaces", "key\r\nheader", "密钥"):
            with self.subTest(key_type=type(key).__name__):
                generator = ImageGenerator(settings(api_key=key))
                self.assertTrue(generator.problems())
                with self.assertRaises(ImageError):
                    await generator.generate("day")

    async def test_empty_prompt(self):
        with self.assertRaises(ImageError):
            await self.generator.generate(" ")

    async def test_close_closes_both_clients_and_prevents_reuse(self):
        api, download = Client(), Client()
        self.use_clients(api, download)
        await self.generator.close()
        await self.generator.close()
        api.close.assert_awaited_once()
        download.close.assert_awaited_once()
        with self.assertRaises(ImageError):
            await self.generator.generate("day")

    async def test_validate_png_jpeg_and_exact_size_bound(self):
        for data in (self.png, picture("JPEG")):
            self.assertEqual(validate_image(data, max_bytes=len(data), max_pixels=12), data)
            with self.assertRaises(ImageError):
                validate_image(data, max_bytes=len(data) - 1)
            with self.assertRaises(ImageError):
                validate_image(data, max_pixels=11)

    async def test_validate_corrupt_truncated_and_other_formats(self):
        for data in (b"", b"not an image", self.png[:40], picture("JPEG")[:100],
                     picture("GIF"), picture("BMP"), b"\x89PNG\r\n\x1a\nprivate-query"):
            with self.subTest(length=len(data)):
                with self.assertRaises(ImageError):
                    validate_image(data)
        corrupt = bytearray(self.png)
        corrupt[-5] ^= 0xFF
        with self.assertRaises(ImageError):
            validate_image(bytes(corrupt))

    async def test_animated_png_rejected(self):
        out = io.BytesIO()
        Image.new("RGB", (3, 3), "red").save(
            out, format="PNG", save_all=True,
            append_images=[Image.new("RGB", (3, 3), "blue")], duration=100, loop=0)
        with self.assertRaises(ImageError):
            validate_image(out.getvalue())

    async def test_pillow_bomb_warning_and_error_are_rejected(self):
        for threshold in (8, 4):
            with self.subTest(threshold=threshold), patch.object(Image, "MAX_IMAGE_PIXELS", threshold):
                with self.assertRaises(ImageError):
                    validate_image(self.png, max_pixels=1000)


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = SimpleNamespace(resolve=AsyncMock(), close=AsyncMock())
        with patch.object(aiohttp.resolver, "DefaultResolver", return_value=self.backend):
            self.resolver = images._PublicResolver()
        self.addAsyncCleanup(self.resolver.close)

    @staticmethod
    def record(host):
        return dict(hostname="example.org", host=host, port=443,
                    family=socket.AF_INET6 if ":" in host else socket.AF_INET,
                    proto=0, flags=socket.AI_NUMERICHOST)

    async def test_all_validated_addresses_returned_unchanged(self):
        records = [self.record("8.8.8.8"), self.record("2606:4700:4700::1111"),
                   self.record("::ffff:8.8.4.4")]
        self.backend.resolve.return_value = records
        self.assertIs(await self.resolver.resolve("example.org", 443, socket.AF_UNSPEC), records)
        self.backend.resolve.assert_awaited_once_with("example.org", 443, socket.AF_UNSPEC)

    async def test_one_private_answer_rejects_entire_set(self):
        for bad in ("127.0.0.1", "192.168.1.1", "169.254.169.254", "::1", "::ffff:10.0.0.1",
                    "host-instead-of-numeric-address", "fe80::1%eth0"):
            with self.subTest(bad=bad):
                self.backend.resolve.return_value = [self.record("8.8.8.8"), self.record(bad)]
                with self.assertRaises(ImageError):
                    await self.resolver.resolve("example.org", 443)

    async def test_rebinding_second_resolution_is_checked_again(self):
        self.backend.resolve.side_effect = [[self.record("8.8.8.8")], [self.record("127.0.0.1")]]
        await self.resolver.resolve("example.org", 443)
        with self.assertRaises(ImageError):
            await self.resolver.resolve("example.org", 443)

    async def test_empty_and_failed_dns_are_sanitized(self):
        self.backend.resolve.return_value = []
        with self.assertRaises(ImageError):
            await self.resolver.resolve("example.org", 443)
        self.backend.resolve.side_effect = OSError("private-query secret https://example.org")
        with self.assertRaises(ImageError) as caught:
            await self.resolver.resolve("example.org", 443)
        self.assertNotIn("private-query", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
