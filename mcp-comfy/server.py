#!/usr/bin/env python3
"""MCP local para ComfyUI en un pod GPU remoto.

Corre en la máquina local (OpenCode lo lanza por stdio) y llega al pod por el
túnel SSH de `pod-up` (ComfyUI 127.0.0.1:8188, Ollama 127.0.0.1:11435). Si el
túnel no está abierto, intenta abrirlo con `pod-up`; si no puede, lo reporta.

Ciclo de un trabajo:
  generate_image / run_workflow  -> preflight de VRAM + estimación de tiempo y peso, encola
  get_result (máx. 25 s/llamada) -> progreso paso a paso (WebSocket de ComfyUI)
                                 -> al terminar: descarga a dest_dir, verifica SHA-256 contra
                                    el pod y borra el original remoto solo si coincide.
Nada se escribe en stdout salvo el protocolo MCP; los logs van a stderr.
"""
import hashlib
import json
import os
import re
import shlex
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import websocket
from mcp.server.fastmcp import FastMCP

COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11435").rstrip("/")
POD_HOST = os.environ.get("POD_HOST", "vast-pod")
POD_UP = os.path.expanduser(os.environ.get("POD_UP", "~/bin/pod-up"))
REMOTE_OUTPUT = os.environ.get("COMFY_REMOTE_OUTPUT", "/workspace/ComfyUI/output")
STATE_DIR = Path(os.path.expanduser(os.environ.get("MCP_COMFY_STATE", "~/.cache/mcp-comfy")))
WORKFLOWS_DIR = Path(__file__).resolve().parent / "workflows"

HTTP_TIMEOUT = 15
MAX_WAIT = 25
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR"]
CLIENT_ID = f"mcp-{uuid.uuid4().hex[:8]}"

# VRAM orientativa (GiB) por familia de modelo, para el preflight.
VRAM_NEED = {"sd15": 4.5, "sdxl": 8.0, "workflow": 10.0}
# Segundos por paso y megapíxel por defecto (RTX 5090) hasta tener historial propio.
DEFAULT_S_PER_STEP_MP = {"sd15": 0.05, "sdxl": 0.10}
MODEL_LOAD_OVERHEAD_S = 8
PNG_BYTES_PER_PIXEL = 1.6

mcp = FastMCP("comfyui")
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def log(msg: str) -> None:
    print(f"[mcp-comfy] {msg}", file=sys.stderr, flush=True)


class ComfyError(Exception):
    pass


def _error(e) -> dict:
    return {"ok": False, "error": str(e)}


def _gib(n) -> float:
    return round((n or 0) / 1024**3, 2)


# --------------------------------------------------------------------------- túnel

