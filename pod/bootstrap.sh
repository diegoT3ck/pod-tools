#!/usr/bin/env bash
# Instala todo en un pod Vast.ai recién creado (root, sin systemd). Idempotente: se puede
# relanzar y salta lo que ya está hecho y verificado.
#   bash /workspace/pod-tools/pod/bootstrap.sh
# Fases:
#   0. Preflight: revisa TODO (GPU, driver, disco, red, URLs, herramientas) antes de instalar nada.
#   1-6. Cada instalación va seguida de su test; si un test falla, se detiene con el motivo.
# Todas las variables se pueden sobreescribir por entorno, p. ej.:
#   LLM_MODEL=otro/modelo:14b LLM_TEXT_ONLY=0 MODELS_FILE= bash bootstrap.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

OLLAMA_VERSION="${OLLAMA_VERSION:-0.40.2}"
OLLAMA_MODELS_DIR="${OLLAMA_MODELS_DIR:-/workspace/ollama-models}"
LLM_MODEL="${LLM_MODEL:-huihui_ai/Qwen3.8-abliterated:27b}"
LLM_SIZE_GB="${LLM_SIZE_GB:-18}"                 # para el chequeo de disco si aún no está descargado
LLM_TEXT_ONLY="${LLM_TEXT_ONLY:-0}"              # 1 = variante sin visión: ~5.5 GB menos de VRAM, pero no ve lo que genera
LLM_TEXT_NAME="${LLM_TEXT_NAME:-qwen3.8-27b-text}"
COMFY_DIR="${COMFY_DIR:-/workspace/ComfyUI}"
COMFY_REPO="${COMFY_REPO:-https://github.com/comfyanonymous/ComfyUI.git}"
TORCH_INDEX="${TORCH_INDEX:-https://download.pytorch.org/whl/cu128}"   # cu128 = RTX 50xx (Blackwell)
MIN_DRIVER="${MIN_DRIVER:-570}"                  # driver mínimo para CUDA 12.8
MODELS_FILE="${MODELS_FILE:-$HERE/models.txt}"   # vacío = no descargar modelos de ComfyUI
VERIFY_SHA="${VERIFY_SHA:-1}"                    # 1 = verificar sha256 de cada modelo contra Hugging Face
RESERVE_VRAM_GB="${RESERVE_VRAM_GB:-2}"
DISK_MARGIN_GB="${DISK_MARGIN_GB:-15}"           # venv de ComfyUI (~8 GB) + holgura

export OLLAMA_HOST=127.0.0.1:11434 OLLAMA_MODELS="$OLLAMA_MODELS_DIR"
LOG_DIR=/workspace

step() { echo; echo "━━ $*"; }
ok()   { echo "  ✓ $*"; }
bad()  { echo "  ✗ $*"; }
fail() { echo "  ✗ $*" >&2; echo "ABORTADO. Corrige lo anterior y relanza: bash $0" >&2; exit 1; }
# Sin grep -q: con pipefail, cerrar la tubería antes de tiempo daría SIGPIPE a awk.
has_model() { ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -xE "$1(:latest)?" >/dev/null; }
models_list() { [[ -n "$MODELS_FILE" ]] && grep -vE '^\s*(#|$)' "$MODELS_FILE" || true; }
head_field() { curl -sIL -m 20 "$1" | tr -d '\r' | awk -v f="$2" 'tolower($1) == f":" {v = $2} END {print v}'; }

# ───────────────────────── 0. Preflight ─────────────────────────
step "0/6 Preflight (no instala nada)"
problems=0
check() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1${3:+ — $3}"; problems=$((problems + 1)); fi; }

check "root" '[[ $EUID -eq 0 ]]'
check "GPU NVIDIA visible" 'nvidia-smi -L' "pod sin GPU o sin driver"
if command -v nvidia-smi >/dev/null; then
  drv="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
  gpu="$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
  echo "    $gpu · driver $drv"
  check "driver ≥ $MIN_DRIVER (CUDA 12.8)" '[[ ${drv%%.*} -ge $MIN_DRIVER ]]' "usa otro TORCH_INDEX o MIN_DRIVER"
fi
# Solo aviso: un CPU lento o PCIe estrecho dejan la GPU ociosa (medido: Xeon Phi 7250 + PCIe
# Gen3 x4 → SDXL a ~1 s/paso con la GPU al 5 %). Conviene elegir otro host en Vast.
cpu="$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')"
pcie="$(nvidia-smi --query-gpu=pcie.link.gen.max,pcie.link.width.max --format=csv,noheader 2>/dev/null | head -1)"
echo "    CPU: $cpu · PCIe (gen, ancho máx): $pcie"
[[ "$cpu" == *"Xeon Phi"* ]] && echo "  ⚠ CPU Xeon Phi: muy lento por hilo; ComfyUI irá varias veces más lento que en otro host"
[[ "${pcie%%,*}" =~ ^[0-9]+$ ]] && { ((${pcie%%,*} < 4)) || [[ "${pcie##* }" =~ ^(1|2|4|8)$ ]]; } \
  && echo "  ⚠ PCIe por debajo de Gen4 x16: cargar modelos y mover pesos será lento"
