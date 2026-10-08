# Codex Image CLI + Claude Code skill

A small, unofficial standalone image-generation/editing CLI. It reads an existing
file-backed **Codex ChatGPT OAuth session** and calls the same internal image routes
found in the official Codex source. It does not need Workspace Bridge, an MCP server,
the Codex app-server, an API key, or a running Codex process.

**This is not an official or supported public API.** Backend compatibility, model
availability, account eligibility, and usage limits can change without notice.
An existing subscription is not a guarantee of image access or unlimited usage.
The implementation was checked against official source, but **no live authenticated
generation or edit was run while building this package**. See [PROTOCOL.md](PROTOCOL.md)
for the exact source revision and compatibility boundaries.

## Install the CLI

Requires Python 3.10 or later. Runtime dependencies: Python standard library only.
Clone the repository and enter its directory first:

```sh
git clone https://github.com/sheldonxxxx/codex-image-cli.git
cd codex-image-cli
```

You can also download and extract the repository's ZIP from GitHub. Run the
installation commands below from that directory. The package does not install
itself or change Codex/Claude configuration automatically.

With pipx (if already installed):

```sh
pipx install .
codex-image --help
```

Or use an isolated virtual environment on macOS/Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/codex-image --help
```

On Windows, use `py -m venv .venv`, then `.venv\Scripts\python -m pip install .`
and `.venv\Scripts\codex-image --help`. Windows has not been tested. Atomic output
publication requires a filesystem with hard-link support (normally APFS, ext4 or
NTFS). Network/removable filesystems may not support it; a recovery PNG is retained
if publication fails.

Installing builds with setuptools. Pip may fetch that build dependency from the
package index. There are no runtime dependencies. To run directly without installing:

```sh
PYTHONPATH=src python3 -m codex_image_cli --help
```

## Existing Codex login

Sign into the official Codex CLI with your ChatGPT account first. This tool reads
`$CODEX_HOME/auth.json`, or `~/.codex/auth.json` when `CODEX_HOME` is unset. An explicit
existing file can be selected with `--auth-file PATH` on any subcommand.

```sh
codex-image doctor --json
```

`doctor` is strictly offline. It checks the file structure, token/account header
values, any locally readable expiry hint, and Unix file permissions. It does not
prove that the session is accepted, that image access exists, or that the backend
still matches this package. JWT hints are decoded without signature verification;
the service remains the authentication authority.

- Credentials are reread for each invocation. No credential cache is created.
- The CLI never refreshes, writes, copies, prints, or stores tokens separately.
- HTTP 401 means: **Run Codex once to renew your ChatGPT session, then retry manually.**
- API-key, keychain-only, external-token and government/FedRAMP logins are not
  supported. Use official Codex for those configurations. Do not paste or export
  credentials into chat, project files or prompt files.
- This standalone tool does not reproduce Codex's managed configuration, custom
  routing, organization policy, or data-residency enforcement. Use official Codex
  where such policies apply; do not use this tool to bypass them.

## Generate an image

Create the output directory, then write a standalone UTF-8 visual brief. Prompt
files avoid putting the prompt in shell history and make review easier.

```sh
mkdir -p generated-images
printf '%s\n' 'A minimal editorial illustration of a copper fox in a snowy pine forest. Wide composition, soft blue shadows, no text.' > generated-images/fox-v01.prompt.txt

codex-image generate \
  --prompt-file generated-images/fox-v01.prompt.txt \
  --output generated-images/fox-v01.png \
  --background opaque \
  --json
```

A short prompt can use `--prompt 'Your complete visual brief'` instead. Provide
exactly one of `--prompt` and `--prompt-file`.

## Edit with local references

```sh
codex-image edit \
  --image generated-images/fox-v01.png \
  --prompt 'Keep the fox, composition and lighting unchanged. Replace the snowy forest with autumn birches. No text.' \
  --output generated-images/fox-v02.png \
  --background opaque
