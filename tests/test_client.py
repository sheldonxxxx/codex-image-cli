import io
import json
import os
import socket
import ssl
import http.client
import urllib.error
import urllib.request
import uuid
from unittest import mock

from codex_image_cli import client
from codex_image_cli.auth import Credentials
from codex_image_cli.errors import CliError
from helpers import ACCOUNT, SYNTHETIC_SECRET, FakeResponse, IsolatedTestCase, png, response_bytes


class ClientTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.credentials = Credentials(SYNTHETIC_SECRET, ACCOUNT)
        self.payload = {"prompt": "Tiny café", "model": "gpt-image-2", "size": "auto",
                        "quality": "auto", "background": "opaque"}

    def test_exact_post_payload_headers_and_fixed_endpoints(self):
        for operation, endpoint in [("generate", "images/generations"), ("edit", "images/edits")]:
            with self.subTest(operation=operation):
                response = FakeResponse()
                opener = mock.Mock()
                opener.open.return_value = response
                image, metadata = client.request_image(operation, self.payload, self.credentials, 123, opener=opener)
                opener.open.assert_called_once()
                request = opener.open.call_args.args[0]
                self.assertEqual(opener.open.call_args.kwargs, {"timeout": 123})
                self.assertEqual(request.full_url, "https://chatgpt.com/backend-api/codex/" + endpoint)
                self.assertEqual(request.method, "POST")
                self.assertEqual(json.loads(request.data), self.payload)
                headers = {key.lower(): value for key, value in request.header_items()}
                self.assertEqual(headers["authorization"], "Bearer " + SYNTHETIC_SECRET)
                self.assertEqual(headers["chatgpt-account-id"], ACCOUNT)
                self.assertEqual(headers["originator"], "codex_image_cli")
                self.assertEqual(headers["content-type"], "application/json")
                self.assertEqual(headers["accept"], "application/json")
                self.assertEqual(headers["accept-encoding"], "identity")
                self.assertTrue(headers["user-agent"].startswith("codex-image-cli/"))
                self.assertEqual(str(uuid.UUID(headers["x-codex-image-turn-id"])), metadata["turn_id"])
                self.assertEqual(image, png())
                self.assertEqual(response.read_sizes, [client.MAX_RESPONSE_BYTES + 1])
                self.assertTrue(response.closed)

    def test_turn_ids_are_unique(self):
        opener = mock.Mock()
        opener.open.side_effect = [FakeResponse(), FakeResponse()]
        _, one = client.request_image("generate", self.payload, self.credentials, 3, opener=opener)
        _, two = client.request_image("generate", self.payload, self.credentials, 3, opener=opener)
        self.assertNotEqual(one["turn_id"], two["turn_id"])

    def test_invalid_operation_stops_before_network(self):
        opener = mock.Mock()
        with self.assertRaises(CliError):
            client.request_image("anything", self.payload, self.credentials, 3, opener=opener)
        opener.open.assert_not_called()

    def test_http_errors_never_retry_or_read_server_secrets(self):
        for status in (301, 302, 307, 308, 400, 401, 403, 404, 429, 500, 502, 503):
            with self.subTest(status=status):
                body = mock.Mock(wraps=io.BytesIO(SYNTHETIC_SECRET.encode()))
                error = urllib.error.HTTPError("https://evil.invalid/" + SYNTHETIC_SECRET, status,
                                               SYNTHETIC_SECRET, {}, body)
                opener = mock.Mock()
                opener.open.side_effect = error
                with self.assertRaises(CliError) as caught:
                    client.request_image("generate", self.payload, self.credentials, 3, opener=opener)
                opener.open.assert_called_once()
                body.read.assert_not_called()
                body.close.assert_called_once()
                self.assertEqual(caught.exception.exit_code, 3 if status == 401 else 4)
                self.assert_secret_absent(str(caught.exception))
                if 300 <= status < 400:
                    self.assertIn("redirect", str(caught.exception))

    def test_non200_response_is_error_without_read(self):
        response = FakeResponse(status=201)
        opener = mock.Mock()
        opener.open.return_value = response
        with self.assertRaises(CliError) as caught:
            client.request_image("generate", self.payload, self.credentials, 3, opener=opener)
        self.assertEqual(caught.exception.exit_code, 4)
        self.assertEqual(response.read_sizes, [])
        opener.open.assert_called_once()

    def test_network_tls_and_timeout_errors_never_retry_or_leak(self):
        failures = [urllib.error.URLError(SYNTHETIC_SECRET), TimeoutError(SYNTHETIC_SECRET),
                    socket.timeout(SYNTHETIC_SECRET), ssl.SSLError(SYNTHETIC_SECRET),
                    http.client.BadStatusLine(SYNTHETIC_SECRET), OSError(SYNTHETIC_SECRET),
                    ValueError(SYNTHETIC_SECRET)]
        for error in failures:
            with self.subTest(error=type(error).__name__):
                opener = mock.Mock()
                opener.open.side_effect = error
                with self.assertRaises(CliError) as caught:
                    client.request_image("generate", self.payload, self.credentials, 3, opener=opener)
                self.assertEqual((caught.exception.kind, caught.exception.exit_code), ("network", 4))
                self.assertIn("quota", str(caught.exception))
                self.assert_secret_absent(str(caught.exception))
                opener.open.assert_called_once()

    def test_response_read_timeout_does_not_retry(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.side_effect = TimeoutError(SYNTHETIC_SECRET)
        opener = mock.Mock()
        opener.open.return_value = response
        with self.assertRaises(CliError) as caught:
            client.request_image("generate", self.payload, self.credentials, 3, opener=opener)
        self.assertEqual(caught.exception.kind, "network")
        opener.open.assert_called_once()
        response.__exit__.assert_called_once()

    def test_oversized_response_is_bounded(self):
        response = FakeResponse(b"x" * 101)
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(client, "MAX_RESPONSE_BYTES", 100):
            with self.assertRaises(CliError) as caught:
                client.request_image("generate", self.payload, self.credentials, 3, opener=opener)
        self.assertEqual(caught.exception.exit_code, 5)
        self.assertEqual(response.read_sizes, [101])
        opener.open.assert_called_once()

    def test_malformed_response_is_not_retried(self):
        opener = mock.Mock()
        opener.open.return_value = FakeResponse(b"secret-invalid-json:" + SYNTHETIC_SECRET.encode())
        with self.assertRaises(CliError) as caught:
            client.request_image("generate", self.payload, self.credentials, 3, opener=opener)
        self.assertEqual(caught.exception.exit_code, 5)
        self.assert_secret_absent(str(caught.exception))
        opener.open.assert_called_once()

    def test_build_opener_disables_env_proxies_and_verifies_tls(self):
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://synthetic.invalid:1", "HTTPS_PROXY": "http://synthetic.invalid:2"}), \
             mock.patch.object(client.urllib.request, "build_opener") as build, \
             mock.patch.object(client.ssl, "create_default_context", wraps=ssl.create_default_context) as context:
            client.build_opener()
        handlers = build.call_args.args
        proxy = next(item for item in handlers if isinstance(item, urllib.request.ProxyHandler))
        https = next(item for item in handlers if isinstance(item, urllib.request.HTTPSHandler))
        self.assertEqual(proxy.proxies, {})
        self.assertTrue(any(isinstance(item, client.NoRedirect) for item in handlers))
        self.assertIsNone(https._context.keylog_filename)
        self.assertTrue(https._context.check_hostname)
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        context.assert_called_once_with()

    def test_all_redirect_targets_are_rejected(self):
        handler = client.NoRedirect()
        request = urllib.request.Request("https://chatgpt.com/backend-api/codex/images/generations")
        for code in (301, 302, 303, 307, 308):
            for target in ("https://chatgpt.com/same-host", "https://evil.invalid/", "http://chatgpt.com/"):
                with self.subTest(code=code, target=target):
                    self.assertIsNone(handler.redirect_request(request, None, code, "redirect", {}, target))
