import base64
import contextlib
import io
import json
import os
from pathlib import Path
from unittest import mock

from codex_image_cli import cli
from codex_image_cli.errors import CliError
from helpers import ACCOUNT, SYNTHETIC_SECRET, IsolatedTestCase, png


class CliTests(IsolatedTestCase):
    def args(self, command="generate", *extra):
        return [command, "--prompt", "A tiny café", "--output", str(self.root / "result.png"), *extra]

    def test_default_payload_is_standalone_gpt_image_2(self):
        args = cli.parser().parse_args(self.args())
        self.assertEqual(cli.build_payload(args), {"prompt": "A tiny café", "model": "gpt-image-2",
            "quality": "auto", "size": "auto", "background": "opaque"})
        self.assertEqual(args.timeout, 300)

    def test_prompt_file_unicode_and_whitespace_preserved(self):
        path = self.root / "prompt.txt"
        path.write_text("  圖像\nA café\n", encoding="utf-8")
        args = cli.parser().parse_args(["generate", "--prompt-file", str(path), "--output", str(self.root / "result.png")])
        self.assertEqual(cli.build_payload(args)["prompt"], "  圖像\nA café\n")

    def test_invalid_prompt_files_and_byte_cap(self):
        path = self.root / "prompt.txt"
        for raw in (b"", b" \n\t", b"\xff", b"x" * (cli.MAX_PROMPT_BYTES + 1)):
            with self.subTest(length=len(raw)):
                path.write_bytes(raw)
                args = cli.parser().parse_args(["generate", "--prompt-file", str(path), "--output", str(self.root / "out.png")])
                with self.assertRaises(CliError):
                    cli.build_payload(args)
        # Limits are UTF-8 bytes, rather than Python character count.
        args = cli.parser().parse_args(self.args("generate", "--prompt", "é" * (cli.MAX_PROMPT_BYTES // 2 + 1)))
        with self.assertRaises(CliError):
            cli.build_payload(args)

    def test_prompt_file_and_inline_prompt_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                cli.parser().parse_args(self.args("generate", "--prompt-file", "anything"))
        self.assertEqual(caught.exception.code, 2)

    def test_required_explicit_output_and_edit_reference(self):
        for args in (["generate", "--prompt", "hello"], ["generate", "--output", "out.png"], self.args("edit")):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(args)
                self.assertEqual(caught.exception.code, 2)

    def test_five_references_preserve_order_and_six_fail_before_auth(self):
        paths = []
        for number in range(6):
            path = self.root / (str(number) + ".png")
            path.write_bytes(png(number + 1, 1))
            paths += ["--image", str(path)]
        args = cli.parser().parse_args(self.args("edit", *paths[:10]))
        payload = cli.build_payload(args)
        self.assertEqual(len(payload["images"]), 5)
        for number, image in enumerate(payload["images"]):
            self.assertEqual(base64.b64decode(image["image_url"].split(",")[1]), png(number + 1, 1))
        with mock.patch.object(cli, "load_credentials") as load, mock.patch.object(cli, "request_image") as request:
            code, stdout, stderr = self.invoke(self.args("edit", *paths))
        self.assertEqual(code, 2)
        self.assertIn("between 1 and 5", stderr)
        load.assert_not_called()
        request.assert_not_called()

    def test_invalid_timeout_model_size_fail_locally(self):
        fixtures = [("--timeout", value) for value in ("nan", "inf", "0", "3601", "-1")]
        fixtures += [("--model", value) for value in ("../model", "model space", "x" * 129, "é")]
        fixtures += [("--size", value) for value in ("0x1", "1x0", "-1x1", "1X1", "1x1x1", "100000x1")]
        for flag, value in fixtures:
            with self.subTest(flag=flag, value=value), mock.patch.object(cli, "load_credentials") as load, \
                 mock.patch.object(cli, "request_image") as request:
                code, stdout, stderr = self.invoke(self.args("generate", flag + "=" + value))
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                load.assert_not_called()
                request.assert_not_called()

    def test_existing_output_fails_before_auth_and_network(self):
        path = self.root / "result.png"
        path.write_bytes(b"original")
        with mock.patch.object(cli, "load_credentials") as load, mock.patch.object(cli, "request_image") as request:
            code, stdout, stderr = self.invoke(self.args())
        self.assertEqual(code, 2)
        self.assertEqual(path.read_bytes(), b"original")
        load.assert_not_called()
        request.assert_not_called()

    def test_generate_json_success_and_no_secret_or_prompt_output(self):
        metadata = {"width": 1, "height": 1, "bytes": len(png()), "turn_id": "synthetic-turn"}
        with mock.patch.object(cli, "request_image", return_value=(png(), metadata)) as request:
            code, stdout, stderr = self.invoke(self.args("generate", "--json"))
        self.assertEqual(code, 0)
        value = json.loads(stdout)
        self.assertTrue(value["ok"])
        self.assertEqual(value["command"], "generate")
        self.assertEqual(value["output"], str(self.root / "result.png"))
        self.assertEqual(Path(value["output"]).read_bytes(), png())
        self.assertEqual(value["turn_id"], "synthetic-turn")
        self.assertEqual(stderr, "")
        self.assert_secret_absent(stdout, stderr)
        self.assertNotIn("A tiny", stdout)
        self.assertEqual(request.call_args.args[2].access_token, SYNTHETIC_SECRET)
        request.assert_called_once()

    def test_plain_generate_outputs_only_path(self):
        with mock.patch.object(cli, "request_image", return_value=(png(), {})):
            code, stdout, stderr = self.invoke(self.args())
        self.assertEqual(code, 0)
        self.assertEqual(stdout, str(self.root / "result.png") + "\n")
        self.assertEqual(stderr, "")

    def test_doctor_is_offline_read_only_and_json(self):
        before = self.auth_path.read_bytes()
        with mock.patch.object(cli, "request_image", side_effect=AssertionError("doctor must remain offline")) as request:
            code, stdout, stderr = self.invoke(["doctor", "--json"])
        self.assertEqual(code, 0)
        value = json.loads(stdout)
        self.assertTrue(value["ok"])
        self.assertFalse(value["network_checked"])
        self.assertEqual(value["command"], "doctor")
        self.assertEqual(value["expiry_hint"], "unknown")
        self.assertIn("untested", value["note"])
        self.assertEqual(stderr, "")
        self.assertEqual(self.auth_path.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["auth.json"])
        self.assert_secret_absent(stdout, stderr)
        request.assert_not_called()

    def test_plain_doctor_explicit_auth_path(self):
        with mock.patch.object(cli, "default_auth_path", side_effect=AssertionError("explicit auth must win")):
            code, stdout, stderr = self.invoke(["doctor", "--auth-file", str(self.auth_path)])
        self.assertEqual(code, 0)
        self.assertIn("No network", stdout)
        self.assertIn("untested", stdout)
        self.assertEqual(stderr, "")

    def test_doctor_permission_warning_does_not_break_stdout_json(self):
        if os.name != "posix":
            self.skipTest("POSIX permission warning")
        self.auth_path.chmod(0o644)
        code, stdout, stderr = self.invoke(["doctor", "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout)["permissions_warning"])
        self.assertIn("group/other", stderr)
        self.assert_secret_absent(stdout, stderr)

    def test_auth_failure_does_not_send_request_or_leave_output(self):
        self.auth_path.unlink()
        with mock.patch.object(cli, "request_image") as request:
            code, stdout, stderr = self.invoke(self.args("generate", "--json"))
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(stderr)["error"], "auth")
        self.assertEqual(stdout, "")
        self.assertFalse((self.root / "result.png").exists())
        self.assertEqual(list(self.root.glob(".codex-image-*")), [])
        request.assert_not_called()

    def test_curated_exit_codes_and_json_error_reports(self):
        for kind, status in [("validation", 2), ("auth", 3), ("http", 4), ("network", 4), ("response", 5), ("output", 6)]:
            with self.subTest(kind=kind), mock.patch.object(cli, "run", side_effect=CliError("Safe message", kind=kind, exit_code=status)):
                code, stdout, stderr = self.invoke(["doctor", "--json"])
                self.assertEqual(code, status)
                self.assertEqual(stdout, "")
                self.assertEqual(json.loads(stderr), {"ok": False, "error": kind, "message": "Safe message"})

    def test_unexpected_and_interrupt_json_errors_never_print_exception_details(self):
        for error, status, kind in [(RuntimeError(SYNTHETIC_SECRET + ACCOUNT), 1, "internal"),
                                    (KeyboardInterrupt(SYNTHETIC_SECRET), 130, "interrupted")]:
            for as_json in (False, True):
                with self.subTest(error=type(error).__name__, as_json=as_json), mock.patch.object(cli, "run", side_effect=error):
                    code, stdout, stderr = self.invoke(["doctor"] + (["--json"] if as_json else []))
                    self.assertEqual(code, status)
                    self.assertEqual(stdout, "")
                    self.assert_secret_absent(stderr)
                    self.assertNotIn("Traceback", stderr)
                    if as_json:
                        self.assertEqual(json.loads(stderr)["error"], kind)
                    else:
                        self.assertTrue(stderr.startswith("codex-image:"))

    def test_network_failure_cleans_staging_and_never_retries(self):
        with mock.patch.object(cli, "request_image", side_effect=CliError("Safe network failure", kind="network", exit_code=4)) as request:
            code, stdout, stderr = self.invoke(self.args("generate", "--json"))
        self.assertEqual(code, 4)
        self.assertFalse((self.root / "result.png").exists())
        self.assertEqual(list(self.root.glob(".codex-image-*")), [])
        request.assert_called_once()
