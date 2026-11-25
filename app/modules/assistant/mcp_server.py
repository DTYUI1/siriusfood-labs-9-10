"""Private MCP stdio server. The trusted launcher supplies the request's student scope."""

import os
import sys

from app.modules.assistant.mcp_contract import (
    MAX_CALLS, MAX_FRAME, PROTOCOL, PUBLIC_TOOLS, TOOLS, decode, encode, list_tools,
)
from app.modules.assistant.mcp_store import ReadUnavailable, execute_tool

SERVER_INFO = {"name": "siriusfood-readonly", "version": "1.0.0"}
CAPABILITIES = {"tools": {"listChanged": False}}


class ProtocolError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message


class ReadSession:
    def __init__(self, dsn, student_id):
        self.dsn = dsn
        self.student_id = student_id
        self.initialized = False
        self.ready = False
        self.calls = 0

    def result(self, method, params):
        if method == "initialize" and not self.initialized:
            if (
                not isinstance(params.get("protocolVersion"), str)
                or not isinstance(params.get("capabilities"), dict)
                or not isinstance(params.get("clientInfo"), dict)
            ):
                raise ProtocolError(-32602, "invalid_initialization")
            self.initialized = True
            return {"protocolVersion": PROTOCOL, "capabilities": CAPABILITIES, "serverInfo": SERVER_INFO}
        if method == "ping":
            return {}
        if not self.ready:
            raise ProtocolError(-32600, "not_initialized")
        if method == "tools/list" and not params:
            return {"tools": list_tools(self.student_id is not None)}
        if method == "tools/call":
            return self.call(params)
        raise ProtocolError(-32601, "method_not_found")

    def call(self, params):
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOLS or set(params) != {"name", "arguments"}:
            raise ProtocolError(-32602, "invalid_tool")
        if self.student_id is None and name not in PUBLIC_TOOLS:
            raise ProtocolError(-32602, "invalid_tool")
        self.calls += 1
        if self.calls > MAX_CALLS:
            raise ProtocolError(-32602, "tool_call_limit")
        input_schema = TOOLS[name][0]
        try:
            arguments = input_schema.model_validate(params["arguments"])
        except ValueError:
            raise ProtocolError(-32602, "invalid_tool_arguments") from None
        try:
            value = execute_tool(name, arguments, self.dsn, self.student_id).model_dump(mode="json")
            return {
                "content": [{"type": "text", "text": encode(value).decode()}],
                "structuredContent": value,
                "isError": False,
            }
        except ReadUnavailable as error:
            return {"content": [{"type": "text", "text": str(error)}], "isError": True}
        except Exception:
            # Driver exceptions may include connection details and parameters.
            return {"content": [{"type": "text", "text": "read_unavailable"}], "isError": True}

    def receive(self, frame):
        request_id = None
        try:
            request = decode(frame)
            if (
                not isinstance(request, dict)
                or request.get("jsonrpc") != "2.0"
                or not isinstance(request.get("method"), str)
                or not isinstance(request.get("params", {}), dict)
            ):
                raise ValueError("invalid_request")
            method = request["method"]
            params = request.get("params", {})
            if "id" not in request:
                if method == "notifications/initialized" and self.initialized:
                    self.ready = True
                return None
            if type(request["id"]) not in (str, int):
                raise ValueError("invalid_request_id")
            request_id = request["id"]
            return {"jsonrpc": "2.0", "id": request_id, "result": self.result(method, params)}
        except ProtocolError as error:
            code, message = error.code, error.message
        except (ValueError, RecursionError):
            code, message = -32600, "invalid_request"
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(stdin, stdout, dsn, student_id):
    session = ReadSession(dsn, student_id)
    for _ in range(16):
        frame = stdin.readline(MAX_FRAME + 1)
        if not frame.endswith(b"\n") or len(frame) > MAX_FRAME:
            return
        response = session.receive(frame)
        if response is not None:
            encoded = encode(response) + b"\n"
            if len(encoded) > MAX_FRAME:
                return
            stdout.write(encoded)
            stdout.flush()


if __name__ == "__main__":
    owner = os.environ.get("SIRIUS_MCP_STUDENT_ID")
    serve(
        sys.stdin.buffer,
        sys.stdout.buffer,
        os.environ.get("SIRIUS_LABS_ASSISTANT_READER_DSN", ""),
        int(owner) if owner else None,
    )
