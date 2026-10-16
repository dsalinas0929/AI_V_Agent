# Install Agent V on your computer

Agent V is a terminal app. After a one-time installation and setup, you can open any project
folder and run `agent-v chat`. You do not need to download the source code, install Python
manually, or have an AI model API key.

## 1. Get your connection details

Ask the person running the Agent V server for:

- **Server address**, preferably an HTTPS address such as `https://agent.example.com`.
- **Your Agent V access token**. This is your login credential; keep it private.
- The **Agent V client release ZIP**, or its download link.

The server administrator handles model keys and model billing. Your token must be registered
on their server. A SayGM/Gemini API key is not an Agent V access token.

If your administrator provides an SSH tunnel instead of an HTTPS address, follow the
[SSH instructions](#optional-connect-through-an-ssh-tunnel) before step 3.

## 2. Download and install

Get `agent-v-0.5.0-client.zip` from your administrator or the repository's
[Releases page](https://github.com/dsalinas0929/AI_V_Agent/releases) **once published**.
Choose the client ZIP under release assets, not GitHub's automatically generated source ZIP.
Extract the ZIP into a new folder. Inside are `install.sh`, `install.ps1`, `INSTALL.md`, a wheel
package, and checksums.

The installer uses [uv](https://docs.astral.sh/uv/) to install the app into an isolated environment
for your user account. It installs uv and downloads Python 3.13 if needed. Internet access is
required for these downloads and the app dependencies. No administrator/root access is needed.
This is an installer bundle, not an offline standalone executable.

### macOS

Open **Terminal** (search for it with Spotlight). If you extracted the ZIP in Downloads:

```sh
cd ~/Downloads/agent-v-0.5.0-client
bash install.sh
```

Wait for “Installed”, then close Terminal and open a new Terminal window.

### Windows

Extract the ZIP in File Explorer. Open the extracted folder, right-click an empty area, and
choose **Open in Terminal** (PowerShell). Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

This permits the installer for this invocation; it does not change your machine's execution
policy permanently. If your organization blocks scripts or downloads, ask your IT administrator.
Wait for “Installed”, then open a new terminal window.

### Linux

Open a terminal in the extracted folder:

```sh
bash install.sh
```

You need `curl` if uv is not installed. After installation, open a new terminal.

### Verify installation

On any platform:

```text
agent-v --version
```

It should print `Agent V 0.5.0`. If the command is not found, see troubleshooting below.

## 3. Connect once

Run:

```text
agent-v setup
```

1. Paste the **server address** your administrator gave you, then press Enter.
2. Paste your **Agent V access token**, then press Enter. The token stays hidden while you type.
3. Wait for “Connected and saved”.

Setup checks your login without making an AI request or spending model credit. Your address and
access token are saved in your OS user profile, so you do not need to export environment variables
on every launch. Run `agent-v setup` again to change servers or replace a token.

The saved token is plaintext in a local configuration file, not an encrypted keychain. On macOS
and Linux the file is readable/writable only by its owner; on Windows it uses your user profile's
permissions. Do not share this file or copy it into a project repository:

| System | Settings file |
| --- | --- |
| macOS | `~/Library/Application Support/agent-v/client.json` |
| Windows | `%LOCALAPPDATA%\agent-v\client.json` |
| Linux | `~/.config/agent-v/client.json` (or under `$XDG_CONFIG_HOME`) |

## 4. Open a folder and chat

Open Terminal/PowerShell and change to the project folder you want help with:

```sh
cd "/path/to/your/project"
agent-v chat
```

Replace the example path with a real folder on your computer. Windows example:

```powershell
cd 'C:\Users\YourName\Documents\MyProject'
agent-v chat
```

You can also select a folder directly:

```sh
agent-v chat --workspace "/path/to/your/project"
```

`--workspace .` means the current folder and is the default. The workspace is on **your computer**,
not the server. Try:

```text
Explain what this project does. Read the README first if there is one.
```

The agent can read/search files in your workspace. File contents and tool results used by the
agent are sent to the server and model provider. Edits show a proposed diff; shell commands show
the command. Enter `y` to approve or another answer to deny. Approved shell commands run with your
OS permissions and can access files outside the workspace.

Useful commands:

| Command | What it does |
| --- | --- |
| `/quit` | Leave chat |
| `/cancel` or Ctrl+C | Stop the current turn; completed actions are not undone |
| `/new` | Start a new conversation |
| `agent-v sessions` | List saved conversations (run outside chat) |
| `agent-v chat --session SESSION_ID` | Resume a conversation in its original workspace |
| `agent-v doctor` | Check server access without calling the model |
| `agent-v logout` | Remove this computer's saved connection |

## Optional: connect through an SSH tunnel

Your administrator must provide an SSH account, host, and authentication method. An Agent V token
alone does not grant SSH access. Your computer needs the `ssh` command (available on macOS/Linux
and Windows with OpenSSH Client enabled).

Open one terminal and run, substituting your actual SSH username and VPS address:

```sh
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18000:127.0.0.1:8000 USER@VPS_HOST
```

Leave that terminal open. No output after authentication is normal. In a second terminal, run
`agent-v setup` and enter **`http://127.0.0.1:18000`** as the server address, followed by your Agent V
token. Then run `agent-v chat` in your local project folder.

The tunnel encrypts the connection to the server. Reopen the tunnel each time you want to use
the client; the saved setup details remain. Your administrator should keep the server listening
on `127.0.0.1:8000`. You do not need public access to VPS port 8000.

## Commands sent by your administrator

While `agent-v chat` is connected and idle, the server administrator can send a command request.
**Starting with version 0.5.0, server commands run automatically by default.** No extra flag is
needed. The terminal announces automatic mode and displays every command and its result.
Execution happens on your PC in the selected workspace with your OS permissions. Results return
to the administrator. No model request is needed for this feature.

Normal launch:

```sh
agent-v chat
```

This trusts the server administrator to run shell commands on your computer. AI-requested commands
and edits still require approval. To explicitly request server-command prompts for one connection:

```sh
agent-v chat --ask-server-commands
```

The previous `--allow-server-commands` flag is still accepted but unnecessary. Approval prompts are
an Agent V feature, not macOS permission dialogs; OS access restrictions remain in effect.

You can use `/cancel` to stop the request or `/quit` to disconnect. Actions already completed are
not undone. Commands cannot be delivered while you are offline. Your ordinary client token is
sufficient; never enter an administrator token in client setup.

## Updates and removal

To update, download the new client release ZIP, extract into a new folder, and run its installer.
It replaces the installed app and retains your saved connection. Do not mix wheels from different
releases in one extracted folder. Follow the new release's installation guide for its folder name.

To remove the app and its saved login:

```text
agent-v logout
uv tool uninstall agent-v
```

This leaves uv/Python installed and does not revoke your server token or delete server-side
conversations. Ask the administrator if you also need those removed.

## Troubleshooting

**`agent-v: command not found` / command not recognized:** Open a new terminal first. If needed,
run `uv tool update-shell` and restart the terminal. To locate the installed commands, use
`uv tool dir --bin`. If uv is also missing from PATH, its default location is `~/.local/bin/uv`
on macOS/Linux or `$env:USERPROFILE\.local\bin\uv.exe` on Windows.

**No access token:** Run `agent-v setup`. Use the token created by the administrator on the server.

**HTTP 403 / token rejected:** The server does not recognize your token. Ask for the correct
server-issued token, then rerun setup. The model API key will not work here.

**Remote servers require HTTPS:** Use the administrator's HTTPS URL, or the SSH tunnel above.
Changing `http` to `https` yourself does not install a certificate on the server.

**Cannot reach the server:** Run `agent-v doctor`. Check the server address and that your SSH
tunnel is still connected if using one. Ask the administrator whether the server is running.

**SayGM credit exhausted:** Your connection works, but the server owner's SayGM credit is
insufficient. Contact the administrator; buying a personal key is not part of client setup.

**Setup succeeded but chat uses old settings:** Command options and `AGENT_V_SERVER` /
`AGENT_V_TOKEN` environment variables override saved setup. Remove old overrides from your shell.
On macOS/Linux: `unset AGENT_V_SERVER AGENT_V_TOKEN`. On PowerShell:
`Remove-Item Env:AGENT_V_SERVER, Env:AGENT_V_TOKEN -ErrorAction SilentlyContinue`.

**Installer checksum failure:** Re-download and extract the release ZIP into an empty folder.
Do not edit files inside the bundle before installing.
