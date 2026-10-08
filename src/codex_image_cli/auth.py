"""Read existing auth on every invocation. No login, refresh, or credential writes."""

import base64
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat
import time

from .errors import CliError, RENEW_SESSION

MAX_AUTH_BYTES = 1024 * 1024


def default_auth_path() -> Path:
    home = os.environ.get("CODEX_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".codex") / "auth.json"


def read_regular_file(path: Path, limit: int, label: str) -> tuple[bytes, int]:
    """Bound reads, including growing files, and never block on a named pipe."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise CliError(f"{label} must be a regular file.")
            if info.st_size > limit:
                raise CliError(f"{label} exceeds the local {limit}-byte safety limit.")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise CliError(f"{label} exceeds the local {limit}-byte safety limit.")
            return data, stat.S_IMODE(info.st_mode)
    except OSError:
        raise CliError(f"Cannot read {label}; check its path and permissions.") from None


def jwt_claims(token: object) -> dict:
    """Unverified local hints only; the service is the authentication authority."""
    if not isinstance(token, str):
        return {}
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        data = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        value = json.loads(data)
        return value if isinstance(value, dict) else {}
    except (ValueError, UnicodeError):
        return {}


def _header_value(value: object) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 64 * 1024
            and all(33 <= ord(char) <= 126 for char in value))


@dataclass(frozen=True, repr=False)
class Credentials:
    access_token: str = field(repr=False)
    account_id: str = field(repr=False)
    expiry: int | None = None
    permissions_warning: bool = False


def load_credentials(path: Path) -> Credentials:
    try:
        raw, mode = read_regular_file(path.expanduser(), MAX_AUTH_BYTES, "Codex auth file")
    except CliError:
        raise CliError(
            "Cannot read a valid file-backed Codex ChatGPT login. Check --auth-file or "
            "CODEX_HOME. Keychain-only logins are not supported by this CLI. " + RENEW_SESSION,
            kind="auth", exit_code=3,
        ) from None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise CliError("Codex auth file is not valid JSON. " + RENEW_SESSION,
                       kind="auth", exit_code=3) from None
    if not isinstance(data, dict):
        raise CliError("Codex auth file must be a JSON object.", kind="auth", exit_code=3)
    if data.get("auth_mode") not in (None, "chatgpt") or data.get("OPENAI_API_KEY"):
        raise CliError("This CLI requires a Codex ChatGPT OAuth login, not API-key or "
                       "other authentication.", kind="auth", exit_code=3)
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        raise CliError("No ChatGPT tokens found in the Codex auth file. " + RENEW_SESSION,
                       kind="auth", exit_code=3)
    token = tokens.get("access_token")
    claims = jwt_claims(tokens.get("id_token"))
    account_claims = claims.get("https://api.openai.com/auth", {})
    if not isinstance(account_claims, dict):
        account_claims = {}
    if account_claims.get("chatgpt_account_is_fedramp"):
        raise CliError("Government/FedRAMP routing is not supported. Use the official "
                       "Codex client for this account.", kind="auth", exit_code=3)
    account = tokens.get("account_id") or account_claims.get("chatgpt_account_id")
    if not _header_value(token) or not _header_value(account):
        raise CliError("Missing or invalid access token/account ID in the Codex auth file. "
                       + RENEW_SESSION, kind="auth", exit_code=3)
    expiry = jwt_claims(token).get("exp")
    if isinstance(expiry, bool) or not isinstance(expiry, int):
        expiry = None
    if expiry is not None and expiry <= time.time():
        raise CliError("The local access-token expiry hint is in the past. " + RENEW_SESSION,
                       kind="auth", exit_code=3)
    return Credentials(token, account, expiry,
                       os.name == "posix" and bool(mode & 0o077))
