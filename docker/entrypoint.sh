#!/bin/bash
# Docker entrypoint: bootstrap config files into the mounted volume, then run.
# Combined architecture:
#   - Port 3000: Combined HTTP server (MCP SSE + Telegram proxy)
#   - Port 3001: Hermes gateway (internal only, proxied via combined server)
set -e

HERMES_HOME="/opt/data"
INSTALL_DIR="/opt/hermes"
GATEWAY_INTERNAL_PORT="${GATEWAY_INTERNAL_PORT:-3001}"

# --- Privilege dropping via gosu ---
if [ "$(id -u)" = "0" ]; then
    if [ -n "$HERMES_UID" ] && [ "$HERMES_UID" != "$(id -u hermes)" ]; then
        echo "Changing hermes UID to $HERMES_UID"
        usermod -u "$HERMES_UID" hermes
    fi

    if [ -n "$HERMES_GID" ] && [ "$HERMES_GID" != "$(id -g hermes)" ]; then
        echo "Changing hermes GID to $HERMES_GID"
        groupmod -g "$HERMES_GID" hermes
    fi

    actual_hermes_uid=$(id -u hermes)
    if [ "$(stat -c %u "$HERMES_HOME" 2>/dev/null)" != "$actual_hermes_uid" ]; then
        echo "$HERMES_HOME is not owned by $actual_hermes_uid, fixing"
        chown -R hermes:hermes "$HERMES_HOME"
    fi

    echo "Dropping root privileges"
    exec gosu hermes "$0" "$@"
fi

# --- Running as hermes from here ---
source "${INSTALL_DIR}/.venv/bin/activate"

mkdir -p "$HERMES_HOME"/{cron,sessions,logs,hooks,memories,skills,skins,plans,workspace,home}

# .env
if [ ! -f "$HERMES_HOME/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$HERMES_HOME/.env"
fi

# config.yaml
if [ ! -f "$HERMES_HOME/config.yaml" ]; then
    cp "$INSTALL_DIR/cli-config.yaml.example" "$HERMES_HOME/config.yaml"
fi

# SOUL.md
if [ ! -f "$HERMES_HOME/SOUL.md" ]; then
    cp "$INSTALL_DIR/docker/SOUL.md" "$HERMES_HOME/SOUL.md"
fi

# Sync bundled skills
if [ -d "$INSTALL_DIR/skills" ]; then
    python3 "$INSTALL_DIR/tools/skills_sync.py"
fi

# --- Architecture ---
# 1. Start Hermes gateway on internal port 3001 (background)
# 2. Start combined HTTP server on port 3000 (foreground)
#    - Serves MCP at /sse and /messages/ for Claude.ai
#    - Proxies /telegram to gateway on 3001

echo "Starting Hermes gateway on internal port $GATEWAY_INTERNAL_PORT..."
export TELEGRAM_WEBHOOK_PORT="$GATEWAY_INTERNAL_PORT"
hermes gateway &
GATEWAY_PID=$!
echo "Gateway started (PID $GATEWAY_PID)"

# Give gateway a moment to start
sleep 2

echo "Starting combined HTTP server on port ${PORT:-3000}..."
exec python3 "$INSTALL_DIR/mcp_http_serve.py"
