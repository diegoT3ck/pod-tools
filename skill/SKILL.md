---
name: comfyui
description: Generar imágenes y animaciones/vídeo con ComfyUI en el pod GPU remoto (Vast.ai) mediante las herramientas MCP `comfyui_*`, y entregarlas en la carpeta del proyecto local. Úsala cuando el usuario pida crear, generar o iterar imágenes o vídeos, o listar modelos/LoRAs de ComfyUI.
---

# ComfyUI en el pod remoto

El MCP `comfyui` corre en la máquina local y controla ComfyUI en el pod por túnel SSH
(lo abre solo si hace falta). Al terminar cada trabajo **descarga el resultado al proyecto
local, verifica el hash y borra el original del pod**. No hace falta `pod-sync`.

## Flujo obligatorio

1. **`pod_status`** primero. Si `ok` es false (pod apagado, túnel imposible, ComfyUI caído),
   muestra el error al usuario tal cual y detente. No intentes arreglarlo por bash.
2. **`list_models`** para usar nombres exactos de checkpoints/LoRAs. Nunca los inventes.
3. **Encola** con `generate_image` (imagen fija SD1.5/SDXL) o `run_workflow` (vídeo, Flux, etc.).
   - `dest_dir`: ruta **absoluta** a `<raíz del proyecto actual>/outputs`.
   - Muestra al usuario el `message` de la respuesta (tiempo y peso estimados).
   - Si devuelve `needs_confirmation`, explica al usuario el problema de VRAM y **pregúntale**
     si repetir con `allow_slow=true` o liberar memoria. No decidas por él.
4. **`get_result`** en bucle mientras `status` sea `queued` o `running`. Cada llamada espera
   ≤ 25 s. **Después de cada llamada muestra el `message`** (paso, %, tiempo restante) para que
   el usuario vea el avance. Imágenes: si pasan ~10 llamadas, pregunta si cancelar
   (`cancel_job`). Vídeo: informa y pregunta antes de pasar de ~40 llamadas.
5. En `success`, informa las rutas locales de `files`, su peso y la duración. Si algún archivo
   dice `CONSERVADO`, avisa de que sigue en el pod.

## Memoria de la GPU

La GPU (32 GB) se comparte con el LLM (~22–24 GB, se mantiene cargado). Quedan ~8–10 GB:

- SD 1.5 / SDXL caben. Vídeo (Wan 2.2 5B) cabe justo y va más lento; 14B no cabe junto al LLM.
- `pod_status` muestra `vram_free_gib` y el LLM cargado. `run_workflow` libera la VRAM de
  ComfyUI al terminar por defecto; para imágenes, ofrece `free_comfy_vram` al acabar una sesión.
- No subas `batch_size` > 2 ni resolución > ~1536 px sin que el usuario lo pida.

## Parámetros orientativos (imagen)

| Familia | Resolución | steps | cfg | sampler / scheduler |
|---|---|---|---|---|
| SDXL / Pony / Illustrious | 1024×1024, 832×1216, 1216×832 | 25–30 | 5–7 | `euler` o `dpmpp_2m` / `karras` |
| SD 1.5 | 512×512, 512×768 | 20–30 | 6–8 | `dpmpp_2m` / `karras` |

- Prompts en inglés y descriptivos: sujeto, acción, entorno, estilo, iluminación, cámara.
- `negative_prompt` para defectos comunes (`blurry, lowres, bad anatomy, watermark, text`).
- Para iterar sobre una imagen que gustó, reutiliza la misma `seed` y cambia solo el prompt.

## Animación y vídeo

`list_workflows` muestra los workflows disponibles, su tiempo estimado y qué entradas acepta
cada nodo; `run_workflow` los ejecuta con `overrides` como `{"6.text": "...", "3.seed": 42}`.
Empieza con pocos frames y resolución baja para validar el prompt; luego sube.

## Archivos olvidados

Si el MCP se reinició a mitad de un trabajo, `list_pending_outputs` muestra lo que quedó en el
pod y `deliver_pending(subfolder=<nombre del proyecto>, dest_dir=...)` lo entrega.

## Reglas

- No borres nada por bash ni en local ni en el pod; el MCP solo borra originales ya verificados.
- No descargues modelos ni checkpoints: eso lo decide el usuario.
- No expongas puertos ni cambies la configuración de red.
