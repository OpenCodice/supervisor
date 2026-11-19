"""La parte determinista de reparar-infra.yml: todo lo que no es el agente.

    python -m supervisor.reparar preparar   …   huella, log recortado, briefs
    python -m supervisor.reparar anotar     …   una línea en estado/acciones.jsonl, y la sube
    python -m supervisor.reparar compuertas …   cerco y compuertas; deja arreglo.patch y compuertas.json
    python -m supervisor.reparar dinamicas  …   las compuertas que ejecutan código del arreglo
    python -m supervisor.reparar publicar   …   decide, publica, anota, relanza y comenta
    python -m supervisor.reparar revertir   …   `git revert` de un arreglo publicado

El agente nunca corre aquí: entrega archivos, y lo que entrega se trata como
datos. Lo que se publica sale siempre de un clon limpio.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import cadenas as _cadenas
from . import huella as _huella
from . import invariantes as inv
from . import politica
from .estado import Estado
from .invariantes import instante, iso
from .observar import Cliente, ErrorApi, cola_del_paso, recortar_corrida, recortar_job

RAIZ = _cadenas.RAIZ
AGENTE = RAIZ / "scripts" / "agente"
# Los arreglos que escribe el agente llevan su autoría, no la de una persona.
AUTOR = ("Claude (agente OpenCodice)", "noreply@anthropic.com")
VEREDICTOS = ("arreglo", "no-es-infra", "no-puedo")
# Lo que el juez tiene que contestar para que el arreglo siga.
JUEZ_ESPERADO = {"arregla_la_causa": "si", "cambia_algo_innecesario": "no", "riesgo": "no"}
_SHA = re.compile(r"[0-9a-f]{7,40}")
_NOMBRE = re.compile(r"[A-Za-z0-9._-]+")


class Descarte(Exception):
    """El arreglo no sigue; el mensaje es el motivo que queda anotado."""


def _ahora() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------

def _git(repo: Path | None, *args: str, check: bool = True, ocultar: str | None = None) -> subprocess.CompletedProcess:
    orden = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false",
             "-c", "protocol.file.allow=always"]
    if repo is not None:
        orden += ["-C", str(repo)]
    entorno = os.environ | {"GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": AUTOR[0], "GIT_AUTHOR_EMAIL": AUTOR[1],
                            "GIT_COMMITTER_NAME": AUTOR[0], "GIT_COMMITTER_EMAIL": AUTOR[1]}
    p = subprocess.run([*orden, *args], capture_output=True, text=True, env=entorno)
    if ocultar:
        p.stdout, p.stderr = p.stdout.replace(ocultar, "***"), p.stderr.replace(ocultar, "***")
    if check and p.returncode != 0:
        visibles = " ".join(a.replace(ocultar, "***") if ocultar else a for a in args)
        raise RuntimeError(f"git {visibles}: {p.stderr.strip()[-400:]}")
    return p


def url_de(org: str, repo: str, token: str | None = None) -> str:
    return f"https://x-access-token:{token}@github.com/{org}/{repo}.git" if token else f"https://github.com/{org}/{repo}.git"


def clonar(url: str, destino: Path, rev: str | None = None, token: str | None = None) -> str:
    """Un clon limpio; devuelve el sha en el que quedó."""
    if destino.exists():
        shutil.rmtree(destino)
    _git(None, "clone", "-q", url, str(destino), ocultar=token)
    if rev:
        _git(destino, "checkout", "-q", "--detach", rev)
    return _git(destino, "rev-parse", "HEAD").stdout.strip()


def subir(repo: Path, url: str, token: str | None = None, intentos: int = 3, dormir=None) -> bool:
    """Sube HEAD a main, rebasando si main avanzó."""
    import time
    dormir = dormir or time.sleep
    for n in range(1, intentos + 1):
        if (_git(repo, "pull", "-q", "--rebase", url, "main", check=False, ocultar=token).returncode == 0
                and _git(repo, "push", "-q", url, "HEAD:main", check=False, ocultar=token).returncode == 0):
            return True
        _git(repo, "rebase", "--abort", check=False)
        if n < intentos:
            dormir(n * 5)
    return False


# --------------------------------------------------------------------------
# Estado: la bitácora que comparte con el reconciliador
# --------------------------------------------------------------------------

def linea_de(accion: str, huella: str, detalle: dict, resultado: str = "ok", ahora: datetime | None = None) -> dict:
    return {
        "cuando": iso(ahora or _ahora()), "accion": accion, "huella": huella, "detalle": detalle,
        "resultado": resultado,
        "corrida_reparar": int(os.environ["GITHUB_RUN_ID"]) if os.environ.get("GITHUB_RUN_ID") else None,
    }


def anotar(raiz: Path, linea: dict, remoto: str | None = "origin", dormir=None) -> bool:
    """Agrega una línea a estado/acciones.jsonl del clon `raiz` y la sube. False si no se pudo subir.

    reparar-infra solo escribe acciones.jsonl; las huellas las lleva el
    reconciliador, que lee esta bitácora en su siguiente vuelta.
    """
    estado = Estado(raiz / "estado")
    estado.anotar_accion(linea)
    estado.guardar()
    _git(raiz, "add", "estado/acciones.jsonl")
    _git(raiz, "commit", "-q", "-m", f"Reparación de infraestructura: {linea['accion']} ({linea['huella']})")
    if remoto is None:
        return True
    return subir(raiz, remoto, dormir=dormir)


def leer_estado(carpeta: Path | str, cadenas: _cadenas.Cadenas, ahora: datetime | None = None) -> Estado:
    """El estado como lo ve el reconciliador: sin lo que ya salió de su ventana. Solo para leer;
    lo que reparar-infra escribe es una línea más en la bitácora (`anotar`)."""
    estado = Estado(carpeta)
    estado.compactar(ahora or _ahora(), cadenas.conservar_dias)
    return estado


def eventos(acciones: list[dict], huella: str) -> list[dict]:
    return [a for a in acciones if a.get("huella") == huella and a.get("accion") in politica.EVENTOS | {"intento-iniciado"}]


def arreglo_vigente(acciones: list[dict], huella: str) -> dict | None:
    """El último arreglo publicado de la huella que no se ha revertido."""
    vigente = None
    for a in eventos(acciones, huella):
        if a["accion"] == "arreglo-publicado" and a.get("resultado") == "ok":
            vigente = a
        elif a["accion"] == "revertido" and vigente and a["detalle"].get("commit") == vigente["detalle"].get("commit"):
            vigente = None
    return vigente


# --------------------------------------------------------------------------
# Preparar: la huella, el log recortado y los briefs
# --------------------------------------------------------------------------

def registro_de(estado: Estado, ident: str, ensayo: dict | None = None) -> dict:
    """La huella tal como está en el estado; en un ensayo se puede dar entera por inputs."""
    if ident in estado.huellas:
        return dict(estado.huellas[ident])
    if ensayo and all(ensayo.get(k) for k in ("repo", "workflow", "corrida")):
        return {"huella": ident, "repo": ensayo["repo"], "workflow": ensayo["workflow"],
                "job": ensayo.get("job") or "", "paso": ensayo.get("paso") or "",
                "corridas": [int(ensayo["corrida"])], "estado": "ensayo", "invariante": "I2"}
    raise Descarte(f"la huella {ident} no está en estado/huellas.jsonl"
                   + (" (en un ensayo se puede dar con repo, workflow, job, paso y corrida)" if ensayo is not None else ""))


def comprobar_registro(reg: dict, cadenas: _cadenas.Cadenas) -> None:
    motivo = politica.fuera_de_alcance(cadenas, reg)
    if motivo:
        raise Descarte(motivo)
    for campo in ("repo", "workflow"):
        if not _NOMBRE.fullmatch(str(reg.get(campo) or "")):
            raise Descarte(f"la huella trae un `{campo}` que no es un nombre: {reg.get(campo)!r}")


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _linea_limpia(linea: str, ancho: int = 400) -> str:
    linea = _CONTROL.sub("", _huella.limpiar(linea))
    return linea if len(linea) <= ancho else linea[:ancho] + " […]"


def contexto_de_corrida(jobs: list[dict], logs: dict[int, str], reg: dict, lineas: int) -> str:
    """Los jobs y pasos de una corrida con su conclusión, y la cola del paso que falló."""
    partes = []
    for crudo in jobs:
        job = recortar_job(crudo)
        partes.append(f"job «{_linea_limpia(job['name'] or '', 120)}»: {job['conclusion'] or job['status']}")
        for s in job["steps"]:
            partes.append(f"  {s['number']:>2}. {_linea_limpia(s['name'] or '', 120)}: {s['conclusion'] or s['status']}")
    for crudo in jobs:
        job = recortar_job(crudo)
        if job["id"] not in logs:
            continue
        paso = next((s for s in job["steps"] if s["name"] == reg.get("paso")), None) or next(
            (s for s in job["steps"] if s["conclusion"] in ("failure", "timed_out")), None)
        cola = [_linea_limpia(l) for l in cola_del_paso(logs[job["id"]], paso, lineas)]
        partes += ["", f"--- últimas {len(cola)} líneas del paso «{_linea_limpia((paso or {}).get('name') or '?', 120)}» "
                       f"del job «{_linea_limpia(job['name'] or '', 120)}» ---", *cola]
    return "\n".join(partes)


def bajar_contexto(cliente: Cliente, org: str, reg: dict, lineas: int = 300, corridas: int = 2,
                   tope_bytes: int = 120_000) -> tuple[str, str | None]:
    """(texto para el agente, firma calculada del log) de las últimas corridas de la huella."""
    partes, firma = [], None
    for ident in list(reg.get("corridas") or [])[-corridas:]:
        ruta = f"repos/{org}/{reg['repo']}/actions/runs/{int(ident)}"
        try:
            corrida = recortar_corrida(cliente.get(ruta))
            jobs = cliente.paginas(f"{ruta}/jobs", "jobs", filter="latest")
        except ErrorApi as e:
            partes.append(f"=== corrida {ident}: no se pudo leer ({e.estado}) ===")
            continue
        logs = {}
        for j in jobs:
            suyo = _huella.normalizar_job(j.get("name") or "") == reg.get("job") or not reg.get("job")
            if j.get("conclusion") in ("failure", "timed_out") and suyo:
                try:
                    logs[j["id"]] = cliente.texto(f"repos/{org}/{reg['repo']}/actions/jobs/{j['id']}/logs")
                except ErrorApi:
                    pass
                else:
                    paso = next((s for s in j.get("steps") or [] if s.get("conclusion") in ("failure", "timed_out")), None)
                    firma = _huella.firma(cola_del_paso(logs[j["id"]], paso, 40)) or firma
        partes.append(f"=== corrida {ident} ({corrida['conclusion']}, {corrida['created_at']}, "
                      f"commit {str(corrida['head_sha'])[:12]}) ===\n" + contexto_de_corrida(jobs, logs, reg, lineas))
    texto = "\n\n".join(partes)
    if len(texto.encode("utf-8")) > tope_bytes:
        texto = "[…]\n" + texto.encode("utf-8")[-tope_bytes:].decode("utf-8", "ignore")
    return texto, firma


def cuerpo_del_issue(cliente: Cliente, repo_propio: str, reg: dict) -> str | None:
    """El cuerpo del issue de la huella, solo si lo escribió el propio supervisor.

    Nunca sus comentarios: en un repo público los escribe cualquiera.
    """
    if not reg.get("issue"):
        return None
    try:
        issue = cliente.get(f"repos/{repo_propio}/issues/{int(reg['issue'])}")
        yo = cliente.get("user")["login"]
    except (ErrorApi, KeyError, TypeError, ValueError):
        return None
    if (issue.get("user") or {}).get("login") != yo or "pull_request" in issue:
        return None
    if not str(issue.get("title") or "").startswith(f"[{reg['huella']}]"):
        return None
    return "\n".join(_linea_limpia(l) for l in str(issue.get("body") or "").splitlines()[:40])


def intentos_previos(acciones: list[dict], ident: str, clon: Path | None = None, lineas: int = 200) -> list[dict]:
    """Lo que pasó con los intentos anteriores: por qué se descartó o se revirtió, y el parche revertido."""
    previos = []
    for a in eventos(acciones, ident):
        det = a.get("detalle") or {}
        if a["accion"] == "descartado":
            previos.append({"resultado": "descartado", "cuando": a["cuando"], "motivo": str(det.get("motivo") or "")[:1500]})
        elif a["accion"] == "revertido":
            parche = None
            if clon is not None and _SHA.fullmatch(str(det.get("commit") or "")):
                p = _git(clon, "show", "--format=", "--no-color", str(det["commit"]), check=False)
                if p.returncode == 0:
                    parche = "\n".join(p.stdout.splitlines()[:lineas])
            previos.append({"resultado": "revertido", "cuando": a["cuando"], "parche": parche,
                            "motivo": str(det.get("motivo") or "no pasó su corrida de prueba")[:600]})
    return previos


def _lista_md(elementos: list[str]) -> str:
    return "\n".join(f"- `{e}`" for e in elementos) or "- (nada)"


def armar_brief(plantilla: str, reg: dict, cadenas: _cadenas.Cadenas, *, rutas: dict, previos: list[dict],
                issue: str | None, puerta: str, limite: str) -> str:
    """El encargo del agente. Solo lleva lo que salió del estado, del log y del issue del supervisor."""
    alcance = cadenas.alcance(reg["repo"]) or {}
    conf = cadenas.reparacion
    huella = {k: reg.get(k) for k in ("huella", "repo", "workflow", "job", "paso", "firma", "clase", "corridas")}
    if previos:
        bloques = []
        for n, p in enumerate(previos, 1):
            bloques.append(f"### Intento {n}: {p['resultado']} ({p['cuando']})\n\nMotivo: {p['motivo']}")
            if p.get("parche"):
                bloques.append("El parche que se publicó y se revirtió (no repitas lo mismo):\n\n```diff\n"
                               + p["parche"].replace("```", "` ` `") + "\n```")
        texto_previos = "\n\n".join(bloques)
    else:
        texto_previos = "Es el primer intento."
    valores = {
        "__REPO__": reg["repo"], "__DIR__": rutas["repo"], "__LOG__": rutas["log"],
        "__VEREDICTO__": rutas["veredicto"], "__LINT__": rutas["lint"],
        "__HUELLA__": json.dumps(huella, ensure_ascii=False, indent=2),
        "__ARCHIVO__": f".github/workflows/{reg['workflow']}",
        "__PERMITIDOS__": _lista_md(alcance.get("permitidos", [])),
        "__NUEVOS__": _lista_md(alcance.get("nuevos", [])),
        "__PROHIBIDOS__": _lista_md(conf.get("prohibidos", [])),
        "__MAX_ARCHIVOS__": str(conf.get("max_archivos", 4)), "__MAX_LINEAS__": str(conf.get("max_lineas", 120)),
        "__MAX_PODER__": str(conf.get("max_lineas_paso_con_poder", 6)),
        "__MAX_IF__": str(conf.get("max_if_con_poder", 1)),
        "__ISSUE__": ("```\n" + issue.replace("```", "` ` `") + "\n```") if issue else "(sin issue)",
        "__PREVIOS__": texto_previos, "__PUERTA__": puerta, "__LIMITE__": limite,
        "__PARCHE__": rutas.get("parche", ""), "__PARTIDA__": rutas.get("partida", ""),
    }
    for k, v in valores.items():
        plantilla = plantilla.replace(k, v)
    return plantilla


def preparar(args, cadenas: _cadenas.Cadenas, cliente: Cliente, ahora: datetime) -> int:
    salida = Path(args.salida)
    salida.mkdir(parents=True, exist_ok=True)
    estado = leer_estado(args.estado, cadenas, ahora)
    ensayo = {k: getattr(args, k) for k in ("repo", "workflow", "job", "paso", "corrida")} if args.ensayo else None
    reg = registro_de(estado, args.huella, ensayo)
    comprobar_registro(reg, cadenas)
    if not args.ensayo:
        if args.base:
            raise Descarte("`base` solo vale con `ensayo`")
        tope = cadenas.politica.get("intentos_agente_por_huella", 2)
        hechos = politica.intentos_de(estado.acciones, reg, cadenas, ahora)
        if hechos >= tope:
            raise Descarte(f"la huella ya gastó sus {tope} intentos")
        if arreglo_vigente(estado.acciones, args.huella):
            raise Descarte("la huella tiene un arreglo publicado en prueba")
    if args.base and not _SHA.fullmatch(args.base):
        raise Descarte("`base` tiene que ser un sha")

    clon = Path(args.clon)
    partida = clonar(url_de(cadenas.org, reg["repo"]), clon, args.base or None)
    log, firma = bajar_contexto(cliente, cadenas.org, reg, lineas=cadenas.reparacion.get("lineas_log", 300))
    if not reg.get("firma"):
        reg["firma"] = firma or ""
    (salida / "log.txt").write_text(log + "\n", encoding="utf-8")
    previos = intentos_previos(estado.acciones, args.huella, clon)
    issue = cuerpo_del_issue(cliente, f"{cadenas.org}/{cadenas.general['repo_supervisor']}", reg)
    minutos = int(args.minutos)
    limite = (ahora + timedelta(minutes=minutos - 5)).strftime("%H:%M UTC")
    rutas = {"repo": args.dir_repo, "log": f"{args.dir_entrada}/log.txt", "veredicto": f"{args.dir_salida}/veredicto.json",
             "lint": f"{args.dir_entrada}/lint_workflows.py", "parche": f"{args.dir_entrada}/arreglo.patch",
             "partida": args.dir_repo}
    for nombre, plantilla in (("brief.md", "brief-infra.md"), ("brief-juez.md", "brief-juez-infra.md")):
        (salida / nombre).write_text(armar_brief(
            (AGENTE / plantilla).read_text(encoding="utf-8"), reg, cadenas, rutas=rutas, previos=previos,
            issue=issue, puerta="__PUERTA__", limite=limite), encoding="utf-8")
    (salida / "huella.json").write_text(json.dumps({
        "registro": reg, "partida": partida, "ensayo": bool(args.ensayo), "intento": len(previos) + 1,
        "cuando": iso(ahora)}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Huella {args.huella}: {reg['repo']}/{reg['workflow']} · {reg.get('job')} / {reg.get('paso')}")
    print(f"Partida {partida[:12]} · {len(log.splitlines())} líneas de log · {len(previos)} intentos previos")
    if firma and reg.get("firma") and firma != reg["firma"]:
        print(f"::warning::La firma del log ({firma}) no es la de la huella ({reg['firma']}).")
    return 0


# --------------------------------------------------------------------------
# Lo que entrega el agente
# --------------------------------------------------------------------------

def superponer(entrega: Path, borrados: list[str], despues: Path, tope_archivo: int = 1_000_000,
               tope_total: int = 20_000_000, tope_n: int = 500) -> None:
    """Pone sobre un clon limpio los archivos que el agente cambió. La entrega es un tar: solo datos."""
    total = 0
    with tarfile.open(entrega) as tar:
        miembros = tar.getmembers()
        if len(miembros) > tope_n:
            raise Descarte(f"la entrega trae {len(miembros)} archivos")
        for m in miembros:
            ruta = Path(m.name)
            if ruta.is_absolute() or ".." in ruta.parts or (ruta.parts and ruta.parts[0] == ".git"):
                raise Descarte(f"la entrega trae una ruta fuera del repo: {m.name[:120]}")
            if m.isdir():
                continue
            if not m.isfile():
                raise Descarte(f"la entrega trae algo que no es un archivo regular: {m.name[:120]}")
            total += m.size
            if m.size > tope_archivo or total > tope_total:
                raise Descarte(f"la entrega es demasiado grande ({m.name[:120]})")
            destino = despues / ruta
            if destino.is_symlink() or any(p.is_symlink() for p in destino.parents if despues in p.parents or p == despues):
                raise Descarte(f"la entrega escribe a través de un enlace simbólico: {m.name[:120]}")
            destino.parent.mkdir(parents=True, exist_ok=True)
            fuente = tar.extractfile(m)
            destino.write_bytes(fuente.read() if fuente else b"")
            destino.chmod(0o755 if m.mode & 0o100 else 0o644)
    for nombre in borrados:
        ruta = Path(nombre)
        if not nombre or ruta.is_absolute() or ".." in ruta.parts or ruta.parts[0] == ".git":
            continue
        (despues / ruta).unlink(missing_ok=True)


def leer_veredicto(ruta: Path) -> dict:
    """El veredicto del agente, o `ausente` si no está o no tiene la forma pedida."""
    try:
        if ruta.stat().st_size > 100_000:
            raise ValueError
        v = json.loads(ruta.read_text(encoding="utf-8"))
        resultado = v["resultado"]
    except (OSError, ValueError, KeyError, TypeError):
        return {"resultado": "ausente"}
    if resultado not in VEREDICTOS:
        return {"resultado": "ausente"}
    texto = lambda k: " ".join(str(v.get(k) or "").split())[:1500]  # noqa: E731
    return {"resultado": resultado, "explicacion": texto("explicacion"), "causa_raiz": texto("causa_raiz"),
            "prueba": texto("prueba")}


def leer_juez(ruta: Path) -> dict | None:
    """Las tres respuestas del juez, de lo último que dijo; None si no hay un JSON con esa forma."""
    try:
        crudo = ruta.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        envoltura = json.loads(crudo)
        texto = envoltura.get("result") if isinstance(envoltura, dict) else None
    except ValueError:
        texto = None
    texto = texto if isinstance(texto, str) else crudo
    # El último objeto JSON del texto que tenga exactamente las tres respuestas.
    for m in reversed(list(re.finditer(r"\{[^{}]*\}", texto, re.S))):
        try:
            v = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(v, dict) and set(JUEZ_ESPERADO) <= set(v):
            respuestas = {k: str(v[k]).strip().lower().replace("í", "i") for k in JUEZ_ESPERADO}
            if all(r in ("si", "no") for r in respuestas.values()):
                return respuestas | {"nota": " ".join(str(v.get("nota") or "").split())[:800]}
            return None
    return None


def decidir(veredicto: dict, compuertas: dict | None, dinamicas_: dict | None, juez: dict | None) -> tuple[bool, str, bool]:
    """(se publica, motivo, definitivo). `definitivo`: otro intento del agente no cambiaría nada."""
    if veredicto.get("resultado") == "no-es-infra":
        return False, f"el agente dice que no es infraestructura: {veredicto.get('explicacion') or 'sin explicación'}", True
    if veredicto.get("resultado") == "no-puedo":
        return False, f"el agente no pudo: {veredicto.get('explicacion') or 'sin explicación'}", False
    if veredicto.get("resultado") != "arreglo":
        return False, "el agente no dejó un veredicto", False
    for nombre, resultado in (("compuertas", compuertas), ("compuertas que ejecutan el arreglo", dinamicas_)):
        if not isinstance(resultado, dict) or "pasa" not in resultado:
            return False, f"las {nombre} no terminaron", False
        if resultado["pasa"] is not True:
            fallas = "; ".join(f"[{f.get('compuerta')}] {f.get('detalle')}" for f in (resultado.get("fallas") or [])[:6])
            return False, f"no pasó las {nombre}: {fallas or 'sin detalle'}", False
    if juez is None:
        return False, "el juez no dejó un veredicto con la forma pedida", False
    if {k: juez.get(k) for k in JUEZ_ESPERADO} != JUEZ_ESPERADO:
        return False, ("el juez no lo aprueba ("
                       + ", ".join(f"{k}={juez.get(k)}" for k in JUEZ_ESPERADO) + f"): {juez.get('nota', '')}"), False
    return True, "", False


# --------------------------------------------------------------------------
# Compuertas (job sin secretos)
# --------------------------------------------------------------------------

def sha256(ruta: Path) -> str:
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


def parche_de(despues: Path) -> str:
    """El diff de un clon con cambios en su árbol, con los archivos nuevos incluidos."""
    nuevos = _git(despues, "ls-files", "-z", "--others", "--exclude-standard").stdout.split("\0")
    nuevos = [n for n in nuevos if n]
    if nuevos:
        _git(despues, "add", "-N", "--", *nuevos)
    return _git(despues, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--binary").stdout


def correr_compuertas(args, cadenas: _cadenas.Cadenas) -> int:
    from . import compuertas as cp
    trabajo = Path(args.trabajo)
    trabajo.mkdir(parents=True, exist_ok=True)
    datos = json.loads(Path(args.huella_json).read_text(encoding="utf-8"))
    reg, antes, despues = datos["registro"], Path(args.antes), Path(args.despues)
    veredicto = leer_veredicto(Path(args.veredicto))
    resultado = cp.Resultado()
    try:
        if _git(antes, "rev-parse", "HEAD").stdout.strip() != datos["partida"]:
            raise Descarte("el clon de partida no está en el commit de partida")
        borrados = Path(args.borrados).read_text(encoding="utf-8").splitlines() if Path(args.borrados).is_file() else []
        if Path(args.entrega).is_file():
            superponer(Path(args.entrega), borrados, despues)
        cambios = cp.cambios_de_git(antes, despues)
        resultado = cp.evaluar(antes, despues, cambios, reg, cadenas, hermanos=Path(args.hermanos) if args.hermanos else None)
        (trabajo / "arreglo.patch").write_text(parche_de(despues), encoding="utf-8")
    except Descarte as e:
        resultado.falla("alcance", str(e))
        (trabajo / "arreglo.patch").write_text("", encoding="utf-8")
    informe = resultado.como_dict() | {"veredicto": veredicto, "partida": datos["partida"],
                                       "sha_parche": sha256(trabajo / "arreglo.patch")}
    (trabajo / "compuertas.json").write_text(json.dumps(informe, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Veredicto del agente: {veredicto['resultado']}")
    print(f"Compuertas: {'pasan' if resultado.pasa else 'NO pasan'} · {len(resultado.archivos)} archivos, {resultado.lineas} líneas")
    for f in resultado.fallas:
        print(f"  ✗ [{f['compuerta']}] {f['detalle']}")
    salida = os.environ.get("GITHUB_OUTPUT")
    if salida:
        with open(salida, "a", encoding="utf-8") as f:
            f.write(f"pasa={'true' if resultado.pasa else 'false'}\nveredicto={veredicto['resultado']}\n"
                    f"sha_parche={informe['sha_parche']}\n")
    return 0


def correr_dinamicas(args, cadenas: _cadenas.Cadenas) -> int:
    from . import compuertas as cp
    informe = json.loads(Path(args.compuertas).read_text(encoding="utf-8"))
    datos = json.loads(Path(args.huella_json).read_text(encoding="utf-8"))
    prefijo = ["sudo", "-n", "-u", args.como, "-H", "env", "-i", f"PATH={os.environ.get('PATH', '')}",
               f"HOME=/home/{args.como}", "LANG=C.UTF-8"] if args.como else None
    r = cp.dinamicas(Path(args.arbol), informe.get("archivos") or [], cadenas, datos["registro"]["repo"], prefijo)
    Path(args.salida).write_text(json.dumps(r.como_dict(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Compuertas que ejecutan el arreglo ({', '.join(r.corridas) or 'ninguna'}): {'pasan' if r.pasa else 'NO pasan'}")
    for f in r.fallas:
        print(f"  ✗ [{f['compuerta']}] {f['detalle']}")
    return 0


# --------------------------------------------------------------------------
# Publicar
# --------------------------------------------------------------------------

def aplicar_y_publicar(url: str, parche: Path, reg: dict, cadenas: _cadenas.Cadenas, trabajo: Path, *,
                       token: str | None = None, cuerpo: str | None = None, hermanos: Path | None = None,
                       cuales: tuple[str, ...] | None = None, dormir=None) -> dict:
    """Aplica el parche sobre un clon limpio de main, repite las compuertas que no ejecutan nada y lo sube.

    Devuelve {commit, archivos}; lanza Descarte si no aplica limpio, no pasa o no se puede subir.
    """
    from . import compuertas as cp
    antes, despues = trabajo / "main", trabajo / "main-con-arreglo"
    cabeza = clonar(url, antes, token=token)
    clonar(url, despues, cabeza, token=token)
    _git(despues, "checkout", "-q", "-B", "main")
    if _git(despues, "apply", "--index", "--whitespace=nowarn", str(parche), check=False).returncode != 0:
        raise Descarte("el parche no aplica limpio sobre main (main cambió desde la partida)")
    cambios = cp.cambios_de_git(antes, despues)
    resultado = cp.evaluar(antes, despues, cambios, reg, cadenas, hermanos=hermanos, cuales=cuales or cp.ESTATICAS)
    if not resultado.pasa:
        raise Descarte("sobre main no pasa las compuertas: "
                       + "; ".join(f"[{f['compuerta']}] {f['detalle']}" for f in resultado.fallas[:6]))
    _git(despues, "commit", "-q", "-m", mensaje_de(reg), *(["-m", cuerpo] if cuerpo else []))
    if not subir(despues, url, token, dormir=dormir):
        raise Descarte("no se pudo subir el arreglo a main tras tres intentos")
    return {"commit": _git(despues, "rev-parse", "HEAD").stdout.strip(), "archivos": resultado.archivos,
            "clon": str(despues)}


def mensaje_de(reg: dict, prefijo: str = "Reparación") -> str:
    que = " ".join(str(reg.get("paso") or reg.get("workflow") or "").split())[:120]
    return f"{prefijo} automática de infraestructura: {que} ({reg['huella']})"


def revertir_commit(url: str, sha: str, reg: dict, cadenas: _cadenas.Cadenas, trabajo: Path, *,
                    token: str | None = None, hermanos: Path | None = None,
                    cuales: tuple[str, ...] | None = None, dormir=None) -> dict:
    """`git revert` de un arreglo sobre un clon limpio de main. Falla cerrado si ya no es un revert limpio."""
    from . import compuertas as cp
    main, resultado, previo = trabajo / "main", trabajo / "main-revertido", trabajo / "antes-del-arreglo"
    cabeza = clonar(url, main, token=token)
    if _git(main, "merge-base", "--is-ancestor", sha, "HEAD", check=False).returncode != 0:
        raise Descarte(f"{sha[:12]} no está en main")
    clonar(url, resultado, cabeza, token=token)
    _git(resultado, "checkout", "-q", "-B", "main")
    if _git(resultado, "revert", "--no-edit", sha, check=False).returncode != 0:
        _git(resultado, "revert", "--abort", check=False)
        raise Descarte(f"revertir {sha[:12]} ya no es limpio: main cambió las mismas líneas")
    _git(resultado, "commit", "-q", "--amend", "-m", mensaje_de(reg, "Reversión"),
         "-m", f"Revierte {sha}: el arreglo no pasó su corrida de prueba.")
    # Un revert vuelve a un estado que ya corría: no puede traer hallazgos que no
    # estuvieran en main ni en el árbol anterior al arreglo.
    clonar(url, previo, f"{sha}~1", token=token)
    formas = cuales or ("yaml", "lint", "actionlint", "shell", "node")
    frente_a = []
    for base in (main, previo):
        cambios = [c for c in cp.cambios_de_arboles(base, resultado)]
        r = cp.evaluar(base, resultado, cambios, reg, cadenas, hermanos=hermanos, cuales=formas)
        frente_a.append({(f["compuerta"], f["detalle"]) for f in r.fallas})
    nuevas = sorted(frente_a[0] & frente_a[1])
    if nuevas:
        raise Descarte("el revert no pasa las compuertas de forma: " + "; ".join(f"[{c}] {d}" for c, d in nuevas[:6]))
    if not subir(resultado, url, token, dormir=dormir):
        raise Descarte("no se pudo subir el revert a main tras tres intentos")
    return {"commit": sha, "commit_revert": _git(resultado, "rev-parse", "HEAD").stdout.strip()}


def foto_del_repo(cliente: Cliente, cadenas: _cadenas.Cadenas, repo: str, ahora: datetime) -> dict:
    desde = (ahora - timedelta(days=cadenas.general.get("dias_foto", 9))).strftime("%Y-%m-%dT%H:%M:%SZ")
    corridas = cliente.paginas(f"repos/{cadenas.org}/{repo}/actions/runs", "workflow_runs", created=f">={desde}")
    return {"repos": {repo: {"observable": True, "corridas": [recortar_corrida(c) for c in corridas]}}, "datos": {}}


def relanzar_prueba(cliente, cadenas: _cadenas.Cadenas, reg: dict, acciones: list[dict], ahora: datetime,
                    foto: dict | None = None) -> dict:
    """Relanza la corrida que falló, con sus inputs, si la regla del único lanzador lo permite.

    Si no se puede ahora, no pasa nada: el reconciliador la lanza en una vuelta siguiente.
    """
    from .actuar import Actor
    wf = cadenas.workflow(reg["repo"], reg.get("workflow") or "")
    if wf is None or wf.solo_grupo:
        return {"lanzada": False, "motivo": "el workflow no está declarado: la prueba es su siguiente corrida"}
    foto = foto or foto_del_repo(cliente, cadenas, reg["repo"], ahora)
    todas = inv.corridas_de(foto, reg["repo"], wf.archivo)
    fallida = next((c for c in todas if c["id"] == (reg.get("corridas") or [None])[-1]), None)
    inputs = inv.inputs_de(fallida, wf) if fallida else None
    if inv.es_manual(inputs, wf) or not inv.relanzable(inputs, wf):
        return {"lanzada": False, "motivo": "la corrida que falló no se puede relanzar con sus inputs"}
    viva = next((c for c in todas if inv.viva(c) and inv.abarca(inv.inputs_de(c, wf), inputs, wf)), None)
    if viva:
        return {"lanzada": False, "motivo": f"la corrida {viva['id']} ya está en curso o en espera", "corrida": viva["id"]}
    vuelta = politica._Vuelta(foto=foto, cadenas=cadenas, ahora=ahora, huellas={}, acciones=acciones)
    paso, motivo = politica._puerta(vuelta, wf, inputs)
    if paso != "lanzar":
        return {"lanzada": False, "motivo": motivo}
    detalle = politica._detalle_lanzar(wf, inputs)
    Actor(cliente, cadenas, "actuar")._lanzar(detalle)
    return {"lanzada": True, "repo": wf.repo, "workflow": wf.archivo, "inputs": detalle["inputs"]}


def comentar(cliente, cadenas: _cadenas.Cadenas, reg: dict, texto: str) -> None:
    if not reg.get("issue"):
        return
    try:
        cliente.post(f"repos/{cadenas.org}/{cadenas.general['repo_supervisor']}/issues/{int(reg['issue'])}/comments",
                     {"body": texto})
    except ErrorApi as e:
        print(f"::warning::No se pudo comentar el issue #{reg['issue']}: {e.estado}")


def _corrida_url() -> str:
    e = os.environ
    if not e.get("GITHUB_RUN_ID"):
        return "corrida local"
    return f"{e.get('GITHUB_SERVER_URL', 'https://github.com')}/{e.get('GITHUB_REPOSITORY', '')}/actions/runs/{e['GITHUB_RUN_ID']}"


def _json(ruta: str | None) -> dict | None:
    try:
        v = json.loads(Path(ruta).read_text(encoding="utf-8")) if ruta else None
    except (OSError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def publicar(args, cadenas: _cadenas.Cadenas, ahora: datetime) -> int:
    datos = _json(args.huella_json)
    if datos is None:
        # Sin la huella no hay ni qué anotar: preparar no terminó y el intento no se contó.
        print("::error::preparar no dejó la huella: no hay nada que publicar ni que anotar.")
        return 1
    reg, trabajo = datos["registro"], Path(args.trabajo)
    trabajo.mkdir(parents=True, exist_ok=True)
    compuertas = _json(args.compuertas)
    parche = Path(args.parche)
    veredicto = (compuertas or {}).get("veredicto") or {"resultado": "ausente"}
    se_publica, motivo, definitivo = decidir(veredicto, compuertas, _json(args.dinamicas), leer_juez(Path(args.juez)))
    if se_publica and (not parche.is_file() or sha256(parche) != (compuertas or {}).get("sha_parche")
                       or sha256(parche) != args.sha_parche):
        se_publica, motivo = False, "el parche no es el que pasó las compuertas"
    resumen = [f"## reparar-infra — huella `{reg['huella']}`", "",
               f"`{reg['repo']}/{reg.get('workflow')}` · {reg.get('job')} / {reg.get('paso')}", "",
               f"Veredicto del agente: **{veredicto.get('resultado')}** — {veredicto.get('explicacion', '')}"]
    codigo = 0
    if args.ensayo:
        resumen.append(f"Ensayo: {'se habría publicado' if se_publica else 'se habría descartado: ' + motivo}. "
                       "No se publicó ni se anotó nada.")
    else:
        token = os.environ.get("SUPERVISOR_TOKEN") or None
        propio = Cliente(os.environ.get("GITHUB_TOKEN"))
        raiz = Path(args.supervisor)
        linea = None
        if se_publica:
            try:
                cuerpo = (f"Causa: {veredicto.get('causa_raiz') or veredicto.get('explicacion') or '—'}\n\n"
                          f"Arreglo escrito por el agente de infraestructura del supervisor; pasó sus compuertas "
                          f"deterministas y un juez independiente. Si su corrida de prueba no pasa el paso, se "
                          f"revierte solo.\nSesión: {_corrida_url()}")
                hecho = aplicar_y_publicar(url_de(cadenas.org, reg["repo"], token), parche, reg, cadenas, trabajo,
                                           token=token, cuerpo=cuerpo,
                                           hermanos=Path(args.hermanos) if args.hermanos else None)
            except (Descarte, RuntimeError) as e:
                se_publica, motivo = False, str(e)
            else:
                publicado = _ahora()
                try:
                    prueba = relanzar_prueba(Cliente(token), cadenas, reg, leer_estado(raiz / "estado", cadenas, publicado).acciones,
                                             publicado)
                except ErrorApi as e:
                    prueba = {"lanzada": False, "motivo": f"no se pudo lanzar: HTTP {e.estado}"}
                prueba["vence"] = iso(publicado + timedelta(hours=cadenas.reparacion.get("prueba_max_h", 12)))
                linea = linea_de("arreglo-publicado", reg["huella"], {
                    "repo": reg["repo"], "workflow": reg.get("workflow"), "commit": hecho["commit"],
                    "archivos": hecho["archivos"], "prueba": prueba}, ahora=publicado)
                if not anotar(raiz, linea):
                    # Un arreglo que el reconciliador no puede vigilar no se queda en main.
                    revertido = "y no se pudo revertir"
                    try:
                        revertir_commit(url_de(cadenas.org, reg["repo"], token), hecho["commit"], reg, cadenas,
                                        trabajo / "deshacer", token=token)
                        revertido = "y se revirtió"
                    except (Descarte, RuntimeError) as e:
                        revertido += f": {e}"
                    print(f"::error::El arreglo {hecho['commit'][:12]} se publicó pero no se pudo anotar, {revertido}.")
                    return 1
                enlace = f"https://github.com/{cadenas.org}/{reg['repo']}/commit/{hecho['commit']}"
                resumen.append(f"**Publicado**: {enlace} ({', '.join(hecho['archivos'])}). Corrida de prueba: "
                               + ("relanzada" if prueba.get("lanzada") else f"la lanza el reconciliador ({prueba.get('motivo')})")
                               + f"; vence {prueba['vence']}.")
                comentar(propio, cadenas, reg, (
                    f"**Arreglo publicado** por el agente de infraestructura: {enlace}\n\n"
                    f"Causa: {veredicto.get('causa_raiz') or '—'}\n\nPasó las compuertas deterministas y el juez. "
                    f"Si la corrida de prueba no pasa el paso antes de {prueba['vence']}, se revierte solo.\n\n"
                    f"Sesión: {_corrida_url()}"))
        if not se_publica:
            fallas = [(f.get("compuerta"), f.get("detalle")) for f in ((compuertas or {}).get("fallas") or [])[:8]]
            linea = linea_de("descartado", reg["huella"], {"motivo": motivo[:1500], "definitivo": definitivo,
                                                          "veredicto": veredicto.get("resultado"), "fallas": fallas})
            if not anotar(raiz, linea):
                print("::error::No se pudo anotar el descarte.")
                codigo = 1
            resumen.append(f"**Descartado**: {motivo}")
            comentar(propio, cadenas, reg, f"**Intento descartado**: {motivo}\n\nSesión: {_corrida_url()}")
    texto = "\n".join(resumen) + "\n"
    print(texto)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(texto)
    return codigo


def revertir(args, cadenas: _cadenas.Cadenas) -> int:
    raiz = Path(args.supervisor)
    estado = leer_estado(raiz / "estado", cadenas)
    reg = registro_de(estado, args.huella)
    vigente = arreglo_vigente(estado.acciones, args.huella)
    # Solo se revierte un commit que este mismo workflow publicó para esta huella.
    if vigente is None or vigente["detalle"].get("commit") != args.sha or not _SHA.fullmatch(args.sha):
        print(f"::error::{args.sha} no es el arreglo vigente de la huella {args.huella}: no se revierte nada.")
        return 1
    comprobar_registro(reg | {"repo": vigente["detalle"]["repo"]}, cadenas)
    token = os.environ.get("SUPERVISOR_TOKEN") or None
    propio = Cliente(os.environ.get("GITHUB_TOKEN"))
    try:
        hecho = revertir_commit(url_de(cadenas.org, vigente["detalle"]["repo"], token), args.sha, reg, cadenas,
                                Path(args.trabajo), token=token, hermanos=Path(args.hermanos) if args.hermanos else None)
    except (Descarte, RuntimeError) as e:
        linea = linea_de("reversion-fallida", args.huella, {"repo": vigente["detalle"]["repo"], "commit": args.sha,
                                                           "motivo": str(e)[:1500]})
        comentar(propio, cadenas, reg, f"**No se pudo revertir** `{args.sha[:12]}`: {e}\n\nSesión: {_corrida_url()}")
        print(f"::error::No se pudo revertir {args.sha[:12]}: {e}")
    else:
        linea = linea_de("revertido", args.huella, {"repo": vigente["detalle"]["repo"], **hecho})
        comentar(propio, cadenas, reg, (
            f"**Arreglo revertido**: `{args.sha[:12]}` no pasó su corrida de prueba; "
            f"revert `{hecho['commit_revert'][:12]}`.\n\nSesión: {_corrida_url()}"))
        print(f"Revertido {args.sha[:12]} con {hecho['commit_revert'][:12]}.")
    if not anotar(raiz, linea):
        print("::error::No se pudo anotar el resultado de la reversión.")
        return 1
    return 0 if linea["accion"] == "revertido" else 1


# --------------------------------------------------------------------------
# Entrada
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    an = argparse.ArgumentParser(prog="python -m supervisor.reparar", description=__doc__.splitlines()[0])
    sub = an.add_subparsers(dest="orden", required=True)
    p = sub.add_parser("preparar")
    p.add_argument("--huella", required=True)
    p.add_argument("--salida", required=True)
    p.add_argument("--clon", required=True, help="dónde clonar el repo afectado")
    p.add_argument("--estado", default=str(RAIZ / "estado"))
    p.add_argument("--ensayo", action="store_true")
    p.add_argument("--base", default="")
    for k in ("repo", "workflow", "job", "paso", "corrida"):
        p.add_argument(f"--{k}", default="")
    p.add_argument("--minutos", default="45")
    p.add_argument("--dir-repo", default="/srv/infra/repo")
    p.add_argument("--dir-entrada", default="/srv/infra/entrada")
    p.add_argument("--dir-salida", default="/srv/infra/salida")
    p = sub.add_parser("anotar")
    p.add_argument("--supervisor", default=str(RAIZ))
    p.add_argument("--huella-json", required=True)
    p.add_argument("--accion", required=True, choices=("intento-iniciado",))
    p = sub.add_parser("compuertas")
    for k in ("huella-json", "antes", "despues", "entrega", "borrados", "veredicto", "trabajo"):
        p.add_argument(f"--{k}", required=True)
    p.add_argument("--hermanos", default="")
    p = sub.add_parser("dinamicas")
    for k in ("huella-json", "compuertas", "arbol", "salida"):
        p.add_argument(f"--{k}", required=True)
    p.add_argument("--como", default="")
    p = sub.add_parser("publicar")
    for k in ("huella-json", "compuertas", "dinamicas", "juez", "parche", "trabajo"):
        p.add_argument(f"--{k}", required=True)
    p.add_argument("--sha-parche", default="")
    p.add_argument("--supervisor", default=str(RAIZ))
    p.add_argument("--hermanos", default="")
    p.add_argument("--ensayo", action="store_true")
    p = sub.add_parser("revertir")
    p.add_argument("--huella", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--trabajo", required=True)
    p.add_argument("--supervisor", default=str(RAIZ))
    p.add_argument("--hermanos", default="")
    args = an.parse_args(argv)
    cadenas = _cadenas.cargar()
    ahora = _ahora()
    try:
        if args.orden == "preparar":
            return preparar(args, cadenas, Cliente(), ahora)
        if args.orden == "anotar":
            datos = json.loads(Path(args.huella_json).read_text(encoding="utf-8"))
            linea = linea_de(args.accion, datos["registro"]["huella"],
                             {"repo": datos["registro"]["repo"], "partida": datos["partida"], "intento": datos["intento"]})
            if not anotar(Path(args.supervisor), linea):
                print("::error::No se pudo anotar el intento: el agente no corre sin su intento contado.")
                return 1
            return 0
        if args.orden == "compuertas":
            return correr_compuertas(args, cadenas)
        if args.orden == "dinamicas":
            return correr_dinamicas(args, cadenas)
        if args.orden == "publicar":
            return publicar(args, cadenas, ahora)
        return revertir(args, cadenas)
    except Descarte as e:
        print(f"::error::{e}")
        return 1
    except ErrorApi as e:
        print(f"::error::{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
