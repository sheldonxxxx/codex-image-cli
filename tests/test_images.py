import base64
import json
import struct
from unittest import mock

from codex_image_cli import images
from codex_image_cli.errors import CliError
from helpers import SYNTHETIC_SECRET, IsolatedTestCase, chunk, png, response_bytes


class ImageTests(IsolatedTestCase):
    def test_reference_magic_not_filename_determines_mime(self):
        for magic, mime in [(png(), "image/png"), (b"\xff\xd8\xffsynthetic", "image/jpeg"),
                            (b"RIFF\x00\x00\x00\x00WEBPsynthetic", "image/webp")]:
            with self.subTest(mime=mime):
                path = self.root / "misleading.txt"
                path.write_bytes(magic)
                value = images.reference(path)
                self.assertEqual(set(value), {"image_url"})
                self.assertEqual(value["image_url"], "data:" + mime + ";base64," + base64.b64encode(magic).decode())

    def test_unsupported_reference_and_cap(self):
        path = self.root / "fake.png"
        for data in (b"", b"GIF89a", b"RIFFshort", b"not an image"):
            path.write_bytes(data)
            with self.assertRaises(CliError):
                images.reference(path)
        path.write_bytes(png())
        with mock.patch.object(images, "MAX_REFERENCE_BYTES", len(png()) - 1):
            with self.assertRaises(CliError) as caught:
                images.reference(path)
            self.assertIn("safety limit", str(caught.exception))

    def test_valid_png_and_safe_metadata_allowlist(self):
        image = png(2, 3)
        raw = response_bytes(image, background="transparent", quality="high", size="2x3",
                             prompt=SYNTHETIC_SECRET, revised_prompt=SYNTHETIC_SECRET,
                             model=SYNTHETIC_SECRET, arbitrary=SYNTHETIC_SECRET)
        actual, metadata = images.decode_response(raw)
        self.assertEqual(actual, image)
        self.assertEqual(metadata, {"width": 2, "height": 3, "bytes": len(image),
                                    "background": "transparent", "quality": "high", "size": "2x3"})
        self.assertNotIn(SYNTHETIC_SECRET, str(metadata))

    def test_unknown_server_metadata_is_never_echoed(self):
        _, metadata = images.decode_response(response_bytes(background=SYNTHETIC_SECRET,
                                                            quality=SYNTHETIC_SECRET,
                                                            size=SYNTHETIC_SECRET))
        self.assertEqual(set(metadata), {"width", "height", "bytes"})

    def test_unexpected_response_shapes_and_base64(self):
        fixtures = [b"not-json", b"\xff", b"[]", b"null", b"{}", b'{"data": []}',
                    b'{"data": [1]}', b'{"data": [{}, {}]}', b'{"data": [{"url": "https://evil.invalid"}]}',
                    b'{"data": [{"b64_json": 1}]}', b'{"data": [{"b64_json": "a==="}]}',
                    b'{"data": [{"b64_json": "\xff"}]}']
        for raw in fixtures:
            with self.subTest(raw=raw):
                with self.assertRaises(CliError) as caught:
                    images.decode_response(raw)
                self.assertEqual((caught.exception.kind, caught.exception.exit_code), ("response", 5))

    def test_encoded_and_decoded_image_caps(self):
        # 4 encoded characters are permitted for a 1-byte cap, but can decode to 3 bytes.
        with mock.patch.object(images, "MAX_IMAGE_BYTES", 1):
            for data in (b"ab", b"abc", b"abcd", png()):
                with self.subTest(size=len(data)):
                    with self.assertRaises(CliError) as caught:
                        images.decode_response(response_bytes(data))
                    self.assertEqual(caught.exception.exit_code, 5)

    def test_reject_non_png_corrupt_truncated_and_missing_chunks(self):
        good = png()
        header = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        zero_header = chunk(b"IHDR", struct.pack(">IIBBBBB", 0, 1, 8, 6, 0, 0, 0))
        corrupt = good[:-5] + bytes([good[-5] ^ 1]) + good[-4:]
        fixtures = [b"JPEG", good[:-1], good + b"junk", corrupt, images.PNG_SIGNATURE,
                    images.PNG_SIGNATURE + header + chunk(b"IEND"),
                    images.PNG_SIGNATURE + chunk(b"IDAT") + chunk(b"IEND"),
                    images.PNG_SIGNATURE + zero_header + chunk(b"IDAT") + chunk(b"IEND"),
                    images.PNG_SIGNATURE + header + header + chunk(b"IDAT") + chunk(b"IEND"),
                    images.PNG_SIGNATURE + header + chunk(b"IDAT") + chunk(b"IEND", b"x")]
        for data in fixtures:
            with self.subTest(data=data[:20], length=len(data)):
                with self.assertRaises(CliError) as caught:
                    images.decode_response(response_bytes(data))
                self.assertEqual(caught.exception.exit_code, 5)

    def test_invalid_ihdr_and_declared_pixel_limits_rejected(self):
        valid = (1, 1, 8, 6, 0, 0, 0)
        variants = [(0, 1, 8, 6, 0, 0, 0), (1, 0, 8, 6, 0, 0, 0),
                    (0x80000000, 1, 8, 6, 0, 0, 0), (1, 0x80000000, 8, 6, 0, 0, 0),
                    (10001, 10000, 8, 6, 0, 0, 0), (1, 1, 1, 6, 0, 0, 0),
                    (1, 1, 8, 1, 0, 0, 0), (1, 1, 8, 6, 1, 0, 0),
                    (1, 1, 8, 6, 0, 1, 0), (1, 1, 8, 6, 0, 0, 2)]
        for fields in variants:
            with self.subTest(fields=fields):
                data = images.PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", *fields)) + chunk(b"IDAT") + chunk(b"IEND")
                with self.assertRaises(CliError) as caught:
                    images.validate_png(data)
                self.assertEqual(caught.exception.exit_code, 5)
