import json
import os
from pathlib import Path
from unittest import mock

from codex_image_cli import auth
from codex_image_cli.errors import CliError
from helpers import ACCOUNT, SYNTHETIC_SECRET, IsolatedTestCase, jwt


class AuthTests(IsolatedTestCase):
    def test_explicit_path_and_default_codex_home(self):
        self.assertEqual(auth.default_auth_path(), self.auth_path)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(Path, "home", return_value=self.root):
            self.assertEqual(auth.default_auth_path(), self.root / ".codex" / "auth.json")
        creds = auth.load_credentials(self.auth_path)
        self.assertEqual(creds.access_token, SYNTHETIC_SECRET)
        self.assertEqual(creds.account_id, ACCOUNT)
        self.assertIsNone(creds.expiry)
        self.assertFalse(creds.permissions_warning)
        self.assert_secret_absent(repr(creds))

    def test_credentials_are_reread_not_cached(self):
        first = auth.load_credentials(self.auth_path)
        self.write_auth(token="synthetic-replacement-token", account="synthetic-other-account")
        second = auth.load_credentials(self.auth_path)
        self.assertNotEqual(first.access_token, second.access_token)
        self.assertEqual(second.account_id, "synthetic-other-account")

    def test_access_expiry_hint_and_boundary(self):
        for expiry, valid in [(1001, True), (1000, False), (999, False)]:
            with self.subTest(expiry=expiry):
                self.write_auth(token=jwt({"exp": expiry}))
                with mock.patch.object(auth.time, "time", return_value=1000):
                    if valid:
                        self.assertEqual(auth.load_credentials(self.auth_path).expiry, expiry)
                    else:
                        with self.assertRaises(CliError) as caught:
                            auth.load_credentials(self.auth_path)
                        self.assertEqual((caught.exception.kind, caught.exception.exit_code), ("auth", 3))
                        self.assertIn("renew", str(caught.exception))

    def test_noninteger_expiry_is_unknown(self):
        for expiry in (True, False, "1234", 1.5, None, [], {}):
            with self.subTest(expiry=expiry):
                self.write_auth(token=jwt({"exp": expiry}))
                self.assertIsNone(auth.load_credentials(self.auth_path).expiry)

    def test_bad_jwt_is_only_an_unknown_local_hint(self):
        for token in (None, 1, "opaque", "a.b.c", "a.%%%%.b", jwt([]), jwt("string")):
            with self.subTest(token=token):
                self.assertEqual(auth.jwt_claims(token), {})

    def test_id_token_account_fallback_and_fedramp_rejection(self):
        claims = {"https://api.openai.com/auth": {"chatgpt_account_id": ACCOUNT}}
        self.write_auth(tokens={"access_token": SYNTHETIC_SECRET, "id_token": jwt(claims)})
        self.assertEqual(auth.load_credentials(self.auth_path).account_id, ACCOUNT)
        claims["https://api.openai.com/auth"]["chatgpt_account_is_fedramp"] = True
        self.write_auth(tokens={"access_token": SYNTHETIC_SECRET, "account_id": ACCOUNT, "id_token": jwt(claims)})
        with self.assertRaises(CliError) as caught:
            auth.load_credentials(self.auth_path)
        self.assertIn("FedRAMP", str(caught.exception))
        self.assertEqual(caught.exception.exit_code, 3)

    def test_unsupported_auth_modes_and_api_keys_rejected(self):
        for fields in ({"auth_mode": "apikey"}, {"auth_mode": "api_key"}, {"auth_mode": "unknown"},
                       {"OPENAI_API_KEY": "synthetic-api-key"}, {"tokens": None}, {"tokens": []}):
            with self.subTest(fields=fields):
                self.write_auth(**fields)
                with self.assertRaises(CliError) as caught:
                    auth.load_credentials(self.auth_path)
                self.assertEqual(caught.exception.exit_code, 3)
                self.assert_secret_absent(str(caught.exception))

    def test_missing_auth_mode_accepted_for_existing_chatgpt_file(self):
        self.write_auth(auth_mode=None)
        self.assertEqual(auth.load_credentials(self.auth_path).account_id, ACCOUNT)

    def test_header_injection_and_nonascii_values_rejected(self):
        for value in ("x\r\nInjected: yes", "x\n", "a b", "a\t", "\x00", "é", "", None, 22, "x" * 65537):
            for field in ("token", "account"):
                with self.subTest(field=field, value=repr(value)[:30]):
                    self.write_auth(**{field: value})
                    with self.assertRaises(CliError) as caught:
                        auth.load_credentials(self.auth_path)
                    self.assertEqual(caught.exception.exit_code, 3)
                    self.assert_secret_absent(str(caught.exception))

    def test_malformed_and_nonobject_auth_get_safe_auth_errors(self):
        for raw in (b"not-json " + SYNTHETIC_SECRET.encode(), b"[]", b"null", b"42", b"\xff"):
            with self.subTest(raw=raw):
                self.auth_path.write_bytes(raw)
                with self.assertRaises(CliError) as caught:
                    auth.load_credentials(self.auth_path)
                self.assertEqual(caught.exception.exit_code, 3)
                self.assert_secret_absent(str(caught.exception))

    def test_missing_oversized_and_directory_auth_rejected(self):
        missing = self.root / "missing.json"
        for path in (missing, self.root):
            with self.subTest(path=path):
                with self.assertRaises(CliError) as caught:
                    auth.load_credentials(path)
                self.assertEqual(caught.exception.exit_code, 3)
        with mock.patch.object(auth, "MAX_AUTH_BYTES", 1):
            with self.assertRaises(CliError) as caught:
                auth.load_credentials(self.auth_path)
            self.assertEqual(caught.exception.exit_code, 3)

    def test_permissions_warning_is_advisory(self):
        if os.name != "posix":
            self.skipTest("POSIX file permissions only")
        self.auth_path.chmod(0o640)
        self.assertTrue(auth.load_credentials(self.auth_path).permissions_warning)

    def test_regular_file_reader_enforces_bound(self):
        path = self.root / "sample"
        path.write_bytes(b"12345")
        self.assertEqual(auth.read_regular_file(path, 5, "test")[0], b"12345")
        with self.assertRaises(CliError):
            auth.read_regular_file(path, 4, "test")

    def test_fifo_read_is_nonblocking_and_rejected(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO unavailable")
        path = self.root / "pipe"
        os.mkfifo(path)
        with self.assertRaises(CliError) as caught:
            auth.read_regular_file(path, 32, "test")
        self.assertIn("regular file", str(caught.exception))

    def test_explicit_tilde_auth_path_expands_under_isolated_home(self):
        self.assertEqual(auth.load_credentials(Path("~/auth.json")).account_id, ACCOUNT)
