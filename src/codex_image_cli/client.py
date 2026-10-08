"""Fixed destination, TLS verification, no redirects/proxies/automatic retries."""

import http.client
import json
import socket
import ssl
import urllib.error
import urllib.request
from uuid import uuid4

from . import __version__
from .auth import Credentials
from .errors import CliError, RENEW_SESSION
from .images import MAX_RESPONSE_BYTES, decode_response

BASE_URL = "https://chatgpt.com/backend-api/codex"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_opener():
    context = ssl.create_default_context()
    context.keylog_filename = None  # Do not log session-decrypting TLS keys from env settings.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),  # Do not transmit session credentials via env proxies.
        NoRedirect(),
        urllib.request.HTTPSHandler(context=context),
    )


def _http_error(status: int) -> CliError:
    if status == 401:
        return CliError("ChatGPT rejected the session (HTTP 401). " + RENEW_SESSION,
                        kind="auth", exit_code=3)
    if 300 <= status < 400:
        message = "The service returned a redirect; it was blocked to protect your session."
    elif status == 403:
        message = "Access denied (HTTP 403). Check account access in official Codex; " \
                  "this CLI cannot determine entitlement or bypass access restrictions."
    elif status == 429:
        message = "The service rejected the request with HTTP 429 (rate or usage limit). " \
                  "Check Codex for account limits and retry manually when appropriate."
    elif status >= 500:
        message = f"The service failed (HTTP {status}); the request may have been processed."
    else:
        message = f"The service rejected the image request (HTTP {status}). " \
                  "Check the options; the internal protocol may have changed."
    return CliError(message + " No automatic retry was made.", kind="http", exit_code=4)


def request_image(operation: str, payload: dict, credentials: Credentials,
                  timeout: float, *, opener=None) -> tuple[bytes, dict]:
    if operation not in ("generate", "edit"):
        raise CliError("Unknown image operation.")
    endpoint = "images/generations" if operation == "generate" else "images/edits"
    turn_id = str(uuid4())
    request = urllib.request.Request(
        f"{BASE_URL}/{endpoint}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {credentials.access_token}",
            "ChatGPT-Account-ID": credentials.account_id,
            "originator": "codex_image_cli",
            "x-codex-image-turn-id": turn_id,
            "User-Agent": f"codex-image-cli/{__version__}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
    )
    try:
        with (opener or build_opener()).open(request, timeout=timeout) as response:
            if response.status != 200:
                raise _http_error(response.status)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise CliError("Image response exceeds the local safety limit. "
                               "No automatic retry was made.", kind="response", exit_code=5)
    except urllib.error.HTTPError as error:
        # Deliberately ignore the body, URL and reason: they can contain private data.
        status = error.code
        try:
            error.close()
        except OSError:
            pass  # Preserve the useful HTTP status even if connection cleanup fails.
        raise _http_error(status) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError,
            http.client.HTTPException, OSError, ValueError):
        raise CliError("Network/TLS failure or timeout. The request may have been processed; "
                       "no automatic retry was made. Retrying manually may use quota again.",
                       kind="network", exit_code=4) from None
    image, metadata = decode_response(raw)
    metadata["turn_id"] = turn_id
    return image, metadata
