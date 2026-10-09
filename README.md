# pod-tools

Servidor MCP mínimo para controlar **ComfyUI** en un pod GPU remoto (p. ej. Vast.ai)
desde un agente como **OpenCode**, sin exponer puertos: el MCP corre en el pod y el
cliente lo lanza por **stdio sobre SSH**.

```
OpenCode (local) ──ssh vast-pod run.sh──▶ mcp-comfy (pod) ──HTTP 127.0.0.1:8188──▶ ComfyUI (pod)
```

## Contenido

- `mcp-comfy/server.py`: servidor MCP (FastMCP) con herramientas `comfy_status`, `list_models`,
  `generate_image`, `get_result`, `list_workflows`, `run_workflow`, `cancel_job`,
  `list_outputs` y `free_comfy_vram`. La generación es asíncrona: se encola y se consulta
  con `get_result` (máx. 25 s por llamada) para no chocar con los timeouts del cliente MCP.
- `mcp-comfy/run.sh`: lanzador stdio. `mcp-comfy/setup.sh`: crea el venv en el pod.
- `mcp-comfy/workflows/`: workflows propios exportados con *Export (API)* (opcional).
- `deploy.sh`: copia `mcp-comfy` al pod (`/workspace/mcp-comfy`) y ejecuta `setup.sh`.
- `skill/SKILL.md`: skill de OpenCode con el flujo de uso y reglas de VRAM.

## Requisitos

- Entrada `Host vast-pod` en `~/.ssh/config` con autenticación por clave.
- ComfyUI escuchando en `127.0.0.1:8188` dentro del pod.

## Instalación

```bash
./deploy.sh                                   # con el pod encendido
mkdir -p ~/.config/opencode/skills/comfyui
cp skill/SKILL.md ~/.config/opencode/skills/comfyui/
```

En `opencode.json`, dentro de `mcp`:

```json
"comfyui": {
  "type": "local",
  "command": ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
              "-o", "LogLevel=ERROR", "vast-pod", "/workspace/mcp-comfy/run.sh"],
  "enabled": true
}
```

Variables opcionales en el pod: `COMFY_URL` (por defecto `http://127.0.0.1:8188`) y
`COMFY_OUTPUT` (por defecto `/workspace/ComfyUI/output`).
