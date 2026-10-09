#!/usr/bin/env python3
"""MCP server para ComfyUI. Corre en el pod y habla con ComfyUI en 127.0.0.1:8188.

Transporte: stdio (OpenCode lo lanza con `ssh vast-pod /workspace/mcp-comfy/run.sh`).
Nada se imprime en stdout salvo el protocolo MCP; los logs van a stderr.
La generación es asíncrona: generate_image/run_workflow encolan y devuelven
prompt_id; get_result consulta con esperas cortas (máx. 25 s por llamada).
"""
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
OUTPUT_DIR = Path(os.environ.get("COMFY_OUTPUT", "/workspace/ComfyUI/output"))
WORKFLOWS_DIR = Path(__file__).resolve().parent / "workflows"
HTTP_TIMEOUT = 15
MAX_WAIT = 25
CLIENT_ID = f"mcp-{uuid.uuid4().hex[:8]}"

mcp = FastMCP("comfyui")


class ComfyError(Exception):
    pass


def _request(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{COMFY_URL}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:2000]
        raise ComfyError(f"ComfyUI respondió HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ComfyError(
            f"No se pudo contactar ComfyUI en {COMFY_URL} ({e}). "
            "¿Está iniciado en el pod (sesión tmux 'comfy')?"
        ) from None
    return json.loads(raw) if raw else {}


def _error(e: Exception) -> dict:
    return {"ok": False, "error": str(e)}


def _gib(n) -> float:
    return round((n or 0) / 1024**3, 2)


def _queue_ids() -> tuple[set, set]:
    q = _request("GET", "/queue")
    running = {item[1] for item in q.get("queue_running", [])}
    pending = {item[1] for item in q.get("queue_pending", [])}
    return running, pending


def _submit(workflow: dict) -> dict:
    res = _request("POST", "/prompt", {"prompt": workflow, "client_id": CLIENT_ID})
    if res.get("node_errors"):
        raise ComfyError(f"Errores en nodos del workflow: {json.dumps(res['node_errors'])[:2000]}")
    return {
        "ok": True,
        "prompt_id": res["prompt_id"],
        "queue_position": res.get("number"),
        "next": "Llama a get_result con este prompt_id hasta que status sea 'success' o 'error'.",
    }


def _list_folder(folder: str) -> list[str]:
    return _request("GET", f"/models/{folder}")


@mcp.tool()
def comfy_status() -> dict:
    """Estado de ComfyUI en el pod: versión, VRAM total/libre y trabajos en cola.
    Úsalo antes de generar para comprobar que el servicio responde y hay VRAM libre."""
    try:
        stats = _request("GET", "/system_stats")
        running, pending = _queue_ids()
        dev = (stats.get("devices") or [{}])[0]
        return {
            "ok": True,
            "comfyui_version": stats.get("system", {}).get("comfyui_version"),
            "gpu": dev.get("name"),
            "vram_total_gib": _gib(dev.get("vram_total")),
            "vram_free_gib": _gib(dev.get("vram_free")),
            "queue_running": len(running),
            "queue_pending": len(pending),
        }
    except ComfyError as e:
        return _error(e)


@mcp.tool()
def list_models(folder: str = "checkpoints") -> dict:
    """Lista archivos de modelos instalados en ComfyUI.
    folder: checkpoints, loras, vae, upscale_models, controlnet, clip, unet, diffusion_models, etc.
    Usa folder="" para ver qué carpetas existen."""
    try:
        if not folder:
            return {"ok": True, "folders": _request("GET", "/models")}
        return {"ok": True, "folder": folder, "files": _list_folder(folder)}
    except ComfyError as e:
        return _error(e)


