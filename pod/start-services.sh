#!/usr/bin/env bash
# Arranca Ollama y ComfyUI en el pod (idempotente). Ejecutar tras cada encendido:
#   bash /workspace/pod-tools/pod/start-services.sh
# Ollama se arranca y precarga primero para que el LLM reserve su VRAM antes que ComfyUI.
set -uo pipefail

OLLAMA_MODELS_DIR="${OLLAMA_MODELS_DIR:-/workspace/ollama-models}"
COMFY_DIR="${COMFY_DIR:-/workspace/ComfyUI}"
LLM_MODEL="${LLM_MODEL:-huihui_ai/Qwen3.8-abliterated:27b}"
RESERVE_VRAM_GB="${RESERVE_VRAM_GB:-2}"   # margen para que el LLM pueda crecer su contexto

start() {
  local name="$1" cmd="$2"
  if tmux has-session -t "$name" 2>/dev/null; then
    echo "· $name ya estaba corriendo"
  else
    tmux new-session -d -s "$name" "$cmd"
    echo "· $name iniciado (tmux attach -t $name para ver la consola)"
  fi
}

wait_for() {
  local name="$1" url="$2" secs="$3"
  for _ in $(seq 1 "$secs"); do
    curl -s -m 2 -o /dev/null "$url" && { echo "✓ $name responde"; return 0; }
    sleep 1
  done
  echo "✗ $name no responde tras ${secs}s (revisa su log en /workspace)"
  return 1
}

# FLASH_ATTENTION=0 y CUDA_DISABLE_GRAPHS=1: sin ellos el modelo qwen35 se cuelga en la RTX 5090.
start ollama "OLLAMA_FLASH_ATTENTION=0 GGML_CUDA_DISABLE_GRAPHS=1 OLLAMA_KEEP_ALIVE=24h \
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_MODELS=$OLLAMA_MODELS_DIR ollama serve 2>&1 | tee -a /workspace/ollama-serve.log"
wait_for Ollama http://127.0.0.1:11434/api/version 30

if [[ -n "$LLM_MODEL" ]]; then
  echo "· precargando $LLM_MODEL (hasta 3 min)…"
  if timeout 180 curl -s -o /dev/null -w '' http://127.0.0.1:11434/api/generate \
       -d "{\"model\":\"$LLM_MODEL\",\"keep_alive\":\"24h\"}"; then
    echo "✓ LLM cargado en VRAM"
  else
    echo "✗ el LLM no terminó de cargar en 3 min (seguirá en segundo plano)"
  fi
fi

start comfy "cd '$COMFY_DIR' && . venv/bin/activate && \
python main.py --listen 127.0.0.1 --port 8188 --reserve-vram $RESERVE_VRAM_GB 2>&1 | tee -a /workspace/comfy.log"
wait_for ComfyUI http://127.0.0.1:8188/ 120

nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader | sed 's/^/VRAM usada: /'
