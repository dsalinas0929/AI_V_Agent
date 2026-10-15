import json
import os

import httpx
import pytest
from typer.testing import CliRunner

from agent_v.cli import app
from agent_v.settings import connection_settings, load_settings, save_settings


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "profile" / "client.json"
    monkeypatch.setattr("agent_v.settings.settings_path", lambda: path)
    monkeypatch.setattr("agent_v.cli.settings_path", lambda: path)
    monkeypatch.delenv("AGENT_V_SERVER", raising=False)
    monkeypatch.delenv("AGENT_V_TOKEN", raising=False)
    return path


def test_settings_and_server_token_boundaries(config_file):
    save_settings("https://server.example/", "saved-token")
    assert connection_settings("", "") == ("https://server.example", "saved-token")
    assert connection_settings("https://server.example/", "override")[1] == "override"
    with pytest.raises(ValueError, match="No access token"):
        connection_settings("https://other.example", "")
    if os.name != "nt":
        assert config_file.stat().st_mode & 0o777 == 0o600
    save_settings("https://new.example", "new-token")
    assert load_settings()["token"] == "new-token"
    assert "saved-token" not in config_file.read_text()


def test_corrupt_settings_do_not_leak_contents(config_file):
    config_file.parent.mkdir()
    config_file.write_text("secret-invalid-json")
    with pytest.raises(ValueError, match="Run agent-v setup") as error:
        load_settings()
    assert "secret-invalid-json" not in str(error.value)


def test_setup_checks_auth_saves_and_doctor_uses_it(config_file, monkeypatch):
    seen = []

    def get(url, **kwargs):
        seen.append((url, kwargs))
        return httpx.Response(200, json=[], request=httpx.Request("GET", url))

    monkeypatch.setattr("agent_v.cli.httpx.get", get)
    runner = CliRunner()
    result = runner.invoke(app, ["setup"], input="https://server.example\nuser-secret\n")
    assert result.exit_code == 0, result.output
    assert "user-secret" not in result.output
    assert json.loads(config_file.read_text())["token"] == "user-secret"
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert seen[-1][0] == "https://server.example/sessions"
    assert seen[-1][1]["headers"]["Authorization"] == "Bearer user-secret"
    result = runner.invoke(app, ["doctor"], env={"AGENT_V_TOKEN": "env-token"})
    assert result.exit_code == 0
    assert seen[-1][1]["headers"]["Authorization"] == "Bearer env-token"
    assert runner.invoke(app, ["logout"]).exit_code == 0
    assert not config_file.exists()


def test_failed_setup_preserves_previous_connection(config_file, monkeypatch):
    save_settings("https://old.example", "old-token")
    monkeypatch.setattr("agent_v.cli.httpx.get", lambda url, **kw: httpx.Response(401))
    result = CliRunner().invoke(app, ["setup"], input="https://new.example\nwrong-token\n")
    assert result.exit_code == 1
    assert "rejected" in result.output
    assert "wrong-token" not in result.output
    assert load_settings()["token"] == "old-token"


def test_setup_rejects_provider_key(config_file):
    result = CliRunner().invoke(app, ["setup"], input="https://server.example\ngm_live_test\n")
    assert result.exit_code == 1
    assert not config_file.exists()
    assert "gm_live_test" not in result.output


def test_chat_uses_saved_connection(config_file, monkeypatch):
    save_settings("https://server.example", "saved-token")
    seen = []

    async def fake_chat(server, token, workspace, session_id):
        seen.append((server, token, workspace, session_id))

    monkeypatch.setattr("agent_v.cli.run_chat", fake_chat)
    result = CliRunner().invoke(app, ["chat", "--workspace", str(config_file.parent)])
    assert result.exit_code == 0, result.output
    assert seen[0][:2] == ("https://server.example", "saved-token")


def test_version_does_not_need_setup(config_file):
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert "Agent V" in result.output
