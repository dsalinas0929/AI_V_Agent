"""Interactive client: all permissions are enforced here, on the user's machine."""

import asyncio
import json
import re
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlparse

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from websockets.asyncio.client import connect

from agent_v.local_tools import LocalTools

console = Console(highlight=False)


def safe_text(value: str) -> str:
    # Prevent model/tool output from injecting terminal control sequences.
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", value)


def validate_server(server: str) -> str:
    parsed = urlparse(server)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Server must be an http:// or https:// URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Server URL must not contain credentials, a query, or a fragment.")
    if parsed.path not in {"", "/"}:
        raise ValueError("Use the server origin, without a path prefix.")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Remote servers require HTTPS to protect your token and files.")
    return server.rstrip("/")


def show_history(history):
    for item in history[-20:]:
        text = "".join(p.get("text", "") for p in item.get("parts", []) if not p.get("thought"))
        if text:
            console.print(f"{item['role']}: {safe_text(text)}", markup=False)


async def chat(server: str, token: str, workspace: Path, session_id: str | None):
    server = validate_server(server)
    socket_url = (
        ("wss" if server.startswith("https:") else "ws") + server[server.index(":") :] + "/ws"
    )
    async with connect(
        socket_url,
        additional_headers={"Authorization": f"Bearer {token}"},
        max_size=2_000_000,
        open_timeout=15,
    ) as ws:

        async def send(event):
            await ws.send(json.dumps(event))

        await send({"type": "start", "workspace": str(workspace), "session_id": session_id})
        initial = json.loads(await ws.recv())
        if initial["type"] != "session":
            raise ValueError(initial.get("message", "Could not open session."))
        console.print(f"Session: {initial['id']}\nWorkspace: {workspace}", markup=False)
        show_history(initial["history"])
        console.print("/new · /cancel · /quit · Ctrl+C cancels; edits/commands ask for y/N")
        console.print("Requested file contents and tool results are sent to the server and model.")
        prompt = PromptSession()
        busy = False
        approval = None
        tool_task = None

        async def approve(title, detail):
            nonlocal approval
            console.print(f"\n{safe_text(title)}\n{safe_text(detail)}", markup=False)
            console.print("Approve? Type y to allow, or anything else to deny.")
            approval = asyncio.get_running_loop().create_future()
            try:
                return await approval
            finally:
                approval = None

        tools = LocalTools(workspace, approve)

        async def perform(event):
            console.print(f"\nTool: {safe_text(event['name'])}", markup=False)
            result = await tools.execute(event["name"], event["args"])
            if "error" in result:
                console.print(f"Tool: {safe_text(result['error'])}", markup=False)
            await send({"type": "tool_result", "id": event["id"], "result": result})

        async def stop_tool():
            nonlocal tool_task
            if tool_task:
                tool_task.cancel()
                with suppress(asyncio.CancelledError):
                    await tool_task
                tool_task = None

        async def receive():
            nonlocal busy, tool_task
            async for raw in ws:
                event = json.loads(raw)
                kind = event["type"]
                if kind == "delta":
                    console.print(safe_text(event["text"]), end="", markup=False)
                elif kind == "tool_request":
                    if tool_task and not tool_task.done():
                        raise RuntimeError("Server requested concurrent local tools.")
                    if tool_task:
                        await tool_task
                    tool_task = asyncio.create_task(perform(event))
                elif kind in {"done", "cancelled"}:
                    await stop_tool()
                    busy = False
                    console.print(
                        "\nCancelled. Local actions already completed are not undone."
                        if kind == "cancelled"
                        else ""
                    )
                elif kind == "error":
                    console.print(f"\nError: {safe_text(event['message'])}", markup=False)
                elif kind == "session":
                    busy = False
                    console.print(f"Session: {event['id']}", markup=False)

        async def input_loop():
            nonlocal busy
            while True:
                try:
                    line = (await prompt.prompt_async("you> ")).strip()
                except KeyboardInterrupt:
                    line = "/cancel"
                except EOFError:
                    return
                if line == "/quit":
                    return
                if line == "/cancel":
                    await stop_tool()
                    await send({"type": "cancel"})
                    continue
                if approval is not None and not approval.done():
                    approval.set_result(line.lower() in {"y", "yes"})
                    continue
                if not line:
                    continue
                if busy:
                    console.print("A turn is running. Use /cancel before sending another message.")
                    continue
                if line == "/new":
                    busy = True
                    await send({"type": "start", "workspace": str(workspace)})
                elif line.startswith("/"):
                    console.print("Commands: /new /cancel /quit")
                elif len(line) > 16_000:
                    console.print("Message is too long (maximum 16,000 characters).")
                else:
                    busy = True
                    await send({"type": "chat", "text": line})

        with patch_stdout(raw=True):
            reader = asyncio.create_task(receive())
            writer = asyncio.create_task(input_loop())
            try:
                done, _ = await asyncio.wait([reader, writer], return_when=asyncio.FIRST_COMPLETED)
                for finished in done:
                    await finished
            finally:
                reader.cancel()
                writer.cancel()
                await asyncio.gather(reader, writer, return_exceptions=True)
                await stop_tool()
