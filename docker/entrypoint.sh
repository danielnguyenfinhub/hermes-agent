#!/bin/bash
set -e

# Source the virtual environment
source /opt/hermes/.venv/bin/activate

echo "Starting Hermes MCP HTTP server on port ${PORT:-3000}..."
exec python /opt/hermes/mcp_http_serve.py
