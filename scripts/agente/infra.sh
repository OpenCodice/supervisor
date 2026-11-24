#!/usr/bin/env bash
# Los dos agentes de reparar-infra.yml. Corren con un usuario sin sudo, sin
# docker y sin acceso al directorio del runner, y con CLAUDE_CODE_OAUTH_TOKEN
# como único secreto: lo demás del flujo (preparar, cerco, compuertas, publicar,
# revertir) es código determinista en supervisor/reparar.py.
#
#   infra.sh aislar USUARIO… — como el usuario del runner, antes de cualquier
#     agente: crea esos usuarios sin privilegios y cierra el directorio del
#     runner.
#   infra.sh como USUARIO FASE… — como el usuario del runner: corre una fase
#     con ese usuario. De su entorno solo pasan el PATH y las variables de los
#     agentes.
#   infra.sh comprobar [PID_DE_LA_PUERTA] — como el usuario del agente: falla
#     si puede usar sudo o docker, leer el directorio del runner, o el entorno
#     o la memoria de la puerta hacia egress-mx. Sin eso, el agente no corre.
#   infra.sh agente — escribe el arreglo en $INFRA/repo y su veredicto en
#     $INFRA/salida/veredicto.json (brief en $INFRA/entrada/brief.md).
#   infra.sh empacar — como el usuario del agente: deja en $INFRA/salida lo
#     que cambió (entrega.tar) y lo que borró (borrados.txt). Son datos: las
#     compuertas los ponen sobre un clon limpio y deciden.
#   infra.sh juez — solo lee: $INFRA/repo es el árbol de partida y
#     $INFRA/entrada/arreglo.patch el parche. Su respuesta es su último
#     mensaje, que queda en la salida estándar.
#
# Entorno: INFRA (/srv/infra), CLAUDE_CODE_OAUTH_TOKEN; opcionales
# AGENTE_MINUTOS, AGENTE_MAX_TURNOS, AGENTE_MODELO.
set -uo pipefail

FASE="${1:?uso: infra.sh aislar|como|comprobar|agente|empacar|juez}"
shift
INFRA="${INFRA:-/srv/infra}"

aislar() {
  local usuario
  for usuario in "$@"; do
    id "$usuario" > /dev/null 2>&1 || sudo useradd --create-home --shell /bin/bash "$usuario"
  done
  # Lo que el runner guarda de la corrida (scripts de cada paso, credenciales
  # del propio runner) no es para los agentes.
  sudo chmod o-rwx /home/runner "${RUNNER_TEMP:-/home/runner/work/_temp}" 2> /dev/null
  sudo chmod 660 /var/run/docker.sock 2> /dev/null
  return 0
}

como() {
  local usuario="$1"
  shift
  sudo -n -u "$usuario" -H \
    --preserve-env=CLAUDE_CODE_OAUTH_TOKEN,AGENTE_MINUTOS,AGENTE_MAX_TURNOS,AGENTE_MODELO \
    env "PATH=$PATH" "INFRA=$INFRA" LANG=C.UTF-8 bash "$INFRA/bin/infra.sh" "$@"
}

comprobar() {
  local puerta="${1:-}" falla=""
  [ "$(id -u)" != 0 ] || falla="corre como root"
  sudo -n true 2> /dev/null && falla="puede usar sudo"
  id -nG | tr ' ' '\n' | grep -qxE 'sudo|docker|adm|runner|root|wheel' && falla="está en un grupo con privilegios"
  [ ! -r /home/runner ] || falla="puede leer el directorio del runner"
  [ ! -w /var/run/docker.sock ] || falla="puede hablar con docker"
  [ ! -r "$INFRA/puerta" ] || falla="puede leer la carpeta de la puerta hacia egress-mx"
  if [ -n "$puerta" ]; then
    [ -d "/proc/$puerta" ] || falla="la puerta hacia egress-mx no está corriendo"
    # Abrir environ o mem de otro usuario exige poder inspeccionarlo (ptrace).
    for f in environ mem; do
      if { : < "/proc/$puerta/$f"; } 2> /dev/null; then falla="puede abrir /proc/$puerta/$f de la puerta hacia egress-mx"; fi
    done
    if ls "/proc/$puerta/fd/" > /dev/null 2>&1; then falla="puede ver los archivos abiertos de la puerta hacia egress-mx"; fi
  fi
  # Ningún proceso de otro usuario le deja ver su entorno.
  local p
  for p in /proc/[0-9]*; do
    [ "$(stat -c %u "$p" 2> /dev/null)" = "$(id -u)" ] && continue
    if { : < "$p/environ"; } 2> /dev/null; then falla="puede leer el entorno de otro usuario ($p)"; fi
  done
  if [ -n "$falla" ]; then
    echo "::error::El usuario del agente $falla: no se le puede aislar." >&2
    return 1
  fi
  echo "Aislamiento comprobado: $(id -un) sin sudo, sin docker, sin el directorio del runner${puerta:+ y sin acceso a la puerta (pid $puerta)}."
}

claude_acotado() {
  local minutos="$1"
  shift
  command -v claude > /dev/null || { echo "::error::claude no está instalado." >&2; return 1; }
  timeout --signal=TERM --kill-after=60 "$((minutos * 60))" claude "$@"
}

agente() {
  rm -f "$INFRA/salida/veredicto.json"
  cd "$INFRA/repo" || return 1
  BASH_DEFAULT_TIMEOUT_MS=600000 BASH_MAX_TIMEOUT_MS=1200000 \
    claude_acotado "${AGENTE_MINUTOS:-45}" -p "$(cat "$INFRA/entrada/brief.md")" \
      --max-turns "${AGENTE_MAX_TURNOS:-100}" \
      --permission-mode acceptEdits \
      --allowedTools Bash Read Edit Write Glob Grep \
      --model "${AGENTE_MODELO:-claude-opus-5-5}" \
      --output-format json \
    || echo "::warning::El agente de infraestructura terminó con error o agotó su tiempo."
}

empacar() {
  cd "$INFRA/repo" || return 1
  rm -f "$INFRA/salida/entrega.tar" "$INFRA/salida/borrados.txt"
  git ls-files --deleted > "$INFRA/salida/borrados.txt" 2> /dev/null
  # Modificados y nuevos que .gitignore no ignora, sin los borrados ni los enormes.
  git ls-files -z --modified --others --exclude-standard 2> /dev/null \
    | while IFS= read -r -d '' f; do
        if [ -f "$f" ] && [ ! -L "$f" ] && [ "$(stat -c %s "$f")" -le 1000000 ]; then printf '%s\0' "$f"; fi
      done \
    | head -z -n 400 \
    | tar --null --no-recursion --files-from=- -cf "$INFRA/salida/entrega.tar"
  tar -tf "$INFRA/salida/entrega.tar" | head -n 40
}

juez() {
  cd "$INFRA/repo" || return 1
  claude_acotado "${AGENTE_MINUTOS:-20}" -p "$(cat "$INFRA/entrada/brief-juez.md")" \
      --max-turns "${AGENTE_MAX_TURNOS:-40}" \
      --allowedTools Read Glob Grep Bash \
      --model "${AGENTE_MODELO:-claude-sonnet-5-5}" \
      --output-format json \
    || echo "::warning::El juez terminó con error o agotó su tiempo." >&2
}

case "$FASE" in
  aislar) aislar "$@" ;;
  como) como "$@" ;;
  comprobar) comprobar "$@" ;;
  agente) agente ;;
  empacar) empacar ;;
  juez) juez ;;
  *) echo "fase desconocida: $FASE" >&2; exit 64 ;;
esac
