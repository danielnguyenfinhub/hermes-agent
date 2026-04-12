#!/bin/bash
set -e

# Source the virtual environment
source /opt/hermes/.venv/bin/activate

# Start MCP HTTP server in background (independent of gateway)
echo "Starting MCP HTTP server on port 3000..."
python /opt/hermes/mcp_http_serve.py &
MCP_PID=$!

# Give MCP server a moment to start
sleep 2

# Start the gateway (Telegram etc) - if it crashes, MCP keeps running
echo "Starting Hermes gateway..."
while true; do
    hermes gateway || true
    echo "Gateway exited, restarting in 10 seconds..."
    sleep 10
done
