# pod-tools

MCP para controlar **ComfyUI** en un pod GPU remoto (p. ej. Vast.ai) desde un agente como
**OpenCode**, entregando cada imagen o vídeo directamente en la carpeta del proyecto local.

```
OpenCode ──stdio──▶ mcp-comfy (local)
                     ├─ HTTP/WebSocket por túnel SSH :8188 ──▶ ComfyUI (pod): generar, progreso, descargar
                     ├─ HTTP por túnel SSH :11435 ───────────▶ Ollama (pod): VRAM ocupada por el LLM
                     └─ ssh <host> ─────────────────────────▶ verificar SHA-256 y borrar el original
```

Ningún servicio se expone a Internet: todo va por túnel SSH ligado a `127.0.0.1`.

## Qué hace

- **Abre el túnel** con `pod-up` si no está abierto; si no puede, devuelve el error.
- **Preflight de VRAM** (GPU compartida con el LLM): si no alcanza, pide confirmación.
- **Estimación** de tiempo y peso al encolar, afinada con el historial de trabajos
  (`~/.cache/mcp-comfy/history.json`).
- **Progreso en vivo** (WebSocket de ComfyUI): paso, %, nodo, tiempo restante. Las llamadas
  esperan como máximo 25 s para no chocar con los timeouts del cliente MCP.
- **Entrega**: descarga a `dest_dir`, verifica el hash contra el pod y **borra el original
  remoto solo si coincide**. Nunca sobrescribe archivos locales.

Herramientas: `pod_status`, `list_models`, `generate_image`, `list_workflows`, `run_workflow`,
`get_result`, `cancel_job`, `list_pending_outputs`, `deliver_pending`, `free_comfy_vram`.

## Estructura

- `mcp-comfy/server.py`: el servidor MCP. `mcp-comfy/run.sh`: lanzador stdio.
- `mcp-comfy/workflows/`: workflows exportados con *Export (API)* para `run_workflow` (vídeo, Flux…).
- `pod/start-services.sh`: arranca Ollama (y precarga el LLM) y ComfyUI en el pod.
- `skill/SKILL.md`: skill de OpenCode con el flujo de uso.
- `setup.sh`: crea `.venv` local con las dependencias.

## Instalación

Local:

```bash
./setup.sh
mkdir -p ~/.config/opencode/skills/comfyui && cp skill/SKILL.md ~/.config/opencode/skills/comfyui/
```

En `opencode.json`, dentro de `mcp`:

```json
"comfyui": { "type": "local", "command": ["/ruta/a/pod-tools/mcp-comfy/run.sh"], "enabled": true }
```

En el pod (tras cada encendido):

```bash
git clone https://github.com/diegoT3ck/pod-tools /workspace/pod-tools   # la primera vez
bash /workspace/pod-tools/pod/start-services.sh
```

## Pod nuevo en 3 pasos

1. **Crea el pod** en Vast.ai (GPU NVIDIA con driver ≥ 570, ~100 GB de disco) con tu clave
   pública en *Account → Keys*. Copia host y puerto de la ventana SSH.
2. **Configura tu máquina** (backup automático de `~/.ssh/config` y `known_hosts`, te muestra
   las huellas del host y pide confirmación) y lanza la instalación en el pod:
   ```bash
   local/configure-pod.sh ssh9.vast.ai 21609 --bootstrap
   ssh vast-pod tail -f /workspace/bootstrap.log     # seguir el progreso (Ctrl+C no lo detiene)
   ```
3. **Úsalo** desde OpenCode con el modelo `ollama-pod/huihui_ai/Qwen3.8-abliterated:27b` y el MCP `comfyui`.

`pod/bootstrap.sh` es idempotente y hace todo en automático:

| Fase | Qué hace | Test tras instalar |
|---|---|---|
| 0. Preflight | Revisa root, GPU, driver, Python, herramientas, red, registro de Ollama, URLs de modelos y disco necesario. **No instala nada si algo falla.** | — |
| 1. Sistema | `apt-get` solo de lo que falte (git, tmux, curl, zstd, python3-venv) | herramientas presentes |
| 2. Ollama | instalador oficial con `OLLAMA_VERSION`, arranca `ollama serve` | versión y servidor responden |
| 3. LLM | `ollama pull` (+ variante solo texto opcional con `LLM_TEXT_ONLY=1`: ~5.5 GB menos de VRAM, pero no ve imágenes) | genera texto, 100% en GPU |
| 4. ComfyUI | clona, venv, torch (`TORCH_INDEX`), requirements | CUDA + matmul en GPU, imports |
| 5. Modelos | descarga `pod/models.txt` (reanudable) | tamaño y SHA-256 contra Hugging Face |
| 6. Servicios | `start-services.sh` (LLM primero, luego ComfyUI) | ComfyUI en CUDA, ve cada modelo, LLM cargado |

Variables (por entorno): `OLLAMA_VERSION`, `LLM_MODEL`, `LLM_TEXT_ONLY`, `LLM_TEXT_NAME`,
`TORCH_INDEX`, `MIN_DRIVER`, `MODELS_FILE` (vacío = sin modelos), `VERIFY_SHA`,
`RESERVE_VRAM_GB`. Para otros modelos de ComfyUI, edita `pod/models.txt`.

Tras reiniciar un pod ya instalado basta `bash /workspace/pod-tools/pod/start-services.sh`
(si cambió host/puerto, antes `local/configure-pod.sh <host> <puerto>`).

## Requisitos y configuración

- `Host` SSH (por defecto `vast-pod`) con autenticación por clave y un script `pod-up` que abra
  los túneles `127.0.0.1:8188 → pod:8188` y `127.0.0.1:11435 → pod:11434`.
- Variables opcionales: `COMFY_URL`, `OLLAMA_URL`, `POD_HOST`, `POD_UP`,
  `COMFY_REMOTE_OUTPUT` (por defecto `/workspace/ComfyUI/output`), `MCP_COMFY_STATE`.
