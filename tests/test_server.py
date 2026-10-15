import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_v.model import DemoModel
from agent_v.server import create_app
from agent_v.store import Store


@pytest.fixture
def setup(tmp_path):
    store = Store(str(tmp_path / "test.sqlite3"))
    token = store.create_user("alice")
    bob = store.create_user("bob")
    app = create_app(store, DemoModel(), rpm=100)
    with TestClient(app) as client:
        yield client, store, token, bob


def headers(token):
    return {"Authorization": "Bearer " + token}


def start(ws, session_id=None, workspace="/project"):
    ws.send_json({"type": "start", "workspace": workspace, "session_id": session_id})
    return ws.receive_json()


def turn(ws, text):
    ws.send_json({"type": "chat", "text": text})
    events = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event["type"] == "done":
            return events


def test_auth_and_token_hashes(setup):
    client, store, token, _ = setup
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/sessions").status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass
    assert token not in store.path.read_bytes().decode(errors="ignore")
    store.revoke("alice")
    assert client.get("/sessions", headers=headers(token)).status_code == 401


def test_chat_resume_and_isolation(setup):
    client, store, token, bob = setup
    with client.websocket_connect("/ws", headers=headers(token)) as ws:
        session = start(ws)
        events = turn(ws, "Hello")
        assert "Hello" in "".join(e.get("text", "") for e in events)
    rows = client.get("/sessions", headers=headers(token)).json()
    assert rows[0]["id"] == session["id"]
    with client.websocket_connect("/ws", headers=headers(token)) as ws:
        resumed = start(ws, session["id"])
        assert resumed["history"][0]["parts"][0]["text"] == "Hello"
        assert start(ws, session["id"], "/different")["type"] == "error"
    with client.websocket_connect("/ws", headers=headers(bob)) as ws:
        assert start(ws, session["id"])["message"] == "Session not found."
    assert client.get("/sessions", headers=headers(bob)).json() == []


def test_tool_round_trip_and_cancel(setup):
    client, store, token, _ = setup
    with client.websocket_connect("/ws", headers=headers(token)) as ws:
        session = start(ws)
        ws.send_json({"type": "chat", "text": "demo files"})
        tool = ws.receive_json()
        assert tool["name"] == "list_files"
        ws.send_json(
            {"type": "tool_result", "id": tool["id"], "result": {"entries": ["README.md"]}}
        )
        text = ""
        while (event := ws.receive_json())["type"] != "done":
            text += event.get("text", "")
        assert "README.md" in text
        ws.send_json({"type": "chat", "text": "demo files"})
        assert ws.receive_json()["type"] == "tool_request"
        ws.send_json({"type": "cancel"})
        assert ws.receive_json()["type"] == "cancelled"
        assert (
            "interrupted"
            in store.session(session["id"], "alice")["history"][-1]["parts"][0]["text"]
        )
        assert not any(e["type"] == "error" for e in turn(ws, "Continue"))


def test_invalid_frames_and_revocation(setup):
    client, store, token, _ = setup
    with client.websocket_connect("/ws", headers=headers(token)) as ws:
        ws.send_text("not json")
        assert ws.receive_json()["type"] == "error"
        start(ws)
        ws.send_json({"type": "chat", "text": "demo files"})
        assert ws.receive_json()["type"] == "tool_request"
        store.revoke("alice")
        ws.send_json({"type": "cancel"})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_shared_quota(tmp_path):
    store = Store(str(tmp_path / "test.sqlite3"))
    token = store.create_user("alice")
    with TestClient(create_app(store, DemoModel(), rpm=1)) as client:
        with client.websocket_connect("/ws", headers=headers(token)) as ws:
            start(ws)
            turn(ws, "first")
            events = turn(ws, "second")
            assert any("quota" in e.get("message", "") for e in events)


def test_concurrent_session_rejected(setup):
    client, _, token, _ = setup
    with client.websocket_connect("/ws", headers=headers(token)) as first:
        session = start(first)
        first.send_json({"type": "chat", "text": "demo files"})
        assert first.receive_json()["type"] == "tool_request"
        with client.websocket_connect("/ws", headers=headers(token)) as second:
            start(second, session["id"])
            events = turn(second, "conflict")
            assert any("busy" in e.get("message", "") for e in events)
        first.send_json({"type": "cancel"})
        assert first.receive_json()["type"] == "cancelled"
