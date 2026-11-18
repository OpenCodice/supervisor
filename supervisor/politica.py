"""Política: hallazgo + estado + ahora → acción.

Módulo puro. Decide; no ejecuta. Cada decisión trae el registro de su huella
(lo observado) y un `efecto` que solo se aplica si la acción se ejecutó.

Acciones: nada, esperar, lanzar, revisar-pr, cerrar-pr, cancelar-y-relanzar,
desplegar-web, agotada, agente.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import huella as _huella
from . import invariantes as inv
from .cadenas import Cadenas, Workflow
from .invariantes import Hallazgo, instante, iso

# Acciones que crean una corrida en el workflow de su detalle.
LANZAN = {"lanzar", "cancelar-y-relanzar", "desplegar-web", "revisar-pr"}
EJECUTABLES = LANZAN | {"cerrar-pr", "agotada", "agente"}
_ORDEN = ["I4", "I5", "I3", "I2", "I6", "I7", "I1", "I8", "I11", "I9", "I10"]


@dataclass
class Decision:
    huella: str
    accion: str
    motivo: str
    hallazgo: Hallazgo | None = None
    clase: str | None = None
    detalle: dict = field(default_factory=dict)
    proxima: str | None = None
    registro: dict | None = None
    efecto: dict = field(default_factory=dict)

    def como_dict(self) -> dict:
        return {
            "huella": self.huella, "accion": self.accion, "motivo": self.motivo, "clase": self.clase,
            "invariante": self.hallazgo.invariante if self.hallazgo else (self.registro or {}).get("invariante"),
            "objeto": self.hallazgo.objeto if self.hallazgo else (self.registro or {}).get("objeto"),
            "detalle": self.detalle, "proxima": self.proxima,
        }


@dataclass
class _Vuelta:
    """Lo que ya se decidió en esta vuelta: dos hallazgos no lanzan lo mismo."""
    foto: dict
    cadenas: Cadenas
    ahora: datetime
    huellas: dict
    acciones: list
    lanzados: list = field(default_factory=list)
    grupos: set = field(default_factory=set)
    agentes: int = 0
    tratadas: dict = field(default_factory=dict)
    ciclos: dict = field(default_factory=dict)


def clasificar(falla: dict | None, wf: Workflow | None, cadenas: Cadenas) -> str:
    """datos | transitoria | determinista | desconocida, sin gastar nada."""
    if falla is None:
        return "desconocida"
    firma = falla.get("firma", "")
    for compuerta in (wf.compuertas if wf else ()):
        if compuerta["paso"] == falla.get("paso") and (
                "firma" not in compuerta or re.search(compuerta["firma"], firma, re.I)):
            return "datos"
    if any(re.search(p, firma, re.I) for p in cadenas.firmas.get("transitorias", [])):
        return "transitoria"
    if any(re.search(p, firma, re.I) for p in cadenas.firmas.get("deterministas", [])):
        return "determinista"
    return "desconocida"


def identificar(h: Hallazgo) -> str:
    if h.falla is not None:
        return _huella.de_falla(h.repo, h.workflow or "", h.falla["job"], h.falla["paso"], h.falla["firma"])
    return _huella.de_objeto(h.invariante, h.objeto)


def aplicar(registro: dict, efecto: dict) -> dict:
    """Registro con el efecto encima; las claves `+k` suman."""
    nuevo = dict(registro)
    for k, v in efecto.items():
        if k.startswith("+"):
            nuevo[k[1:]] = nuevo.get(k[1:], 0) + v
        else:
            nuevo[k] = v
    return nuevo


def _registro(h: Hallazgo, ident: str, clase: str | None, previo: dict | None, ahora: datetime) -> dict:
    vigente = previo if previo and previo.get("estado") != "resuelta" else {}
    misma_base = vigente.get("base") == h.base
    estado = vigente.get("estado", "abierta")
    if estado == "agotada" and not misma_base:
        estado = "abierta"
    falla = h.falla or {}
    return {
        "huella": ident, "invariante": h.invariante, "clase": clase,
        "repo": h.repo, "workflow": h.workflow,
        "job": falla.get("job"), "paso": falla.get("paso"), "firma": falla.get("firma"),
        "objeto": h.objeto,
        "primera": vigente.get("primera") or h.cuando or iso(ahora),
        "ultima": h.cuando or vigente.get("ultima") or iso(ahora),
        "corridas": sorted(set(vigente.get("corridas", [])) | set(h.corridas)),
        "base": h.base,
        "lanzamientos": vigente.get("lanzamientos", 0) if misma_base else 0,
        "errores": vigente.get("errores", 0) if misma_base else 0,
        "intentos_agente": vigente.get("intentos_agente", 0),
        "ultima_accion": vigente.get("ultima_accion") if misma_base else None,
        "etapa": vigente.get("etapa") if misma_base else None,
        "motivo": vigente.get("motivo") if estado == vigente.get("estado") else None,
        "issue": vigente.get("issue"),
        "estado": "datos" if clase == "datos" else ("abierta" if estado == "datos" else estado),
    }


# --------------------------------------------------------------------------
# Reglas del único lanzador
# --------------------------------------------------------------------------

def _lanzamientos_24h(v: _Vuelta, repo: str, archivo: str) -> int:
    desde = v.ahora - timedelta(hours=24)
    previos = sum(1 for a in v.acciones
                  if a.get("accion") in LANZAN and a.get("detalle", {}).get("repo") == repo
                  and a.get("detalle", {}).get("workflow") == archivo and instante(a["cuando"]) >= desde)
    return previos + sum(1 for r, a, _ in v.lanzados if (r, a) == (repo, archivo))


def _grupo_en_espera(v: _Vuelta, wf: Workflow, inputs: dict | None, salvo: int | None = None) -> int | str | None:
    """Corrida que ya ocupa el único lugar de espera del grupo (o un lanzamiento de esta vuelta)."""
    grupo = wf.grupo_de(inputs)
    if not grupo:
        return None
    if (wf.repo, grupo) in v.grupos:
        return "un lanzamiento de esta vuelta"
    for c in inv.corridas_del_grupo(v.foto, v.cadenas, wf.repo, grupo):
        if inv.en_espera(c) and c["id"] != salvo:
            return c["id"]
    return None


def _puerta(v: _Vuelta, wf: Workflow, inputs: dict | None, salvo: int | None = None) -> tuple[str, str]:
    """¿Se puede lanzar este workflow ahora? (lanzar | nada | esperar | ciclo, motivo)."""
    for repo, archivo, lanzados in v.lanzados:
        if (repo, archivo) == wf.clave and inv.abarca(lanzados, inputs, wf):
            return "nada", "lo abarca otro lanzamiento de esta vuelta"
    tope = v.cadenas.politica.get("max_lanzamientos_24h", 12)
    if _lanzamientos_24h(v, wf.repo, wf.archivo) >= tope:
        return "ciclo", f"cortacircuitos: {tope} lanzamientos de {wf.archivo} en 24 h"
    ocupa = _grupo_en_espera(v, wf, inputs, salvo)
    if ocupa is not None:
        return "esperar", f"el grupo {wf.grupo_de(inputs)} ya tiene en espera a {ocupa}"
    return "lanzar", ""


def _anotar_lanzamiento(v: _Vuelta, wf: Workflow, inputs: dict | None) -> None:
    # Sin inputs se lanza la vuelta completa, que abarca cualquier otra.
    if inputs is None:
        inputs = {k: wf.valor_omision(k) for k in wf.inputs}
    v.lanzados.append((wf.repo, wf.archivo, inputs))
    if wf.grupo_de(inputs):
        v.grupos.add((wf.repo, wf.grupo_de(inputs)))


def _detalle_lanzar(wf: Workflow, inputs: dict | None) -> dict:
    return {"repo": wf.repo, "workflow": wf.archivo, "inputs": inv.para_lanzar(inputs, wf),
            "origen": "origen" in wf.inputs}


# --------------------------------------------------------------------------
# Caminos
# --------------------------------------------------------------------------

def _gracia(v: _Vuelta) -> timedelta:
    return timedelta(minutes=v.cadenas.politica.get("gracia_lanzamiento_min", 30))


def _agotar(d: Decision, motivo: str) -> Decision:
    """Constancia pública, una sola vez por motivo."""
    if d.registro["estado"] == "agotada" and d.registro.get("motivo") == motivo:
        d.accion, d.motivo = "nada", f"agotada: {motivo}"
    else:
        d.accion, d.motivo = "agotada", motivo
        d.efecto = {"estado": "agotada", "motivo": motivo}
    return d


def _agente(v: _Vuelta, d: Decision, motivo: str) -> Decision:
    reg = d.registro
    if reg["estado"] == "pendiente-agente":
        d.accion, d.motivo = "nada", "ya anotada como pendiente-agente"
    elif reg["intentos_agente"] >= v.cadenas.politica.get("intentos_agente_por_huella", 2):
        _agotar(d, "se agotaron los intentos del agente")
    else:
        desde = v.ahora - timedelta(hours=24)
        hoy = sum(1 for a in v.acciones if a.get("accion") == "agente" and instante(a["cuando"]) >= desde)
        if hoy + v.agentes >= v.cadenas.politica.get("sesiones_agente_por_dia", 3):
            d.accion, d.motivo = "esperar", "tope diario de sesiones de agente"
        else:
            v.agentes += 1
            d.accion, d.motivo = "agente", motivo
            d.efecto = {"estado": "pendiente-agente", "motivo": motivo}
    return d


def _ciclo(v: _Vuelta, wf: Workflow, motivo: str) -> None:
    ident = _huella.de_objeto("ciclo", f"{wf.repo}/{wf.archivo}")
    previo = v.huellas.get(ident)
    vigente = previo if previo and previo.get("estado") != "resuelta" else None
    registro = vigente or {
        "huella": ident, "invariante": "ciclo", "clase": None, "repo": wf.repo, "workflow": wf.archivo,
        "objeto": f"{wf.repo}/{wf.archivo}", "primera": iso(v.ahora), "ultima": iso(v.ahora),
        "corridas": [], "estado": "abierta", "motivo": None, "issue": None,
    }
    d = Decision(huella=ident, accion="nada", motivo=motivo, registro=registro)
    v.ciclos[ident] = _agotar(d, motivo)


def _lanzar(v: _Vuelta, d: Decision, wf: Workflow | None, espera_h: float, accion: str = "lanzar",
            repeticion_h: list | None = None) -> Decision:
    """Camino común de todo lo que termina en un dispatch."""
    h, reg, ahora = d.hallazgo, d.registro, v.ahora
    if h.en_vuelo:
        d.accion, d.motivo = "nada", f"la corrida {h.en_vuelo} está en curso o en espera"
        return d
    if wf is None or not h.relanzable:
        return _agotar(d, "no se puede relanzar: sus inputs no se conocen y el workflow no admite relanzarse sin ellos")
    if h.cuando:
        listo = instante(h.cuando) + timedelta(hours=espera_h)
        if ahora < listo:
            d.accion, d.motivo, d.proxima = "esperar", f"reintento tras {espera_h:g} h", iso(listo)
            return d
    if reg["ultima_accion"]:
        desde = ahora - instante(reg["ultima_accion"])
        hechos = reg["lanzamientos"] + reg["errores"]
        if repeticion_h:
            pausa = timedelta(hours=repeticion_h[min(max(hechos, 1), len(repeticion_h)) - 1])
        else:
            pausa = max(_gracia(v), timedelta(hours=1)) if desde >= _gracia(v) else _gracia(v)
            if desde >= _gracia(v) and hechos >= v.cadenas.politica.get("max_lanzamientos_por_corrida", 3):
                return _agotar(d, f"{hechos} lanzamientos sin que aparezca una corrida nueva")
        if desde < pausa:
            d.accion, d.motivo = "esperar", "ya se lanzó; se espera a que aparezca o termine la corrida"
            d.proxima = iso(instante(reg["ultima_accion"]) + pausa)
            return d
    paso, motivo = _puerta(v, wf, h.inputs)
    if paso == "ciclo":
        _ciclo(v, wf, motivo)
        d.accion, d.motivo = "esperar", motivo
        return d
    if paso != "lanzar":
        d.accion, d.motivo = paso, motivo
        return d
    _anotar_lanzamiento(v, wf, h.inputs)
    d.accion, d.motivo = accion, h.motivo
    d.detalle = _detalle_lanzar(wf, h.inputs)
    d.efecto = {"+lanzamientos": 1, "ultima_accion": iso(ahora)}
    return d


def _falla(v: _Vuelta, d: Decision, wf: Workflow | None) -> Decision:
    """I2: según la clase y cuántas veces seguidas se ha visto la huella."""
    h = d.hallazgo
    if d.clase == "datos":
        d.accion, d.motivo = "nada", "falla de una compuerta de datos: la llevan los agentes de las vigilancias"
        return d
    esperas = v.cadenas.politica.get(f"espera_{d.clase}_h", [1])
    if h.repeticiones > len(esperas):
        if not h.en_vuelo:
            d.clase = d.registro["clase"] = "determinista"
            return _agente(v, d, f"la misma huella se repitió {h.repeticiones} veces")
    return _lanzar(v, d, wf, esperas[min(h.repeticiones, len(esperas)) - 1])


def _cronico(v: _Vuelta, d: Decision) -> Decision:
    if d.clase == "datos":
        d.accion, d.motivo = "nada", "falla de una compuerta de datos"
        return d
    d.clase = d.registro["clase"] = "determinista"
    return _agente(v, d, d.hallazgo.motivo)


def _pr(v: _Vuelta, d: Decision) -> Decision:
    """I5: primero revisar-pr; si el PR sigue abierto, cerrarlo y relanzar su vigilancia."""
    h, reg, ahora = d.hallazgo, d.registro, v.ahora
    if h.en_vuelo:
        d.accion, d.motivo = "nada", f"la corrida {h.en_vuelo} todavía puede fusionarlo"
        return d
    if reg["ultima_accion"] and ahora - instante(reg["ultima_accion"]) < _gracia(v):
        d.accion, d.motivo = "esperar", "se espera el resultado de la última acción sobre el PR"
        d.proxima = iso(instante(reg["ultima_accion"]) + _gracia(v))
        return d
    comun = {"repo_datos": h.repo, "pr": h.extra["pr"], "rama": h.extra["rama"]}
    if reg["etapa"] is None:
        rev = h.extra["revisor"]
        d.accion, d.motivo = "revisar-pr", h.motivo
        d.detalle = comun | {"repo": rev["repo"], "workflow": rev["archivo"],
                             "inputs": h.extra["inputs_revisor"], "origen": False}
        d.efecto = {"etapa": "revisado", "ultima_accion": iso(ahora)}
        return d
    if reg["errores"] >= v.cadenas.politica.get("max_lanzamientos_por_corrida", 3):
        return _agotar(d, "no se pudo cerrar el PR")
    d.accion, d.motivo = "cerrar-pr", "sigue abierto después de revisar-pr: se cierra sin publicar nada"
    d.detalle = comun
    d.efecto = {"etapa": "cerrado", "estado": "relanzar-pendiente", "ultima_accion": iso(ahora),
                "vigilancia": h.extra["vigilancia"]}
    return d


def _atorada(v: _Vuelta, d: Decision, wf: Workflow) -> Decision:
    """I4: cancelar y, si el grupo queda libre, relanzar con los mismos inputs."""
    h, reg, ahora = d.hallazgo, d.registro, v.ahora
    corrida = int(h.base)
    if reg["ultima_accion"]:
        if ahora - instante(reg["ultima_accion"]) < _gracia(v):
            d.accion, d.motivo = "esperar", "ya se pidió cancelarla"
            return d
        if reg["lanzamientos"] + reg["errores"] >= v.cadenas.politica.get("max_lanzamientos_por_corrida", 3):
            return _agotar(d, "la corrida no se deja cancelar")
    relanzar = False
    if h.relanzable:
        paso, motivo = _puerta(v, wf, h.inputs, salvo=corrida)
        if paso == "ciclo":
            _ciclo(v, wf, motivo)
        relanzar = paso == "lanzar"
    if relanzar:
        _anotar_lanzamiento(v, wf, h.inputs)
    d.accion, d.motivo = "cancelar-y-relanzar", h.motivo
    d.detalle = _detalle_lanzar(wf, h.inputs) | {"corrida": corrida, "relanzar": relanzar}
    d.efecto = {"+lanzamientos": 1, "ultima_accion": iso(ahora)}
    return d


def _relanzar_pendiente(v: _Vuelta, reg: dict) -> Decision:
    """Tras cerrar un PR atorado queda debiéndose la vigilancia que lo abrió."""
    vig = reg["vigilancia"]
    wf = v.cadenas.workflow(vig["repo"], vig["archivo"])
    d = Decision(huella=reg["huella"], accion="nada", motivo="", registro=dict(reg))
    cerrado = instante(reg["ultima_accion"])
    despues = [c for c in inv.corridas_de(v.foto, vig["repo"], vig["archivo"])
               if instante(c["created_at"]) >= cerrado and inv.cuenta_como_posterior(c)]
    if wf is None or despues:
        d.motivo = "resuelta: la vigilancia ya volvió a correr" if despues else "resuelta"
        d.registro |= {"estado": "resuelta", "resuelta": iso(v.ahora)}
        return d
    inputs = {k: str(vig.get("inputs", {}).get(k, wf.valor_omision(k))) for k in wf.inputs}
    paso, motivo = _puerta(v, wf, inputs)
    if paso == "ciclo":
        _ciclo(v, wf, motivo)
        paso = "esperar"
    if paso != "lanzar":
        d.accion, d.motivo = ("esperar" if paso == "esperar" else "nada"), motivo
        if paso == "nada":
            d.registro |= {"estado": "resuelta", "resuelta": iso(v.ahora)}
        return d
    _anotar_lanzamiento(v, wf, inputs)
    d.accion, d.motivo = "lanzar", "relanzar la vigilancia tras cerrar su PR atorado"
    d.detalle = _detalle_lanzar(wf, inputs)
    d.efecto = {"estado": "resuelta", "resuelta": iso(v.ahora)}
    return d


def _se_ve(foto: dict, reg: dict) -> bool:
    """¿La foto alcanza a ver el repo de esta huella? Lo que no se ve no se da por resuelto."""
    repo = reg.get("repo")
    if reg.get("invariante") == "I5":
        return repo in foto.get("datos", {})
    return inv.observable(foto, repo)


# --------------------------------------------------------------------------
# Entrada
# --------------------------------------------------------------------------

def decidir(hallazgos: list[Hallazgo], huellas: dict, acciones: list, foto: dict,
            cadenas: Cadenas, ahora: datetime) -> list[Decision]:
    """Una decisión por hallazgo, más las que salen del estado (resueltas, pendientes, ciclos)."""
    v = _Vuelta(foto=foto, cadenas=cadenas, ahora=ahora, huellas=huellas, acciones=acciones)
    decisiones: list[Decision] = []
    orden = {nombre: n for n, nombre in enumerate(_ORDEN)}
    for h in sorted(hallazgos, key=lambda h: (orden.get(h.invariante, 99), h.repo, h.objeto)):
        wf = cadenas.workflow(h.repo, h.workflow) if h.workflow else None
        ident = identificar(h)
        clase = clasificar(h.falla, wf, cadenas) if h.invariante in ("I2", "I11") else None
        d = Decision(huella=ident, accion="nada", motivo="", hallazgo=h, clase=clase)
        if ident in v.tratadas:
            d.accion, d.motivo = "esperar", f"la misma huella ya se trata en {v.tratadas[ident]}"
            decisiones.append(d)
            continue
        v.tratadas[ident] = h.objeto
        d.registro = _registro(h, ident, clase, huellas.get(ident), ahora)
        if h.invariante == "I2":
            _falla(v, d, wf)
        elif h.invariante == "I11":
            _cronico(v, d)
        elif h.invariante == "I5":
            _pr(v, d)
        elif h.invariante == "I4":
            _atorada(v, d, wf)
        elif h.invariante == "I8":
            _lanzar(v, d, wf, 0, accion="desplegar-web")
        elif h.invariante == "I1":
            # Una falla del mismo workflow que espera su reintento manda: la cadencia no se salta esa espera.
            dueno = next((o for o in decisiones if o.hallazgo is not None and o.accion == "esperar"
                          and o.hallazgo.invariante in ("I2", "I3", "I6", "I7")
                          and (o.hallazgo.repo, o.hallazgo.workflow) == (h.repo, h.workflow)
                          and (o.hallazgo.inputs is None or inv.abarca(o.hallazgo.inputs, h.inputs, wf))), None)
            if dueno is not None and not h.en_vuelo:
                d.accion, d.motivo, d.proxima = "esperar", f"lo lleva la huella {dueno.huella}", dueno.proxima
            else:
                _lanzar(v, d, wf, 0, repeticion_h=cadenas.politica.get("espera_cadencia_h", [1, 6, 24]))
        elif h.invariante in ("I9", "I10"):
            _agotar(d, h.motivo)
        else:
            _lanzar(v, d, wf, 0)
        decisiones.append(d)

    for ident, reg in huellas.items():
        if ident in v.tratadas or reg.get("estado") == "resuelta" or reg.get("invariante") == "ciclo":
            continue
        if not _se_ve(foto, reg):
            continue
        if reg.get("estado") == "relanzar-pendiente":
            decisiones.append(_relanzar_pendiente(v, reg))
        else:
            decisiones.append(Decision(
                huella=ident, accion="nada", motivo="resuelta: ya no aparece",
                registro=dict(reg) | {"estado": "resuelta", "resuelta": iso(ahora)},
                detalle={"issue": reg.get("issue")} if reg.get("issue") else {}))

    decisiones.extend(v.ciclos.values())
    for ident, reg in huellas.items():
        if reg.get("invariante") == "ciclo" and reg.get("estado") != "resuelta" and ident not in v.ciclos:
            decisiones.append(Decision(
                huella=ident, accion="nada", motivo="resuelta: el cortacircuitos ya no está abierto",
                registro=dict(reg) | {"estado": "resuelta", "resuelta": iso(ahora)},
                detalle={"issue": reg.get("issue")} if reg.get("issue") else {}))
    return decisiones
