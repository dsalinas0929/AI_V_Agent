#!/usr/bin/env bash
set -euo pipefail

release_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$release_dir"
# Validate the bundled files before installing anything.
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum -c SHA256SUMS
else
  shasum -a 256 -c SHA256SUMS
fi

if ! command -v uv >/dev/null 2>&1; then
  if [ -x "$HOME/.local/bin/uv" ]; then
    export PATH="$HOME/.local/bin:$PATH"
  else
    echo "Installing uv from astral.sh for your user account..."
    installer="$(mktemp)"
    trap 'rm -f "$installer"' EXIT
    curl --proto '=https' --tlsv1.2 -fsSL https://astral.sh/uv/install.sh -o "$installer"
    sh "$installer"
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi

wheels=("$release_dir"/agent_v-*.whl)
if [ "${#wheels[@]}" -ne 1 ] || [ ! -f "${wheels[0]}" ]; then
  echo "Expected exactly one Agent V wheel. Extract the release ZIP into an empty folder." >&2
  exit 1
fi
uv tool install --reinstall --python 3.13 "${wheels[0]}"
uv tool update-shell
bin_dir="$(uv tool dir --bin)"
"$bin_dir/agent-v" --version
printf '\nInstalled. Open a NEW terminal, then run: agent-v setup\n'
