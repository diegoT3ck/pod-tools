#!/usr/bin/env bash
# Crea el entorno local del MCP (.venv). Usa PyPI público explícitamente para no depender
# de índices privados configurados globalmente en pip.
set -euo pipefail
cd "$(dirname "$0")"
[[ -d .venv ]] || python3 -m venv .venv
timeout 300 .venv/bin/pip install -q --index-url https://pypi.org/simple -r mcp-comfy/requirements.txt
chmod +x mcp-comfy/run.sh pod/start-services.sh
.venv/bin/python -c "import websocket, mcp.server.fastmcp; print('mcp-comfy listo')"
