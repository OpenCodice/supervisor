"""Observa el estado real con la API de GitHub y lo deja en una «foto» JSON.

Solo lectura. La foto es lo único que ven los invariantes: con la misma foto
y el mismo `ahora`, la evaluación es la misma.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from . import huella as _huella
from . import invariantes as inv
from .cadenas import Cadenas

API = "https://api.github.com"
_CAMPOS_CORRIDA = ("id", "name", "path", "status", "conclusion", "display_title", "created_at",
                   "updated_at", "run_started_at", "head_sha", "event", "run_attempt", "html_url")
_CAMPOS_JOB = ("id", "name", "status", "conclusion", "started_at", "completed_at")
_CAMPOS_PASO = ("name", "number", "status", "conclusion", "started_at", "completed_at")
_SIGUIENTE = re.compile(r'<([^>]+)>;\s*rel="next"')
_MARCA = re.compile(r"^﻿?(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?Z ")


class ErrorApi(RuntimeError):
    def __init__(self, estado: int, metodo: str, url: str, cuerpo: str = ""):
        super().__init__(f"{metodo} {url}: HTTP {estado} {cuerpo[:300]}".strip())
        self.estado = estado


class LimiteDeUso(ErrorApi):
    """La API pidió esperar más de lo que esta corrida puede esperar."""


class _SinRedireccion(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _transporte(metodo: str, url: str, cabeceras: dict, cuerpo: bytes | None) -> tuple[int, dict, bytes]:
    """Una petición HTTP sin seguir redirecciones (el token no debe viajar a otro host)."""
    pedido = urllib.request.Request(url, data=cuerpo, headers=cabeceras, method=metodo)
    abridor = urllib.request.build_opener(_SinRedireccion)
    try:
        with abridor.open(pedido, timeout=60) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


class Cliente:
    """Cliente mínimo de la API REST: paginación y espera acotada ante el límite de uso."""

    def __init__(self, token: str | None = None, transporte=_transporte, dormir=time.sleep,
                 reloj=time.time, espera_max: float = 90):
        self.token = token if token is not None else (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
        self._transporte, self._dormir, self._reloj = transporte, dormir, reloj
        self._espera_restante = espera_max
        self.llamadas = 0

    def _cabeceras(self, con_token: bool = True) -> dict:
        cabeceras = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "opencodice-supervisor"}
        if con_token and self.token:
            cabeceras["Authorization"] = f"Bearer {self.token}"
        return cabeceras

    def pedir(self, metodo: str, ruta: str, cuerpo: dict | None = None, **params) -> tuple[int, dict, bytes]:
        url = ruta if ruta.startswith("http") else f"{API}/{ruta.lstrip('/')}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
        cabeceras = self._cabeceras()
        if datos is not None:
            cabeceras["Content-Type"] = "application/json"
        while True:
            self.llamadas += 1
            estado, cab, contenido = self._transporte(metodo, url, cabeceras, datos)
            espera = self._espera_pedida(estado, cab)
            if espera is None:
                break
            if espera > self._espera_restante:
                raise LimiteDeUso(estado, metodo, url,
                                  f"límite de uso de la API: pide esperar {espera:.0f} s y quedan "
                                  f"{self._espera_restante:.0f} s de espera en esta corrida")
            self._espera_restante -= espera
            self._dormir(espera)
        if estado >= 400:
            raise ErrorApi(estado, metodo, url, contenido.decode("utf-8", "replace"))
        return estado, cab, contenido

    def _espera_pedida(self, estado: int, cab: dict) -> float | None:
        if estado not in (403, 429):
            return None
        if cab.get("retry-after"):
            return float(cab["retry-after"]) + 1
        if cab.get("x-ratelimit-remaining") == "0" and cab.get("x-ratelimit-reset"):
            return max(1.0, float(cab["x-ratelimit-reset"]) - self._reloj() + 1)
        return 30.0 if estado == 429 else None

    def get(self, ruta: str, **params):
        return json.loads(self.pedir("GET", ruta, **params)[2] or b"null")

    def post(self, ruta: str, cuerpo: dict | None = None):
        contenido = self.pedir("POST", ruta, cuerpo if cuerpo is not None else {})[2]
        return json.loads(contenido) if contenido.strip() else None

    def patch(self, ruta: str, cuerpo: dict):
        return json.loads(self.pedir("PATCH", ruta, cuerpo)[2] or b"null")

    def delete(self, ruta: str) -> None:
        self.pedir("DELETE", ruta)

    def paginas(self, ruta: str, clave: str | None = None, max_paginas: int = 20, **params) -> list:
        """Todas las páginas de una lista (`clave` cuando viene envuelta en un objeto)."""
        elementos, url = [], ruta
        params = {"per_page": 100, **params}
        for _ in range(max_paginas):
            _, cab, contenido = self.pedir("GET", url, **params)
            pagina = json.loads(contenido)
            elementos.extend(pagina[clave] if clave else pagina)
            siguiente = _SIGUIENTE.search(cab.get("link", ""))
            if not siguiente:
                break
            url, params = siguiente.group(1), {}
        return elementos

    def texto(self, ruta: str) -> str:
        """Un recurso de texto que la API entrega con una redirección a su almacén."""
        estado, cab, contenido = self.pedir("GET", ruta)
        if estado in (301, 302, 303, 307, 308) and cab.get("location"):
            self.llamadas += 1
            estado, cab, contenido = self._transporte("GET", cab["location"], {"User-Agent": "opencodice-supervisor"}, None)
            if estado >= 400:
                raise ErrorApi(estado, "GET", ruta, "no se pudo bajar el log")
        return contenido.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# Recortes
# --------------------------------------------------------------------------

def recortar_corrida(c: dict) -> dict:
    return {k: c.get(k) for k in _CAMPOS_CORRIDA}


def recortar_job(j: dict) -> dict:
    job = {k: j.get(k) for k in _CAMPOS_JOB}
    job["steps"] = [{k: s.get(k) for k in _CAMPOS_PASO} for s in j.get("steps") or []]
    return job


def cola_del_paso(log: str, paso: dict | None, n: int) -> list[str]:
    """Los últimos renglones que el log del job tiene dentro del tiempo del paso, hasta su error."""
    lineas = log.splitlines()
    if paso and paso.get("started_at") and paso.get("completed_at"):
        desde, hasta = paso["started_at"][:19], paso["completed_at"][:19]
        dentro, vigente = [], False
        for linea in lineas:
            m = _MARCA.match(linea)
            if m:
                vigente = desde <= m.group(1) <= hasta
            if vigente:
                dentro.append(linea)
        if dentro:
            lineas = dentro
    errores = [i for i, l in enumerate(lineas) if "##[error]" in l]
    if errores:
        lineas = lineas[:errores[-1] + 1]
    # El runner repite en color cada renglón del script antes de correrlo: no es salida del programa.
    lineas = [l for l in lineas if "\x1b[36;1m" not in l]
    return [_huella.limpiar(l) for l in lineas[-n:]]


# --------------------------------------------------------------------------
# La foto
# --------------------------------------------------------------------------

def _jobs_de(cliente: Cliente, org: str, repo: str, corrida: dict, lineas: int, cache: Path | None) -> list[dict]:
    """Jobs de una corrida terminada, con la cola del log de los que fallaron. No cambian: se guardan."""
    # Los jobs de una corrida en curso todavía cambian: no se guardan.
    terminada = corrida.get("status") == "completed"
    archivo = cache / f"{repo}-{corrida['id']}-{corrida.get('run_attempt') or 1}.json" if cache and terminada else None
    if archivo and archivo.exists():
        return json.loads(archivo.read_text(encoding="utf-8"))
    crudos = cliente.paginas(f"repos/{org}/{repo}/actions/runs/{corrida['id']}/jobs", "jobs", filter="latest")
    jobs = []
    for crudo in crudos:
        job = recortar_job(crudo)
        if job["conclusion"] in ("failure", "timed_out") and job["steps"]:
            paso = next((s for s in job["steps"] if s["conclusion"] in ("failure", "timed_out")), None)
            try:
                job["log"] = cola_del_paso(cliente.texto(f"repos/{org}/{repo}/actions/jobs/{job['id']}/logs"),
                                           paso, lineas)
            except LimiteDeUso:
                raise
            except ErrorApi as e:
                # Un log vencido o ilegible no impide la huella: queda la conclusión del job.
                job["log"] = []
                job["log_error"] = str(e)
        jobs.append(job)
    if archivo:
        archivo.parent.mkdir(parents=True, exist_ok=True)
        archivo.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
    return jobs


def tomar_foto(cliente: Cliente, cadenas: Cadenas, ahora: datetime, cache: str | Path | None = None,
               huellas: dict | None = None) -> dict:
    org = cadenas.org
    dias = cadenas.general.get("dias_foto", 9)
    desde = (ahora - timedelta(days=dias)).strftime("%Y-%m-%dT%H:%M:%SZ")
    foto = {"cuando": inv.iso(ahora), "org": org, "dias": dias, "repos": {}, "datos": {}, "errores": []}
    cache = Path(cache) if cache else None

    repos = cadenas.repos_con_workflows()
    if (cadenas.encendido("I9") or cadenas.reparacion) and cadenas.general["repo_supervisor"] not in repos:
        repos.append(cadenas.general["repo_supervisor"])

    for repo in repos:
        entrada = {"observable": True, "corridas": [], "ultima_exitosa": {}}
        foto["repos"][repo] = entrada
        try:
            corridas = cliente.paginas(f"repos/{org}/{repo}/actions/runs", "workflow_runs", created=f">={desde}")
        except LimiteDeUso:
            raise
        except ErrorApi as e:
            # Un repo privado sin permiso responde 404: lo que no se ve no se evalúa.
            entrada["observable"] = False
            foto["errores"].append(f"{repo}: {e}")
            continue
        entrada["corridas"] = [recortar_corrida(c) for c in corridas]

        declarados = [w for w in cadenas.workflows if w.repo == repo]
        for wf in declarados:
            sin_inputs = any(not cad.get("inputs") for cad in wf.cadencias)
            es_web = cadenas.web.get("repo") == repo and cadenas.web.get("archivo") == wf.archivo
            if not (sin_inputs or es_web):
                continue
            if any(inv.exitosa(c) for c in inv.corridas_de(foto, repo, wf.archivo)):
                continue
            try:
                previas = cliente.get(f"repos/{org}/{repo}/actions/workflows/{wf.archivo}/runs",
                                      status="success", per_page=1)["workflow_runs"]
            except LimiteDeUso:
                raise
            except ErrorApi as e:
                foto["errores"].append(f"{repo}/{wf.archivo}: {e}")
                continue
            if previas:
                entrada["ultima_exitosa"][wf.archivo] = recortar_corrida(previas[0])

        rutas = sorted({r for wf in declarados for r in wf.extractor_nuevo})
        if rutas:
            ultimos = []
            for ruta in rutas:
                try:
                    commits = cliente.get(f"repos/{org}/{repo}/commits", sha="main", path=ruta, per_page=1)
                except LimiteDeUso:
                    raise
                except ErrorApi as e:
                    foto["errores"].append(f"{repo}:{ruta}: {e}")
                    continue
                if commits:
                    ultimos.append({"sha": commits[0]["sha"], "fecha": commits[0]["commit"]["committer"]["date"]})
            if ultimos:
                entrada["extractor"] = max(ultimos, key=lambda c: c["fecha"])

    lineas = cadenas.general.get("lineas_log", 40)
    en_prueba = tuple((r["repo"], r["workflow"], r["arreglo"]["cuando"]) for r in (huellas or {}).values()
                      if r.get("estado") == "arreglo-publicado" and r.get("arreglo") and r.get("workflow"))
    for repo, ids in inv.necesitan_jobs(foto, cadenas, en_prueba).items():
        por_id = {c["id"]: c for c in foto["repos"][repo]["corridas"]}
        for ident in ids:
            try:
                por_id[ident]["jobs"] = _jobs_de(cliente, org, repo, por_id[ident], lineas, cache)
            except LimiteDeUso:
                raise
            except ErrorApi as e:
                foto["errores"].append(f"{repo}#{ident}: {e}")

    for d in cadenas.datos:
        repo = d["repo"]
        try:
            cabeza = cliente.get(f"repos/{org}/{repo}/commits/main")
            abiertos = cliente.paginas(f"repos/{org}/{repo}/pulls", state="open")
        except LimiteDeUso:
            raise
        except ErrorApi as e:
            foto["errores"].append(f"{repo}: {e}")
            continue
        foto["datos"][repo] = {
            "main": {"sha": cabeza["sha"], "fecha": cabeza["commit"]["committer"]["date"]},
            "prs": [{
                "number": p["number"], "head_ref": p["head"]["ref"],
                "labels": [e["name"] for e in p.get("labels", [])],
                "created_at": p["created_at"], "updated_at": p["updated_at"],
                "cross": (p["head"].get("repo") or {}).get("full_name") != p["base"]["repo"]["full_name"],
            } for p in abiertos],
        }
    foto["llamadas"] = cliente.llamadas
    return foto
