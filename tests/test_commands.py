import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agent_v.cli import app as cli
from agent_v.model import DemoModel
from agent_v.server import create_app
from agent_v.store import Store

ADMIN = "admin-test-token-" + "x" * 32
AUTH = {"Authorization": f"Bearer {ADMIN}"}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_V_ADMIN_TOKEN", ADMIN)
    store = Store(str(tmp_path / "server.sqlite3"))
    token = store.create_user("alice")
    with TestClient(create_app(store, DemoModel(), rpm=100)) as client:
        yield client, store, token


def open_session(ws, support=True):
    ws.send_json({"type": "start", "workspace": "/workspace", "command_support": support})
    return ws.receive_json()["client_id"]


def post(client, client_id):
    return client.post(
        f"/admin/clients/{client_id}/command", headers=AUTH, json={"command": "echo hello"}
    )


def test_admin_auth_and_feature_disabled(environment, monkeypatch, tmp_path):
    client, _, token = environment
    assert client.get("/admin/clients").status_code == 403
    assert (
        client.get("/admin/clients", headers={"Authorization": f"Bearer {token}"}).status_code
        == 403
    )
    assert client.post("/admin/clients/missing/command", json={"command": "pwd"}).status_code == 403
    monkeypatch.delenv("AGENT_V_ADMIN_TOKEN")
    store = Store(str(tmp_path / "disabled.sqlite3"))
    with TestClient(create_app(store, DemoModel())) as disabled:
        assert disabled.get("/admin/clients", headers=AUTH).status_code == 503


def test_dispatch_approval_result_and_cleanup(environment):
    client, _, token = environment
    with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {token}"}) as ws:
        identifier = open_session(ws)
        listed = client.get("/admin/clients", headers=AUTH).json()
        assert listed == [
            {
                "id": identifier,
                "user": "alice",
                "workspace": "/workspace",
                "command_support": True,
                "session_id": listed[0]["session_id"],
                "busy": False,
            }
        ]
        with ThreadPoolExecutor() as pool:
            request = pool.submit(post, client, identifier)
            event = ws.receive_json()
            assert event == {
                "type": "tool_request",
                "origin": "server",
                "id": event["id"],
                "name": "run_command",
                "args": {"command": "echo hello"},
            }
            assert client.get("/admin/clients", headers=AUTH).json()[0]["busy"]
            assert post(client, identifier).status_code == 409
            # Wrong result IDs cannot complete the request.
            ws.send_json({"type": "tool_result", "id": "wrong", "result": {"output": "wrong"}})
            result = {"exit_code": 0, "output": "hello\n", "timed_out": False, "truncated": False}
            ws.send_json({"type": "tool_result", "id": event["id"], "result": result})
            assert ws.receive_json()["type"] == "done"
            response = request.result(timeout=5)
            assert response.status_code == 200
            assert response.json() == {"client_id": identifier, "result": result}
        assert not client.get("/admin/clients", headers=AUTH).json()[0]["busy"]
    assert client.get("/admin/clients", headers=AUTH).json() == []
    assert post(client, identifier).status_code == 404


def test_denied_and_cancelled_commands(environment):
    client, _, token = environment
    with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {token}"}) as ws:
        identifier = open_session(ws)
        with ThreadPoolExecutor() as pool:
            request = pool.submit(post, client, identifier)
            event = ws.receive_json()
            ws.send_json(
                {
                    "type": "tool_result",
                    "id": event["id"],
                    "result": {"error": "User denied command."},
                }
            )
            assert ws.receive_json()["type"] == "done"
            assert "denied" in request.result(timeout=5).json()["result"]["error"]
            request = pool.submit(post, client, identifier)
            assert ws.receive_json()["type"] == "tool_request"
            ws.send_json({"type": "cancel"})
            assert ws.receive_json()["type"] == "cancelled"
            assert "cancelled" in request.result(timeout=5).json()["result"]["error"]
            assert not client.get("/admin/clients", headers=AUTH).json()[0]["busy"]


def test_disconnect_resolves_admin_request(environment):
    client, _, token = environment
    with ThreadPoolExecutor() as pool:
        with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {token}"}) as ws:
            identifier = open_session(ws)
            request = pool.submit(post, client, identifier)
            assert ws.receive_json()["type"] == "tool_request"
        assert "disconnected" in request.result(timeout=5).json()["result"]["error"]


def test_busy_legacy_revoked_and_invalid_commands(environment):
    client, store, token = environment
    with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {token}"}) as ws:
        identifier = open_session(ws, support=False)
        assert post(client, identifier).status_code == 409
        identifier = open_session(ws)
        assert (
            client.post(
                f"/admin/clients/{identifier}/command", headers=AUTH, json={"command": "x" * 8193}
            ).status_code
            == 422
        )
        ws.send_json({"type": "chat", "text": "demo files"})
        assert ws.receive_json()["type"] == "tool_request"
        assert post(client, identifier).status_code == 409
        ws.send_json({"type": "cancel"})
        assert ws.receive_json()["type"] == "cancelled"
        store.revoke("alice")
        assert client.get("/admin/clients", headers=AUTH).json() == []
        assert post(client, identifier).status_code == 404


