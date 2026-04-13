#!/usr/bin/env python3
"""
Hermes MCP HTTP Server

Exposes Hermes's 10 MCP tools over HTTP+SSE for Claude.ai.
Tools: conversations_list, conversation_get, messages_read, attachments_fetch,
       events_poll, events_wait, messages_send, channels_list,
       permissions_list_open, permissions_respond

Endpoints:
  GET  /sse       — SSE stream (sends endpoint URL, then JSON-RPC responses)
  POST /messages  — JSON-RPC requests from MCP client
  GET  /health    — Health check
"""
import asyncio
import json
import logging
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("hermes.mcp_http")

SERVER_PORT = int(os.environ.get("PORT", "3000"))

# ---------------------------------------------------------------------------
# MCP Server initialization
# ---------------------------------------------------------------------------
mcp_server = None
bridge = None
tool_count = 0

try:
    from mcp_serve import create_mcp_server, EventBridge
    bridge = EventBridge()
    bridge.start()
    mcp_server = create_mcp_server(event_bridge=bridge)
    # Count tools
    tools = getattr(mcp_server, '_tools', {})
    tool_count = len(tools)
    logger.info("MCP server initialized with %d tools", tool_count)
except Exception as e:
    logger.error("Failed to initialize MCP server: %s", e)
    import traceback
    traceback.print_exc()

# ---------------------------------------------------------------------------
# SSE Session management
# ---------------------------------------------------------------------------
class McpSession:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self._closed = False

    async def send_event(self, event_type: str, data: str):
        if not self._closed:
            await self.queue.put((event_type, data))

    def close(self):
        self._closed = True

sessions: dict = {}

# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------
async def execute_tool(tool_name: str, arguments: dict) -> dict:
    if not mcp_server:
        return {"error": "MCP server not initialized"}
    try:
        tools = getattr(mcp_server, '_tools', {})
        if tool_name not in tools:
            return {"error": f"Tool not found: {tool_name}"}
        func = tools[tool_name].fn
        if asyncio.iscoroutinefunction(func):
            result = await func(**arguments)
        else:
            result = func(**arguments)
        return {"content": [{"type": "text", "text": str(result)}]}
    except Exception as e:
        logger.error("Tool execution error for %s: %s", tool_name, e)
        return {"error": str(e)}


def get_tools_list() -> list:
    if not mcp_server:
        return []
    try:
        tools = getattr(mcp_server, '_tools', {})
        result = []
        for name, tool in tools.items():
            schema = getattr(tool, 'parameters', None)
            if not schema:
                schema = {"type": "object", "properties": {}}
            result.append({
                "name": name,
                "description": getattr(tool, 'description', '') or '',
                "inputSchema": schema,
            })
        return result
    except Exception as e:
        logger.error("Failed to list tools: %s", e)
        return []

# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------
try:
    from aiohttp import web
except ImportError:
    logger.error("aiohttp not available — cannot start server")
    sys.exit(1)


def cors_headers():
    return {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': '*',
        'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    }


async def handle_health(request: web.Request) -> web.Response:
    tools = get_tools_list()
    return web.json_response(
        {"status": "ok", "server": "hermes-mcp", "tools": len(tools)},
        headers=cors_headers(),
    )


async def handle_sse(request: web.Request) -> web.StreamResponse:
    session_id = str(uuid.uuid4())
    session = McpSession(session_id)
    sessions[session_id] = session

    response = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            **cors_headers(),
        },
    )
    await response.prepare(request)

    messages_url = f"/messages?sessionId={session_id}"
    await response.write(f"event: endpoint\ndata: {messages_url}\n\n".encode())
    logger.info("SSE client connected: %s", session_id)

    try:
        while True:
            try:
                event_type, data = await asyncio.wait_for(
                    session.queue.get(), timeout=30.0
                )
                await response.write(f"event: {event_type}\ndata: {data}\n\n".encode())
            except asyncio.TimeoutError:
                await response.write(b": keepalive\n\n")
            except (ConnectionResetError, ConnectionAbortedError):
                break
    except Exception as e:
        logger.info("SSE disconnected: %s (%s)", session_id, e)
    finally:
        session.close()
        sessions.pop(session_id, None)

    return response


async def handle_messages(request: web.Request) -> web.Response:
    session_id = request.query.get('sessionId', '')
    session = sessions.get(session_id)

    if not session:
        return web.json_response(
            {"jsonrpc": "2.0", "error": {"code": -32000, "message": "Invalid session"}},
            status=400, headers=cors_headers(),
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}},
            status=400, headers=cors_headers(),
        )

    method = body.get("method", "")
    params = body.get("params", {})
    msg_id = body.get("id")

    logger.info("JSON-RPC: method=%s id=%s", method, msg_id)

    result = None

    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "hermes", "version": "1.0.0"},
        }
    elif method == "notifications/initialized":
        return web.Response(status=204, headers=cors_headers())
    elif method == "tools/list":
        result = {"tools": get_tools_list()}
    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        tool_result = await execute_tool(tool_name, arguments)
        if "error" in tool_result:
            result = {"content": [{"type": "text", "text": tool_result["error"]}], "isError": True}
        else:
            result = tool_result
    elif method == "ping":
        result = {}
    else:
        resp_data = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}
        await session.send_event("message", json.dumps(resp_data))
        return web.json_response(resp_data, headers=cors_headers())

    resp_data = {"jsonrpc": "2.0", "id": msg_id, "result": result}
    await session.send_event("message", json.dumps(resp_data))
    return web.json_response(resp_data, headers=cors_headers())


async def handle_cors(request: web.Request) -> web.Response:
    return web.Response(
        status=200,
        headers={**cors_headers(), 'Access-Control-Max-Age': '86400'},
    )


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_route('OPTIONS', '/{path:.*}', handle_cors)
    app.router.add_get('/health', handle_health)
    app.router.add_get('/sse', handle_sse)
    app.router.add_post('/messages', handle_messages)
    app.router.add_get('/', handle_health)
    return app


def main():
    logger.info("=" * 50)
    logger.info("Hermes MCP HTTP Server")
    logger.info("Port: %d", SERVER_PORT)
    logger.info("Tools: %d", tool_count)
    logger.info("SSE endpoint: /sse")
    logger.info("Messages endpoint: /messages")
    logger.info("=" * 50)
    app = create_app()
    web.run_app(app, host='0.0.0.0', port=SERVER_PORT, print=None)


if __name__ == "__main__":
    main()