def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _ensure_tunnel() -> None:
    """Abre el túnel con pod-up si ComfyUI no es alcanzable. Lanza ComfyError si no puede."""
    comfy_port = urllib.parse.urlparse(COMFY_URL).port or 80
    if _port_open(comfy_port):
        return
    if not os.access(POD_UP, os.X_OK):
        raise ComfyError(f"El túnel no está abierto y no encuentro {POD_UP} para abrirlo.")
    log("túnel cerrado; ejecutando pod-up")
    try:
        r = subprocess.run([POD_UP], capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        raise ComfyError("pod-up no terminó en 45 s. ¿Está encendido el pod?") from None
    if r.returncode != 0 or not _port_open(comfy_port):
        detail = (r.stderr or r.stdout).strip()[-800:]
        raise ComfyError(
            "No pude abrir el túnel al pod. Comprueba que el pod está encendido y que el "
            f"puerto SSH no cambió. Detalle de pod-up:\n{detail}"
        )


# --------------------------------------------------------------------------- HTTP

def _request(method: str, url: str, body: dict | None = None, timeout: int = HTTP_TIMEOUT):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:2000]
        raise ComfyError(f"HTTP {e.code} en {url}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ComfyError(f"Sin respuesta de {url} ({e}). ¿Servicio iniciado en el pod?") from None
    return json.loads(raw) if raw else {}


def comfy(method: str, path: str, body: dict | None = None):
    _ensure_tunnel()
    return _request(method, f"{COMFY_URL}{path}", body)


def _queue_state() -> tuple[set, list]:
    q = comfy("GET", "/queue")
    running = {item[1] for item in q.get("queue_running", [])}
    pending = [item[1] for item in sorted(q.get("queue_pending", []), key=lambda i: i[0])]
    return running, pending


def _vram() -> dict:
    stats = comfy("GET", "/system_stats")
    dev = (stats.get("devices") or [{}])[0]
    # torch_vram_total = VRAM que ya tiene reservada ComfyUI (modelos en caché); puede reutilizarla
    # o liberarla, así que cuenta como disponible para el siguiente trabajo.
    info = {"gpu": dev.get("name"), "vram_total_gib": _gib(dev.get("vram_total")),
            "vram_free_gib": _gib(dev.get("vram_free")),
            "comfy_reserved_gib": _gib(dev.get("torch_vram_total")),
            "vram_available_for_comfy_gib": _gib((dev.get("vram_free") or 0) + (dev.get("torch_vram_total") or 0)),
            "comfyui_version": stats.get("system", {}).get("comfyui_version")}
    try:
        ps = _request("GET", f"{OLLAMA_URL}/api/ps", timeout=5)
        info["llm_loaded"] = [{"model": m.get("name"), "vram_gib": _gib(m.get("size_vram"))}
                              for m in ps.get("models", [])]
    except ComfyError:
        info["llm_loaded"] = "desconocido (Ollama no responde por el túnel)"
    return info


# --------------------------------------------------------------------------- progreso (WebSocket)

def _ws_loop() -> None:
    url = COMFY_URL.replace("http", "ws", 1) + f"/ws?clientId={CLIENT_ID}"
    while True:
        try:
            ws = websocket.create_connection(url, timeout=10)
            ws.settimeout(None)
            log("websocket conectado")
            while True:
                msg = ws.recv()
                if isinstance(msg, bytes):
                    continue  # previews binarias
                _on_ws_message(json.loads(msg))
        except Exception as e:  # noqa: BLE001 - el hilo nunca debe morir
            log(f"websocket desconectado ({type(e).__name__}); reintento en 5 s")
            time.sleep(5)


def _on_ws_message(m: dict) -> None:
    data = m.get("data") or {}
    pid = data.get("prompt_id")
    with JOBS_LOCK:
        job = JOBS.get(pid)
        if not job:
            return
        now = time.time()
        kind = m.get("type")
        if kind == "execution_start":
            job["started_at"] = now
        elif kind == "executing" and data.get("node") is not None:
            job.setdefault("started_at", now)
            job["node"] = data["node"]
            job["step"], job["max"], job["step_t0"] = 0, 0, None
        elif kind == "progress":
            if not job.get("step_t0"):
                job["step_t0"] = now
            job["step"], job["max"] = data.get("value", 0), data.get("max", 0)
            job["step_t"] = now
            job["node"] = data.get("node", job.get("node"))


# --------------------------------------------------------------------------- historial y estimaciones

def _history_path() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / "history.json"


def _history() -> dict:
    try:
        return json.loads(_history_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _record(key: str, value: float) -> None:
    h = _history()
    h[key] = (h.get(key, []) + [round(value, 4)])[-20:]
    _history_path().write_text(json.dumps(h, indent=1))


def _family(checkpoint: str) -> str:
    return "sdxl" if re.search(r"xl|pony|illustrious|noob", checkpoint, re.I) else "sd15"


def _estimate_image(checkpoint: str, width: int, height: int, steps: int, batch: int) -> dict:
    mp = width * height / 1e6 * batch
    samples = _history().get(f"img:{checkpoint}")
    s_per = statistics.median(samples) if samples else DEFAULT_S_PER_STEP_MP[_family(checkpoint)]
    seconds = s_per * steps * mp + MODEL_LOAD_OVERHEAD_S
    return {"seconds": round(seconds), "size_mb": round(width * height * PNG_BYTES_PER_PIXEL * batch / 1e6, 1),
            "basis": "historial propio" if samples else "valores por defecto (sin historial aún)"}


def _estimate_workflow(name: str) -> dict:
    samples = _history().get(f"wf:{name}")
    if not samples:
        return {"seconds": None, "basis": "sin historial: primera ejecución de este workflow"}
    return {"seconds": round(statistics.median(samples)), "basis": "historial propio"}


def _fmt_s(s) -> str:
    if s is None:
        return "?"
    s = int(s)
    return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


# --------------------------------------------------------------------------- preflight y envío

def _preflight(need_gib: float, allow_slow: bool) -> dict | None:
    v = _vram()
    available = v["vram_available_for_comfy_gib"]
    if available >= need_gib or allow_slow:
        return None
    return {"ok": False, "needs_confirmation": True, "vram": v,
            "detail": (f"VRAM disponible para ComfyUI {available} GiB < ~{need_gib} GiB estimados. ComfyUI "
                       "descargará parte a RAM y será varias veces más lento. Opciones: repetir con "
                       "allow_slow=true, o liberar memoria (free_comfy_vram / descargar el LLM).")}


def _prefix(dest_dir: Path, label: str) -> str:
    project = dest_dir.parent.name if dest_dir.name == "outputs" else dest_dir.name
    project = re.sub(r"[^A-Za-z0-9_-]+", "-", project).strip("-") or "proyecto"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", label.lower()).strip("-")[:30] or "gen"
    return f"{project}/{time.strftime('%Y%m%d-%H%M%S')}_{slug}"


def _dest(dest_dir: str) -> Path:
    p = Path(os.path.expanduser(dest_dir))
    if not p.is_absolute():
        raise ComfyError("dest_dir debe ser una ruta absoluta (p. ej. /home/.../mi-proyecto/outputs).")
    return p


def _submit(wf: dict, job: dict) -> dict:
    res = comfy("POST", "/prompt", {"prompt": wf, "client_id": CLIENT_ID})
    if res.get("node_errors"):
        raise ComfyError(f"Errores en nodos del workflow: {json.dumps(res['node_errors'])[:2000]}")
    pid = res["prompt_id"]
    job.update({"submitted_at": time.time(), "classes": {k: v.get("class_type") for k, v in wf.items()}})
    with JOBS_LOCK:
        JOBS[pid] = job
    est = job["estimate"]
    return {"ok": True, "prompt_id": pid, "estimate": est,
            "message": (f"Encolado. Estimado: {_fmt_s(est.get('seconds'))}"
                        + (f", ~{est['size_mb']} MB" if est.get("size_mb") else "")
                        + f" ({est['basis']}). Consulta get_result para ver el progreso.")}


@mcp.tool()
def pod_status() -> dict:
    """Estado del pod: túnel (lo abre si hace falta), ComfyUI, VRAM libre, LLM cargado y cola.
    Úsalo antes de generar."""
    try:
        v = _vram()
        running, pending = _queue_state()
        return {"ok": True, **v, "queue_running": len(running), "queue_pending": len(pending)}
    except ComfyError as e:
        return _error(e)


@mcp.tool()
def list_models(folder: str = "checkpoints") -> dict:
    """Lista modelos instalados en ComfyUI (checkpoints, loras, vae, diffusion_models, ...).
    folder="" lista las carpetas disponibles."""
    try:
        if not folder:
            return {"ok": True, "folders": comfy("GET", "/models")}
        return {"ok": True, "folder": folder, "files": comfy("GET", f"/models/{folder}")}
    except ComfyError as e:
        return _error(e)


@mcp.tool()
def generate_image(
    prompt: str,
    dest_dir: str,
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
    allow_slow: bool = False,
    unload_after: bool = False,
    keep_remote: bool = False,
) -> dict:
    """Encola texto→imagen (SD1.5/SDXL; para otros modelos usa run_workflow).
    dest_dir: ruta ABSOLUTA local donde se guardará (p. ej. <proyecto>/outputs).
    Devuelve prompt_id y estimación de tiempo/peso; luego llama a get_result.
    Si la VRAM no alcanza devuelve needs_confirmation (repite con allow_slow=true).
    Al terminar, el original del pod se borra tras verificar el hash (keep_remote=true lo evita)."""
    import random
    try:
        dest = _dest(dest_dir)
        if not checkpoint:
            ckpts = comfy("GET", "/models/checkpoints")
            if not ckpts:
                return _error("No hay checkpoints instalados en el pod.")
            checkpoint = ckpts[0]
        if not (64 <= width <= 2048 and 64 <= height <= 2048):
            return _error("width/height deben estar entre 64 y 2048.")
        if not 1 <= batch_size <= 4 or not 1 <= steps <= 100:
            return _error("batch_size 1–4 y steps 1–100.")
        blocked = _preflight(VRAM_NEED[_family(checkpoint)], allow_slow)
        if blocked:
            return blocked
        if seed < 0:
            seed = random.randint(0, 2**32 - 1)

        model_src, clip_src = ["4", 0], ["4", 1]
        wf = {"4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
              "5": {"class_type": "EmptyLatentImage",
                    "inputs": {"width": width, "height": height, "batch_size": batch_size}}}
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
            "9": {"class_type": "SaveImage", "inputs": {
                "filename_prefix": _prefix(dest, prompt), "images": ["8", 0]}},
        })
        job = {"kind": "image", "dest": str(dest), "unload_after": unload_after,
               "keep_remote": keep_remote, "history_key": f"img:{checkpoint}",
               "history_norm": steps * width * height / 1e6 * batch_size,
               "estimate": _estimate_image(checkpoint, width, height, steps, batch_size)}
        res = _submit(wf, job)
        res.update({"checkpoint": checkpoint, "seed": seed})
        return res
    except ComfyError as e:
        return _error(e)


