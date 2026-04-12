#!/usr/bin/env python3
"""
Hermes Combined HTTP Server — port 3000

Serves both:
  1. MCP SSE endpoints (/sse, /messages/) for Claude.ai
  2. Reverse proxy for /telegram to Hermes gateway on internal port 3001

This allows a single Railway domain to handle both MCP and Telegram.
"""
import os
import sys
import logging
import asyncio
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger("hermes.combined")

GATEWAY_PORT = int(os.environ.get("GATEWAY_INTERNAL_PORT", "3001"))
SERVER_PORT = int(os.environ.get("PORT", "3000"))


def main():
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    try:
        import uvicorn
        from starlette.applications import Starlette
        from starlette.routing import Route, Mount
        from starlette.responses import JSONResponse, Response
        from starlette.requests import Request
        from starlette.middleware.cors import CORSMiddleware
    except ImportError as e:
        logger.error("Missing dependency: %s", e)
        sys.exit(1)

    # --- MCP Server setup ---
    mcp_server = None
    sse_transport = None
    try:
        from mcp_serve import create_mcp_server, EventBridge
        from mcp.server.sse import SseServerTransport

        bridge = EventBridge()
        bridge.start()
        mcp_obj = create_mcp_server(event_bridge=bridge)
        mcp_server = mcp_obj._mcp_server
        sse_transport = SseServerTransport("/messages/")
        logger.info("MCP server initialized with 10 tools")
    except Exception as e:
        logger.error("Failed to init MCP server: %s", e)
        logger.info("Will run as proxy-only (no MCP)")

    # --- Route handlers ---
    async def handle_sse(request: Request):
        if not sse_transport or not mcp_server:
            return JSONResponse({"error": "MCP not available"}, status_code=503)
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            await mcp_server.run(
                read_stream, write_stream, mcp_server.create_initialization_options()
            )

    async def health(request: Request):
        return JSONResponse({
            "status": "ok",
            "server": "hermes-combined",
            "mcp_tools": 10 if mcp_server else 0,
            "gateway_port": GATEWAY_PORT,
        })

    async def proxy_to_gateway(request: Request):
        """Reverse proxy any request to the Hermes gateway on internal port."""
        target = f"http://127.0.0.1:{GATEWAY_PORT}{request.url.path}"
        body = await request.body()
        headers = dict(request.headers)
        headers.pop("host", None)

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(
                    method=request.method,
                    url=target,
                    content=body,
                    headers=headers,
                )
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    headers=dict(resp.headers),
                )
        except httpx.ConnectError:
            logger.error("Gateway not reachable at port %d", GATEWAY_PORT)
            return JSONResponse(
                {"error": "Gateway not ready"},
                status_code=502,
            )
        except Exception as e:
            logger.error("Proxy error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=502)

    # Build routes
    routes = [
        Route("/health", endpoint=health),
        Route("/telegram", endpoint=proxy_to_gateway, methods=["GET", "POST"]),
        Route("/telegram/{path:path}", endpoint=proxy_to_gateway, methods=["GET", "POST"]),
    ]

    if sse_transport:
        routes.append(Route("/sse", endpoint=handle_sse))
        routes.append(Mount("/messages/", app=sse_transport.handle_post_message))

    app = Starlette(routes=routes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    logger.info("Starting combined server on 0.0.0.0:%d", SERVER_PORT)
    logger.info("  MCP: /sse, /messages/")
    logger.info("  Telegram proxy: /telegram -> 127.0.0.1:%d", GATEWAY_PORT)

    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)


if __name__ == "__main__":
    main()
