"""Console entry points for clients and server administrators."""

import asyncio
import os
import re
import sqlite3
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import httpx
import typer
import uvicorn
from rich.console import Console
from websockets.exceptions import InvalidStatus, WebSocketException

from agent_v.client import chat as run_chat
from agent_v.client import safe_text, validate_server
from agent_v.config import model_config
from agent_v.settings import connection_settings, save_settings, settings_path
from agent_v.store import Store

app = typer.Typer(no_args_is_help=True, help="Agent V — a small client/server CLI agent.")
users = typer.Typer(help="Manage access tokens locally on the server machine.")
app.add_typer(users, name="users")
console = Console(highlight=False)


def database() -> Store:
    return Store(os.getenv("AGENT_V_DB", ".agent-v/server.sqlite3"))


@app.callback(invoke_without_command=True)
def main(show_version: bool = typer.Option(False, "--version", is_eager=True)):
    """Agent V client and server commands."""
    if show_version:
        typer.echo(f"Agent V {version('agent-v')}")
        raise typer.Exit()


def require_connection(server, token):
    try:
        return connection_settings(server, token)
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from None


def check_connection(server: str, token: str):
    try:
        response = httpx.get(
            server + "/sessions",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
            follow_redirects=False,
        )
        if response.status_code in {401, 403}:
            raise ValueError(
                "Access token rejected. Ask your administrator for a token from this server."
            )
        response.raise_for_status()
        if not isinstance(response.json(), list):
            raise ValueError("This address did not return an Agent V sessions response.")
    except httpx.HTTPError:
        raise ValueError("Cannot reach Agent V. Check the server address or SSH tunnel.") from None


@app.command()
def setup():
    """Save your server address and user token after checking the connection."""
    console.print("Enter the server address and Agent V user token supplied by your administrator.")
    console.print("The token is saved in your user profile for future connections.")
    server = typer.prompt("Server URL", default="http://127.0.0.1:18000").strip()
    token = typer.prompt("Agent V access token (not a model API key)", hide_input=True).strip()
    try:
        server = validate_server(server)
        if not token or token.startswith("gm_live_"):
            raise ValueError("Enter an Agent V user token, not a SayGM API key.")
        check_connection(server, token)
        save_settings(server, token)
    except (OSError, ValueError) as exc:
        console.print(f"Setup failed: {safe_text(str(exc))}", markup=False)
        raise typer.Exit(1)
    console.print("Connected and saved. Open your project folder, then run: agent-v chat")
    if os.getenv("AGENT_V_SERVER") or os.getenv("AGENT_V_TOKEN"):
        console.print(
            "Existing AGENT_V_SERVER / AGENT_V_TOKEN environment variables override setup."
        )


@app.command()
def logout():
    """Remove the saved address and token from this computer."""
    settings_path().unlink(missing_ok=True)
    console.print(
        "Saved connection removed. Environment variables and server tokens are unchanged."
    )


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    demo: bool = typer.Option(False, help="Use an offline demo responder instead of an AI model."),
    ask_api_key: bool = typer.Option(
        False, help="Prompt privately for the server provider key; do not save it to disk."
    ),
):
    """Run the shared server (one process)."""
    if demo:
        os.environ["AGENT_V_DEMO"] = "1"
    if ask_api_key and os.getenv("AGENT_V_DEMO") != "1":
        provider = os.getenv("AGENT_V_PROVIDER", "saygm").strip().lower()
        key_names = {"saygm": "GM_API_KEY", "gemini": "GEMINI_API_KEY"}
        if provider not in key_names:
            raise typer.BadParameter("AGENT_V_PROVIDER must be saygm or gemini.")
        os.environ[key_names[provider]] = typer.prompt(f"{provider} API key", hide_input=True)
    try:
        config = model_config()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    console.print(f"Provider: {config.provider} | Model: {config.model}", markup=False)
    uvicorn.run(
        "agent_v.server:create_app",
        factory=True,
        host=host,
        port=port,
        ws_max_size=262_144,
        ws_max_queue=16,
    )


@users.command("add")
def add_user(name: str):
    """Create a user and print their token once. Run on the server machine."""
    if not name.strip() or len(name) > 100:
        raise typer.BadParameter("Use a name between 1 and 100 characters.")
    try:
        token = database().create_user(name)
    except sqlite3.IntegrityError:
        raise typer.BadParameter("User exists. Revoke their token before issuing a replacement.")
    console.print(f"Access token for {name} (save it now):", markup=False)
    typer.echo(token)


@users.command("revoke")
def revoke_user(name: str):
    """Revoke a user's token. Existing conversations remain stored."""
    if not database().revoke(name):
        raise typer.BadParameter("User not found.")
    typer.echo(f"Revoked token for {name}.")


ServerOption = Annotated[str, typer.Option(envvar="AGENT_V_SERVER")]
TokenOption = Annotated[str, typer.Option(envvar="AGENT_V_TOKEN", show_envvar=True)]


