"""Per-user client settings. These never contain a model-provider key."""

import json
import os
import sys
import tempfile
from pathlib import Path

from agent_v.client import validate_server


def settings_path() -> Path:
    if sys.platform == "win32":
        root = Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return root / "agent-v" / "client.json"


def load_settings() -> dict:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("token"), str):
            raise ValueError()
        server = validate_server(data["server"])
        return {"server": server, "token": data["token"]}
    except (ValueError, KeyError, TypeError):
        raise ValueError("Saved client settings are invalid. Run agent-v setup again.") from None


def save_settings(server: str, token: str):
    path = settings_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".client-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"server": validate_server(server), "token": token}, file)
            file.write("\n")
        # mkstemp creates owner-only files on POSIX. The containing Windows profile supplies ACLs.
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def connection_settings(server: str, token: str) -> tuple[str, str]:
    saved = load_settings()
    selected = validate_server(server or saved.get("server", "http://127.0.0.1:8000"))
    # Never silently send a saved token to a different server supplied on the command line.
    if not token and selected == saved.get("server"):
        token = saved.get("token", "")
    token = token.strip()
    if not token:
        raise ValueError("No access token for this server. Run agent-v setup or set AGENT_V_TOKEN.")
    if token.startswith("gm_live_"):
        raise ValueError(
            "Use your Agent V user token, not a SayGM API key. Ask your administrator."
        )
    return selected, token