@mcp.tool()
def generate_image(
    prompt: str,
    negative_prompt: str = "",
    checkpoint: str = "",
    width: int = 1024,
    height: int = 1024,
    steps: int = 25,
    cfg: float = 6.0,
    sampler: str = "euler",
    scheduler: str = "normal",
    seed: int = -1,
    batch_size: int = 1,
    lora: str = "",
    lora_strength: float = 0.8,
    filename_prefix: str = "mcp",
) -> dict:
    """Encola una generación texto→imagen con el workflow básico (checkpoint + KSampler).
    Válido para SD1.5 / SDXL y derivados. Para Flux u otros, usa run_workflow.
    checkpoint vacío = el primero instalado. seed -1 = aleatoria.
    Devuelve prompt_id; luego llama a get_result."""
    try:
        if not checkpoint:
            ckpts = _list_folder("checkpoints")
            if not ckpts:
                return _error(ComfyError("No hay checkpoints instalados en ComfyUI/models/checkpoints."))
            checkpoint = ckpts[0]
        if not (64 <= width <= 2048 and 64 <= height <= 2048):
            return _error(ComfyError("width/height deben estar entre 64 y 2048."))
        if not 1 <= batch_size <= 4:
            return _error(ComfyError("batch_size debe estar entre 1 y 4."))
        if not 1 <= steps <= 100:
            return _error(ComfyError("steps debe estar entre 1 y 100."))
        if seed < 0:
            seed = random.randint(0, 2**32 - 1)

        model_src, clip_src = ["4", 0], ["4", 1]
        wf = {
            "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
            "5": {"class_type": "EmptyLatentImage",
                  "inputs": {"width": width, "height": height, "batch_size": batch_size}},
        }
        if lora:
            wf["10"] = {"class_type": "LoraLoader", "inputs": {
                "lora_name": lora, "strength_model": lora_strength, "strength_clip": lora_strength,
                "model": ["4", 0], "clip": ["4", 1]}}
            model_src, clip_src = ["10", 0], ["10", 1]
        wf.update({
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": clip_src}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative_prompt, "clip": clip_src}},
            "3": {"class_type": "KSampler", "inputs": {
                "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": sampler,
                "scheduler": scheduler, "denoise": 1.0, "model": model_src,
                "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": filename_prefix, "images": ["8", 0]}},
        })
        result = _submit(wf)
        result.update({"checkpoint": checkpoint, "seed": seed})
        return result
    except ComfyError as e:
        return _error(e)


@mcp.tool()
def list_workflows() -> dict:
    """Lista los workflows guardados (formato API de ComfyUI) disponibles para run_workflow,
    con los nodos y entradas que se pueden sobrescribir."""
    out = []
    for f in sorted(WORKFLOWS_DIR.glob("*.json")):
        try:
            wf = json.loads(f.read_text())
            nodes = {nid: {"class_type": n.get("class_type"),
                           "inputs": [k for k, v in n.get("inputs", {}).items() if not isinstance(v, list)]}
                     for nid, n in wf.items()}
            out.append({"name": f.stem, "nodes": nodes})
        except (json.JSONDecodeError, AttributeError) as e:
            out.append({"name": f.stem, "error": f"JSON inválido: {e}"})
    return {"ok": True, "workflows_dir": str(WORKFLOWS_DIR), "workflows": out}


