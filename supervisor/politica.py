"""Política: hallazgo + estado + ahora → acción.

Módulo puro. Decide; no ejecuta. Cada decisión trae el registro de su huella
(lo observado) y un `efecto` que solo se aplica si la acción se ejecutó.

Acciones: nada, esperar, lanzar, revisar-pr, cerrar-pr, cancelar-y-relanzar,
desplegar-web, agotada, agente, revertir.

Todo lo que termina en un dispatch pasa por `_lanzar`: lo que toca por horario
(I1), lo que pidió una corrida (I12), la tanda pendiente (I6), los reintentos
(I2, I3). Una petición no caduca mientras la corrida que la hizo siga en la
foto: espera su lugar en el grupo y se lanza cuando queda libre. Cuando esa
corrida sale de la ventana, la petición deja de verse y la retoma la vuelta
completa de la cadencia semanal.

Una huella determinista pasa por: abierta → en-reparacion (se lanzó
reparar-infra) → arreglo-publicado (se vigila su corrida de prueba) → resuelta,
o → revertido (y, si le quedan intentos, otra vez al agente). Lo que hace
reparar-infra llega por su bitácora en acciones.jsonl (EVENTOS), no por la foto.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import huella as _huella
from . import invariantes as inv
from .cadenas import Cadenas, Workflow
from .invariantes import Hallazgo, instante, iso

LANZAN = inv.LANZAN
EJECUTABLES = LANZAN | {"cerrar-pr", "agotada"}
# Lo que reparar-infra anota en acciones.jsonl y cambia el estado de una huella.
EVENTOS = {"arreglo-publicado", "descartado", "revertido", "reversion-fallida"}
EN_REPARACION = {"en-reparacion", "arreglo-publicado"}
_ORDEN = ["I4", "I5", "I3", "I12", "I2", "I6", "I7", "I1", "I8", "I11", "I9", "I10", "I13"]


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
    """datos | credencial | transitoria | determinista | desconocida, sin gastar nada."""
    if falla is None:
        return "desconocida"
    firma = falla.get("firma", "")
    for compuerta in (wf.compuertas if wf else ()):
        if compuerta["paso"] == falla.get("paso") and (
                "firma" not in compuerta or re.search(compuerta["firma"], firma, re.I)):
            return "datos"
    if any(re.search(p, firma, re.I) for p in cadenas.firmas.get("credenciales", [])):
        return "credencial"
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
    } | {k: vigente[k] for k in _DE_LA_REPARACION if k in vigente} | (
        # Aunque la huella se haya resuelto y vuelva: lo ya leído de la bitácora no se lee dos veces.
        {"evento_visto": previo["evento_visto"]} if previo and previo.get("evento_visto") else {})


# Lo que la reparación guarda en el registro de la huella, pase lo que pase con su corrida base.
_DE_LA_REPARACION = ("reparacion", "arreglo", "reversion", "intentos_extra", "sesiones", "bloqueo")


# --------------------------------------------------------------------------
# Reglas del único lanzador
# --------------------------------------------------------------------------

def _lanzamientos_24h(v: _Vuelta, repo: str, archivo: str) -> int:
    desde = v.ahora - timedelta(hours=24)
    # Lo lanzado por horario no cuenta: va una vez por cita, no puede ciclarse.
    previos = sum(1 for a in v.acciones
                  if a.get("accion") in LANZAN and not a.get("detalle", {}).get("horario")
                  and a.get("detalle", {}).get("repo") == repo
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


def _puerta(v: _Vuelta, wf: Workflow, inputs: dict | None, salvo: int | None = None,
            por_horario: bool = False) -> tuple[str, str]:
    """¿Se puede lanzar este workflow ahora? (lanzar | nada | esperar | ciclo, motivo)."""
    for repo, archivo, lanzados in v.lanzados:
        if (repo, archivo) == wf.clave and inv.abarca(lanzados, inputs, wf):
            return "nada", "lo abarca otro lanzamiento de esta vuelta"
    tope = v.cadenas.politica.get("max_lanzamientos_24h", 12)
    if not por_horario and _lanzamientos_24h(v, wf.repo, wf.archivo) >= tope:
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


def _ocupada_fuera_de_la_foto(v: _Vuelta, wf: Workflow) -> str | None:
    """Para quien declara `espera_libre`: la cadena a la que espera, ocupada por un lanzamiento que la
    foto todavía no trae (uno de esta misma vuelta, o uno reciente cuya corrida aún no aparece)."""
    for otro in wf.espera_libre:
        if any((repo, archivo) == (wf.repo, otro) for repo, archivo, _ in v.lanzados):
            return f"{otro} se lanza en esta misma vuelta"
        corridas = inv.corridas_de(v.foto, wf.repo, otro)
        for a in v.acciones:
            det = a.get("detalle") or {}
            if a.get("resultado") != "ok" or (det.get("repo"), det.get("workflow")) != (wf.repo, otro):
                continue
            lanzo = (det.get("relanzar", True) if a.get("accion") in LANZAN
                     else a.get("accion") == "arreglo-publicado" and (det.get("prueba") or {}).get("lanzada"))
            cuando = instante(a["cuando"])
            if lanzo and v.ahora - cuando < _gracia(v) and not any(instante(c["created_at"]) >= cuando for c in corridas):
                return f"{otro} se lanzó a las {a['cuando']} y su corrida todavía no aparece"
    return None


def _agotar(d: Decision, motivo: str) -> Decision:
    """Constancia pública, una sola vez por motivo."""
    if d.registro["estado"] == "agotada" and d.registro.get("motivo") == motivo:
        d.accion, d.motivo = "nada", f"agotada: {motivo}"
    else:
        d.accion, d.motivo = "agotada", motivo
        d.efecto = {"estado": "agotada", "motivo": motivo}
    return d


def fuera_de_alcance(cadenas: Cadenas, reg: dict) -> str | None:
    """Por qué el agente de infraestructura no puede tomar esta huella; None si puede."""
    if not cadenas.reparacion or cadenas.workflow_reparacion is None:
        return "no hay agente de infraestructura declarado"
    if cadenas.alcance(reg.get("repo")) is None:
        return f"{reg.get('repo')} está fuera del alcance del agente de infraestructura"
    return None


def _sin_agente(cadenas: Cadenas, reg: dict) -> str | None:
    """Por qué la política no lanza al agente para esta huella."""
    fuera = fuera_de_alcance(cadenas, reg)
    if fuera:
        return fuera
    if not cadenas.reparacion.get("activa", False):
        return "el agente de infraestructura está apagado ([reparacion].activa)"
    if not reg.get("workflow"):
        return "la huella no es la falla de un workflow"
    return None


def intentos_de(acciones: list, reg: dict, cadenas: Cadenas, ahora: datetime) -> int:
    """Intentos del agente gastados en una huella: cada sesión que empezó, y uno más por cada
    arreglo revertido por romper algo anterior."""
    desde = ahora - timedelta(days=cadenas.politica.get("intentos_agente_caducan_dias", 28))
    iniciados = sum(1 for a in acciones if a.get("accion") == "intento-iniciado"
                    and a.get("huella") == reg["huella"] and instante(a["cuando"]) >= desde)
    return iniciados + reg.get("intentos_extra", 0)


def _reparaciones_vivas(v: _Vuelta) -> list:
    wf = v.cadenas.workflow_reparacion
    return [c for c in inv.corridas_de(v.foto, wf.repo, wf.archivo) if inv.viva(c)]


def _lanzar_reparacion(v: _Vuelta, d: Decision, accion: str, inputs: dict, motivo: str, efecto: dict,
                       sola: bool) -> Decision:
    """Dispatch de reparar-infra con la regla del único lanzador. `sola`: no convive con otra corrida viva."""
    wf = v.cadenas.workflow_reparacion
    reciente = [a for a in v.acciones if a.get("accion") in ("agente", "revertir") and a.get("resultado") == "ok"
                and v.ahora - instante(a["cuando"]) < _gracia(v)]
    if sola and (_reparaciones_vivas(v) or reciente or (wf.repo, wf.grupo) in v.grupos):
        d.accion, d.motivo = "esperar", "ya hay una reparación de infraestructura en curso"
        return d
    paso, por_que = _puerta(v, wf, inputs)
    if paso == "ciclo":
        _ciclo(v, wf, por_que)
        paso = "esperar"
    if paso != "lanzar":
        d.accion, d.motivo = "esperar", por_que
        return d
    v.lanzados.append((wf.repo, wf.archivo, inputs))
    v.grupos.add((wf.repo, wf.grupo))
    d.accion, d.motivo = accion, motivo
    d.detalle = {"repo": wf.repo, "workflow": wf.archivo, "inputs": inputs, "origen": True}
    d.efecto = efecto | {"ultima_accion": iso(v.ahora)}
    return d


def _agente(v: _Vuelta, d: Decision, motivo: str) -> Decision:
    """Lanza reparar-infra para la huella, con sus topes."""
    reg, pol = d.registro, v.cadenas.politica
    fuera = _sin_agente(v.cadenas, reg)
    if fuera:
        return _agotar(d, fuera)
    if reg.get("bloqueo"):
        # Mientras la huella siga apareciendo: otra corrida fallida no le da otra sesión.
        return _agotar(d, reg["bloqueo"])
    reg["intentos_agente"] = intentos_de(v.acciones, reg, v.cadenas, v.ahora)
    if reg["intentos_agente"] >= pol.get("intentos_agente_por_huella", 2):
        return _agotar(d, "se agotaron los intentos del agente")
    tope = pol.get("max_lanzamientos_por_corrida", 3)
    if reg.get("sesiones", 0) - reg["intentos_agente"] >= tope:
        return _agotar(d, f"reparar-infra se lanzó {tope} veces sin llegar a empezar su intento")
    if reg["errores"] >= tope:
        return _agotar(d, "no se pudo lanzar reparar-infra")
    if reg["ultima_accion"] and v.ahora - instante(reg["ultima_accion"]) < _gracia(v):
        d.accion, d.motivo = "esperar", "se espera el resultado de la última acción sobre la huella"
        d.proxima = iso(instante(reg["ultima_accion"]) + _gracia(v))
        return d
    en_prueba = next((o for o in v.huellas.values() if o.get("huella") != reg["huella"]
                      and o.get("estado") in EN_REPARACION
                      and (o.get("repo"), o.get("workflow")) == (reg["repo"], reg["workflow"])), None)
    if en_prueba:
        d.accion, d.motivo = "esperar", f"ese workflow tiene un arreglo en curso (huella {en_prueba['huella']})"
        return d
    desde = v.ahora - timedelta(hours=24)
    hoy = sum(1 for a in v.acciones if a.get("accion") == "agente" and a.get("resultado") == "ok"
              and instante(a["cuando"]) >= desde)
    if hoy + v.agentes >= pol.get("sesiones_agente_por_dia", 3):
        d.accion, d.motivo = "esperar", "tope diario de sesiones de agente"
        return d
    _lanzar_reparacion(v, d, "agente", {"huella": reg["huella"]}, motivo, {
        "estado": "en-reparacion", "motivo": motivo, "reparacion": {"lanzada": iso(v.ahora)}, "+sesiones": 1}, sola=True)
    if d.accion == "agente":
        v.agentes += 1
    return d


# --------------------------------------------------------------------------
# La reparación en curso: lo que anotó reparar-infra y la corrida de prueba
# --------------------------------------------------------------------------

def _asimilar(v: _Vuelta, d: Decision) -> bool:
    """Pasa al registro lo que reparar-infra anotó desde la última vuelta. True si cambió su estado."""
    reg = d.registro
    visto = reg.get("evento_visto") or ""
    nuevos = [a for a in v.acciones if a.get("huella") == reg["huella"] and a.get("accion") in EVENTOS
              and a.get("resultado", "ok") == "ok" and a["cuando"] > visto]
    if not nuevos:
        return False
    ev = max(nuevos, key=lambda a: a["cuando"])
    det = ev.get("detalle") or {}
    reg["evento_visto"] = ev["cuando"]
    reg["intentos_agente"] = intentos_de(v.acciones, reg, v.cadenas, v.ahora)
    d.accion = "nada"
    if reg["estado"] == "resuelta" and ev["accion"] != "arreglo-publicado":
        # La falla desapareció antes de que reparar-infra terminara: solo queda constancia de que se leyó.
        d.motivo = f"resuelta; reparar-infra terminó después con `{ev['accion']}`"
        return True
    if ev["accion"] == "arreglo-publicado":
        prueba = det.get("prueba") or {}
        vence = prueba.get("vence") or iso(instante(ev["cuando"]) + timedelta(
            hours=v.cadenas.reparacion.get("prueba_max_h", 12)))
        reg |= {"estado": "arreglo-publicado", "reversion": None, "motivo": None, "resuelta": None, "arreglo": {
            "commit": det.get("commit"), "repo": det.get("repo"), "archivos": det.get("archivos") or [],
            "cuando": ev["cuando"], "vence": vence, "pruebas": 1 if prueba.get("lanzada") else 0,
            "prueba_lanzada": ev["cuando"] if prueba.get("lanzada") else None}}
        d.motivo = f"arreglo publicado ({str(det.get('commit'))[:12]}): se vigila su corrida de prueba"
    elif ev["accion"] == "descartado":
        reg |= {"estado": "abierta", "reparacion": None, "motivo": f"intento descartado: {det.get('motivo')}"}
        d.motivo = reg["motivo"]
        if det.get("definitivo"):
            reg["bloqueo"] = f"el agente la devolvió: {det.get('motivo')}"
            _agotar(d, reg["bloqueo"])
    elif ev["accion"] == "revertido":
        doble = bool((reg.get("reversion") or {}).get("doble"))
        reg |= {"estado": "revertido", "arreglo": None, "reversion": None, "reparacion": None,
                "intentos_extra": reg.get("intentos_extra", 0) + (1 if doble else 0),
                "motivo": f"arreglo {str(det.get('commit'))[:12]} revertido"}
        reg["intentos_agente"] = intentos_de(v.acciones, reg, v.cadenas, v.ahora)
        d.motivo = reg["motivo"]
    else:
        reg |= {"reversion": None, "arreglo": None,
                "bloqueo": f"no se pudo revertir {str(det.get('commit'))[:12]}: {det.get('motivo')}"}
        _agotar(d, reg["bloqueo"])
    return True


def _toco(arreglo: dict, workflow: str, falla: dict) -> bool:
    """¿La falla está en algo que el commit del arreglo tocó?"""
    archivos = arreglo.get("archivos") or []
    if f".github/workflows/{workflow}" in archivos:
        return True
    return any(a.rsplit("/", 1)[-1] in (falla.get("firma") or "") for a in archivos)


def _juzgar_prueba(c: dict, reg: dict, wf: Workflow | None, cadenas: Cadenas) -> str | None:
    """pasa | misma | antes | arranque, o None si esta corrida todavía no dice nada del paso de la huella."""
    if c.get("conclusion") == "startup_failure":
        return "arranque"
    if "jobs" not in c:
        return None
    if not reg.get("paso"):
        # La huella era que el workflow no arrancaba: basta con que ahora corra algún paso.
        return "pasa" if inv.ejecuto_algo(c) else None
    veredictos = set()
    for j in c["jobs"]:
        if _huella.normalizar_job(j.get("name", "")) != reg.get("job"):
            continue
        paso = next((s for s in j.get("steps") or [] if s.get("name") == reg["paso"]), None)
        if paso is None:
            continue
        if paso.get("conclusion") == "success":
            veredictos.add("pasa")
        elif paso.get("conclusion") in ("failure", "timed_out"):
            firma = _huella.firma(j.get("log") or []) or j.get("conclusion")
            veredictos.add("misma" if firma == reg.get("firma") else "otra")
    for veredicto in ("misma", "otra", "pasa"):
        if veredicto in veredictos:
            return None if veredicto == "otra" else veredicto
    if inv.fallida(c):
        falla = inv.falla_de(c)
        if falla and clasificar(falla, wf, cadenas) != "datos" and _toco(reg["arreglo"], reg["workflow"], falla):
            return "antes"
    return None


def _pedir_reversion(v: _Vuelta, d: Decision, motivo: str, doble: bool) -> Decision:
    reg = d.registro
    veces = (reg.get("reversion") or {}).get("veces", 0)
    if veces >= v.cadenas.politica.get("max_lanzamientos_por_corrida", 3):
        # El arreglo sigue en main: mientras tanto la huella no vuelve al agente.
        reg["bloqueo"] = f"no se pudo revertir {str(reg['arreglo']['commit'])[:12]}: {veces} intentos sin resultado"
        return _agotar(d, reg["bloqueo"])
    return _lanzar_reparacion(
        v, d, "revertir", {"huella": reg["huella"], "revertir": reg["arreglo"]["commit"]}, motivo,
        {"reversion": {"pedida": iso(v.ahora), "motivo": motivo, "doble": doble, "veces": veces + 1}}, sola=False)


def _vigilar(v: _Vuelta, d: Decision) -> Decision:
    """Con un arreglo publicado: ¿pasó el paso de la huella en una corrida posterior al commit?"""
    reg, ahora, rep = d.registro, v.ahora, v.cadenas.reparacion
    arreglo, h = reg["arreglo"], d.hallazgo
    wf = v.cadenas.workflow(reg["repo"], reg["workflow"])
    publicado = instante(arreglo["cuando"])
    # Posteriores al arreglo: creadas después, o sobre su mismo commit (por si los relojes no coinciden).
    pruebas = [c for c in inv.corridas_de(v.foto, reg["repo"], reg["workflow"])
               if instante(c["created_at"]) >= publicado or c.get("head_sha") == arreglo.get("commit")]
    for c in pruebas:
        veredicto = _juzgar_prueba(c, reg, wf, v.cadenas)
        if veredicto == "pasa":
            d.accion, d.motivo = "nada", f"resuelta: la corrida {c['id']} pasó el paso tras el arreglo"
            reg |= {"estado": "resuelta", "resuelta": iso(ahora), "reversion": None}
            d.detalle = {"issue": reg["issue"]} if reg.get("issue") else {}
            return d
        if veredicto == "misma":
            return _pedir_reversion(v, d, f"la corrida {c['id']} falló en el mismo paso con la misma firma", False)
        if veredicto == "antes":
            return _pedir_reversion(v, d, f"la corrida {c['id']} falló antes del paso, en algo que el arreglo tocó", True)
        if veredicto == "arranque":
            return _pedir_reversion(v, d, f"la corrida {c['id']} ya no arranca (startup_failure)", True)
    # Otro workflow que el commit tocó y que dejó de arrancar.
    for archivo in arreglo.get("archivos") or []:
        if archivo.startswith(".github/workflows/") and archivo.rsplit("/", 1)[-1] != reg["workflow"]:
            rota = next((c for c in inv.corridas_de(v.foto, reg["repo"], archivo.rsplit("/", 1)[-1])
                         if instante(c["created_at"]) >= publicado and c.get("conclusion") == "startup_failure"), None)
            if rota:
                return _pedir_reversion(v, d, f"{archivo} ya no arranca (corrida {rota['id']})", True)
    vivas = [c for c in pruebas if inv.viva(c)]
    vence = instante(arreglo["vence"])
    if ahora >= vence:
        # Una corrida de prueba que sigue viva dentro de su tiempo todavía puede dar el resultado.
        a_tiempo = [c for c in vivas if wf is None or not wf.max_min
                    or ahora - inv.inicio(c) <= timedelta(minutes=wf.max_min)]
        if not a_tiempo:
            return _pedir_reversion(v, d, f"sin resultado en {rep.get('prueba_max_h', 12)} h: ninguna corrida "
                                          f"posterior al arreglo pasó el paso", False)
    d.accion, d.motivo, d.proxima = "esperar", "se vigila la corrida de prueba del arreglo", arreglo["vence"]
    # Si nadie lanzó la prueba (el grupo estaba ocupado al publicar), se lanza aquí.
    if not pruebas and h is not None and wf is not None and not wf.solo_grupo and h.relanzable and not h.en_vuelo:
        ultimo = instante(arreglo.get("prueba_lanzada") or arreglo["cuando"])
        if ahora - ultimo >= _gracia(v) and arreglo.get("pruebas", 0) < v.cadenas.politica.get("max_lanzamientos_por_corrida", 3):
            paso, por_que = _puerta(v, wf, h.inputs)
            if paso == "lanzar":
                _anotar_lanzamiento(v, wf, h.inputs)
                d.accion, d.motivo = "lanzar", "corrida de prueba del arreglo publicado"
                d.detalle = _detalle_lanzar(wf, h.inputs)
                d.efecto = {"arreglo": arreglo | {"prueba_lanzada": iso(ahora), "pruebas": arreglo.get("pruebas", 0) + 1}}
            elif paso == "ciclo":
                _ciclo(v, wf, por_que)
    return d


def _reparacion(v: _Vuelta, d: Decision) -> Decision:
    """Una huella con reparar-infra lanzado o con un arreglo publicado."""
    reg, ahora = d.registro, v.ahora
    espera = timedelta(hours=v.cadenas.reparacion.get("sesion_max_h", 4))
    if reg["estado"] == "en-reparacion":
        lanzada = instante((reg.get("reparacion") or {}).get("lanzada") or reg.get("ultima_accion") or iso(ahora))
        if ahora - lanzada < espera or _reparaciones_vivas(v):
            d.accion, d.motivo, d.proxima = "esperar", "reparar-infra está trabajando en la huella", iso(lanzada + espera)
        else:
            d.accion, d.motivo = "nada", "reparar-infra no dejó resultado"
            reg |= {"estado": "abierta", "reparacion": None, "motivo": d.motivo}
        return d
    pendiente = reg.get("reversion")
    if pendiente:
        pedida = instante(pendiente["pedida"])
        if ahora - pedida < espera or _reparaciones_vivas(v):
            d.accion, d.motivo, d.proxima = "esperar", f"se pidió revertir el arreglo: {pendiente['motivo']}", iso(pedida + espera)
            return d
        return _pedir_reversion(v, d, pendiente["motivo"], bool(pendiente.get("doble")))
    return _vigilar(v, d)


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
    if h.extra.get("pr_abierto"):
        repo_datos, numero = h.extra["pr_abierto"]
        d.accion = "esperar"
        d.motivo = f"el PR #{numero} de {repo_datos} sigue abierto: se lanza cuando se fusione (o cuando I5 lo cierre)"
        return d
    ocupada = h.extra.get("ocupada") or (_ocupada_fuera_de_la_foto(v, wf) if h.extra.get("horario") else None)
    if ocupada:
        d.accion, d.motivo = "esperar", f"se lanza cuando la cadena quede libre: {ocupada}"
        return d
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
    por_horario = bool(h.extra.get("horario"))
    paso, motivo = _puerta(v, wf, h.inputs, por_horario=por_horario)
    if paso == "ciclo":
        _ciclo(v, wf, motivo)
        d.accion, d.motivo = "esperar", motivo
        return d
    if paso != "lanzar":
        d.accion, d.motivo = paso, motivo
        return d
    _anotar_lanzamiento(v, wf, h.inputs)
    d.accion, d.motivo = accion, h.motivo
    d.detalle = _detalle_lanzar(wf, h.inputs) | ({"horario": h.extra["horario"]} if por_horario else {})
    d.efecto = {"+lanzamientos": 1, "ultima_accion": iso(ahora)}
    return d


def _falla(v: _Vuelta, d: Decision, wf: Workflow | None) -> Decision:
    """I2: según la clase y cuántas veces seguidas se ha visto la huella."""
    h = d.hallazgo
    if d.clase == "datos":
        d.accion, d.motivo = "nada", "falla de una compuerta de datos: la llevan los agentes de las vigilancias"
        return d
    if d.clase == "credencial":
        if h.en_vuelo:
            d.accion, d.motivo = "nada", f"la corrida {h.en_vuelo} está en curso o en espera"
            return d
        # Ni reintento ni agente: la renueva una persona. Cada corrida que vuelve a fallar lo recuerda en su issue.
        corrida = next((c for c in inv.corridas_de(v.foto, h.repo, h.workflow) if str(c["id"]) == h.base), {})
        return _agotar(d, inv.error_de(corrida) or "una credencial vencida o por vencer: la renueva una persona")
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


def _credencial(v: _Vuelta, d: Decision) -> Decision:
    """I13: constancia en un issue y un recordatorio cada tantos días. No lanza nada ni va al agente."""
    reg, pol = d.registro, v.cadenas.politica
    motivo = "la cambia una persona: el supervisor solo deja constancia"
    if reg["ultima_accion"]:
        # Un aviso que no se pudo escribir se reintenta antes que el recordatorio, sin insistir en cada vuelta.
        pausa = (timedelta(days=pol.get("aviso_repeticion_dias", 7)) if reg["estado"] == "agotada"
                 else timedelta(hours=6))
        listo = instante(reg["ultima_accion"]) + pausa
        if v.ahora < listo:
            d.accion, d.motivo, d.proxima = "nada", f"agotada: {motivo}", iso(listo)
            return d
    d.accion, d.motivo = "agotada", motivo
    d.efecto = {"estado": "agotada", "motivo": motivo, "ultima_accion": iso(v.ahora)}
    return d


def _se_ve(foto: dict, reg: dict) -> bool:
    """¿La foto alcanza a ver el repo de esta huella? Lo que no se ve no se da por resuelto."""
    repo = reg.get("repo")
    if reg.get("invariante") == "I13":
        _, _, worker = (reg.get("objeto") or "").partition("@")
        if worker:
            return bool((foto.get("workers", {}).get(worker) or {}).get("observable"))
        # Solo una vuelta con el secret, y a la que la API le contestó, sabe cómo está el token.
        leido = (foto.get("credenciales") or {}).get("github") or {}
        return bool(leido.get("observado") and leido.get("propio"))
    if reg.get("invariante") == "I5":
        return repo in foto.get("datos", {})
    if reg.get("invariante") == "I10":
        worker = (reg.get("objeto") or "").split("/")[0]
        return bool((foto.get("workers", {}).get(worker) or {}).get("observable"))
    return inv.observable(foto, repo)


# --------------------------------------------------------------------------
# Entrada
# --------------------------------------------------------------------------

def _espera_a_otra(h: Hallazgo, cadenas: Cadenas) -> bool:
    wf = cadenas.workflow(h.repo, h.workflow) if h.workflow else None
    return bool(h.invariante == "I1" and h.extra.get("horario") and wf is not None and wf.espera_libre)


def _tratar(v: _Vuelta, h: Hallazgo, decisiones: list[Decision]) -> Decision:
    """La decisión de un hallazgo, sabiendo lo que ya se decidió en esta vuelta."""
    cadenas, huellas, ahora = v.cadenas, v.huellas, v.ahora
    wf = cadenas.workflow(h.repo, h.workflow) if h.workflow else None
    ident = identificar(h)
    clase = clasificar(h.falla, wf, cadenas) if h.invariante in ("I2", "I11") else None
    if h.invariante == "I10":
        clase = "determinista"
    elif h.invariante == "I13":
        clase = "credencial"
    elif h.extra.get("rechazada"):
        clase = "rechazada"
    d = Decision(huella=ident, accion="nada", motivo="", hallazgo=h, clase=clase)
    if ident in v.tratadas:
        d.accion, d.motivo = "esperar", f"la misma huella ya se trata en {v.tratadas[ident]}"
        return d
    v.tratadas[ident] = h.objeto
    d.registro = _registro(h, ident, clase, huellas.get(ident), ahora)
    if _asimilar(v, d):
        pass  # cambió de estado por lo que anotó reparar-infra: se actúa en la vuelta siguiente
    elif d.registro["estado"] in EN_REPARACION:
        d.clase = d.registro["clase"] = huellas[ident].get("clase") or clase
        _reparacion(v, d)
    elif h.invariante == "I2":
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
        # Tampoco insiste si falló por una credencial por renovar: relanzarlo no la renueva.
        # Lo que toca por horario no espera a nadie: cada cita se lanza a su hora.
        dueno = None if h.extra.get("horario") else next((
            o for o in decisiones if o.hallazgo is not None and (o.accion == "esperar" or o.clase == "credencial")
            and o.hallazgo.invariante in ("I2", "I3", "I6", "I7", "I12")
            and (o.hallazgo.repo, o.hallazgo.workflow) == (h.repo, h.workflow)
            and (o.hallazgo.inputs is None or inv.abarca(o.hallazgo.inputs, h.inputs, wf))), None)
        if dueno is not None and not h.en_vuelo:
            d.accion, d.motivo, d.proxima = "esperar", f"lo lleva la huella {dueno.huella}", dueno.proxima
        else:
            _lanzar(v, d, wf, 0, repeticion_h=cadenas.politica.get("espera_cadencia_h", [1, 6, 24]))
    elif h.invariante == "I9":
        _agotar(d, h.motivo)
    elif h.invariante == "I10":
        # El motivo de la constancia no lleva las cuentas del sitio, que cambian en cada vuelta.
        _agotar(d, "el sitio no avanza en el Worker: es un defecto del Worker, no la falla de una corrida, "
                   "y el agente de infraestructura no lo toma")
    elif h.invariante == "I13":
        _credencial(v, d)
    elif h.extra.get("rechazada"):
        _agotar(d, h.extra["rechazada"])
    else:
        _lanzar(v, d, wf, 0)
    return d


def decidir(hallazgos: list[Hallazgo], huellas: dict, acciones: list, foto: dict,
            cadenas: Cadenas, ahora: datetime) -> list[Decision]:
    """Una decisión por hallazgo, más las que salen del estado (resueltas, pendientes, ciclos)."""
    v = _Vuelta(foto=foto, cadenas=cadenas, ahora=ahora, huellas=huellas, acciones=acciones)
    decisiones: list[Decision] = []
    orden = {nombre: n for n, nombre in enumerate(_ORDEN)}
    ordenados = sorted(hallazgos, key=lambda h: (orden.get(h.invariante, 99), h.repo, h.objeto))
    # Quien espera a que otra cadena quede libre se decide al final, cuando ya se sabe todo lo que esta
    # vuelta lanza: así no depende del orden en que salgan los hallazgos.
    al_final = [h for h in ordenados if _espera_a_otra(h, cadenas)]
    aplazadas = {identificar(h) for h in al_final}
    for h in ordenados:
        if identificar(h) not in aplazadas:
            decisiones.append(_tratar(v, h, decisiones))

    for ident, reg in huellas.items():
        if ident in v.tratadas or ident in aplazadas or reg.get("invariante") == "ciclo" or not _se_ve(foto, reg):
            continue
        sin_leer = any(a.get("huella") == ident and a.get("accion") in EVENTOS
                       and a["cuando"] > (reg.get("evento_visto") or "") for a in acciones)
        if reg.get("estado") == "resuelta" and not sin_leer:
            continue
        if sin_leer or reg.get("estado") == "arreglo-publicado":
            # Aunque la falla ya no aparezca: un arreglo en main se vigila hasta saber si sirvió.
            d = Decision(huella=ident, accion="nada", motivo="", clase=reg.get("clase"), registro=dict(reg))
            if not _asimilar(v, d) and d.registro["estado"] == "arreglo-publicado":
                _reparacion(v, d)
            decisiones.append(d)
        elif reg.get("estado") == "relanzar-pendiente":
            decisiones.append(_relanzar_pendiente(v, reg))
        else:
            decisiones.append(Decision(
                huella=ident, accion="nada", motivo="resuelta: ya no aparece",
                registro=dict(reg) | {"estado": "resuelta", "resuelta": iso(ahora)},
                detalle={"issue": reg.get("issue")} if reg.get("issue") else {}))

    for h in al_final:
        decisiones.append(_tratar(v, h, decisiones))

    decisiones.extend(v.ciclos.values())
    for ident, reg in huellas.items():
        if reg.get("invariante") == "ciclo" and reg.get("estado") != "resuelta" and ident not in v.ciclos:
            decisiones.append(Decision(
                huella=ident, accion="nada", motivo="resuelta: el cortacircuitos ya no está abierto",
                registro=dict(reg) | {"estado": "resuelta", "resuelta": iso(ahora)},
                detalle={"issue": reg.get("issue")} if reg.get("issue") else {}))
    return decisiones