def test_admin_cli_uses_separate_token(monkeypatch):
    seen = []

    def request(method, url, **kwargs):
        import httpx

        seen.append(kwargs)
        return httpx.Response(
            200,
            json={"result": {"output": "hello", "exit_code": 0}},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("agent_v.cli.httpx.request", request)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["send-command", "a" * 32, "-c", "echo hello"],
        env={"AGENT_V_ADMIN_TOKEN": ADMIN, "AGENT_V_TOKEN": "ordinary"},
    )
    assert result.exit_code == 0, result.output
    assert seen[0]["headers"]["Authorization"] == f"Bearer {ADMIN}"
    assert ADMIN not in result.output


@pytest.mark.parametrize("automatic", [None, False, True])
async def test_client_server_command_approval_mode(tmp_path, monkeypatch, automatic):
    import agent_v.client as module

    incoming, input_lines, sent = asyncio.Queue(), asyncio.Queue(), asyncio.Queue()
    asked = asyncio.Event()
    executed = []

    class Socket:
        async def send(self, raw):
            import json

            await sent.put(json.loads(raw))

        async def recv(self):
            import json

            return json.dumps(await incoming.get())

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.recv()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Prompt:
        async def prompt_async(self, *_):
            return await input_lines.get()

    original_print = module.console.print

    def print_capture(*args, **kwargs):
        if args and str(args[0]).startswith("Approve?"):
            asked.set()
        original_print(*args, **kwargs)

    async def command(self, text):
        executed.append(text)
        return {"output": "hello", "exit_code": 0, "timed_out": False, "truncated": False}

    monkeypatch.setattr(module, "connect", lambda *a, **kw: Socket())
    monkeypatch.setattr(module, "PromptSession", Prompt)
    monkeypatch.setattr(module.console, "print", print_capture)
    monkeypatch.setattr(module.LocalTools, "command", command)
    await incoming.put({"type": "session", "id": "session", "client_id": "client", "history": []})
    options = {} if automatic is None else {"allow_server_commands": automatic}
    task = asyncio.create_task(
        module.chat("http://localhost:8000", "token", tmp_path, None, **options)
    )
    try:
        assert (await asyncio.wait_for(sent.get(), 2))["command_support"]
        scenarios = (
            [("server", None, True), ("model", "n", False), ("model", "y", True)]
            if automatic is not False
            else [("server", "n", False), ("server", "y", True)]
        )
        for origin, answer, should_run in scenarios:
            executed.clear()
            asked.clear()
            await incoming.put(
                {
                    "type": "tool_request",
                    "origin": origin,
                    "id": origin + str(answer),
                    "name": "run_command",
                    "args": {"command": "echo hello"},
                }
            )
            if answer is not None:
                await asyncio.wait_for(asked.wait(), 2)
                assert not executed
                await input_lines.put(answer)
            result = await asyncio.wait_for(sent.get(), 2)
            assert result["type"] == "tool_result"
            assert bool(executed) == should_run
            assert asked.is_set() == (answer is not None)
            if not should_run:
                assert "denied" in result["result"]["error"]
            await incoming.put({"type": "done"})
            # Let the receiver finish the previous local task before the next request.
            await asyncio.sleep(0.01)
        await incoming.put(
            {
                "type": "tool_request",
                "origin": "server",
                "id": "bad-tool",
                "name": "write_file",
                "args": {"path": "unexpected.txt", "content": "bad", "expected_sha256": "new"},
            }
        )
        result = await asyncio.wait_for(sent.get(), 2)
        assert "Unsupported" in result["result"]["error"]
        assert not (tmp_path / "unexpected.txt").exists()
        await incoming.put({"type": "done"})
        await input_lines.put("/quit")
        await asyncio.wait_for(task, 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_approval_timeout_releases_client(environment, monkeypatch):
    client, _, token = environment
    monkeypatch.setattr("agent_v.server.COMMAND_TIMEOUT", 0.05)
    with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {token}"}) as ws:
        identifier = open_session(ws)
        with ThreadPoolExecutor() as pool:
            request = pool.submit(post, client, identifier)
            assert ws.receive_json()["type"] == "tool_request"
            assert ws.receive_json()["type"] == "error"
            assert ws.receive_json()["type"] == "done"
            assert "expired" in request.result(timeout=5).json()["result"]["error"]
        assert not client.get("/admin/clients", headers=AUTH).json()[0]["busy"]
