# Build and distribute a client release

Users should receive the client ZIP and follow [INSTALL.md](INSTALL.md). They do not need a source
checkout or a model provider key. Provide an HTTPS server address and a separate Agent V user
token for each user; SSH tunneling is an alternative for users with VPS SSH access.

## Build

From this repository, with uv installed:

```sh
uv sync --locked
uv run pytest -q
uv run ruff check src tests scripts
uv run python scripts/build_release.py
```

For version 0.2.0 this produces:

- `dist/agent-v-0.2.0-client.zip`: installers, wheel, installation guide, and bundled checksums.
- `dist/agent-v-0.2.0-client.zip.sha256`: checksum for the ZIP.
- `dist/agent_v-0.2.0-py3-none-any.whl`: package for manual installation.

The builder uses an explicit allowlist. It does not include `.venv`, `.agent-v`, `.env`, databases,
local notes, provider credentials, or client settings. The wheel is platform-independent Python;
installers fetch the appropriate runtime and dependencies for the user's OS. An internet connection
is required. The app package still includes server commands, but ordinary users only need setup/chat.

## Verify and publish

1. Test the extracted installer on the operating systems you intend to support. Use a fresh user
   account or a disposable environment. macOS/Linux use `bash install.sh`; Windows uses
   `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1`.
2. Run `agent-v --version`, `agent-v setup`, `agent-v doctor`, and a chat/tool round trip against your
   test server. Verify an upgrade retains settings and `agent-v logout` removes them.
3. In GitHub, create a release for the intended commit/tag (for this version, `v0.2.0`). Attach the
   client ZIP and its `.sha256` file. Include the wheel if you want a manual-install option.
4. Link users to that release and [INSTALL.md](INSTALL.md), and send each user's token privately.

Building does not create a Git tag, publish to PyPI, push code, or create a GitHub release. The
installers are scripts, not signed native applications; test each target OS before claiming support.

Before the next release, change the version in `pyproject.toml`, refresh `uv.lock` with `uv lock`,
and update versioned examples in INSTALL.md, README.md, and this file. Rebuild into a new release ZIP.

## Manual installation

For users who already have uv:

```sh
uv tool install --reinstall --python 3.13 /path/to/agent_v-0.2.0-py3-none-any.whl
uv tool update-shell
```

Open a new terminal, then run `agent-v setup`. This installation is independent of the repository;
`agent-v chat` can run in any folder. To remove it, run `agent-v logout` and `uv tool uninstall agent-v`.