@mcp.tool()
def list_workflows() -> dict:
    """Workflows guardados (formato API) para run_workflow, con nodos y entradas sobrescribibles."""
    out = []
    for f in sorted(WORKFLOWS_DIR.glob("*.json")):
        try:
            wf = json.loads(f.read_text())
            out.append({"name": f.stem, "estimate": _estimate_workflow(f.stem), "nodes": {
                nid: {"class_type": n.get("class_type"),
                      "inputs": [k for k, v in n.get("inputs", {}).items() if not isinstance(v, list)]}
                for nid, n in wf.items()}})
        except (json.JSONDecodeError, AttributeError) as e:
            out.append({"name": f.stem, "error": f"JSON inválido: {e}"})
    return {"ok": True, "workflows_dir": str(WORKFLOWS_DIR), "workflows": out}


@mcp.tool()
def run_workflow(
    name: str,
    dest_dir: str,
    overrides: dict | None = None,
    label: str = "",
    vram_need_gib: float = VRAM_NEED["workflow"],
    allow_slow: bool = False,
    unload_after: bool = True,
    keep_remote: bool = False,
) -> dict:
    """Encola un workflow guardado (workflows/<name>.json, exportado con 'Export (API)'):
    vídeo/animación, Flux, img2img, etc. overrides: {"<node_id>.<input>": valor}.
    dest_dir: ruta ABSOLUTA local de destino. Los filename_prefix se fijan automáticamente.
    Por defecto libera la VRAM de ComfyUI al terminar (unload_after)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return _error("Nombre de workflow inválido (solo letras, números, _ y -).")
    path = WORKFLOWS_DIR / f"{name}.json"
    if not path.is_file():
        return _error(f"No existe {path}. Usa list_workflows.")
    try:
        dest = _dest(dest_dir)
        wf = json.loads(path.read_text())
        for key, value in (overrides or {}).items():
            node_id, _, field = key.partition(".")
            if node_id not in wf or not field:
                return _error(f"Override inválido '{key}': nodo o campo inexistente.")
            wf[node_id].setdefault("inputs", {})[field] = value
        prefix = _prefix(dest, label or name)
        for node in wf.values():
            if "filename_prefix" in node.get("inputs", {}):
                node["inputs"]["filename_prefix"] = prefix
        blocked = _preflight(vram_need_gib, allow_slow)
        if blocked:
            return blocked
        job = {"kind": "workflow", "dest": str(dest), "unload_after": unload_after,
               "keep_remote": keep_remote, "history_key": f"wf:{name}", "history_norm": 1,
               "estimate": _estimate_workflow(name)}
        return _submit(wf, job)
    except (ComfyError, json.JSONDecodeError) as e:
        return _error(e)


# --------------------------------------------------------------------------- entrega

def _download(item: dict, dest: Path) -> tuple[Path, str, int]:
    """Descarga un output vía /view al directorio local sin sobrescribir nada.
    Si ya existe un archivo idéntico (mismo hash) lo reutiliza. Devuelve ruta, sha256, bytes."""
    dest.mkdir(parents=True, exist_ok=True)
    name = Path(item["filename"])
    q = urllib.parse.urlencode({"filename": item["filename"], "subfolder": item.get("subfolder", ""),
                                "type": "output"})
    h, size = hashlib.sha256(), 0
    tmp = dest / f".{name.name}.{uuid.uuid4().hex[:6]}.part"
    try:
        with urllib.request.urlopen(f"{COMFY_URL}/view?{q}", timeout=60) as resp, open(tmp, "wb") as out:
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                h.update(chunk)
                size += len(chunk)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        tmp.unlink(missing_ok=True)
        raise ComfyError(f"Fallo al descargar {item['filename']}: {e}") from None
    digest = h.hexdigest()
    target, n = dest / name.name, 1
    while target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            tmp.unlink()
            return target, digest, size
        target = dest / f"{name.stem}-{n}{name.suffix}"
        n += 1
    tmp.rename(target)
    return target, digest, size


def _ssh(cmd: str, stdin: bytes = b"", timeout: int = 60) -> str:
    r = subprocess.run(["ssh", *SSH_OPTS, POD_HOST, cmd], input=stdin, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise ComfyError(f"ssh {POD_HOST} falló: {r.stderr.decode(errors='replace')[-500:]}")
    return r.stdout.decode()


def _safe_rel(rel: str) -> bool:
    p = Path(rel)
    return not p.is_absolute() and ".." not in p.parts and rel.strip() != ""


def _remote_hashes(rels: list[str]) -> dict[str, str]:
    base = shlex.quote(REMOTE_OUTPUT)
    out = _ssh(f"cd {base} && xargs -0 sha256sum --", stdin="\0".join(rels).encode())
    hashes = {}
    for line in out.splitlines():
        digest, _, name = line.partition("  ")
        hashes[name] = digest
    return hashes


def _remote_delete(rels: list[str]) -> None:
    base = shlex.quote(REMOTE_OUTPUT)
    _ssh(f"cd {base} && xargs -0 rm -f --", stdin="\0".join(rels).encode())


def _deliver(items: list[dict], dest: Path, keep_remote: bool) -> list[dict]:
    results, to_delete = [], []
    downloaded = []
    for item in items:
        rel = str(Path(item.get("subfolder", "")) / item["filename"])
        if not _safe_rel(rel):
            results.append({"remote": rel, "error": "ruta remota no segura; ignorada"})
            continue
        local, digest, size = _download(item, dest)
        downloaded.append((rel, local, digest, size))
    remote = _remote_hashes([d[0] for d in downloaded]) if downloaded and not keep_remote else {}
    for rel, local, digest, size in downloaded:
        entry = {"local_path": str(local), "size_mb": round(size / 1e6, 2), "sha256": digest[:16]}
        if keep_remote:
            entry["remote"] = "conservado (keep_remote)"
        elif remote.get(rel) == digest:
            to_delete.append(rel)
            entry["remote"] = "borrado tras verificar hash"
        else:
            entry["remote"] = "CONSERVADO: el hash no coincide o no se pudo leer"
        results.append(entry)
    if to_delete:
        _remote_delete(to_delete)
    return results


def _collect_outputs(hist: dict) -> list[dict]:
    items = []
    for node_out in hist.get("outputs", {}).values():
        for values in node_out.values():
            if isinstance(values, list):
                items += [i for i in values if isinstance(i, dict) and i.get("filename") and i.get("type") == "output"]
    return items


def _exec_seconds(status: dict) -> float | None:
    """Duración real según las marcas de ComfyUI (execution_start → execution_success)."""
    ts = {m[0]: m[1].get("timestamp") for m in status.get("messages", [])
          if isinstance(m, list) and len(m) == 2 and isinstance(m[1], dict)}
    start, end = ts.get("execution_start"), ts.get("execution_success")
    return (end - start) / 1000 if start and end else None


def _progress(pid: str, job: dict, status: str, position: int | None) -> dict:
    now = time.time()
    elapsed = now - job.get("started_at", job["submitted_at"])
    est = job["estimate"].get("seconds")
    out = {"ok": True, "status": status, "prompt_id": pid, "elapsed_s": round(now - job["submitted_at"])}
    if status == "queued":
        out["message"] = f"En cola (posición {position}). Estimado una vez inicie: {_fmt_s(est)}."
        return out
    step, mx, node = job.get("step", 0), job.get("max", 0), job.get("node")
    node_name = job["classes"].get(node, node) if node else "iniciando"
    eta = None
    if mx and step >= 2 and job.get("step_t0"):
        eta = (job["step_t"] - job["step_t0"]) / (step - 1) * (mx - step)
    elif est:
        eta = max(est - elapsed, 0)
    pct = f" · {step}/{mx} ({round(step * 100 / mx)}%)" if mx else ""
    out.update({"node": node_name, "step": step, "max_steps": mx,
                "eta_s": round(eta) if eta is not None else None,
                "message": f"Generando · {node_name}{pct} · {_fmt_s(elapsed)} transcurridos · "
                           f"~{_fmt_s(eta)} restantes"})
    return out


@mcp.tool()
def get_result(prompt_id: str, wait_seconds: int = 20) -> dict:
    """Progreso y resultado de un trabajo (espera máx. 25 s por llamada).
    status: queued | running | success | error. Muestra siempre `message` al usuario.
    En success ya descargó los archivos a dest_dir, verificó el hash y borró el original del pod."""
    deadline = time.monotonic() + max(0, min(wait_seconds, MAX_WAIT))
    with JOBS_LOCK:
        job = JOBS.get(prompt_id)
    if not job:
        return _error("prompt_id desconocido en esta sesión del MCP (¿se reinició?). "
                      "Usa list_pending_outputs y deliver_pending para recuperar archivos.")
    if job.get("result"):
        return job["result"]
    try:
        while True:
            hist = comfy("GET", f"/history/{prompt_id}").get(prompt_id)
            if hist:
                st = hist.get("status", {})
                if st.get("status_str") == "error":
                    msgs = [m for m in st.get("messages", []) if m and m[0] == "execution_error"]
                    job["result"] = {"ok": False, "status": "error", "prompt_id": prompt_id,
                                     "detail": json.dumps(msgs)[:2000]}
                    return job["result"]
                total = _exec_seconds(st) or time.time() - job.get("started_at", job["submitted_at"])
                files = _deliver(_collect_outputs(hist), Path(job["dest"]), job["keep_remote"])
                if job.get("history_norm"):
                    norm = (total - MODEL_LOAD_OVERHEAD_S) / job["history_norm"] if job["kind"] == "image" else total
                    _record(job["history_key"], max(norm, 0.001))
                freed = False
                if job["unload_after"]:
                    try:
                        comfy("POST", "/free", {"unload_models": True, "free_memory": True})
                        freed = True
                    except ComfyError:
                        pass
                job["result"] = {"ok": True, "status": "success", "prompt_id": prompt_id,
                                 "duration_s": round(total), "files": files, "comfy_vram_freed": freed,
                                 "message": f"Listo en {_fmt_s(total)}: {len(files)} archivo(s) en {job['dest']}"}
                return job["result"]
            running, pending = _queue_state()
            if prompt_id in running:
                status, pos = "running", None
            elif prompt_id in pending:
                status, pos = "queued", pending.index(prompt_id) + 1
            else:
                return _error("El trabajo no está en cola ni en historial (¿se reinició ComfyUI?).")
            if time.monotonic() >= deadline:
                with JOBS_LOCK:
                    return _progress(prompt_id, job, status, pos)
            time.sleep(1)
    except (ComfyError, subprocess.TimeoutExpired) as e:
        return _error(e)


@mcp.tool()
def cancel_job(prompt_id: str) -> dict:
    """Cancela un trabajo: lo quita de la cola o interrumpe si está en ejecución."""
    try:
        running, pending = _queue_state()
        if prompt_id in pending:
            comfy("POST", "/queue", {"delete": [prompt_id]})
            return {"ok": True, "action": "removed_from_queue"}
        if prompt_id in running:
            comfy("POST", "/interrupt", {"prompt_id": prompt_id})
            return {"ok": True, "action": "interrupted"}
        return _error("El trabajo no está en cola ni en ejecución.")
    except ComfyError as e:
        return _error(e)


@mcp.tool()
def list_pending_outputs(subfolder: str = "") -> dict:
    """Archivos que siguen en el pod sin entregar (p. ej. si el MCP se reinició a mitad)."""
    if subfolder and not _safe_rel(subfolder):
        return _error("subfolder no válido.")
    try:
        _ensure_tunnel()
        base = shlex.quote(str(Path(REMOTE_OUTPUT) / subfolder))
        out = _ssh(f"[ -d {base} ] && cd {shlex.quote(REMOTE_OUTPUT)} && "
                   f"find {shlex.quote(subfolder or '.')} -type f ! -name '_output_images_will_be_put_here' "
                   "-printf '%s\\t%P\\n' | head -500 || true")
        files = []
        for line in out.splitlines():
            size, _, rel = line.partition("\t")
            rel = str(Path(subfolder) / rel) if subfolder else rel
            files.append({"relative": rel, "size_mb": round(int(size) / 1e6, 2)})
        return {"ok": True, "total": len(files), "files": files}
    except (ComfyError, subprocess.TimeoutExpired) as e:
        return _error(e)


@mcp.tool()
def deliver_pending(subfolder: str, dest_dir: str, keep_remote: bool = False) -> dict:
    """Descarga a dest_dir los archivos pendientes de un subfolder del pod (normalmente el nombre
    del proyecto), verifica hash y borra el original. Usa list_pending_outputs antes."""
    if not _safe_rel(subfolder):
        return _error("subfolder obligatorio y relativo (p. ej. el nombre del proyecto).")
    listing = list_pending_outputs(subfolder)
    if not listing.get("ok"):
        return listing
    try:
        items = [{"filename": Path(f["relative"]).name, "subfolder": str(Path(f["relative"]).parent)}
                 for f in listing["files"]]
        return {"ok": True, "files": _deliver(items, _dest(dest_dir), keep_remote)}
    except (ComfyError, subprocess.TimeoutExpired) as e:
        return _error(e)


@mcp.tool()
def free_comfy_vram() -> dict:
    """Descarga los modelos de ComfyUI de la VRAM (no afecta al LLM)."""
    try:
        comfy("POST", "/free", {"unload_models": True, "free_memory": True})
        return {"ok": True, "detail": "Modelos de ComfyUI descargados de la VRAM."}
    except ComfyError as e:
        return _error(e)


if __name__ == "__main__":
    threading.Thread(target=_ws_loop, daemon=True).start()
    mcp.run()