@app.command()
def chat(
    server: ServerOption = "",
    token: TokenOption = "",
    workspace: Path = typer.Option(Path("."), exists=True, file_okay=False, resolve_path=True),
    session: str | None = typer.Option(None, help="Resume an existing session ID."),
    allow_server_commands: bool = typer.Option(
        True,
        "--allow-server-commands/--ask-server-commands",
        help="Server commands run automatically by default; --ask-server-commands enables prompts.",
    ),
):
    """Chat and perform approved actions in a local workspace."""
    server, token = require_connection(server, token)
    try:
        asyncio.run(
            run_chat(
                server,
                token,
                workspace.resolve(),
                session,
                allow_server_commands=allow_server_commands,
            )
        )
    except InvalidStatus as exc:
        if exc.response.status_code in {401, 403}:
            console.print("Access token rejected. Run agent-v setup with a token from this server.")
        else:
            console.print(f"Server rejected the connection (HTTP {exc.response.status_code}).")
        raise typer.Exit(1)
    except (OSError, ValueError, RuntimeError, WebSocketException) as exc:
        console.print(f"Connection error: {safe_text(str(exc))}", markup=False)
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print("Disconnected. Resume the session to continue.")


@app.command()
def sessions(server: ServerOption = "", token: TokenOption = ""):
    """List your most recent sessions and their workspace paths."""
    server, token = require_connection(server, token)
    try:
        server = validate_server(server)
        response = httpx.get(
            server + "/sessions",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
            follow_redirects=False,
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            console.print("No saved sessions yet.")
        for item in rows:
            console.print(
                safe_text(
                    f"{item['id']}  {item['updated']}  {item['title']}\n  {item['workspace']}"
                ),
                markup=False,
            )
    except (httpx.HTTPError, ValueError) as exc:
        console.print(f"Cannot list sessions: {safe_text(str(exc))}", markup=False)
        raise typer.Exit(1)


@app.command()
def doctor(server: ServerOption = "", token: TokenOption = ""):
    """Check server access without calling the AI model or spending model credit."""
    server, token = require_connection(server, token)
    try:
        check_connection(server, token)
    except ValueError as exc:
        console.print(safe_text(str(exc)), markup=False)
        raise typer.Exit(1)
    console.print("Connection and user token OK. No AI request was made.")


AdminTokenOption = Annotated[str, typer.Option(envvar="AGENT_V_ADMIN_TOKEN", show_envvar=True)]


def admin_request(method: str, server: str, admin_token: str, path: str, body=None):
    if not admin_token:
        raise typer.BadParameter("Set AGENT_V_ADMIN_TOKEN to the server's admin token.")
    try:
        server = validate_server(server)
        response = httpx.request(
            method,
            server + path,
            json=body,
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=310,
            follow_redirects=False,
        )
        if response.is_error:
            detail = response.json().get("detail", "Request rejected")
            console.print(f"Server: {safe_text(str(detail))}", markup=False)
            raise typer.Exit(1)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        console.print(
            "Admin request failed. Check the connection; a submitted command may still be running. "
            "Do not resend without checking the client."
        )
        raise typer.Exit(1)


@app.command()
def clients(
    server: ServerOption = "http://127.0.0.1:8000",
    admin_token: AdminTokenOption = "",
):
    """Admin: list connected clients. Run on the server with its admin token."""
    rows = admin_request("GET", server, admin_token, "/admin/clients")
    if not rows:
        console.print("No connected clients.")
    for row in rows:
        state = "busy" if row["busy"] else "idle"
        if not row["command_support"]:
            state = "upgrade required"
        console.print(
            safe_text(f"{row['id']}  {row['user']}  {state}\n  {row['workspace']}"), markup=False
        )


@app.command("send-command")
def send_command(
    client_id: str,
    command: str = typer.Option(
        ..., "--command", "-c", help="Exact shell command to ask the client to run."
    ),
    server: ServerOption = "http://127.0.0.1:8000",
    admin_token: AdminTokenOption = "",
):
    """Admin: ask one online client to approve/run a command and wait for its result."""
    if not re.fullmatch(r"[a-f0-9]{32}", client_id):
        raise typer.BadParameter("Use a client ID from agent-v clients.")
    if not command.strip() or len(command) > 8192:
        raise typer.BadParameter("Command must contain 1 to 8192 characters.")
    console.print("Waiting for the client's approval and result (up to 5 minutes)...")
    result = admin_request(
        "POST",
        server,
        admin_token,
        f"/admin/clients/{client_id}/command",
        {"command": command},
    )["result"]
    if "error" in result:
        console.print(safe_text(str(result["error"])), markup=False)
        raise typer.Exit(1)
    console.print(safe_text(str(result.get("output", ""))), markup=False)
    console.print(
        f"Exit: {result.get('exit_code')} | Timed out: {result.get('timed_out')} "
        f"| Output truncated: {result.get('truncated')}",
        markup=False,
    )
    if result.get("timed_out") or result.get("exit_code") != 0:
        raise typer.Exit(1)
