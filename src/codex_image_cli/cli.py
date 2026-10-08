import argparse
import json
import math
from pathlib import Path
import re
import sys

from . import __version__
from .auth import default_auth_path, load_credentials, read_regular_file
from .client import BASE_URL, request_image
from .errors import CliError
from .images import MAX_REFERENCES, reference
from .output import OutputTarget

MAX_PROMPT_BYTES = 128 * 1024


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="codex-image",
        description="Unofficial standalone Codex image CLI. Uses an existing file-backed "
                    "ChatGPT login; never refreshes or writes credentials.",
    )
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("generate", "edit", "doctor"):
        command = commands.add_parser(name, help={
            "generate": "Generate one PNG", "edit": "Edit from local reference images",
            "doctor": "Check local credentials only; sends no network request",
        }[name])
        command.add_argument("--auth-file", type=Path, help="Existing Codex auth.json "
                             "(default: CODEX_HOME/auth.json or ~/.codex/auth.json)")
        command.add_argument("--json", action="store_true", help="Print a safe JSON result")
        if name == "doctor":
            continue
        prompt = command.add_mutually_exclusive_group(required=True)
        prompt.add_argument("--prompt", help="Standalone image instructions (visible to shell history)")
        prompt.add_argument("--prompt-file", type=Path, help="UTF-8 prompt file (recommended)")
        command.add_argument("--output", type=Path, required=True, help="New .png path; never overwritten")
        command.add_argument("--model", default="gpt-image-2", help="Default: gpt-image-2")
        command.add_argument("--quality", choices=("auto", "low", "medium", "high"), default="auto")
        command.add_argument("--size", default="auto", help="auto or WIDTHxHEIGHT; backend decides availability")
        command.add_argument("--background", choices=("auto", "transparent", "opaque"), default="opaque")
        command.add_argument("--timeout", type=float, default=300, help="Socket timeout in seconds (default 300)")
        if name == "edit":
            command.add_argument("--image", type=Path, required=True, action="append",
                                 help="Local PNG/JPEG/WebP; repeat up to 5 times")
    return result


def build_payload(args) -> dict:
    if not math.isfinite(args.timeout) or not 1 <= args.timeout <= 3600:
        raise CliError("--timeout must be finite and between 1 and 3600 seconds.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", args.model):
        raise CliError("--model must be a simple model identifier.")
    if not re.fullmatch(r"auto|[1-9][0-9]{0,4}x[1-9][0-9]{0,4}", args.size):
        raise CliError("--size must be auto or WIDTHxHEIGHT with positive integer dimensions.")
    if args.prompt_file:
        raw, _ = read_regular_file(args.prompt_file.expanduser(), MAX_PROMPT_BYTES, "prompt file")
        try:
            prompt = raw.decode("utf-8")
        except UnicodeError:
            raise CliError("Prompt file must be UTF-8.") from None
    else:
        prompt = args.prompt
    if not prompt.strip() or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise CliError("Prompt must be nonempty and no more than 128 KiB of UTF-8.")
    payload = {"prompt": prompt, "model": args.model, "quality": args.quality,
               "size": args.size, "background": args.background}
    if args.command == "edit":
        if not 1 <= len(args.image) <= MAX_REFERENCES:
            raise CliError("Edit requires between 1 and 5 reference images.")
        payload["images"] = [reference(path.expanduser()) for path in args.image]
    return payload


def run(args) -> dict:
    # Fail local payload/output validation before touching auth or sending a request.
    if args.command == "doctor":
        credentials = load_credentials(args.auth_file or default_auth_path())
        return {"ok": True, "command": "doctor", "network_checked": False,
                "auth": "file-backed ChatGPT session found",
                "expiry_hint": "future" if credentials.expiry else "unknown",
                "permissions_warning": credentials.permissions_warning,
                "endpoint": BASE_URL,
                "note": "Offline check only; account access, entitlement and backend compatibility are untested."}
    payload = build_payload(args)
    with OutputTarget(args.output) as output:
        credentials = load_credentials(args.auth_file or default_auth_path())
        image, metadata = request_image(args.command, payload, credentials, args.timeout)
        path = output.save(image)
    return {"ok": True, "command": args.command, "output": str(path),
            "permissions_warning": credentials.permissions_warning, **metadata}


def _report_error(as_json: bool, kind: str, message: str) -> None:
    if as_json:
        print(json.dumps({"ok": False, "error": kind, "message": message}, ensure_ascii=True),
              file=sys.stderr)
    else:
        print(f"codex-image: {message}", file=sys.stderr)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        result = run(args)
        if args.json:
            print(json.dumps(result, ensure_ascii=True))
        elif args.command == "doctor":
            print("Local file-backed ChatGPT session found. No network check was made.")
            print("Account access, entitlement and backend compatibility remain untested.")
        else:
            print(result["output"])
        if result.get("permissions_warning"):
            print("Warning: the auth file permits group/other access; review its permissions.", file=sys.stderr)
        return 0
    except CliError as error:
        _report_error(args.json, error.kind, str(error))
        return error.exit_code
    except KeyboardInterrupt:
        _report_error(args.json, "interrupted", "Interrupted. If a request was sent, it may "
                      "still complete; retrying may use quota again.")
        return 130
    except Exception:
        # Never print a traceback that might capture headers, tokens or image data.
        _report_error(args.json, "internal", "Unexpected local failure. No automatic retry "
                      "was made. If a request was sent, check before retrying.")
        return 1
