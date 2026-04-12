#!/usr/bin/env python3
"""
Hermes Combined HTTP Server — port 3000

Serves both:
  1. MCP SSE endpoints (/sse, /messages) for Claude.ai
  2. Reverse proxy for /telegram to Hermes gateway on internal port 3001

Uses aiohttp which is guaranteed to be installed (messaging dependency).
"""
import asyncio
import json
import logging
import os
import sys
import uuid
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("hermes.combined")

GATEWAY_PORT = int(os.environ.get("GATEWAY_INTERNAL_PORT", "3001"))
SERVER_PORT = int(os.environ.get("PORT", "3000"))

# ---------------------------------------------------------------------------
# MCP Server initialization
# ---------------------------------------------------------------------------
mcp_server = None
bridge = None

try:
    from mcp_serve import create_mcp_server, EventBridge
    bridge = EventBridge()
    bridge.start()
    mcp_server = create_mcp_server(event_bridge=bridge)
    logger.info("MCP server initialized successfully with 10 tools")
except Exception as e:
    logger.error("Failed to initialize MCP server: %s", e)
    logger.info("Will run as proxy-only mode (no MCP tools)")

# ---------------------------------------------------------------------------
# SSE Session management
# ---------------------------------------------------------------------------
class McpSession:
    """Manages an SSE session for one MCP client."""
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self._closed = False

    async def send_event(self, event_type: str, data: str):
        if not self._closed:
            await self.queue.put((event_type, data))

    def close(self):
        self._closed = True

sessions: dict = {}  # session_id -> McpSession

# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------
async def execute_tool(tool_name: str, arguments: dict) -> dict:
    """Execute an MCP tool and return the result."""
    if not mcp_server:
        return {"error": "MCP server not available"}

    try:
        # FastMCP stores tools in a dict
        internal = mcp_server._mcp_server if hasattr(mcp_server, '_mcp_server') else mcp_server
        
        # Try calling the tool through FastMCP's call_tool
        if hasattr(mcp_server, 'call_tool'):
            result = await mcp_server.call_tool(tool_name, arguments)
            return {"content": [{"type": "text", "text": str(result)}]}
        
        # Fallback: access the tool function directly
        tools = getattr(mcp_server, '_tools', {})
        if tool_name in tools:
            func = tools[tool_name].fn
            if asyncio.iscoroutinefunction(func):
                result = await func(**arguments)
            else:
                result = func(**arguments)
            return {"content": [{"type": "text", "text": str(result)}]}
        
        return {"error": f"Tool not found: {tool_name}"}
    except Exception as e:
        logger.error("Tool execution error: %s", e)
        return {"error": str(e)}

def get_tools_list() -> list:
    """Get the list of available MCP tools."""
    if not mcp_server:
        return []
    try:
        tools = getattr(mcp_server, '_tools', {})
        result = []
        for name, tool in tools.items():
            result.append({
                "name": name,
                "description": getattr(tool, 'description', '') or '',
                "inputSchema": getattr(tool, 'parameters', {}) or {"type": "object", "properties": {}},
            })
        return result
    except Exception as e:
        logger.error("Failed to list tools: %s", e)
        return []

# ---------------------------------------------------------------------------
# HTTP handlers (aiohttp)
# ---------------------------------------------------------------------------
try:
    from aiohttp import web, ClientSession
except ImportError:
    logger.error("aiohttp not available!")
    sys.exit(1)


async def handle_health(request: web.Request) -> web.Response:
    tools = get_tools_list()
    return web.json_response({
        "status": "ok",
        "server": "hermes-combined",
        "mcp_tools": len(tools),
        "gateway_port": GATEWAY_PORT,
    })


async def handle_sse(request: web.Request) -> web.StreamResponse:
    """SSE endpoint for MCP clients. Sends endpoint URL then keeps alive."""
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
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': '*',
            'Access-Control-Allow-Methods': '*',
        },
    )
    await response.prepare(request)

    # Send the endpoint URL as the first event
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
                # Send keepalive
                await response.write(b": keepalive\n\n")
            except (ConnectionResetError, ConnectionAbortedError):
                break
    except Exception as e:
        logger.info("SSE client disconnected: %s (%s)", session_id, e)
    finally:
        session.close()
        sessions.pop(session_id, None)

    return response


