import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from google.genai import types
from typer.testing import CliRunner

from agent_v.cli import app
from agent_v.config import model_config
from agent_v.saygm import BASE_URL, ProviderError, SayGMModel, messages_from_history
from agent_v.server import create_app
from agent_v.store import Store


def sse(*chunks):
    return "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def chunk(delta=None, finish=None):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


class FragmentedStream(httpx.AsyncByteStream):
    def __init__(self, text):
        self.data = text.encode()
        self.closed = False

    async def __aiter__(self):
        for offset in range(0, len(self.data), 7):
            yield self.data[offset : offset + 7]

    async def aclose(self):
        self.closed = True


async def discard(_):
    pass


async def test_streamed_tool_call_and_history_round_trip():
    seen = []
    streams = []

    def handle(request):
        assert str(request.url) == BASE_URL + "/chat/completions"
        assert request.headers["authorization"] == "Bearer server-test-key"
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-5.4-mini"
        assert payload["stream"]
        assert {t["function"]["name"] for t in payload["tools"]} >= {"read_file", "write_file"}
        seen.append(payload)
        if len(seen) == 1:
            body = sse(
                chunk({"content": "Reading now."}),
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_read",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"pa'},
                            }
                        ]
                    }
                ),
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'th":"a.txt"}'}}]}),
                chunk(finish="tool_calls"),
            )
        else:
            assert payload["messages"][-2]["tool_calls"][0]["id"] == "call_read"
            assert payload["messages"][-1] == {
                "role": "tool",
                "tool_call_id": "call_read",
                "content": '{"content": "hello"}',
            }
            body = sse(chunk({"content": "The file says hello."}), chunk(finish="stop"))
        stream = FragmentedStream(body)
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    model = SayGMModel("server-test-key", transport=httpx.MockTransport(handle))
    history = [{"role": "user", "parts": [{"text": "Read a.txt"}]}]
    events = []

    async def emit(event):
        events.append(event)

    try:
        content = await model.generate(history, emit)
        assert content.parts[1].function_call.args == {"path": "a.txt"}
        history.append(content.model_dump(mode="json", exclude_none=True))
        history.append(
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="read_file", id="call_read", response={"content": "hello"}
                        )
                    )
                ],
            ).model_dump(mode="json", exclude_none=True)
        )
        answer = await model.generate(history, emit)
        assert answer.parts[0].text == "The file says hello."
        assert events == [
            {"type": "delta", "text": "Reading now."},
            {"type": "delta", "text": "The file says hello."},
        ]
        assert all(stream.closed for stream in streams)
    finally:
        await model.close()


@pytest.mark.parametrize(
    "status, message",
    [
        (401, "API key"),
        (402, "credit"),
        (403, "denied"),
        (404, "model"),
        (429, "rate limit"),
        (500, "HTTP 500"),
        (302, "HTTP 302"),
    ],
)
async def test_errors_are_sanitized_and_redirects_not_followed(status, message):
    def handle(request):
        return httpx.Response(
            status,
            text="secret-key-and-private-provider-data",
            headers={"location": "https://untrusted.example"},
        )

    model = SayGMModel("secret-key", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ProviderError, match=message) as error:
            await model.generate([], discard)
        assert "secret-key" not in str(error.value)
        assert "private-provider-data" not in str(error.value)
    finally:
        await model.close()


@pytest.mark.parametrize(
    "body",
    [
        sse(chunk({"content": "partial"})),
        sse(chunk({"content": "truncated"}), chunk(finish="length")),
        sse({"error": {"message": "secret-key"}}),
        "data: not-json\n\n",
        sse(
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "c",
                            "function": {"name": "write_file", "arguments": '{"path":'},
                        }
                    ]
                }
            ),
            chunk(finish="tool_calls"),
        ),
    ],
)
async def test_incomplete_or_invalid_stream_does_not_return_tools(body):
    model = SayGMModel(
        "secret-key", transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
    )
    try:
        with pytest.raises(ProviderError) as error:
            await model.generate([], discard)
        assert "secret-key" not in str(error.value)
    finally:
        await model.close()


