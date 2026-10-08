# Reviewed protocol and compatibility

Reviewed on **2026-10-07** against the official `openai/codex` repository at
commit **`bd5564fec447bd42b4e60d051967670d2eaa2aa8`**. This records the specific
source evidence; it does not establish a stable API contract or prove that every
account can use the currently deployed service.

## Source map

- [Provider base URL](https://github.com/openai/codex/blob/bd5564fec447bd42b4e60d051967670d2eaa2aa8/codex-rs/model-provider-info/src/lib.rs): `CHATGPT_CODEX_BASE_URL` is `https://chatgpt.com/backend-api/codex`.
- [Image request/response structs](https://github.com/openai/codex/blob/bd5564fec447bd42b4e60d051967670d2eaa2aa8/codex-rs/codex-api/src/images.rs): JSON generation/edit schemas, enum values and `data[].b64_json`.
- [Image endpoint implementation and tests](https://github.com/openai/codex/blob/bd5564fec447bd42b4e60d051967670d2eaa2aa8/codex-rs/codex-api/src/endpoint/images.rs): JSON POST to `images/generations` and `images/edits`, inline `image_url` references.
- [Image backend headers](https://github.com/openai/codex/blob/bd5564fec447bd42b4e60d051967670d2eaa2aa8/codex-rs/ext/image-generation/src/backend.rs): `x-codex-image-turn-id` and originator handling.
- [Image tool defaults and limits](https://github.com/openai/codex/blob/bd5564fec447bd42b4e60d051967670d2eaa2aa8/codex-rs/ext/image-generation/src/tool.rs): `gpt-image-2`, five references, `quality: auto`, `size: auto`, omitted `n`, PNG result handling.
- [Bearer and account headers](https://github.com/openai/codex/blob/bd5564fec447bd42b4e60d051967670d2eaa2aa8/codex-rs/model-provider/src/bearer_auth_provider.rs): `Authorization: Bearer ...` and `ChatGPT-Account-ID`.
- [Originator handling](https://github.com/openai/codex/blob/bd5564fec447bd42b4e60d051967670d2eaa2aa8/codex-rs/login/src/auth/default_client.rs): `originator` header and official default `codex_cli_rs`.
- [Auth file structure](https://github.com/openai/codex/blob/bd5564fec447bd42b4e60d051967670d2eaa2aa8/codex-rs/login/src/auth/storage.rs) and [token fields/claims](https://github.com/openai/codex/blob/bd5564fec447bd42b4e60d051967670d2eaa2aa8/codex-rs/login/src/token_data.rs): `auth_mode`, `tokens.access_token`, `tokens.account_id`, ID-token claims, expiry hints and FedRAMP flag.

## Implemented wire shape

Generation uses:

```http
POST https://chatgpt.com/backend-api/codex/images/generations
Authorization: Bearer <existing access token>
ChatGPT-Account-ID: <existing account ID>
originator: codex_image_cli
x-codex-image-turn-id: <new UUID per command>
Content-Type: application/json
```

```json
{
  "prompt": "A standalone visual brief",
  "model": "gpt-image-2",
  "quality": "auto",
  "size": "auto",
  "background": "opaque"
}
```

Editing uses `/images/edits` and adds an `images` array of
`{"image_url": "data:image/png;base64,..."}` objects, with appropriate JPEG/WebP MIME
types. It is JSON, not multipart. The CLI deliberately supports local inline
references only; source support for server file IDs is not exposed.

Successful source responses contain a `data` array with `b64_json`, optionally
`generation_id`, plus fields such as `created`, `background`, `quality`, and `size`.
The CLI requires exactly one image and checks bounded PNG bytes for framing, legal
IHDR fields, declared pixel count, and chunk CRCs. This is not a full pixel decode
or validation of every PNG ordering rule. IHDR validation follows the [PNG specification](https://www.w3.org/TR/png/#11IHDR). It ignores response fields it does not
need. It does not request a made-up
`response_format`, `output_format`, stream mode, tool wrapper or Responses API call.

## Deliberate differences and unverified behavior

- This is an independent Python client, not copied official Codex code. It identifies
  itself as `codex_image_cli`/`codex-image-cli`, rather than claiming to be an official
  client. Actual backend acceptance of that originator is untested.
- Only an existing file-backed ChatGPT session is supported. Official Codex has
  broader authentication, refresh, routing, managed policy and storage logic.
- Five references mirrors the current tool limit, not a claimed backend maximum.
  Older design notes proposing 16 references are not used. The 25 MiB reference
  cap is chosen locally, not inferred from an undocumented service guarantee.
- The socket timeout, local size caps, strict response checks, no retries, blocked
  redirects/proxies, and atomic output writes are defensive choices in this CLI.
- The local JWT expiry and routing claims are unverified hints. The backend decides
  authentication and model/account access. No tokens are refreshed or rewritten.
- There is no deduplication guarantee for the turn-ID header. Ambiguous failures
  must not trigger automatic replays.
- Actual image calls, output appearance, transparency, model availability, billing
  and quota behavior were **not tested live**. Mocked protocol tests only establish
  that the client follows the reviewed shape and its local safety invariants.

If the service changes, compare its new official source with these files before
modifying the client. Do not guess new fields, weaken TLS, follow login redirects
with bearer credentials, or forward session tokens to an alternate host.