@mcp.tool()
def run_workflow(name: str, overrides: dict | None = None) -> dict:
    """Encola un workflow guardado en workflows/<name>.json (exportado con 'Export (API)').
    overrides: {"<node_id>.<input>": valor}, p. ej. {"6.text": "un gato", "3.seed": 42}.
    Devuelve prompt_id; luego llama a get_result."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return _error(ValueError("Nombre de workflow inválido (solo letras, números, _ y -)."))
    path = WORKFLOWS_DIR / f"{name}.json"
    if not path.is_file():
        return _error(FileNotFoundError(f"No existe {path}. Usa list_workflows."))
    try:
        wf = json.loads(path.read_text())
        for key, value in (overrides or {}).items():
            node_id, _, field = key.partition(".")
            if node_id not in wf or not field:
                return _error(KeyError(f"Override inválido '{key}': nodo o campo inexistente."))
            wf[node_id].setdefault("inputs", {})[field] = value
        return _submit(wf)
    except (ComfyError, json.JSONDecodeError) as e:
        return _error(e)


@mcp.tool()
def get_result(prompt_id: str, wait_seconds: int = 20) -> dict:
    """Consulta un trabajo. Espera hasta wait_seconds (máx. 25) a que termine.
    status: queued | running | success | error. En success devuelve las rutas de las imágenes
    en el pod. Si sigue queued/running, vuelve a llamar."""
    deadline = time.monotonic() + max(0, min(wait_seconds, MAX_WAIT))
    try:
        while True:
            hist = _request("GET", f"/history/{prompt_id}").get(prompt_id)
            if hist:
                st = hist.get("status", {})
                if st.get("status_str") == "error":
                    msgs = [m for m in st.get("messages", []) if m and m[0] == "execution_error"]
                    return {"ok": False, "status": "error", "prompt_id": prompt_id,
                            "detail": json.dumps(msgs)[:2000]}
                images = []
                for node_out in hist.get("outputs", {}).values():
                    for img in node_out.get("images", []):
                        if img.get("type") == "output":
                            rel = Path(img.get("subfolder", "")) / img["filename"]
                            images.append({"relative": str(rel), "pod_path": str(OUTPUT_DIR / rel)})
                return {"ok": True, "status": "success", "prompt_id": prompt_id, "images": images,
                        "sync_hint": "En la máquina local: pod-sync <carpeta_del_proyecto>/outputs"}
            running, pending = _queue_ids()
            if prompt_id in running:
                status = "running"
            elif prompt_id in pending:
                status = "queued"
            else:
                return {"ok": False, "status": "unknown", "prompt_id": prompt_id,
                        "detail": "No está en cola ni en historial (¿se reinició ComfyUI?)."}
            if time.monotonic() >= deadline:
                return {"ok": True, "status": status, "prompt_id": prompt_id,
                        "next": "Aún no termina; vuelve a llamar a get_result."}
            time.sleep(1)
    except ComfyError as e:
        return _error(e)


@mcp.tool()
def cancel_job(prompt_id: str) -> dict:
    """Cancela un trabajo: lo quita de la cola si está pendiente o interrumpe si está en curso."""
    try:
        running, pending = _queue_ids()
        if prompt_id in pending:
            _request("POST", "/queue", {"delete": [prompt_id]})
            return {"ok": True, "action": "removed_from_queue"}
        if prompt_id in running:
            _request("POST", "/interrupt", {"prompt_id": prompt_id})
            return {"ok": True, "action": "interrupted"}
        return {"ok": False, "detail": "El trabajo no está en cola ni en ejecución."}
    except ComfyError as e:
        return _error(e)


@mcp.tool()
def list_outputs(limit: int = 20, subfolder: str = "") -> dict:
    """Lista las imágenes más recientes en la carpeta de outputs del pod (no borra nada)."""
    base = OUTPUT_DIR.resolve()
    root = (base / subfolder).resolve()
    if base != root and base not in root.parents:
        return _error(ValueError("subfolder fuera de la carpeta de outputs."))
    if not root.is_dir():
        return _error(FileNotFoundError(f"No existe {root}"))
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4"}]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    limit = max(1, min(limit, 200))
    return {"ok": True, "total": len(files), "files": [
        {"relative": str(p.relative_to(base)), "size_kb": round(p.stat().st_size / 1024),
         "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))}
        for p in files[:limit]]}


@mcp.tool()
def free_comfy_vram() -> dict:
    """Descarga los modelos de ComfyUI de la VRAM (no afecta a Ollama).
    Útil para dejar sitio al LLM cuando ya no vas a generar imágenes."""
    try:
        _request("POST", "/free", {"unload_models": True, "free_memory": True})
        return {"ok": True, "detail": "Modelos de ComfyUI descargados de la VRAM."}
    except ComfyError as e:
        return _error(e)


if __name__ == "__main__":
    mcp.run()
