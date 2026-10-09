#!/usr/bin/env bash
# Instala el entorno del MCP en el pod (una sola vez, o tras cambiar requirements.txt)
set -euo pipefail
cd "$(dirname "$0")"
[[ -d venv ]] || python3 -m venv venv
timeout 300 venv/bin/pip install -q --upgrade pip
timeout 300 venv/bin/pip install -q -r requirements.txt
mkdir -p workflows
chmod +x run.sh
venv/bin/python -c "import mcp.server.fastmcp; print('mcp-comfy listo')"
