"""Local tools. File tools are workspace-scoped; approved shell commands are not sandboxed."""

import asyncio
import difflib
import hashlib
import os
import signal
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

MAX_FILE = 65_536
MAX_OUTPUT = 16_384
EXCLUDED = {"node_modules", "__pycache__", "venv", "dist", "build", "credentials.json"}


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PathArgs(Arguments):
    path: str = Field(min_length=1, max_length=4096)


class SearchArgs(PathArgs):
    query: str = Field(min_length=1, max_length=1000)


class WriteArgs(PathArgs):
    content: str = Field(max_length=MAX_FILE)
    expected_sha256: str = Field(pattern=r"^(new|[a-f0-9]{64})$")


class CommandArgs(Arguments):
    command: str = Field(min_length=1, max_length=8192)


SCHEMAS = {
    "list_files": PathArgs,
    "read_file": PathArgs,
    "search_files": SearchArgs,
    "write_file": WriteArgs,
    "run_command": CommandArgs,
}


def blocked(path: Path) -> bool:
    return any(
        (part.startswith(".") and part not in {".", ".."})
        or part in EXCLUDED
        or part.lower().endswith((".pem", ".key"))
        for part in path.parts
    )


class LocalTools:
    def __init__(self, workspace: Path, approve, command_timeout: float = 60):
        self.workspace = workspace.resolve(strict=True)
        self.approve = approve
        self.command_timeout = command_timeout

    def path(self, name: str) -> Path:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or blocked(relative):
            raise ValueError(
                "Use a relative workspace path; hidden and sensitive paths are blocked."
            )
        path = (self.workspace / relative).resolve()
        if not path.is_relative_to(self.workspace) or blocked(path.relative_to(self.workspace)):
            raise ValueError("Path escapes the workspace or resolves to a blocked location.")
        return path

    @staticmethod
    def read(path: Path) -> tuple[str, str]:
        if not path.is_file():
            raise ValueError("Only regular files are supported.")
        with path.open("rb") as file:
            data = file.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise ValueError("File exceeds the 64 KiB limit.")
        text = data.decode("utf-8")
        if "\x00" in text:
            raise ValueError("Binary files are not supported.")
        return text, hashlib.sha256(data).hexdigest()

    async def execute(self, name: str, raw: dict) -> dict:
        try:
            if name not in SCHEMAS:
                raise ValueError("Unknown tool.")
            args = SCHEMAS[name].model_validate(raw)
            if name == "run_command":
                if not await self.approve("Run shell command (not sandboxed)", args.command):
                    return {"error": "User denied command. Do not retry without being asked."}
                return await self.command(args.command)
            path = self.path(args.path)
            if name == "read_file":
                content, digest = self.read(path)
                return {"content": content, "sha256": digest}
            if name == "list_files":
                entries = []
                for entry in sorted(path.iterdir()):
                    try:
                        self.path(str(entry.relative_to(self.workspace)))
                    except ValueError:
                        continue
                    entries.append(entry.name + ("/" if entry.is_dir() else ""))
                    if len(entries) >= 300:
                        break
                return {"entries": entries, "limit": 300}
            if name == "search_files":
                return self.search(path, args.query)
            if name == "write_file":
                if len(args.content.encode()) > MAX_FILE:
                    raise ValueError("File exceeds the 64 KiB limit.")
                old, digest = self.read(path) if path.exists() else ("", "new")
                if digest != args.expected_sha256:
                    raise ValueError(
                        "File changed or already exists. Read it again before editing."
                    )
                diff = "".join(
                    difflib.unified_diff(
                        old.splitlines(keepends=True),
                        args.content.splitlines(keepends=True),
                        fromfile=args.path,
                        tofile=args.path,
                    )
                )
                if old == args.content and digest != "new":
                    return {"status": "unchanged"}
                if not await self.approve(f"Write {args.path}", diff or "Create empty file"):
                    return {"error": "User denied edit. Do not retry without being asked."}
                # Revalidate after the human approval wait, including symlink resolution.
                if self.path(args.path) != path:
                    raise ValueError("Path changed while waiting for approval.")
                current = self.read(path)[1] if path.exists() else "new"
                if current != digest:
                    raise ValueError("File changed while waiting for approval. Read it again.")
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".agent-v-")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
                        file.write(args.content)
                    if path.exists():
                        os.chmod(temporary, path.stat().st_mode & 0o777)
                    os.replace(temporary, path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
                return {
                    "status": "written",
                    "sha256": hashlib.sha256(args.content.encode()).hexdigest(),
                }
        except (OSError, ValueError) as exc:
            return {"error": str(exc)}

    def search(self, root: Path, query: str) -> dict:
        matches = []
        examined = 0
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not blocked(Path(d)))
            for name in sorted(files):
                examined += 1
                if examined > 2000:
                    return {"matches": matches, "truncated": True}
                try:
                    relative = str((Path(directory) / name).relative_to(self.workspace))
                    path = self.path(relative)
                    content, _ = self.read(path)
                except (OSError, ValueError):
                    continue
                for number, line in enumerate(content.splitlines(), 1):
                    if query in line:
                        matches.append({"path": relative, "line": number, "text": line[:300]})
                        if len(matches) >= 40:
                            return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    async def command(self, command: str) -> dict:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=(os.name != "nt"),
        )
        output = bytearray()
        truncated = False

        async def drain():
            nonlocal truncated
            while chunk := await process.stdout.read(4096):
                remaining = MAX_OUTPUT - len(output)
                output.extend(chunk[:remaining])
                truncated |= len(chunk) > remaining
            await process.wait()

        timed_out = False
        try:
            async with asyncio.timeout(self.command_timeout):
                await drain()
        except TimeoutError:
            timed_out = True
        finally:
            # Kill the process group even if the shell exited but left descendants running.
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.returncode is None:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            await process.wait()
        return {
            "exit_code": process.returncode,
            "output": output.decode(errors="replace"),
            "truncated": truncated,
            "timed_out": timed_out,
        }
