#!/usr/bin/env bash
# Lanzador stdio del MCP (corre en la máquina local; OpenCode lo ejecuta directamente).
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  echo "mcp-comfy: falta .venv; ejecuta ./setup.sh" >&2
  exit 1
fi
exec .venv/bin/python -u mcp-comfy/server.py