async def test_cancel_closes_provider_stream():
    started = asyncio.Event()

    class WaitingStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield ("data: " + json.dumps(chunk({"content": "hello"})) + "\n\n").encode()
            started.set()
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    stream = WaitingStream()
    model = SayGMModel(
        "test-key", transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
    )
    try:
        task = asyncio.create_task(model.generate([], discard))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
    finally:
        await model.close()


def test_legacy_gemini_history_without_call_ids():
    history = [
        {
            "role": "model",
            "parts": [{"function_call": {"name": "list_files", "args": {"path": "."}}}],
        },
        {
            "role": "user",
            "parts": [{"function_response": {"name": "list_files", "response": {"entries": []}}}],
        },
    ]
    messages = messages_from_history(history)
    assert messages[-1]["tool_call_id"] == messages[-2]["tool_calls"][0]["id"]


@pytest.fixture
def clean_env(monkeypatch):
    for key in [
        "AGENT_V_DEMO",
        "AGENT_V_PROVIDER",
        "AGENT_V_MODEL",
        "GM_API_KEY",
        "GEMINI_API_KEY",
    ]:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_provider_config_and_key_separation(clean_env):
    clean_env.setenv("AGENT_V_TOKEN", "client-token-is-not-a-model-key")
    with pytest.raises(ValueError, match="GM_API_KEY"):
        model_config()
    clean_env.setenv("GM_API_KEY", "private-server-key")
    config = model_config()
    assert config.provider == "saygm"
    assert config.model == "gpt-5.4-mini"
    assert "private-server-key" not in repr(config)
    clean_env.setenv("AGENT_V_PROVIDER", "gemini")
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        model_config()
    clean_env.setenv("GEMINI_API_KEY", "gemini-server-key")
    assert model_config().provider == "gemini"
    clean_env.setenv("AGENT_V_DEMO", "1")
    assert model_config().provider == "demo"


def test_private_key_prompt(clean_env):
    seen = []
    clean_env.setattr("agent_v.cli.uvicorn.run", lambda *a, **kw: seen.append(model_config()))
    result = CliRunner().invoke(app, ["serve", "--ask-api-key"], input="private-test-key\n")
    assert result.exit_code == 0, result.output
    assert seen[0].api_key == "private-test-key"
    assert "private-test-key" not in result.output
    assert "saygm" in result.output


def test_saygm_server_round_trip(tmp_path):
    store = Store(str(tmp_path / "server.sqlite3"))
    token = store.create_user("alice")
    provider_key = "server-only-key"
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            body = sse(
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_list",
                                "function": {"name": "list_files", "arguments": '{"path":"."}'},
                            }
                        ]
                    }
                ),
                chunk(finish="tool_calls"),
            )
        else:
            assert calls[-1]["messages"][-1]["tool_call_id"] == "call_list"
            body = sse(chunk({"content": "README.md is here."}), chunk(finish="stop"))
        return httpx.Response(200, text=body)

    model = SayGMModel(provider_key, transport=httpx.MockTransport(handle))
    with TestClient(create_app(store, model, rpm=100)) as client:
        with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {token}"}) as ws:
            ws.send_json({"type": "start", "workspace": "/project"})
            session_id = ws.receive_json()["id"]
            ws.send_json({"type": "chat", "text": "List files"})
            event = ws.receive_json()
            assert event["type"] == "tool_request"
            assert provider_key not in json.dumps(event)
            ws.send_json(
                {"type": "tool_result", "id": event["id"], "result": {"entries": ["README.md"]}}
            )
            assert ws.receive_json() == {"type": "delta", "text": "README.md is here."}
            assert ws.receive_json()["type"] == "done"
        assert provider_key not in json.dumps(store.session(session_id, "alice"))
