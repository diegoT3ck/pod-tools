#!/usr/bin/env bash
# Apunta el alias SSH local al pod actual (host y puerto cambian con cada pod o reinicio).
#   configure-pod.sh <host> <puerto> [--bootstrap]
#   configure-pod.sh ssh9.vast.ai 21609 --bootstrap
# - Hace backup con fecha de ~/.ssh/config y ~/.ssh/known_hosts antes de tocarlos.
# - Actualiza HostName/Port del bloque "Host $POD_ALIAS" (lo crea si no existe).
# - Muestra las huellas SHA256 del host y pide confirmación antes de confiar en ellas.
# - --bootstrap: clona/actualiza el repo en el pod y lanza pod/bootstrap.sh en tmux.
set -euo pipefail

POD_ALIAS="${POD_ALIAS:-vast-pod}"
POD_USER="${POD_USER:-root}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
REPO_URL="${REPO_URL:-https://github.com/diegoT3ck/pod-tools}"
REMOTE_DIR="${REMOTE_DIR:-/workspace/pod-tools}"

usage() { echo "uso: $0 <host> <puerto> [--bootstrap]" >&2; exit 2; }
[[ $# -ge 2 ]] || usage
HOST="$1"; PORT="$2"; BOOTSTRAP=0
[[ "${3:-}" == "--bootstrap" ]] && BOOTSTRAP=1
[[ "$PORT" =~ ^[0-9]+$ ]] || usage
[[ "$HOST" =~ ^[A-Za-z0-9.-]+$ ]] || usage
[[ -f "$SSH_KEY" ]] || { echo "✗ no existe la clave $SSH_KEY" >&2; exit 1; }

CFG="$HOME/.ssh/config"; KH="$HOME/.ssh/known_hosts"; TS="$(date +%Y%m%d%H%M%S)"
mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"
touch "$CFG" "$KH"; chmod 600 "$CFG"
cp -p "$CFG" "$CFG.bak.$TS"; cp -p "$KH" "$KH.bak.$TS"
echo "· backups: $CFG.bak.$TS  $KH.bak.$TS"

# 1. ~/.ssh/config
if grep -qE "^Host[[:space:]]+$POD_ALIAS[[:space:]]*$" "$CFG"; then
  tmp="$(mktemp)"
  awk -v a="$POD_ALIAS" -v h="$HOST" -v p="$PORT" '
    function flush() { if (inb) { if (!sh) print "    HostName " h; if (!sp) print "    Port " p } inb = 0 }
    /^Host[ \t]/ || /^Match[ \t]/ { flush(); if ($1 == "Host" && $2 == a && NF == 2) { inb = 1; sh = sp = 0 } print; next }
    inb && tolower($1) == "hostname" { print "    HostName " h; sh = 1; next }
    inb && tolower($1) == "port"     { print "    Port " p; sp = 1; next }
    { print }
    END { flush() }' "$CFG" > "$tmp"
  cat "$tmp" > "$CFG"; rm -f "$tmp"
  echo "✓ bloque Host $POD_ALIAS actualizado → $HOST:$PORT"
else
  [[ -s "$CFG" && -n "$(tail -c1 "$CFG")" ]] && echo >> "$CFG"
  cat >> "$CFG" <<EOF

Host $POD_ALIAS
    HostName $HOST
    Port $PORT
    User $POD_USER
    IdentityFile $SSH_KEY
    IdentitiesOnly yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
EOF
  echo "✓ bloque Host $POD_ALIAS creado → $HOST:$PORT"
fi

# 2. known_hosts: nunca se confía a ciegas.
KEY_NAME="$HOST"; [[ "$PORT" != 22 ]] && KEY_NAME="[$HOST]:$PORT"
scan="$(mktemp)"; trap 'rm -f "$scan"' EXIT
timeout 20 ssh-keyscan -T 10 -p "$PORT" "$HOST" 2>/dev/null > "$scan" || true
[[ -s "$scan" ]] || { echo "✗ ssh-keyscan no obtuvo claves de $HOST:$PORT (¿pod apagado o en Scheduling?)" >&2; exit 1; }

if ssh-keygen -F "$KEY_NAME" -f "$KH" >/dev/null; then
  known="$(ssh-keygen -F "$KEY_NAME" -f "$KH" | grep -v '^#' | awk '{print $2, $3}' | sort)"
  scanned="$(awk '{print $2, $3}' "$scan" | sort)"
  if [[ "$known" == "$scanned" ]]; then
    echo "✓ huellas de $KEY_NAME ya conocidas y sin cambios"
  else
    echo "⚠ $KEY_NAME ya estaba en known_hosts con OTRAS claves."
    echo "  Normal si es un pod nuevo en el mismo host:puerto; si no, podría ser un ataque."
    echo "  Huellas nuevas:"; ssh-keygen -lf "$scan" | sed 's/^/    /'
    read -r -p "  Compáralas con las del panel de Vast. ¿Reemplazar? [s/N] " ans
    [[ "$ans" =~ ^[sS]$ ]] || { echo "✗ cancelado; known_hosts sin cambios" >&2; exit 1; }
    ssh-keygen -R "$KEY_NAME" -f "$KH" >/dev/null 2>&1
    cat "$scan" >> "$KH"; echo "✓ claves reemplazadas"
  fi
else
  echo "Huellas de $KEY_NAME:"; ssh-keygen -lf "$scan" | sed 's/^/    /'
  read -r -p "¿Confiar en estas claves? [s/N] " ans
  [[ "$ans" =~ ^[sS]$ ]] || { echo "✗ cancelado; known_hosts sin cambios" >&2; exit 1; }
  cat "$scan" >> "$KH"; echo "✓ claves añadidas a known_hosts"
fi

# 3. Prueba de conexión (con los archivos recién escritos)
SSH=(ssh -F "$CFG" -o UserKnownHostsFile="$KH" -o BatchMode=yes)
if timeout 25 "${SSH[@]}" -o ConnectTimeout=15 "$POD_ALIAS" true 2>/dev/null; then
  echo "✓ ssh $POD_ALIAS funciona"
else
  echo "✗ ssh $POD_ALIAS falla. Error exacto:" >&2
  timeout 25 "${SSH[@]}" -o ConnectTimeout=15 "$POD_ALIAS" true || true
  echo "  ¿Está tu clave pública ($SSH_KEY.pub) en Vast → Account → Keys?" >&2
  exit 1
fi

# 4. Bootstrap remoto opcional (en tmux para que sobreviva a cortes de SSH)
if ((BOOTSTRAP)); then
  timeout 120 "${SSH[@]}" "$POD_ALIAS" "set -e
    if [ -d '$REMOTE_DIR/.git' ]; then git -C '$REMOTE_DIR' pull --ff-only -q; else git clone -q '$REPO_URL' '$REMOTE_DIR'; fi
    if tmux has-session -t bootstrap 2>/dev/null; then echo '· bootstrap ya está corriendo'; exit 0; fi
    tmux new-session -d -s bootstrap 'bash $REMOTE_DIR/pod/bootstrap.sh 2>&1 | tee -a /workspace/bootstrap.log'
    echo '✓ bootstrap lanzado en el pod (tmux: bootstrap)'" 2>/dev/null
  echo "  Seguir el progreso:  ssh $POD_ALIAS tail -f /workspace/bootstrap.log"
  echo "  Primera vez: ~20-40 min (LLM 17 GB + modelos ComfyUI ~25 GB + variante texto ~5 min)."
fi
