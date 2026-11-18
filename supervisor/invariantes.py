"""Invariantes sobre el estado observado: foto + cadenas + ahora → hallazgos.

Módulo puro: no toca la red ni el disco. Cada invariante es una función
`iN(foto, cadenas, ahora) -> list[Hallazgo]` registrada en INVARIANTES.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from . import huella as _huella
from .cadenas import Cadenas, Workflow

EN_ESPERA = {"queued", "pending", "waiting", "requested"}
FALLAS = {"failure", "timed_out", "startup_failure"}

_TITULO = re.compile(r"^(?P<wf>[a-z0-9-]+)(?P<pares>(?: [a-z_]+=\[[^\]]*\])*)$")
_PAR = re.compile(r" ([a-z_]+)=\[([^\]]*)\]")
_ESTADO = re.compile(r"^estado pendiente=\[(true|false)\]$")
_DIAS = {"lun": 0, "mar": 1, "mie": 2, "jue": 3, "vie": 4, "sab": 5, "dom": 6}


@dataclass
class Hallazgo:
    invariante: str
    repo: str
    workflow: str | None
    objeto: str
    motivo: str
    # Inputs con los que habría que lanzar; None = desconocidos.
    inputs: dict | None = None
    relanzable: bool = True
    # Lo que identifica esta ocurrencia: si cambia, es un caso nuevo.
    base: str | None = None
    # Desde cuándo está rota (ISO): de ahí se cuentan las esperas.
    cuando: str | None = None
    corridas: list = field(default_factory=list)
    falla: dict | None = None
    repeticiones: int = 1
    # Corrida viva que, si termina bien, la resuelve.
    en_vuelo: int | None = None
    extra: dict = field(default_factory=dict)

    def como_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# Tiempo
# --------------------------------------------------------------------------

def instante(texto: str | None) -> datetime | None:
    if not texto:
        return None
    return datetime.fromisoformat(texto.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso(momento: datetime) -> str:
    return momento.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Corridas
# --------------------------------------------------------------------------

def archivo_de(corrida: dict) -> str:
    return (corrida.get("path") or "").split("@")[0].rsplit("/", 1)[-1]


def corridas_de(foto: dict, repo: str, archivo: str | None = None) -> list[dict]:
    """Corridas del repo (o de uno de sus workflows), de la más vieja a la más nueva."""
    todas = foto.get("repos", {}).get(repo, {}).get("corridas", [])
    if archivo is not None:
        todas = [c for c in todas if archivo_de(c) == archivo]
    return sorted(todas, key=lambda c: (c["created_at"], c["id"]))


def observable(foto: dict, repo: str) -> bool:
    return bool(foto.get("repos", {}).get(repo, {}).get("observable"))


def en_espera(c: dict) -> bool:
    return c.get("status") in EN_ESPERA


def en_curso(c: dict) -> bool:
    return c.get("status") == "in_progress"


def viva(c: dict) -> bool:
    return en_espera(c) or en_curso(c)


def exitosa(c: dict) -> bool:
    return c.get("status") == "completed" and c.get("conclusion") == "success"


def ejecuto_algo(c: dict) -> bool | None:
    """¿Llegó a correr algún paso? None si la foto no trae sus jobs."""
    if "jobs" not in c:
        return None
    return any(j.get("steps") for j in c["jobs"])


def perdida(c: dict) -> bool:
    """Cancelada mientras esperaba: nunca corrió nada."""
    return (c.get("status") == "completed" and c.get("conclusion") == "cancelled"
            and ejecuto_algo(c) is False)


def fallida(c: dict) -> bool:
    """Terminó mal habiendo corrido: en rojo, o cancelada con jobs ejecutados."""
    if c.get("status") != "completed":
        return False
    if c.get("conclusion") in FALLAS:
        return True
    return c.get("conclusion") == "cancelled" and ejecuto_algo(c) is True


def cuenta_como_posterior(c: dict) -> bool:
    """Una corrida que ya hizo, hace o hará el trabajo (o cuya falla ya tiene dueño)."""
    return viva(c) or exitosa(c) or fallida(c)


def inicio(c: dict) -> datetime:
    return instante(c.get("run_started_at") or c["created_at"])


def fin(c: dict) -> datetime:
    return instante(c.get("updated_at") or c["created_at"])


# --------------------------------------------------------------------------
# Inputs: se leen del título (run-name del contrato)
# --------------------------------------------------------------------------

def analizar_titulo(titulo: str | None) -> tuple[str, dict] | None:
    m = _TITULO.match(titulo or "")
    if not m:
        return None
    return m.group("wf"), dict(_PAR.findall(m.group("pares")))


def inputs_de(c: dict, wf: Workflow) -> dict | None:
    """Inputs de una corrida, completos con sus valores por omisión; None = desconocidos."""
    leido = analizar_titulo(c.get("display_title"))
    if leido and leido[0] == wf.nombre:
        return {k: leido[1].get(k, wf.valor_omision(k)) for k in wf.inputs}
    for previo in wf.titulos_previos:
        if re.search(previo["patron"], c.get("display_title") or ""):
            return {k: str(previo.get("inputs", {}).get(k, wf.valor_omision(k))) for k in wf.inputs}
    return None


def _significativos(wf: Workflow) -> list[str]:
    return [k for k in wf.inputs if k not in wf.inputs_ignorados]


def _lista(valor: str) -> frozenset:
    return frozenset((valor or "").split())


def es_manual(inputs: dict | None, wf: Workflow) -> bool:
    return inputs is not None and any(inputs.get(k, "") != wf.valor_omision(k) for k in wf.inputs_manuales)


def completa(inputs: dict | None, wf: Workflow) -> bool:
    """Sin restricciones: hace todo el trabajo del workflow."""
    if inputs is None:
        return False
    for k in _significativos(wf):
        esperado = "" if k in wf.inputs_lista or k in wf.inputs_uno else wf.valor_omision(k)
        if inputs.get(k, "") != esperado:
            return False
    return True


def abarca(a: dict | None, b: dict | None, wf: Workflow) -> bool:
    """¿Una corrida con inputs `a` hace todo lo que haría una con inputs `b`?"""
    if not _significativos(wf):
        return True
    if b is None:
        return a is None or completa(a, wf)
    if a is None:
        return False
    for k in _significativos(wf):
        va, vb = a.get(k, ""), b.get(k, "")
        if k in wf.inputs_lista:
            if va and not (vb and _lista(vb) <= _lista(va)):
                return False
        elif k in wf.inputs_uno:
            if va and va != vb:
                return False
        elif va != vb:
            return False
    return True


def unir(varios: list[dict | None], wf: Workflow) -> dict | None:
    """Los inputs de una sola corrida que abarca a todas; None = sin inputs (completa)."""
    if not varios or any(v is None for v in varios):
        return None
    unidos = {}
    for k in wf.inputs:
        valores = [v.get(k, "") for v in varios]
        if k in wf.inputs_ignorados:
            unidos[k] = wf.valor_omision(k)
        elif k in wf.inputs_lista:
            unidos[k] = "" if any(not x for x in valores) else " ".join(sorted(set().union(*map(_lista, valores))))
        elif k in wf.inputs_uno:
            unidos[k] = valores[0] if len(set(valores)) == 1 else ""
        else:
            if len(set(valores)) != 1:
                raise ValueError(f"{wf.archivo}: `{k}` no se puede unir: {sorted(set(valores))}")
            unidos[k] = valores[0]
    return unidos


def _escalares(inputs: dict | None, wf: Workflow) -> tuple:
    if inputs is None:
        return ("?",)
    return tuple(inputs.get(k, "") for k in _significativos(wf)
                 if k not in wf.inputs_lista and k not in wf.inputs_uno)


def para_lanzar(inputs: dict | None, wf: Workflow) -> dict:
    """Solo lo que difiere de la omisión: lo demás lo pone el workflow."""
    if inputs is None:
        return {}
    return {k: v for k, v in inputs.items()
            if k in wf.inputs and k not in wf.inputs_ignorados and v != wf.valor_omision(k)}


def relanzable(inputs: dict | None, wf: Workflow) -> bool:
    return inputs is not None or wf.relanzar_sin_inputs


def corridas_del_grupo(foto: dict, cadenas: Cadenas, repo: str, grupo: str | None) -> list[dict]:
    """Corridas del repo que comparten ese grupo de concurrency (y, con él, su único lugar de espera)."""
    if not grupo:
        return []
    return [c for w in cadenas.workflows if w.repo == repo
            for c in corridas_de(foto, repo, w.archivo)
            if w.grupo_de(None if w.solo_grupo else inputs_de(c, w)) == grupo]


def _vigilados(cadenas: Cadenas) -> list[Workflow]:
    return [w for w in cadenas.workflows if not w.solo_grupo]


def _posteriores(todas: list[dict], c: dict) -> list[dict]:
    return [p for p in todas if (p["created_at"], p["id"]) > (c["created_at"], c["id"])]


# --------------------------------------------------------------------------
# Falla principal de una corrida
# --------------------------------------------------------------------------

def falla_de(c: dict) -> dict | None:
    """Job, paso y firma de lo primero que falló; None si la foto no trae sus jobs."""
    if c.get("conclusion") == "startup_failure":
        return {"job": "", "paso": "", "firma": "startup_failure"}
    if "jobs" not in c:
        return None
    jobs = sorted(c["jobs"], key=lambda j: (j.get("completed_at") or "", j.get("id") or 0))
    for conclusiones in (("failure", "timed_out"), ("cancelled",)):
        for j in jobs:
            if j.get("conclusion") not in conclusiones or not j.get("steps"):
                continue
            paso = next((s for s in j["steps"] if s.get("conclusion") in conclusiones), None)
            firma = _huella.firma(j.get("log") or []) or j["conclusion"]
            return {"job": _huella.normalizar_job(j.get("name", "")),
                    "paso": paso["name"] if paso else "", "firma": firma}
    return {"job": "", "paso": "", "firma": c.get("conclusion") or ""}


def _misma_falla(a: dict | None, b: dict | None) -> bool:
    return a is not None and b is not None and (a["job"], a["paso"], a["firma"]) == (b["job"], b["paso"], b["firma"])


# --------------------------------------------------------------------------
# Qué jobs hacen falta (lo usa observar para no pedir de más)
# --------------------------------------------------------------------------

def necesitan_jobs(foto: dict, cadenas: Cadenas) -> dict[str, list[int]]:
    """Corridas cuyos jobs hay que leer para evaluar los invariantes, por repo."""
    pedir: dict[str, list[int]] = {}
    tope = cadenas.general.get("max_corridas_con_jobs", 40)
    cronico = cadenas.politica.get("cronico_corridas", 3)
    for repo in foto.get("repos", {}):
        ids: list[int] = []
        por_archivo: dict[str, list[dict]] = {}
        for c in corridas_de(foto, repo):
            por_archivo.setdefault(archivo_de(c), []).append(c)
        for archivo, todas in por_archivo.items():
            wf = cadenas.workflow(repo, archivo)
            if wf is None or wf.solo_grupo:
                cola = [c for c in todas if c.get("status") == "completed"
                        and c.get("conclusion") in FALLAS | {"success"}][-cronico:]
                if len(cola) == cronico and not any(exitosa(c) for c in cola):
                    ids.append(cola[-1]["id"])
                continue
            for c in todas:
                if c.get("status") != "completed" or c.get("conclusion") not in FALLAS | {"cancelled"}:
                    continue
                if c.get("conclusion") == "startup_failure":
                    continue
                ins = inputs_de(c, wf)
                cubierta = any((exitosa(p) or viva(p)) and abarca(inputs_de(p, wf), ins, wf)
                               for p in _posteriores(todas, c))
                if not cubierta:
                    ids.append(c["id"])
            if wf.encadenado:
                buenas = [c for c in todas if exitosa(c)]
                if buenas:
                    ids.append(buenas[-1]["id"])
        pedir[repo] = sorted(set(ids), reverse=True)[:tope]
    return pedir


# --------------------------------------------------------------------------
# I1 — Cadencia
# --------------------------------------------------------------------------

def _cumple_cadencia(c: dict, wf: Workflow, cadencia: dict) -> bool:
    ins = inputs_de(c, wf)
    filtro = {k: str(v) for k, v in cadencia.get("inputs", {}).items()}
    if ins is None:
        return not filtro
    if es_manual(ins, wf):
        return False
    if any(ins.get(k, "") != v for k, v in filtro.items()):
        return False
    # Una corrida limitada a una lista (un estado, unas claves) no es la vuelta completa.
    return all(not ins.get(k, "") for k in (*wf.inputs_lista, *wf.inputs_uno))


def _ultima_cita(horario: tuple[dict, ...], ahora: datetime) -> tuple[datetime, dict] | None:
    """La hora más reciente (≤ ahora) en que tocaba lanzar, y con qué inputs."""
    mejor = None
    for entrada in horario:
        hora, minuto = entrada["hora"].split(":")
        dias = entrada.get("dias", ["*"])
        cada_hora = hora == "*"
        paso = timedelta(hours=1) if cada_hora else timedelta(days=1)
        cita = ahora.replace(minute=int(minuto), second=0, microsecond=0)
        if not cada_hora:
            cita = cita.replace(hour=int(hora))
        # Ocho días (u ocho días de horas) hacia atrás alcanzan cualquier día de la semana.
        for _ in range(8 * 24 if cada_hora else 8):
            if cita <= ahora and ("*" in dias or cita.weekday() in {_DIAS[d] for d in dias}):
                # A la misma hora, la entrada con hora fija manda sobre la de cada hora.
                if mejor is None or (cita, not cada_hora) > (mejor[0], mejor[2]):
                    mejor = (cita, entrada, not cada_hora)
                break
            cita -= paso
    return (mejor[0], mejor[1]) if mejor else None


def i1(foto: dict, cadenas: Cadenas, ahora: datetime) -> list[Hallazgo]:
    hallazgos = []
    for wf in _vigilados(cadenas):
        if not observable(foto, wf.repo):
            continue
        todas = corridas_de(foto, wf.repo, wf.archivo)
        for n, cadencia in enumerate(wf.cadencias):
            ventana = timedelta(hours=cadencia["ventana_h"])
            candidatas = [c for c in todas if _cumple_cadencia(c, wf, cadencia)]
            buenas = [c for c in candidatas if exitosa(c)]
            ultima = max(buenas, key=inicio, default=None)
            previa = foto["repos"][wf.repo].get("ultima_exitosa", {}).get(wf.archivo)
            if ultima is None and previa and not cadencia.get("inputs"):
                ultima = previa
            if ultima is not None and ahora - inicio(ultima) <= ventana:
                continue
            vivas = [c for c in candidatas if viva(c)]
            filtro = {k: str(v) for k, v in cadencia.get("inputs", {}).items()}
            sufijo = "".join(f" {k}={v}" for k, v in filtro.items())
            hallazgos.append(Hallazgo(
                invariante="I1", repo=wf.repo, workflow=wf.archivo,
                objeto=f"{wf.repo}/{wf.archivo}{sufijo}",
                motivo=(f"sin corrida exitosa en {cadencia['ventana_h']} h"
                        + (f" (la última: {ultima['id']}, {iso(inicio(ultima))})" if ultima else "")),
                inputs={k: filtro.get(k, wf.valor_omision(k)) for k in wf.inputs},
                base=f"cadencia-{n}", cuando=iso(inicio(ultima) + ventana) if ultima else None,
                en_vuelo=vivas[-1]["id"] if vivas else None,
            ))
        if wf.lanza_horario and wf.horario:
            cita = _ultima_cita(wf.horario, ahora)
            tolerancia = timedelta(minutes=cadenas.politica.get("tolerancia_horario_min", 180))
            if cita is None or ahora - cita[0] > tolerancia:
                continue
            momento, entrada = cita
            if any(instante(c["created_at"]) >= momento - timedelta(minutes=2) for c in todas):
                continue
            filtro = {k: str(v) for k, v in entrada.get("inputs", {}).items()}
            hallazgos.append(Hallazgo(
                invariante="I1", repo=wf.repo, workflow=wf.archivo,
                objeto=f"{wf.repo}/{wf.archivo}@{iso(momento)}",
                motivo=f"tocaba lanzarlo a las {iso(momento)}",
                inputs={k: filtro.get(k, wf.valor_omision(k)) for k in wf.inputs},
                base=f"horario-{iso(momento)}", cuando=iso(momento),
            ))
    return hallazgos


# --------------------------------------------------------------------------
# I2 — Toda falla tiene dueño
# --------------------------------------------------------------------------

def i2(foto: dict, cadenas: Cadenas, ahora: datetime) -> list[Hallazgo]:
    hallazgos = []
    for wf in _vigilados(cadenas):
        todas = corridas_de(foto, wf.repo, wf.archivo)
        for c in todas:
            if not fallida(c):
                continue
            ins = inputs_de(c, wf)
            if es_manual(ins, wf):
                continue
            despues = [p for p in _posteriores(todas, c) if abarca(inputs_de(p, wf), ins, wf)]
            if any(exitosa(p) or fallida(p) for p in despues):
                continue
            vivas = [p for p in despues if viva(p)]
            # La racha: fallas seguidas del workflow desde su última corrida exitosa.
            racha = []
            for previa in todas:
                if (previa["created_at"], previa["id"]) > (c["created_at"], c["id"]):
                    break
                if exitosa(previa):
                    racha = []
                elif fallida(previa):
                    racha.append(previa)
            falla = falla_de(c)
            iguales = [r for r in racha if _misma_falla(falla_de(r), falla)]
            hallazgos.append(Hallazgo(
                invariante="I2", repo=wf.repo, workflow=wf.archivo,
                objeto=f"{wf.repo}/{wf.archivo}#{c['id']}",
                motivo=f"la corrida {c['id']} terminó {c['conclusion']}"
                       + (f" en «{falla['job']} / {falla['paso']}»" if falla and falla["paso"] else ""),
                inputs=ins, relanzable=relanzable(ins, wf), base=str(c["id"]),
                cuando=iso(fin(c)), corridas=[r["id"] for r in iguales] or [c["id"]],
                falla=falla, repeticiones=max(1, len(iguales)),
                en_vuelo=vivas[-1]["id"] if vivas else None,
            ))
    return hallazgos


# --------------------------------------------------------------------------
# I3 — Nada se pierde en la cola
# --------------------------------------------------------------------------

def i3(foto: dict, cadenas: Cadenas, ahora: datetime) -> list[Hallazgo]:
    hallazgos = []
    for wf in _vigilados(cadenas):
        todas = corridas_de(foto, wf.repo, wf.archivo)
        grupos: dict[tuple, list[dict]] = {}
        for c in todas:
            if not perdida(c):
                continue
            ins = inputs_de(c, wf)
            if es_manual(ins, wf):
                continue
            if any(cuenta_como_posterior(p) and abarca(inputs_de(p, wf), ins, wf)
                   for p in _posteriores(todas, c)):
                continue
            grupos.setdefault(_escalares(ins, wf), []).append(c)
        for perdidas in grupos.values():
            unidos = unir([inputs_de(c, wf) for c in perdidas], wf)
            ids = [c["id"] for c in perdidas]
            hallazgos.append(Hallazgo(
                invariante="I3", repo=wf.repo, workflow=wf.archivo,
                objeto=f"{wf.repo}/{wf.archivo}#{ids[-1]}",
                motivo=("cancelada en espera sin que otra corrida abarque sus inputs: "
                        + ", ".join(map(str, ids))),
                inputs=unidos, relanzable=relanzable(unidos, wf),
                base=",".join(map(str, ids)), cuando=iso(fin(perdidas[-1])), corridas=ids,
            ))
    return hallazgos


# --------------------------------------------------------------------------
# I4 — Nada se atora
# --------------------------------------------------------------------------

def i4(foto: dict, cadenas: Cadenas, ahora: datetime) -> list[Hallazgo]:
    hallazgos = []
    cola_max = timedelta(minutes=cadenas.politica.get("en_cola_max_min", 120))
    for wf in cadenas.workflows:
        todas = corridas_de(foto, wf.repo, wf.archivo)
        for c in todas:
            motivo = None
            ins = None if wf.solo_grupo else inputs_de(c, wf)
            del_grupo = corridas_del_grupo(foto, cadenas, wf.repo, wf.grupo_de(ins)) or todas
            if en_espera(c) and ahora - instante(c["created_at"]) > cola_max:
                # `queued` espera un runner; las demás esperan su turno en el grupo,
                # y eso solo es un atasco si no hay nada corriendo delante.
                if c["status"] == "queued" or not any(en_curso(o) for o in del_grupo):
                    motivo = f"la corrida {c['id']} lleva en espera desde {c['created_at']}"
            elif en_curso(c) and wf.max_min and ahora - inicio(c) > timedelta(minutes=wf.max_min):
                motivo = f"la corrida {c['id']} lleva en curso más de {wf.max_min} min"
            if not motivo:
                continue
            hallazgos.append(Hallazgo(
                invariante="I4", repo=wf.repo, workflow=wf.archivo,
                objeto=f"{wf.repo}/{wf.archivo}#{c['id']}", motivo=motivo,
                inputs=ins, relanzable=not es_manual(ins, wf) and relanzable(ins, wf),
                base=str(c["id"]), cuando=c["created_at"], corridas=[c["id"]],
            ))
    return hallazgos


# --------------------------------------------------------------------------
# I5 — Ningún PR automático queda abierto
# --------------------------------------------------------------------------

def regla_pr(cadenas: Cadenas, repo_datos: str, pr: dict) -> dict | None:
    for d in cadenas.datos:
        if d["repo"] != repo_datos:
            continue
        for regla in d.get("prs", []):
            if regla["etiqueta"] in pr.get("labels", []) and re.search(regla["rama"], pr.get("head_ref", "")):
                return regla
    return None


def i5(foto: dict, cadenas: Cadenas, ahora: datetime) -> list[Hallazgo]:
    hallazgos = []
    maximo = timedelta(minutes=cadenas.politica.get("pr_abierto_max_min", 90))
    for d in cadenas.datos:
        for pr in foto.get("datos", {}).get(d["repo"], {}).get("prs", []):
            regla = regla_pr(cadenas, d["repo"], pr)
            if regla is None or pr.get("cross"):
                continue
            if ahora - instante(pr["created_at"]) <= maximo:
                continue
            vig, rev = regla["vigilancia"], regla["revisor"]
            vivas = [c for c in corridas_de(foto, vig["repo"], vig["archivo"]) if viva(c)]
            vivas += [c for c in corridas_de(foto, rev["repo"], rev["archivo"]) if viva(c)]
            inputs_revisor = {"pr": str(pr["number"])}
            if "prefijo" in regla:
                inputs_revisor |= {"repo": f"{cadenas.org}/{d['repo']}", "prefijo": regla["prefijo"]}
            hallazgos.append(Hallazgo(
                invariante="I5", repo=d["repo"], workflow=None,
                objeto=f"{d['repo']}#{pr['number']}",
                motivo=f"el PR #{pr['number']} ({pr['head_ref']}) lleva abierto desde {pr['created_at']}",
                base=str(pr["number"]), cuando=pr["created_at"],
                en_vuelo=vivas[-1]["id"] if vivas else None,
                extra={"pr": pr["number"], "rama": pr["head_ref"], "revisor": rev,
                       "inputs_revisor": inputs_revisor, "vigilancia": vig},
            ))
    return hallazgos


# --------------------------------------------------------------------------
# I6 — Trabajo pendiente declarado
# --------------------------------------------------------------------------

def pendiente_de(c: dict) -> bool | None:
    """Lo que declaró el job final `estado`; None si no está en la foto."""
    for j in c.get("jobs", []):
        m = _ESTADO.match(j.get("name", ""))
        if m:
            return m.group(1) == "true"
    return None


def i6(foto: dict, cadenas: Cadenas, ahora: datetime) -> list[Hallazgo]:
    hallazgos = []
    for wf in _vigilados(cadenas):
        if not wf.encadenado:
            continue
        todas = corridas_de(foto, wf.repo, wf.archivo)
        buenas = [c for c in todas if exitosa(c)]
        if not buenas:
            continue
        ultima = buenas[-1]
        ins = inputs_de(ultima, wf)
        if pendiente_de(ultima) is not True or es_manual(ins, wf):
            continue
        if any(cuenta_como_posterior(p) and abarca(inputs_de(p, wf), ins, wf)
               for p in _posteriores(todas, ultima)):
            continue
        hallazgos.append(Hallazgo(
            invariante="I6", repo=wf.repo, workflow=wf.archivo,
            objeto=f"{wf.repo}/{wf.archivo}#{ultima['id']}",
            motivo=f"la corrida {ultima['id']} declaró trabajo pendiente y no hay otra después",
            inputs=ins, relanzable=relanzable(ins, wf), base=str(ultima["id"]),
            cuando=iso(fin(ultima)), corridas=[ultima["id"]],
        ))
    return hallazgos


# --------------------------------------------------------------------------
# I7 — Extractor nuevo ⇒ reintento
# --------------------------------------------------------------------------

def i7(foto: dict, cadenas: Cadenas, ahora: datetime) -> list[Hallazgo]:
    hallazgos = []
    for wf in _vigilados(cadenas):
        if not wf.extractor_nuevo:
            continue
        commit = foto.get("repos", {}).get(wf.repo, {}).get("extractor")
        if not commit:
            continue
        fecha = instante(commit["fecha"])
        todas = corridas_de(foto, wf.repo, wf.archivo)
        buenas = [c for c in todas if exitosa(c)]
        if not buenas or inicio(buenas[-1]) >= fecha:
            continue
        # Cualquier vuelta completa creada después del commit ya lo trae.
        if any(cuenta_como_posterior(c) and instante(c["created_at"]) >= fecha
               and (inputs_de(c, wf) is None or completa(inputs_de(c, wf), wf)) for c in todas):
            continue
        impactos = corridas_de(foto, wf.repo, "impacto.yml")
        if any(viva(c) for c in impactos):
            continue
        medido = any(c.get("status") == "completed" and instante(c["created_at"]) >= fecha - timedelta(minutes=2)
                     for c in impactos)
        if not medido and ahora - fecha < timedelta(hours=1):
            continue
        hallazgos.append(Hallazgo(
            invariante="I7", repo=wf.repo, workflow=wf.archivo,
            objeto=f"{wf.repo}/{wf.archivo}@{commit['sha'][:7]}",
            motivo=(f"el extractor cambió ({commit['sha'][:7]}, {commit['fecha']}) después del inicio de la "
                    f"última corrida exitosa ({buenas[-1]['id']})"),
            inputs={k: wf.valor_omision(k) for k in wf.inputs},
            base=commit["sha"], cuando=commit["fecha"],
        ))
    return hallazgos


# --------------------------------------------------------------------------
# I8 — La web está al día
# --------------------------------------------------------------------------

def i8(foto: dict, cadenas: Cadenas, ahora: datetime) -> list[Hallazgo]:
    web = cadenas.web
    if not web or not observable(foto, web["repo"]):
        return []
    despliegues = corridas_de(foto, web["repo"], web["archivo"])
    buenos = [c for c in despliegues if exitosa(c)]
    ultimo = buenos[-1] if buenos else foto["repos"][web["repo"]].get("ultima_exitosa", {}).get(web["archivo"])
    commits = [(instante(d["main"]["fecha"]), repo, d["main"]["sha"])
               for repo, d in foto.get("datos", {}).items() if d.get("main")]
    if not commits:
        return []
    fecha, repo_datos, sha = max(commits)
    if ultimo is not None and inicio(ultimo) >= fecha:
        return []
    if ahora < fecha + timedelta(minutes=cadenas.politica.get("web_margen_min", 15)):
        return []
    vivos = [c for c in despliegues if viva(c)]
    return [Hallazgo(
        invariante="I8", repo=web["repo"], workflow=web["archivo"], objeto=web["repo"],
        motivo=(f"main de {repo_datos} ({sha[:7]}, {iso(fecha)}) es posterior al último despliegue exitoso"
                + (f" ({ultimo['id']}, {iso(inicio(ultimo))})" if ultimo else "")),
        inputs={}, base=sha, cuando=iso(fecha), en_vuelo=vivos[-1]["id"] if vivos else None,
    )]


# --------------------------------------------------------------------------
# I9 — Los relojes viven · I10 — Los Workers avanzan
# --------------------------------------------------------------------------

def i9(foto: dict, cadenas: Cadenas, ahora: datetime) -> list[Hallazgo]:
    repo = cadenas.general["repo_supervisor"]
    if not observable(foto, repo):
        return []
    limite = ahora - timedelta(hours=cadenas.politica.get("relojes_max_h", 3))
    for c in corridas_de(foto, repo, "supervisor.yml"):
        leido = analizar_titulo(c.get("display_title"))
        if leido and leido[1].get("origen") == "scheduler" and instante(c["created_at"]) >= limite:
            return []
    return [Hallazgo(
        invariante="I9", repo=repo, workflow="supervisor.yml", objeto="scheduler-mudo",
        motivo="ninguna corrida de supervisor.yml lanzada por el Worker scheduler en las últimas horas",
        relanzable=False, base="scheduler-mudo",
    )]


def i10(foto: dict, cadenas: Cadenas, ahora: datetime) -> list[Hallazgo]:
    # Necesita GET /salud en el Worker congreso-archivo, que todavía no existe.
    return []


# --------------------------------------------------------------------------
# I11 — Rojo crónico
# --------------------------------------------------------------------------

def i11(foto: dict, cadenas: Cadenas, ahora: datetime) -> list[Hallazgo]:
    hallazgos = []
    minimo = cadenas.politica.get("cronico_corridas", 3)
    for repo in foto.get("repos", {}):
        por_archivo: dict[str, list[dict]] = {}
        for c in corridas_de(foto, repo):
            por_archivo.setdefault(archivo_de(c), []).append(c)
        for archivo, todas in sorted(por_archivo.items()):
            wf = cadenas.workflow(repo, archivo)
            if wf is not None and not wf.solo_grupo:
                continue  # I2 ya le da dueño a cada falla
            terminadas = [c for c in todas if c.get("status") == "completed"
                          and c.get("conclusion") in FALLAS | {"success"}]
            racha = []
            for c in reversed(terminadas):
                if exitosa(c):
                    break
                racha.append(c)
            if len(racha) < minimo:
                continue
            ultima = racha[0]
            hallazgos.append(Hallazgo(
                invariante="I11", repo=repo, workflow=archivo, objeto=f"{repo}/{archivo}",
                motivo=f"{len(racha)} corridas seguidas en rojo (la última: {ultima['id']})",
                relanzable=False, base=str(ultima["id"]), cuando=iso(fin(ultima)),
                corridas=[c["id"] for c in reversed(racha)], falla=falla_de(ultima),
                repeticiones=len(racha),
            ))
    return hallazgos


INVARIANTES = {"I1": i1, "I2": i2, "I3": i3, "I4": i4, "I5": i5, "I6": i6,
               "I7": i7, "I8": i8, "I9": i9, "I10": i10, "I11": i11}


def evaluar(foto: dict, cadenas: Cadenas, ahora: datetime) -> list[Hallazgo]:
    """Todos los invariantes encendidos, en orden."""
    hallazgos: list[Hallazgo] = []
    for nombre, funcion in INVARIANTES.items():
        if cadenas.encendido(nombre):
            hallazgos.extend(funcion(foto, cadenas, ahora))
    return hallazgos
