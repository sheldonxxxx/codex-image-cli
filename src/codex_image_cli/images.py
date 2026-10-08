import base64
import json
import re
import struct
import zlib
from pathlib import Path

from .auth import read_regular_file
from .errors import CliError

MAX_REFERENCES = 5  # Matches the reviewed official Codex image tool.
MAX_REFERENCE_BYTES = 25 * 1024 * 1024  # Our local safety cap, not a backend claim.
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 48 * 1024 * 1024
MAX_IMAGE_PIXELS = 100_000_000  # Local decompression-risk guard, not a backend limit.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def reference(path: Path) -> dict:
    data, _ = read_regular_file(path, MAX_REFERENCE_BYTES, "reference image")
    if data.startswith(PNG_SIGNATURE):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise CliError("Reference image must have PNG, JPEG, or WebP file content.")
    return {"image_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}


def validate_png(data: bytes) -> tuple[int, int]:
    """Validate PNG framing, chunk CRCs and dimensions, not a full pixel decode."""
    if not data.startswith(PNG_SIGNATURE):
        raise CliError("The service returned non-PNG image bytes.", kind="response", exit_code=5)
    offset, width, height, saw_idat = 8, 0, 0, False
    first = True
    while offset + 12 <= len(data):
        size = int.from_bytes(data[offset:offset + 4], "big")
        end = offset + 12 + size
        if end > len(data):
            break
        tag = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + size]
        crc = int.from_bytes(data[offset + 8 + size:end], "big")
        if zlib.crc32(tag + body) & 0xFFFFFFFF != crc:
            break
        if first:
            if tag != b"IHDR" or size != 13:
                break
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", body)
            depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8),
                      4: (8, 16), 6: (8, 16)}
            if (not 0 < width <= 0x7FFFFFFF or not 0 < height <= 0x7FFFFFFF
                    or width * height > MAX_IMAGE_PIXELS
                    or depth not in depths.get(color, ())
                    or compression != 0 or filtering != 0 or interlace not in (0, 1)):
                break
            first = False
        elif tag == b"IHDR":
            break
        if tag == b"IDAT":
            saw_idat = True
        if tag == b"IEND":
            if size == 0 and end == len(data) and saw_idat:
                return width, height
            break
        offset = end
    raise CliError("The service returned an incomplete or corrupt PNG.",
                   kind="response", exit_code=5)


def decode_response(raw: bytes) -> tuple[bytes, dict]:
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        items = value.get("data")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError
        encoded = items[0].get("b64_json")
        if not isinstance(encoded, str) or len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
            raise ValueError
        image = base64.b64decode(encoded, validate=True)
        if len(image) > MAX_IMAGE_BYTES:
            raise ValueError
    except (ValueError, UnicodeError):
        raise CliError("Unexpected image response; expected one bounded base64 PNG. "
                       "The internal backend protocol may have changed. No retry was made.",
                       kind="response", exit_code=5) from None
    width, height = validate_png(image)
    metadata = {"width": width, "height": height, "bytes": len(image)}
    # Never emit arbitrary server strings or the request prompt/reference bytes.
    if value.get("background") in ("transparent", "opaque", "auto"):
        metadata["background"] = value["background"]
    if value.get("quality") in ("low", "medium", "high", "auto"):
        metadata["quality"] = value["quality"]
    if isinstance(value.get("size"), str) and re.fullmatch(r"auto|[0-9]{1,5}x[0-9]{1,5}", value["size"]):
        metadata["size"] = value["size"]
    return image, metadata
