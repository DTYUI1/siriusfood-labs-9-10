"""Launch the read-only MCP server and exchange bounded JSON-RPC frames over stdio."""

import os
from pathlib import Path
import selectors
import subprocess
import sys
from time import monotonic

from app.modules.assistant.mcp_contract import (
    MAX_CALLS, MAX_FRAME, PROTOCOL, TOOLS, decode, encode, list_tools,
)


class MCPUnavailable(Exception):
    pass


class MCPClient:
    def __init__(self, dsn, *, student_id=None):
        self.dsn = dsn
        self.student_id = student_id
        self.calls = 0
        self.sequence = 0

    def __enter__(self):
        root = Path(__file__).resolve().parents[3]
        env = {"LANG": "C.UTF-8", "SIRIUS_LABS_ASSISTANT_READER_DSN": self.dsn}
        if self.student_id is not None:
            env["SIRIUS_MCP_STUDENT_ID"] = str(self.student_id)
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-s", "-m", "app.modules.assistant.mcp_server"],
                cwd=root,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError:
            raise MCPUnavailable("mcp_start_failed") from None
        try:
            initialized = self.request("initialize", {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "siriusfood-assistant", "version": "1.0.0"},
            })
            if initialized.get("protocolVersion") != PROTOCOL or "tools" not in initialized.get("capabilities", {}):
                raise MCPUnavailable("mcp_protocol_error")
            self.send("notifications/initialized", {})
            listed = self.request("tools/list", {})
            if listed != {"tools": list_tools(self.student_id is not None)}:
                raise MCPUnavailable("mcp_tool_mismatch")
            self.listed = listed["tools"]
            self.tools = frozenset(tool["name"] for tool in listed["tools"])
        except BaseException:
            self.__exit__()
            raise
        return self

    def send(self, method, params, request_id=None):
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        frame = encode(message) + b"\n"
        if len(frame) > MAX_FRAME:
            raise MCPUnavailable("mcp_frame_limit")
        try:
            self.process.stdin.write(frame)
        except OSError:
            raise MCPUnavailable("mcp_disconnected") from None

    def request(self, method, params):
        self.sequence += 1
        self.send(method, params, self.sequence)
        frame = bytearray()
        deadline = monotonic() + 4
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while not frame.endswith(b"\n"):
                if not selector.select(max(0, deadline - monotonic())):
                    raise MCPUnavailable("mcp_timeout")
                chunk = os.read(self.process.stdout.fileno(), 4096)
                if not chunk:
                    raise MCPUnavailable("mcp_disconnected")
                frame.extend(chunk)
                if len(frame) > MAX_FRAME:
                    raise MCPUnavailable("mcp_frame_limit")
        try:
            response = decode(frame)
            if (
                response["jsonrpc"] == "2.0"
                and type(response["id"]) is int
                and response["id"] == self.sequence
                and isinstance(response["result"], dict)
            ):
                return response["result"]
        except (ValueError, KeyError, TypeError, RecursionError):
            pass
        raise MCPUnavailable("mcp_protocol_error")

    def call(self, name, arguments):
        if name not in self.tools or self.calls >= MAX_CALLS:
            raise MCPUnavailable("mcp_tool_or_call_limit")
        input_schema, output_schema, _ = TOOLS[name]
        checked = input_schema.model_validate(arguments).model_dump(mode="json")
        self.calls += 1
        result = self.request("tools/call", {"name": name, "arguments": checked})
        if result.get("isError") is True:
            raise MCPUnavailable("mcp_read_unavailable")
        try:
            data = result["structuredContent"]
            if result["isError"] is not False or result["content"] != [
                {"type": "text", "text": encode(data).decode()},
            ]:
                raise ValueError("invalid_tool_result")
            return output_schema.model_validate(data)
        except (KeyError, TypeError, ValueError):
            raise MCPUnavailable("mcp_schema_error") from None

    def __exit__(self, *_):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        finally:
            self.process.stdout.close()
