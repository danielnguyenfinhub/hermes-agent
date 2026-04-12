#!/usr/bin/env python3
"""
Hermes MCP HTTP Server — exposes 10 Hermes messaging tools via HTTP/SSE
for remote MCP clients like Claude.ai.

Starts alongside the Hermes gateway (Telegram, Discord, etc.) and provides
HTTP access to conversations, messages, and platform channels.

Tools exposed:
  conversations_list, conversation_get, messages_read, attachments_fetch,
  events_poll, events_wait, messages_send, channels_list,
  permissions_list_open, permissions_respond

Usage:
    MCP_PORT=8080 python mcp_http_serve.py
"""
import os
import sys
import logging

# Ensure Hermes source is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp_serve import create_mcp_server, EventBridge

logger = logging.getLogger("hermes.mcp_http")


def main():
    port = int(os.environ.get("MCP_PORT", "8080"))
    host = "0.0.0.0"

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logger.info("Starting Hermes MCP HTTP server on %s:%d", host, port)

    bridge = EventBridge()
    bridge.start()

    mcp = create_mcp_server(event_bridge=bridge)

    # Configure host/port for SSE transport
    try:
        settings = getattr(mcp, "settings", getattr(mcp, "_settings", None))
        if settings is not None:
            settings.host = host
            settings.port = port
    except Exception as e:
        logger.warning("Could not set FastMCP settings: %s", e)

    try:
        # FastMCP.run(transport="sse") starts a uvicorn SSE server
        mcp.run(transport="sse")
    except TypeError:
        # Older mcp versions may not support transport arg — fall back
        logger.info("Falling back to manual SSE server")
        _run_manual_sse(mcp, host, port)
    finally:
        bridge.stop()


def _run_manual_sse(mcp, host: str, port: int):
    """Fallback: manually create SSE transport with CORS support."""
    import asyncio

    try:
        import uvicorn
        from starlette.applications import Starlette
        from starlette.routing import Route, Mount
        from starlette.responses import JSONResponse
    except ImportError:
        logger.error("uvicorn/starlette not installed — cannot start HTTP server")
        sys.exit(1)

    from mcp.server.sse import SseServerTransport

    sse = SseServerTransport("/messages/")
    server = mcp._mcp_server

    async def handle_sse(request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )

    async def health(request):
        return JSONResponse({"status": "ok", "server": "hermes-mcp", "tools": 10})

    app = Starlette(
        routes=[
            Route("/health", endpoint=health),
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ]
    )

    # Add CORS middleware
    try:
        from starlette.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
    except ImportError:
        logger.warning("CORS middleware unavailable")

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
