#!/usr/bin/env bash
# Lanzador stdio del MCP de ComfyUI. Lo ejecuta OpenCode vía: ssh vast-pod /workspace/mcp-comfy/run.sh
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x venv/bin/python ]]; then
  echo "mcp-comfy: falta el venv; ejecuta setup.sh en el pod" >&2
  exit 1
fi
exec venv/bin/python -u server.py
