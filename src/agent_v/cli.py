"""Console entry points for clients and server administrators."""

import asyncio
import os
import sqlite3
from pathlib import Path
from typing import Annotated

import httpx
import typer
import uvicorn
from rich.console import Console
from websockets.exceptions import WebSocketException

from agent_v.client import chat as run_chat
from agent_v.client import safe_text, validate_server
from agent_v.config import model_config
from agent_v.store import Store

app = typer.Typer(no_args_is_help=True, help="Agent V — a small client/server CLI agent.")
users = typer.Typer(help="Manage access tokens locally on the server machine.")
app.add_typer(users, name="users")
console = Console(highlight=False)


def database() -> Store:
    return Store(os.getenv("AGENT_V_DB", ".agent-v/server.sqlite3"))


def require_token(token):
    if not token:
        raise typer.BadParameter("Set AGENT_V_TOKEN to your server-issued access token.")
    return token


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
    server: ServerOption = "http://127.0.0.1:8000",
    token: TokenOption = "",
    workspace: Path = typer.Option(Path("."), exists=True, file_okay=False, resolve_path=True),
    session: str | None = typer.Option(None, help="Resume an existing session ID."),
):
    """Chat and perform approved actions in a local workspace."""
    require_token(token)
    try:
        asyncio.run(run_chat(server, token, workspace.resolve(), session))
    except (OSError, ValueError, RuntimeError, WebSocketException) as exc:
        console.print(f"Connection error: {safe_text(str(exc))}", markup=False)
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print("Disconnected. Resume the session to continue.")


@app.command()
def sessions(server: ServerOption = "http://127.0.0.1:8000", token: TokenOption = ""):
    """List your most recent sessions and their workspace paths."""
    require_token(token)
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