check "python3 ≥ 3.10" 'python3 -c "import sys; assert sys.version_info >= (3, 10)"'

missing=()
for c in git tmux curl zstd; do command -v "$c" >/dev/null || missing+=("$c"); done
python3 -c 'import venv, ensurepip' 2>/dev/null || missing+=(python3-venv)
if ((${#missing[@]})); then
  echo "  · faltan paquetes del sistema: ${missing[*]} (se instalarán con apt)"
  check "apt-get disponible" 'command -v apt-get'
else
  ok "herramientas: git tmux curl zstd python3-venv"
fi

for u in https://ollama.com https://github.com https://huggingface.co "$TORCH_INDEX/"; do
  check "red → ${u#https://}" "curl -sf -m 15 -o /dev/null -I '$u' || curl -sf -m 15 -o /dev/null '$u'"
done
repo="${LLM_MODEL%%:*}"; [[ "$repo" == */* ]] || repo="library/$repo"
tag="latest"; [[ "$LLM_MODEL" == *:* ]] && tag="${LLM_MODEL##*:}"
check "registro Ollama tiene $LLM_MODEL" \
  "curl -sf -m 15 -o /dev/null https://registry.ollama.ai/v2/$repo/manifests/$tag -H 'Accept: application/vnd.docker.distribution.manifest.v2+json'"

need_gb=$DISK_MARGIN_GB
has_ollama_model=0
command -v ollama >/dev/null && curl -s -m 3 -o /dev/null "http://$OLLAMA_HOST/api/version" && has_model "$LLM_MODEL" && has_ollama_model=1
((has_ollama_model)) || [[ -d "$OLLAMA_MODELS_DIR/manifests/registry.ollama.ai/${LLM_MODEL%%:*}" ]] || need_gb=$((need_gb + LLM_SIZE_GB))
declare -A MODEL_SIZE MODEL_SHA
if [[ -n "$MODELS_FILE" ]]; then
  check "lista de modelos $MODELS_FILE" '[[ -f "$MODELS_FILE" ]]'
  while read -r folder url _; do
    name="${url##*/}"; name="${name%%\?*}"
    size="$(head_field "$url" x-linked-size)"; [[ -n "$size" ]] || size="$(head_field "$url" content-length)"
    if [[ "$size" =~ ^[0-9]+$ ]] && ((size > 0)); then
      MODEL_SIZE[$name]=$size
      MODEL_SHA[$name]="$(head_field "$url" x-linked-etag | tr -d '"')"
      ok "URL $folder/$name ($((size / 1024 / 1024)) MiB)"
      [[ -f "$COMFY_DIR/models/$folder/$name" ]] || need_gb=$((need_gb + size / 1024**3 + 1))
    else
      bad "URL $folder/$name no responde"; problems=$((problems + 1))
    fi
  done < <(models_list)
fi
free_gb=$(df -BG --output=avail /workspace | tail -1 | tr -dc 0-9)
check "disco: ${free_gb} GB libres ≥ ${need_gb} GB necesarios" '((free_gb >= need_gb))'

((problems == 0)) || fail "preflight: $problems problema(s). No se instaló nada."
ok "preflight correcto"

# ───────────────────────── 1. Paquetes del sistema ─────────────────────────
step "1/6 Paquetes del sistema"
if ((${#missing[@]})); then
  timeout 300 apt-get update -qq
  DEBIAN_FRONTEND=noninteractive timeout 600 apt-get install -y -qq "${missing[@]}" >/dev/null
fi
for c in git tmux curl zstd; do command -v "$c" >/dev/null || fail "test: falta $c"; done
python3 -c 'import venv, ensurepip' || fail "test: python3-venv no funciona"
ok "test: herramientas presentes"

# ───────────────────────── 2. Ollama ─────────────────────────
step "2/6 Ollama $OLLAMA_VERSION"
if command -v ollama >/dev/null && ollama --version 2>/dev/null | grep -q "$OLLAMA_VERSION"; then
  ok "ya instalado"
else
  timeout 60 curl -fsSL https://ollama.com/install.sh -o /tmp/ollama-install.sh
  OLLAMA_VERSION="$OLLAMA_VERSION" timeout 900 sh /tmp/ollama-install.sh 2>&1 | tail -3
fi
ollama --version 2>/dev/null | grep -q "$OLLAMA_VERSION" || fail "test: ollama --version no es $OLLAMA_VERSION"
mkdir -p "$OLLAMA_MODELS_DIR"
# Solo el servidor de Ollama (sin precargar ni arrancar ComfyUI) para poder hacer pull.
LLM_MODEL="" START_COMFY=0 bash "$HERE/start-services.sh" >/dev/null
curl -sf -m 5 "http://$OLLAMA_HOST/api/version" >/dev/null || fail "test: ollama serve no responde (log: $LOG_DIR/ollama-serve.log)"
ok "test: versión $OLLAMA_VERSION y servidor respondiendo"

# ───────────────────────── 3. LLM ─────────────────────────
llm_test() {  # genera unos tokens y comprueba que salió texto y que corre en GPU
  local m="$1" r
  r="$(timeout 240 curl -s -m 240 "http://$OLLAMA_HOST/api/generate" \
       -d "{\"model\":\"$m\",\"prompt\":\"Di solo: OK\",\"stream\":false,\"keep_alive\":\"5m\",\"options\":{\"num_predict\":8}}")" || return 1
  python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d.get("done") and d.get("eval_count",0)>0, d' "$r" || return 1
  curl -s -m 5 "http://$OLLAMA_HOST/api/ps" | python3 -c '
import json,sys
m=[x for x in json.load(sys.stdin)["models"] if x["name"].split(":")[0]==sys.argv[1].split(":")[0]]
assert m and m[0]["size_vram"]>=0.95*m[0]["size"], "modelo no está entero en GPU"' "$m"
}

step "3/6 Modelo LLM $LLM_MODEL"
if has_model "$LLM_MODEL"; then
  ok "ya descargado"
else
  timeout 3600 ollama pull "$LLM_MODEL" </dev/null 2>&1 | tr '\r' '\n' | grep -vE '%|^\s*$' | tail -3
fi
has_model "$LLM_MODEL" || fail "test: $LLM_MODEL no aparece en ollama list"
SERVE_MODEL="$LLM_MODEL"

if [[ "$LLM_TEXT_ONLY" == 1 ]]; then
  echo "  · variante solo texto $LLM_TEXT_NAME"
  if has_model "$LLM_TEXT_NAME"; then
    ok "ya existe"
  elif [[ "$(ollama show --modelfile "$LLM_MODEL" | grep -c '^FROM ')" == 1 ]]; then
    echo "  · $LLM_MODEL no tiene proyector de visión; se usa tal cual"
  else
    mf="$LOG_DIR/Modelfile.$LLM_TEXT_NAME"
    # Quita comentarios, la segunda línea FROM (proyector de visión mmproj) y el bloque LICENSE.
    ollama show --modelfile "$LLM_MODEL" | awk '
      lic { if (index($0, "\"\"\"")) lic = 0; next }
      /^LICENSE/ { n = gsub(/"""/, "&"); if (n == 1) lic = 1; next }
      /^#/ { next }
      /^FROM / { if (++from > 1) next }
      { print }' > "$mf"
    [[ "$(grep -c '^FROM ' "$mf")" == 1 ]] || fail "test: Modelfile inesperado en $mf"
    echo "  · creando (hashea los pesos, ~5 min)…"
    timeout 600 ollama create "$LLM_TEXT_NAME" -f "$mf" </dev/null 2>&1 | tr '\r' '\n' | grep -vE 'gathering|^\s*$' | tail -1
    has_model "$LLM_TEXT_NAME" || fail "test: no se creó $LLM_TEXT_NAME"
  fi
  if has_model "$LLM_TEXT_NAME"; then
    ollama show "$LLM_TEXT_NAME" | grep -iE '^\s+vision\s*$' >/dev/null && fail "test: $LLM_TEXT_NAME aún tiene visión"
    ok "test: $LLM_TEXT_NAME sin visión"
    SERVE_MODEL="$LLM_TEXT_NAME"
  fi
fi
echo "  · probando $SERVE_MODEL (carga ~30 s la primera vez)…"
llm_test "$SERVE_MODEL" || fail "test: $SERVE_MODEL no generó texto o no cabe entero en GPU (log: $LOG_DIR/ollama-serve.log)"
ok "test: $SERVE_MODEL genera texto, 100% en GPU"

# ───────────────────────── 4. ComfyUI ─────────────────────────
step "4/6 ComfyUI en $COMFY_DIR"
[[ -d "$COMFY_DIR/.git" ]] || timeout 300 git clone -q --depth 1 "$COMFY_REPO" "$COMFY_DIR"
py="$COMFY_DIR/venv/bin/python"
torch_test() {
  "$py" -c '
import torch
assert torch.cuda.is_available(), "CUDA no disponible"
x = torch.randn(1024, 1024, device="cuda"); y = (x @ x).sum().item()
print("    torch", torch.__version__, "·", torch.cuda.get_device_name(0), "· matmul OK")'
}
if [[ -f "$COMFY_DIR/.bootstrap-ok" ]] && torch_test 2>/dev/null; then
  ok "ya instalado"
else
  [[ -x "$py" ]] || python3 -m venv "$COMFY_DIR/venv"
  timeout 300 "$py" -m pip install -q --upgrade pip
  timeout 1800 "$py" -m pip install -q torch torchvision torchaudio --index-url "$TORCH_INDEX"
  torch_test || fail "test: torch no usa la GPU (revisa TORCH_INDEX=$TORCH_INDEX y el driver)"
  timeout 900 "$py" -m pip install -q -r "$COMFY_DIR/requirements.txt"
  "$py" -m pip check >/dev/null || echo "  · aviso: pip check reporta conflictos (ver: $py -m pip check)"
  touch "$COMFY_DIR/.bootstrap-ok"
fi
torch_test
(cd "$COMFY_DIR" && timeout 120 "$py" -c 'import comfy.model_management' >/dev/null 2>&1) \
  || fail "test: ComfyUI no importa (prueba: cd $COMFY_DIR && $py -c 'import comfy.model_management')"
ok "test: torch en GPU e imports de ComfyUI"

# ───────────────────────── 5. Modelos de ComfyUI ─────────────────────────
step "5/6 Modelos de ComfyUI"
errors=0
while read -r folder url _; do
  dir="$COMFY_DIR/models/$folder"; name="${url##*/}"; name="${name%%\?*}"; f="$dir/$name"
  mkdir -p "$dir"
  if [[ ! -f "$f" ]]; then
    echo "  · descargando $folder/$name"
    if timeout 3600 curl -fL --retry 3 -C - -sS -o "$f.part" "$url"; then mv "$f.part" "$f"
    else bad "$folder/$name: descarga falló (se reanuda al relanzar)"; errors=$((errors + 1)); continue; fi
  fi
  exp="${MODEL_SIZE[$name]:-}"; real=$(stat -c %s "$f")
  if [[ -n "$exp" && "$real" != "$exp" ]]; then
    bad "$folder/$name: tamaño $real ≠ $exp; renombrado a .part para reanudar"; mv "$f" "$f.part"; errors=$((errors + 1)); continue
  fi
  sha="${MODEL_SHA[$name]:-}"
  if [[ "$VERIFY_SHA" == 1 && "$sha" =~ ^[0-9a-f]{64}$ ]]; then
    if [[ -f "$f.sha256ok" ]] && [[ "$(cat "$f.sha256ok")" == "$sha" ]]; then :
    elif [[ "$(sha256sum "$f" | cut -d' ' -f1)" == "$sha" ]]; then echo "$sha" > "$f.sha256ok"
    else bad "$folder/$name: sha256 no coincide; renombrado a .bad"; mv "$f" "$f.bad"; errors=$((errors + 1)); continue; fi
    ok "test: $folder/$name tamaño y sha256 correctos"
  else
    ok "test: $folder/$name tamaño correcto"
  fi
done < <(models_list)
((errors == 0)) || fail "$errors modelo(s) con error"

# ───────────────────────── 6. Servicios ─────────────────────────
step "6/6 Servicios"
LLM_MODEL="$SERVE_MODEL" RESERVE_VRAM_GB="$RESERVE_VRAM_GB" bash "$HERE/start-services.sh"
stats="$(curl -sf -m 10 http://127.0.0.1:8188/system_stats)" || fail "test: ComfyUI no responde (log: $LOG_DIR/comfy.log)"
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["devices"][0]["type"]=="cuda", d["devices"]' "$stats" \
  || fail "test: ComfyUI no está usando la GPU"
ok "test: ComfyUI responde y usa CUDA"
while read -r folder url _; do
  name="${url##*/}"; name="${name%%\?*}"
  curl -sf -m 10 "http://127.0.0.1:8188/models/$folder" | grep -F "\"$name\"" >/dev/null \
    && ok "test: ComfyUI ve $folder/$name" || fail "test: ComfyUI no lista $folder/$name"
done < <(models_list)
curl -s -m 5 "http://$OLLAMA_HOST/api/ps" | grep -F "\"$SERVE_MODEL" >/dev/null || fail "test: $SERVE_MODEL no quedó cargado"
ok "test: $SERVE_MODEL cargado en VRAM"

step "Listo"
echo "  LLM para OpenCode: $SERVE_MODEL"
nvidia-smi --query-compute-apps=process_name,used_memory --format=csv,noheader | sed 's/^/  VRAM /'
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader | sed 's/^/  VRAM total usada: /'
df -h /workspace | awk 'NR==2{print "  Disco /workspace: " $3 " usados, " $4 " libres"}'
