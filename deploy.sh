#!/usr/bin/env bash
# Copia mcp-comfy al pod y prepara su entorno. No borra nada remoto (sin --delete).
set -euo pipefail
cd "$(dirname "$0")"
HOST="${POD_HOST:-vast-pod}"

timeout 120 rsync -av --protect-args --exclude venv --exclude __pycache__ \
  -e "ssh -o BatchMode=yes -o ConnectTimeout=15 -o LogLevel=ERROR" \
  mcp-comfy/ "$HOST:/workspace/mcp-comfy/"
timeout 600 ssh -o BatchMode=yes -o ConnectTimeout=15 -o LogLevel=ERROR "$HOST" \
  'bash /workspace/mcp-comfy/setup.sh'
