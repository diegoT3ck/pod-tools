---
name: comfyui
description: Generar imágenes con ComfyUI en el pod GPU remoto (Vast.ai) mediante las herramientas MCP `comfyui_*`. Úsala cuando el usuario pida crear, generar, iterar o revisar imágenes, listar modelos/LoRAs de ComfyUI, o traer outputs del pod a su proyecto.
---

# ComfyUI en el pod remoto

ComfyUI corre en el pod (RTX 5090, 32 GB VRAM) y se controla con las herramientas
del MCP `comfyui`. Las imágenes se guardan **en el pod** (`/workspace/ComfyUI/output`);
no llegan al proyecto local hasta que se sincronizan.

## Flujo obligatorio

1. **`comfy_status`** primero. Si `ok` es false, ComfyUI no está iniciado o el pod está
   apagado: díselo al usuario y detente. No intentes arreglarlo tú.
2. **`list_models`** (`checkpoints`, y `loras` si hace falta) para usar nombres exactos.
   Nunca inventes nombres de archivos de modelo.
3. **`generate_image`** (o `run_workflow` para workflows guardados). Devuelve un `prompt_id`
   al instante; la imagen todavía no existe.
4. **`get_result`** con ese `prompt_id`, repitiendo mientras `status` sea `queued` o `running`.
   Cada llamada espera como máximo 25 s. Si tras ~10 llamadas sigue sin terminar, informa
   al usuario y pregunta si cancelar (`cancel_job`).
5. Al terminar, indica al usuario las rutas devueltas y que las traiga con:
   `pod-sync <carpeta_del_proyecto>/outputs` (ejecútalo tú solo si el usuario lo pide).

## Memoria de la GPU (importante)

La GPU se comparte con el LLM de Ollama, que ocupa ~22–24 GB. Quedan ~8–10 GB:

- SD 1.5 / SDXL: caben. Flux/SD3.5 completos: no caben junto al LLM.
- Revisa `vram_free_gib` en `comfy_status`. Si es < 6 GB, avisa al usuario de que
  la generación será lenta (ComfyUI descarga parte a RAM).
- Cuando el usuario termine de generar, ofrece `free_comfy_vram` para devolver memoria al LLM.
- No subas `batch_size` por encima de 2 ni la resolución por encima de ~1536 px sin
  que el usuario lo pida.

## Parámetros orientativos

| Familia | Resolución | steps | cfg | sampler / scheduler |
|---|---|---|---|---|
| SDXL / Pony / Illustrious | 1024×1024, 832×1216, 1216×832 | 25–30 | 5–7 | `euler` o `dpmpp_2m` / `karras` |
| SD 1.5 | 512×512, 512×768 | 20–30 | 6–8 | `dpmpp_2m` / `karras` |

- Prompts en inglés, descriptivos: sujeto, acción, entorno, estilo, iluminación, cámara.
- Usa `negative_prompt` para defectos comunes (`blurry, lowres, bad anatomy, watermark, text`).
- Para iterar sobre una imagen que gustó, reutiliza la misma `seed` y cambia solo el prompt.
- Comprueba en el nombre del checkpoint si es SD1.5 o SDXL antes de elegir resolución.

## Workflows guardados

`list_workflows` muestra los workflows en `/workspace/mcp-comfy/workflows/` (exportados desde
ComfyUI con *Export (API)*) y qué entradas se pueden sobrescribir. `run_workflow` acepta
`overrides` como `{"6.text": "...", "3.seed": 42}`. Úsalos para Flux, img2img, upscale, etc.

## Reglas

- Nunca borres outputs ni modelos del pod; ninguna herramienta lo hace y no lo intentes por bash.
- No descargues modelos ni checkpoints: eso lo decide el usuario.
- No expongas puertos ni cambies la configuración de red; el acceso es solo por túnel SSH.
