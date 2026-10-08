"""Independent, fully offline security/regression audit tests."""
import base64
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import urllib.response
import zlib

from codex_image_cli import cli
from codex_image_cli.auth import Credentials, load_credentials
from codex_image_cli.client import BASE_URL, NoRedirect, request_image
from codex_image_cli.errors import CliError
from codex_image_cli.images import PNG_SIGNATURE, decode_response
from codex_image_cli.output import OutputTarget

TOKEN = "synthetic-access-token-never-real"
ACCOUNT = "synthetic-account"


def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)


def png():
    return (PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff")) + chunk(b"IEND", b""))


def response():
    return json.dumps({"created": 1, "data": [{"b64_json": base64.b64encode(png()).decode()}]}).encode()


class StubResponse(io.BytesIO):
    status = 200


class CaptureOpener:
    def __init__(self, body=None):
        self.calls = []
        self.body = response() if body is None else body

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        return StubResponse(self.body)


class OfflineAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.auth = self.directory / "auth.json"
        self.auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
            "access_token": TOKEN, "account_id": ACCOUNT, "refresh_token": "synthetic-refresh-never-send",
            "id_token": "synthetic-id-never-send"}}))
        self.auth.chmod(0o600)
        self.network_guard = patch("socket.create_connection", side_effect=AssertionError("No live network in audit"))
        self.network_guard.start()

    def tearDown(self):
        self.network_guard.stop()
        self.tmp.cleanup()

    def invoke(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_complete_cli_uses_only_access_token_and_never_changes_auth(self):
        opener = CaptureOpener()
        original = self.auth.read_bytes()
        before = self.auth.stat()
        path = self.directory / "result.png"
        with patch("codex_image_cli.client.build_opener", return_value=opener):
            code, out, err = self.invoke(["generate", "--auth-file", str(self.auth), "--prompt", "red pixel", "--output", str(path), "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(path.read_bytes(), png())
        self.assertEqual(len(opener.calls), 1)
        req, timeout = opener.calls[0]
        self.assertEqual(req.full_url, BASE_URL + "/images/generations")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(req.get_header("Chatgpt-account-id"), ACCOUNT)
        self.assertEqual(json.loads(req.data), {"prompt": "red pixel", "model": "gpt-image-2", "quality": "auto", "size": "auto", "background": "opaque"})
        for private in (TOKEN, ACCOUNT, "synthetic-refresh-never-send", "synthetic-id-never-send", "red pixel"):
            self.assertNotIn(private, out + err)
        self.assertNotIn(b"synthetic-refresh", req.data)
        self.assertNotIn("synthetic-refresh", str(req.headers))
        self.assertEqual(self.auth.read_bytes(), original)
        after = self.auth.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns, before.st_mode), (after.st_ino, after.st_mtime_ns, after.st_mode))
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(list(self.directory.glob(".codex-image-*")), [])
        self.assertTrue(json.loads(out)["ok"])

    def test_error_does_not_leak_headers_body_or_retry(self):
        error = urllib.error.HTTPError("https://host/" + TOKEN, 401, TOKEN, {"x-private": TOKEN}, io.BytesIO(TOKEN.encode()))
        opener = CaptureOpener()
        opener.open = unittest.mock.Mock(side_effect=error)
        with patch("codex_image_cli.client.build_opener", return_value=opener):
            code, out, err = self.invoke(["generate", "--auth-file", str(self.auth), "--prompt", "x", "--output", str(self.directory / "result.png"), "--json"])
        self.assertEqual(code, 3)
        self.assertNotIn(TOKEN, out + err)
        self.assertIn("Run Codex once", err)
        opener.open.assert_called_once()
        self.assertEqual(list(self.directory.glob(".codex-image-*")), [])

    def test_auth_invalid_header_values_fail_closed(self):
        for token, account in [(TOKEN + "\r\nInjected: yes", ACCOUNT), (TOKEN, "bad\naccount"), ("", ACCOUNT), (TOKEN, "")]:
            self.auth.write_text(json.dumps({"tokens": {"access_token": token, "account_id": account}}))
            with self.assertRaises(CliError):
                load_credentials(self.auth)

    def test_output_concurrent_path_and_dangling_symlink_never_clobbered(self):
        path = self.directory / "result.png"
        with OutputTarget(path) as target:
            path.write_bytes(b"existing owner content")
            with self.assertRaises(CliError):
                target.save(png())
            recovery = target.temp_path
        self.assertEqual(path.read_bytes(), b"existing owner content")
        self.assertEqual(recovery.read_bytes(), png())
        link = self.directory / "dangling.png"
        link.symlink_to(self.directory / "absent.png")
        with self.assertRaises(CliError):
            OutputTarget(link)
        self.assertTrue(link.is_symlink())

    def test_redirects_do_not_follow_even_same_origin(self):
        for status in (301, 302, 303, 307, 308):
            for destination in ("https://attacker.invalid/", BASE_URL + "/elsewhere"):
                calls = []
                class FakeHTTPS(urllib.request.HTTPSHandler):
                    def https_open(self, request):
                        calls.append(request.full_url)
                        result = urllib.response.addinfourl(io.BytesIO(b""), {"Location": destination}, request.full_url, code=status)
                        result.msg = "Synthetic redirect"
                        return result
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), FakeHTTPS())
                with self.assertRaises(CliError) as raised:
                    request_image("generate", {}, Credentials(TOKEN, ACCOUNT), 1, opener=opener)
                self.assertIn("redirect", str(raised.exception))
                self.assertEqual(calls, [BASE_URL + "/images/generations"])

    def test_malformed_response_is_redacted(self):
        for raw in (TOKEN.encode(), b"null", b"{}", b'{"data":[]}', b'{"data":[{"b64_json":"' + TOKEN.encode() + b'"}]}'):
            with self.assertRaises(CliError) as raised:
                decode_response(raw)
            self.assertNotIn(TOKEN, str(raised.exception))

    def test_server_arbitrary_fields_not_in_metadata(self):
        value = json.loads(response())
        value.update({"quality": TOKEN, "size": TOKEN, "background": TOKEN, "token": TOKEN})
        value["data"][0]["generation_id"] = TOKEN
        _, metadata = decode_response(json.dumps(value).encode())
        self.assertNotIn(TOKEN, str(metadata))
        self.assertEqual(metadata, {"width": 1, "height": 1, "bytes": len(png())})


if __name__ == "__main__":
    unittest.main()
