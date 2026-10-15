import asyncio
import hashlib
import os
import shlex
import sys

import pytest

from agent_v.local_tools import LocalTools


async def allow(*_):
    return True


async def deny(*_):
    return False


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "/etc/passwd", ".env", ".git/config", "private.key", "credentials.json"],
)
async def test_file_boundaries(tmp_path, path):
    result = await LocalTools(tmp_path, allow).execute("read_file", {"path": path})
    assert "error" in result


async def test_symlink_escape(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    (workspace / "link").symlink_to(tmp_path / "secret.txt")
    result = await LocalTools(workspace, allow).execute("read_file", {"path": "link"})
    assert "error" in result


async def test_edit_denied_and_stale(tmp_path):
    path = tmp_path / "hello.txt"
    path.write_text("original")
    digest = hashlib.sha256(b"original").hexdigest()
    args = {"path": "hello.txt", "content": "changed", "expected_sha256": digest}
    denied = await LocalTools(tmp_path, deny).execute("write_file", args)
    assert "denied" in denied["error"]
    assert path.read_text() == "original"

    async def concurrent_edit(*_):
        path.write_text("someone else's edit")
        return True

    result = await LocalTools(tmp_path, concurrent_edit).execute("write_file", args)
    assert "changed" in result["error"]
    assert path.read_text() == "someone else's edit"


async def test_create_and_read_and_search(tmp_path):
    tools = LocalTools(tmp_path, allow)
    result = await tools.execute(
        "write_file",
        {"path": "src/hello.py", "content": "print('hello')\n", "expected_sha256": "new"},
    )
    assert result["status"] == "written"
    read = await tools.execute("read_file", {"path": "src/hello.py"})
    assert read["sha256"] == result["sha256"]
    (tmp_path / ".env").write_text("hello=secret")
    found = await tools.execute("search_files", {"path": ".", "query": "hello"})
    assert [match["path"] for match in found["matches"]] == ["src/hello.py"]
    assert "error" in await tools.execute(
        "write_file", {"path": "src/hello.py", "content": "overwrite", "expected_sha256": "new"}
    )


async def test_unknown_tool_and_argument_rejected(tmp_path):
    tools = LocalTools(tmp_path, allow)
    assert "error" in await tools.execute("delete_everything", {})
    assert "error" in await tools.execute("read_file", {"path": ".", "extra": True})


def python_command(code):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


async def test_command_denial_output_limit_and_timeout(tmp_path):
    denied = await LocalTools(tmp_path, deny).execute(
        "run_command", {"command": python_command("open('bad', 'w').write('bad')")}
    )
    assert "denied" in denied["error"]
    assert not (tmp_path / "bad").exists()
    tools = LocalTools(tmp_path, allow, command_timeout=0.3)
    output = await tools.execute("run_command", {"command": python_command("print('x' * 50000)")})
    assert output["exit_code"] == 0
    assert output["truncated"]
    assert len(output["output"]) == 16384
    result = await tools.execute(
        "run_command", {"command": python_command("import time; time.sleep(10)")}
    )
    assert result["timed_out"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process group assertion")
async def test_cancel_kills_command(tmp_path):
    tools = LocalTools(tmp_path, allow)
    code = "import os,time; open('pid','w').write(str(os.getpid())); time.sleep(30)"
    task = asyncio.create_task(tools.command(python_command(code)))
    async with asyncio.timeout(5):
        while not (tmp_path / "pid").exists():
            await asyncio.sleep(0.01)
    pid = int((tmp_path / "pid").read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The shell and its child are terminated. Reaping an orphan can take a moment.
    async with asyncio.timeout(3):
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.02)