async def handle_messages(request: web.Request) -> web.Response:
    """Handle JSON-RPC messages from MCP clients."""
    session_id = request.query.get('sessionId', '')
    session = sessions.get(session_id)

    if not session:
        return web.json_response(
            {"jsonrpc": "2.0", "error": {"code": -32000, "message": "Invalid session"}},
            status=400,
            headers={'Access-Control-Allow-Origin': '*'},
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}},
            status=400,
            headers={'Access-Control-Allow-Origin': '*'},
        )

    method = body.get("method", "")
    params = body.get("params", {})
    msg_id = body.get("id")

    logger.info("MCP request: method=%s id=%s", method, msg_id)

    result = None

    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": "hermes",
                "version": "0.8.0",
            },
        }

    elif method == "notifications/initialized":
        # No response needed for notifications
        return web.Response(
            status=204,
            headers={'Access-Control-Allow-Origin': '*'},
        )

    elif method == "tools/list":
        tools = get_tools_list()
        result = {"tools": tools}

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        tool_result = await execute_tool(tool_name, arguments)
        if "error" in tool_result:
            result = {
                "content": [{"type": "text", "text": tool_result["error"]}],
                "isError": True,
            }
        else:
            result = tool_result

    elif method == "ping":
        result = {}

    else:
        response_data = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
        resp = web.json_response(response_data, headers={'Access-Control-Allow-Origin': '*'})
        # Also send via SSE
        await session.send_event("message", json.dumps(response_data))
        return resp

    response_data = {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": result,
    }

    # Send response via SSE channel
    await session.send_event("message", json.dumps(response_data))

    return web.json_response(
        response_data,
        headers={'Access-Control-Allow-Origin': '*'},
    )


async def handle_telegram_proxy(request: web.Request) -> web.Response:
    """Proxy /telegram requests to Hermes gateway on internal port."""
    target = f"http://127.0.0.1:{GATEWAY_PORT}{request.path}"
    body = await request.read()
    headers = {k: v for k, v in request.headers.items() if k.lower() != 'host'}

    try:
        async with ClientSession() as client:
            async with client.request(
                method=request.method,
                url=target,
                data=body,
                headers=headers,
                timeout=30,
            ) as resp:
                resp_body = await resp.read()
                return web.Response(
                    body=resp_body,
                    status=resp.status,
                    headers={k: v for k, v in resp.headers.items()
                             if k.lower() not in ('transfer-encoding', 'content-encoding')},
                )
    except Exception as e:
        logger.error("Telegram proxy error: %s", e)
        return web.json_response({"error": "Gateway not ready"}, status=502)


async def handle_cors_preflight(request: web.Request) -> web.Response:
    """Handle CORS preflight requests."""
    return web.Response(
        status=200,
        headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': '*',
            'Access-Control-Max-Age': '86400',
        },
    )


def create_app() -> web.Application:
    app = web.Application()
    
    # CORS preflight for all paths
    app.router.add_route('OPTIONS', '/{path:.*}', handle_cors_preflight)
    
    # Health check
    app.router.add_get('/health', handle_health)
    
    # MCP SSE endpoints
    app.router.add_get('/sse', handle_sse)
    app.router.add_post('/messages', handle_messages)
    
    # Telegram proxy
    app.router.add_route('*', '/telegram', handle_telegram_proxy)
    app.router.add_route('*', '/telegram/{path:.*}', handle_telegram_proxy)
    
    # Root health check
    app.router.add_get('/', handle_health)
    
    return app


def main():
    logger.info("Starting combined server on 0.0.0.0:%d", SERVER_PORT)
    logger.info("  MCP SSE: /sse, /messages")
    logger.info("  Telegram proxy: /telegram -> 127.0.0.1:%d", GATEWAY_PORT)
    logger.info("  Health: /health")
    
    app = create_app()
    web.run_app(app, host='0.0.0.0', port=SERVER_PORT, print=None)


if __name__ == "__main__":
    main()