```

Repeat `--image` to supply up to **5** local PNG, JPEG or WebP references, in prompt
order. The limit of 5 mirrors the reviewed official Codex tool. Each reference is
limited locally to **25 MiB**; that is this package's defensive cap, not a verified
server maximum. Images are sent inline as base64 data URLs; their bytes and metadata
are not stripped or transformed. Only send images you are authorized to share with
OpenAI. URLs, masks, seeds, batches and a separate upload service are not implemented.

## Options and output

- `--model`: `gpt-image-2` by default, matching the reviewed official tool
- `--quality`: `auto` (default), `low`, `medium`, `high`
- `--size`: `auto` (default) or `WIDTHxHEIGHT`; accepted dimensions depend on the backend
- `--background`: `opaque` (default), `transparent`, `auto`
- `--timeout`: per-socket timeout in seconds, default 300, range 1–3600; not an overall wall-clock deadline
- `--json`: safe machine-readable success on stdout or error on stderr
- `--auth-file`: explicitly selected existing Codex auth file

One request asks for one image; the optional wire field `n` is omitted, matching
the official tool. Only a response containing exactly one PNG is accepted. PNG
framing, IHDR fields, and chunk CRCs are checked, but pixels are not fully decoded and visual
quality is not evaluated by the CLI. Always open and inspect the result.

The output must end in `.png`, have an existing writable parent directory and be
a new path. Existing files and symlinks are never overwritten, including a file
created by another process while the request is running. Output is staged privately
in the destination directory, then published with an atomic no-clobber hard link.
Unix staging/final permissions are 0600. If publication fails after generation,
the error gives a recovery-file path. Check that file before regenerating; a disk
write failure can leave it partial. A hard crash may leave a hidden staging file.

Limits are 128 KiB UTF-8 for a prompt, 1 MiB for an auth file, 48 MiB for a response
body, 32 MiB for decoded output, and 100 million declared output pixels. These are local safety limits.

Without `--json`, success prints the output path. With it, the report contains the
output path, dimensions, byte count, selected safe server metadata and a locally
generated request-turn ID. Prompts, reference bytes, account IDs, tokens, and raw
server errors are never included. Argument-parser errors retain normal argparse
text formatting even with `--json`.

Exit codes: `0` success; `1` unexpected local failure; `2` input validation;
`3` authentication; `4` network/HTTP failure; `5` unexpected image response;
`6` output-file failure; `130` interrupted.

## Retry and transport safety

**No automatic retries.** This includes HTTP 401/429/5xx and transport timeouts.
The turn-ID header is not treated as an idempotency guarantee. A request may have
been processed even when no image arrives; manually repeating it may use quota
again. Review an ambiguous failure before choosing to repeat it.

The only destination is `https://chatgpt.com/backend-api/codex/images/...`, with
normal TLS certificate verification. There is no endpoint override, no redirect
following, no environment HTTP proxy support, and no TLS session-key logging. No browser cookies, refresh
token, ID token, or API key are sent. Only the access token and account ID are used
for authentication. If your network requires a proxy or special policy, use the
official Codex client rather than weakening these protections.

## Install the Claude Code skill

These locations follow the [official Claude Code skills documentation](https://code.claude.com/docs/en/skills#choose-where-skills-load).

Install the CLI first and make `codex-image` available on the PATH used by Claude
Code (or activate your virtual environment before starting Claude Code).

For one project, from the package directory, set the path and copy the skill:

```sh
PROJECT=/absolute/path/to/your/project
mkdir -p "$PROJECT/.claude/skills"
test ! -e "$PROJECT/.claude/skills/codex-image" && test ! -L "$PROJECT/.claude/skills/codex-image" && \
  cp -R skills/codex-image "$PROJECT/.claude/skills/codex-image"
```

Or for all your Claude Code projects:

```sh
mkdir -p "$HOME/.claude/skills"
test ! -e "$HOME/.claude/skills/codex-image" && test ! -L "$HOME/.claude/skills/codex-image" && \
  cp -R skills/codex-image "$HOME/.claude/skills/codex-image"
```

These examples refuse to replace an existing skill; review it before updating.
The skill is a portable `SKILL.md` folder without MCP dependencies. It guides the
agent to inspect the project, prepare a complete visual brief, run the CLI, view
the actual PNG, and make targeted versioned edits. It does not grant additional
permissions or install software automatically.

Example request in Claude Code: “Use codex-image to create a wide illustration for
this page. Match the existing palette, save a versioned PNG, and inspect it before
integrating it.” Only ask for integration if you also want the project modified.

## Local verification

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

Tests use synthetic credentials and mocked transport; they do not read your real
Codex auth file or call the image service. At packaging, **69 tests passed** against
both the source tree and the installed wheel in an isolated Linux Python 3.12
environment. Packaging and CLI help were checked there, and source syntax was
checked for Python 3.10. The same 69 offline tests also passed on macOS with Python
3.14.7 on 2026-10-08. Windows execution and live account, quota, image
generation/editing, and real PNG quality remain untested.

## Troubleshooting and feedback

If the command is missing, check the PATH used by your shell or Claude Code; with
the virtual environment installation, use its `codex-image` executable or activate
that environment. If `doctor` fails, check the selected auth-file path and the
supported login types above. A successful `doctor` result is an offline check only.

Report reproducible problems in [GitHub Issues](https://github.com/sheldonxxxx/codex-image-cli/issues).
Include your OS, Python and CLI versions, the command with prompts and private
paths replaced by placeholders, the exit code, and the CLI's curated error message.
Do not attach auth files, tokens, private prompts, reference images, raw backend
responses, or credential-bearing logs. Review a failed request before manually
repeating it, since it may already have consumed usage.

## License

No project license has been selected.
