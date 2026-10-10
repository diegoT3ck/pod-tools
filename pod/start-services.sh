#!/usr/bin/env bash
# Arranca Ollama y ComfyUI en el pod (idempotente). Ejecutar tras cada encendido:
#   bash /workspace/pod-tools/pod/start-services.sh
# Ollama se arranca y precarga primero para que el LLM reserve su VRAM antes que ComfyUI.
set -uo pipefail

OLLAMA_MODELS_DIR="${OLLAMA_MODELS_DIR:-/workspace/ollama-models}"
COMFY_DIR="${COMFY_DIR:-/workspace/ComfyUI}"
# LLM a precargar (con visión, para que pueda ver lo que genera). LLM_MODEL="" = no precargar nada.
LLM_MODEL="${LLM_MODEL-huihui_ai/Qwen3.8-abliterated:27b}"
START_COMFY="${START_COMFY:-1}"           # 0 = solo Ollama (lo usa bootstrap.sh)
RESERVE_VRAM_GB="${RESERVE_VRAM_GB:-0.5}" # margen libre; el LLM reserva todo al cargar y no crece
# --disable-dynamic-vram: con "dynamic VRAM" ComfyUI recopia los pesos desde RAM en cada paso;
# en hosts con PCIe estrecho (Gen3 x4 medido) eso hacía SDXL ~1.6x más lento.
# --disable-cuda-malloc: el asignador clásico devuelve la VRAM al liberar (residuo 0.9 GB vs 1.3 GB).
COMFY_EXTRA_ARGS="${COMFY_EXTRA_ARGS---disable-dynamic-vram --disable-cuda-malloc}"

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

[[ "$START_COMFY" == 1 ]] || exit 0

start comfy "cd '$COMFY_DIR' && . venv/bin/activate && \
python main.py --listen 127.0.0.1 --port 8188 --reserve-vram $RESERVE_VRAM_GB $COMFY_EXTRA_ARGS 2>&1 | tee -a /workspace/comfy.log"
wait_for ComfyUI http://127.0.0.1:8188/ 120

nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader | sed 's/^/VRAM usada: /'
