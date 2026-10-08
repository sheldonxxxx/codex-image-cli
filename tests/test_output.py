import os
from pathlib import Path
from unittest import mock

from codex_image_cli import output
from codex_image_cli.errors import CliError
from helpers import SYNTHETIC_SECRET, IsolatedTestCase, png


class OutputTests(IsolatedTestCase):
    def test_new_output_published_exactly_and_staging_removed(self):
        path = self.root / "result.png"
        with output.OutputTarget(path) as target:
            staging = target.temp_path
            self.assertTrue(staging.exists())
            if os.name == "posix":
                self.assertEqual(staging.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.save(png()), path.absolute())
        self.assertEqual(path.read_bytes(), png())
        self.assertFalse(staging.exists())
        self.assertEqual(list(self.root.glob(".codex-image-*")), [])

    def test_existing_file_directory_and_symlinks_are_not_overwritten(self):
        existing = self.root / "existing.png"
        existing.write_bytes(b"existing")
        directory = self.root / "directory.png"
        directory.mkdir()
        dangling = self.root / "dangling.png"
        dangling.symlink_to(self.root / "missing")
        pointing = self.root / "pointing.png"
        pointing.symlink_to(existing)
        for path in (existing, directory, dangling, pointing):
            with self.subTest(path=path):
                with self.assertRaises(CliError) as caught:
                    output.OutputTarget(path)
                self.assertIn("already exists", str(caught.exception))
        self.assertEqual(existing.read_bytes(), b"existing")
        self.assertTrue(dangling.is_symlink())
        self.assertTrue(pointing.is_symlink())

    def test_extension_and_existing_parent_required(self):
        for path in (self.root / "x.jpg", self.root / "missing" / "x.png"):
            with self.subTest(path=path):
                with self.assertRaises(CliError) as caught:
                    output.OutputTarget(path)
                self.assertEqual(caught.exception.exit_code, 2)
        self.assertEqual(list(self.root.glob(".codex-image-*")), [])

    def test_prepare_error_is_curated(self):
        with mock.patch.object(output.tempfile, "mkstemp", side_effect=PermissionError(SYNTHETIC_SECRET)):
            with self.assertRaises(CliError) as caught:
                output.OutputTarget(self.root / "result.png")
        self.assertEqual(caught.exception.exit_code, 6)
        self.assert_secret_absent(str(caught.exception))

    def test_request_failure_cleans_staging(self):
        with self.assertRaisesRegex(RuntimeError, "synthetic request failed"):
            with output.OutputTarget(self.root / "result.png") as target:
                staging = target.temp_path
                raise RuntimeError("synthetic request failed")
        self.assertFalse(staging.exists())
        self.assertFalse((self.root / "result.png").exists())

    def test_atomic_publish_race_keeps_destination_and_recovery_file(self):
        for race_type in ("file", "symlink"):
            with self.subTest(race_type=race_type):
                path = self.root / (race_type + ".png")
                protected = self.root / (race_type + "-protected")
                protected.write_bytes(b"protected")
                with self.assertRaises(CliError) as caught:
                    with output.OutputTarget(path) as target:
                        staging = target.temp_path
                        if race_type == "file":
                            path.write_bytes(b"race winner")
                        else:
                            path.symlink_to(protected)
                        target.save(png())
                self.assertEqual(caught.exception.exit_code, 6)
                self.assertEqual(staging.read_bytes(), png())
                self.assertIn(str(staging), str(caught.exception))
                self.assertIn("do not regenerate", str(caught.exception))
                self.assertEqual(path.read_bytes(), b"race winner" if race_type == "file" else b"protected")
                self.assertEqual(protected.read_bytes(), b"protected")
                if race_type == "symlink":
                    self.assertTrue(path.is_symlink())

    def test_link_failure_preserves_complete_image_for_recovery(self):
        path = self.root / "result.png"
        with mock.patch.object(output.os, "link", side_effect=OSError(SYNTHETIC_SECRET)):
            with self.assertRaises(CliError) as caught:
                with output.OutputTarget(path) as target:
                    staging = target.temp_path
                    target.save(png())
        self.assertEqual(staging.read_bytes(), png())
        self.assertFalse(path.exists())
        self.assertEqual(caught.exception.exit_code, 6)
        self.assert_secret_absent(str(caught.exception))

    def test_fsync_failure_preserves_recovery(self):
        path = self.root / "result.png"
        with mock.patch.object(output.os, "fsync", side_effect=OSError(SYNTHETIC_SECRET)):
            with self.assertRaises(CliError) as caught:
                with output.OutputTarget(path) as target:
                    staging = target.temp_path
                    target.save(png())
        self.assertEqual(staging.read_bytes(), png())
        self.assertIn(str(staging), str(caught.exception))
        self.assertFalse(path.exists())

    def test_write_or_flush_failure_and_close_failure_keep_original_recovery_error(self):
        for failing_method in ("write", "flush"):
            with self.subTest(method=failing_method):
                target = output.OutputTarget(self.root / (failing_method + ".png"))
                target.stream.close()
                stream = mock.Mock()
                getattr(stream, failing_method).side_effect = OSError(SYNTHETIC_SECRET)
                stream.close.side_effect = OSError("synthetic second close failure")
                target.stream = stream
                with self.assertRaises(CliError) as caught:
                    with target:
                        target.save(png())
                self.assertEqual(caught.exception.exit_code, 6)
                self.assertIn("request completed", str(caught.exception))
                self.assertIn(str(target.temp_path), str(caught.exception))
                self.assertTrue(target.temp_path.exists())
                self.assert_secret_absent(str(caught.exception))

    def test_close_failure_without_other_exception_is_curated(self):
        target = output.OutputTarget(self.root / "result.png")
        target.stream.close()
        target.stream = mock.Mock()
        target.stream.close.side_effect = OSError(SYNTHETIC_SECRET)
        with self.assertRaises(CliError) as caught:
            with target:
                pass
        self.assertEqual(caught.exception.exit_code, 6)
        self.assertIn(str(target.temp_path), str(caught.exception))
        self.assertTrue(target.temp_path.exists())
        self.assert_secret_absent(str(caught.exception))

    def test_cleanup_failure_does_not_hide_success_or_repeat_request(self):
        path = self.root / "result.png"
        with output.OutputTarget(path) as target:
            target.save(png())
            with mock.patch.object(Path, "unlink", side_effect=OSError(SYNTHETIC_SECRET)):
                target.__exit__(None, None, None)
            self.assertEqual(path.read_bytes(), png())
