"""Synthetic fixtures and a fail-closed test environment (no real auth/network)."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import unittest
from unittest import mock
import zlib

from codex_image_cli import cli

SYNTHETIC_SECRET = "synthetic-secret-DO-NOT-PRINT"
ACCOUNT = "synthetic-account"


def jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return "synthetic." + payload + ".synthetic-signature"


def chunk(tag, body=b""):
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)


def png(width=1, height=1):
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    pixels = b"".join(b"\x00" + b"\x00\x00\x00\xff" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND")


def response_bytes(image=None, **fields):
    payload = {"data": [{"b64_json": base64.b64encode(image if image is not None else png()).decode()}]}
    payload.update(fields)
    return json.dumps(payload).encode()


class FakeResponse:
    def __init__(self, data=None, status=200):
        self.data = response_bytes() if data is None else data
        self.status = status
        self.read_sizes = []
        self.closed = False

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.data if size < 0 else self.data[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class IsolatedTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-image-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.auth_path = self.root / "auth.json"
        # Even default-path tests must never touch the host's actual Codex files.
        environment = mock.patch.dict(os.environ, {"HOME": str(self.root), "CODEX_HOME": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        # A missed HTTP mock must fail locally instead of contacting any service.
        for target in ("socket.socket.connect", "socket.create_connection", "urllib.request.urlopen"):
            patcher = mock.patch(target, side_effect=AssertionError("Tests must not use the network"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.write_auth()

    def write_auth(self, token=SYNTHETIC_SECRET, account=ACCOUNT, **fields):
        value = {"auth_mode": "chatgpt", "tokens": {"access_token": token, "account_id": account}}
        value.update(fields)
        self.auth_path.write_text(json.dumps(value), encoding="utf-8")
        self.auth_path.chmod(0o600)
        return self.auth_path

    def invoke(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_secret_absent(self, *outputs):
        for text in outputs:
            self.assertNotIn(SYNTHETIC_SECRET, str(text))
            self.assertNotIn(ACCOUNT, str(text))
