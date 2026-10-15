"""One ASGI process owns the agent loops and shared provider quota."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from google.genai import errors, types
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from agent_v.config import create_model
from agent_v.model import QuotaError, RateLimiter
from agent_v.store import Store

logger = logging.getLogger(__name__)
MAX_FRAME = 262_144
MAX_CONTEXT = 120_000


class Frame(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Start(Frame):
    type: Literal["start"]
    workspace: str = Field(min_length=1, max_length=4096)
    session_id: str | None = Field(default=None, max_length=64)


class Chat(Frame):
    type: Literal["chat"]
    text: str = Field(min_length=1, max_length=16_000)


class Cancel(Frame):
    type: Literal["cancel"]


class ToolResult(Frame):
    type: Literal["tool_result"]
    id: str = Field(max_length=64)
    result: dict


FRAMES = TypeAdapter(Annotated[Start | Chat | Cancel | ToolResult, Field(discriminator="type")])


def create_app(store: Store | None = None, model=None, rpm: int | None = None):
    store = store or Store(os.getenv("AGENT_V_DB", ".agent-v/server.sqlite3"))
    limiter = RateLimiter(rpm or max(1, int(os.getenv("AGENT_V_RPM", "5"))))
    active: dict[str, str] = {}

    @asynccontextmanager
    async def lifespan(app):
        nonlocal model
        if model is None:
            model = create_model()
        yield
        await model.close()

    app = FastAPI(title="Agent V", lifespan=lifespan)

    def authenticate(header):
        if not header or not header.startswith("Bearer "):
            return None
        return store.authenticate(header[7:])

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/sessions")
    async def sessions(authorization: str | None = Header(default=None)):
        owner = authenticate(authorization)
        if not owner:
            raise HTTPException(401, "Invalid or revoked access token")
        return store.sessions(owner)

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        header = ws.headers.get("authorization")
        owner = authenticate(header)
        if not owner:
            await ws.close(code=1008)
            return
        await ws.accept()
        session_id = None
        connection_id = uuid4().hex
        task = None
        pending: dict[str, asyncio.Future] = {}

        async def emit(event):
            await ws.send_json(event)

        async def run_turn(message):
            session = store.session(session_id, owner)
            original = session["history"]
            user = {"role": "user", "parts": [{"text": message}]}
            history = original + [user]
            outcomes = []
            title = session["title"] if original else message
            try:
                async with asyncio.timeout(300):
                    for _ in range(10):
                        if authenticate(header) != owner:
                            raise RuntimeError("Access token was revoked.")
                        if len(json.dumps(history)) > MAX_CONTEXT:
                            raise RuntimeError("Conversation is full. Start a new session.")
                        limiter.acquire()
                        content = await model.generate(history, emit)
                        history.append(content.model_dump(mode="json", exclude_none=True))
                        calls = [p.function_call for p in content.parts or [] if p.function_call]
                        if not calls:
                            store.save(session_id, owner, history, title)
                            await emit({"type": "done"})
                            return
                        if len(calls) > 8:
                            raise RuntimeError("Model requested too many tools in one step.")
                        responses = []
                        for call in calls:
                            request_id = uuid4().hex
                            future = asyncio.get_running_loop().create_future()
                            pending[request_id] = future
                            try:
                                await emit(
                                    {
                                        "type": "tool_request",
                                        "id": request_id,
                                        "name": call.name,
                                        "args": call.args or {},
                                    }
                                )
                                result = await future
                            finally:
                                pending.pop(request_id, None)
                            outcomes.append({"tool": call.name, "result": result})
                            responses.append(
                                types.Part(
                                    function_response=types.FunctionResponse(
                                        name=call.name,
                                        id=call.id,
                                        response=result,
                                    )
                                )
                            )
                        history.append(
                            types.Content(role="user", parts=responses).model_dump(
                                mode="json", exclude_none=True
                            )
                        )
                    raise RuntimeError("Agent step limit reached. Send a follow-up to continue.")
            except (Exception, asyncio.CancelledError) as exc:
                # Keep a valid conversation even when cancelled between a call and its response.
                # Local side effects cannot be rolled back by cancelling a model request.
                note = "Turn interrupted; some requested actions may have run."
                if outcomes:
                    note += " Completed tool results: " + json.dumps(outcomes)[-16_000:]
                store.save(
                    session_id,
                    owner,
                    original + [user, {"role": "model", "parts": [{"text": note}]}],
                    title,
                )
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, errors.APIError):
                    detail = (
                        "Provider quota exceeded; retry later or check your API quota."
                        if exc.code == 429
                        else "Model provider request failed. Check server model/key configuration."
                    )
                elif isinstance(exc, TimeoutError):
                    detail = "Turn timed out after 5 minutes."
                elif isinstance(exc, (QuotaError, RuntimeError)):
                    detail = str(exc)
                else:
                    logger.error("Agent turn failed (%s)", type(exc).__name__)
                    detail = "Agent request failed. Check server logs and retry."
                await emit({"type": "error", "message": detail})
                await emit({"type": "done"})
            finally:
                if active.get(session_id) == connection_id:
                    active.pop(session_id, None)

        try:
            while True:
                raw = await ws.receive_text()
                if len(raw.encode()) > MAX_FRAME:
                    await ws.close(code=1009)
                    break
                if authenticate(header) != owner:
                    await ws.close(code=1008)
                    break
                try:
                    frame = FRAMES.validate_json(raw)
                except ValidationError:
                    await emit({"type": "error", "message": "Invalid protocol message."})
                    continue
                running = task is not None and not task.done()
                if isinstance(frame, Start):
                    if running:
                        await emit({"type": "error", "message": "Cancel the active turn first."})
                        continue
                    candidate = frame.session_id
                    if candidate:
                        session = store.session(candidate, owner)
                        if not session:
                            await emit({"type": "error", "message": "Session not found."})
                            continue
                        if session["workspace"] != frame.workspace:
                            await emit(
                                {
                                    "type": "error",
                                    "message": "Workspace differs. Start a new session.",
                                }
                            )
                            continue
                    else:
                        candidate = store.create_session(owner, frame.workspace)
                        session = store.session(candidate, owner)
                    session_id = candidate
                    await emit({"type": "session", "id": session_id, "history": session["history"]})
                elif isinstance(frame, Chat):
                    if not session_id:
                        await emit({"type": "error", "message": "Start a session first."})
                    elif running:
                        await emit({"type": "error", "message": "This session is already busy."})
                    elif session_id in active:
                        await emit({"type": "error", "message": "This session is already busy."})
                        await emit({"type": "done"})
                    elif len(active) >= 4:
                        await emit({"type": "error", "message": "Server busy. Try again shortly."})
                        await emit({"type": "done"})
                    else:
                        active[session_id] = connection_id
                        task = asyncio.create_task(run_turn(frame.text))
                elif isinstance(frame, ToolResult):
                    future = pending.get(frame.id)
                    if future is not None and not future.done():
                        future.set_result(frame.result)
                elif isinstance(frame, Cancel):
                    if running:
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task
                    if active.get(session_id) == connection_id:
                        active.pop(session_id, None)
                    await emit({"type": "cancelled"})
        except WebSocketDisconnect:
            pass
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                    await task
            if active.get(session_id) == connection_id:
                active.pop(session_id, None)

    return app
