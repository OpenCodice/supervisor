"""Lee cadenas.toml: lo declarado sobre cada workflow vigilado."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
POR_OMISION = RAIZ / "cadenas.toml"


@dataclass(frozen=True)
class Workflow:
    repo: str
    archivo: str
    grupo: str | None = None
    grupos: tuple[dict, ...] = ()
    cancela_en_curso: bool = False
    inputs: tuple[str, ...] = ()
    inputs_lista: tuple[str, ...] = ()
    inputs_uno: tuple[str, ...] = ()
    inputs_ignorados: tuple[str, ...] = ()
    inputs_manuales: tuple[str, ...] = ()
    omision: dict = field(default_factory=dict)
    relanzar_sin_inputs: bool = False
    encadenado: bool = False
    solo_grupo: bool = False
    cadencias: tuple[dict, ...] = ()
    horario: tuple[dict, ...] = ()
    lanza_horario: bool = False
    max_min: int | None = None
    compuertas: tuple[dict, ...] = ()
    extractor_nuevo: tuple[str, ...] = ()
    titulos_previos: tuple[dict, ...] = ()
    puede_pedir: tuple[str, ...] = ()
    espera_libre: tuple[str, ...] = ()

    @property
    def clave(self) -> tuple[str, str]:
        return (self.repo, self.archivo)

    @property
    def nombre(self) -> str:
        return self.archivo.removesuffix(".yml")

    def grupo_de(self, inputs: dict | None) -> str | None:
        """Grupo de concurrency de una corrida con esos inputs (el general si no se conocen)."""
        for caso in self.grupos:
            if inputs is not None and all(inputs.get(k, "") == str(v) for k, v in caso["inputs"].items()):
                return caso["grupo"]
        return self.grupo

    def valor_omision(self, k: str) -> str:
        return str(self.omision.get(k, ""))


@dataclass(frozen=True)
class Cadenas:
    general: dict
    invariantes: dict
    politica: dict
    firmas: dict
    workflows: tuple[Workflow, ...]
    datos: tuple[dict, ...]
    repos: tuple[str, ...]
    web: dict
    reparacion: dict = field(default_factory=dict)
    workers: tuple[dict, ...] = ()
    credenciales: tuple[dict, ...] = ()

    @property
    def org(self) -> str:
        return self.general["org"]

    def workflow(self, repo: str, archivo: str) -> Workflow | None:
        for w in self.workflows:
            if w.repo == repo and w.archivo == archivo:
                return w
        return None

    def pedible(self, origen: Workflow, repo: str, archivo: str) -> bool:
        """¿`origen` tiene permiso de pedir ese workflow? (`archivo.yml` en su repo, `repo/archivo.yml` en otro)."""
        return (archivo if repo == origen.repo else f"{repo}/{archivo}") in origen.puede_pedir

    def repos_con_workflows(self) -> list[str]:
        vistos = list(self.repos)
        for w in self.workflows:
            if w.repo not in vistos:
                vistos.append(w.repo)
        return vistos

    def encendido(self, invariante: str) -> bool:
        return bool(self.invariantes.get(invariante, False))

    def alcance(self, repo: str | None) -> dict | None:
        """Lo que el agente de infraestructura puede tocar en ese repo; None = fuera de su alcance."""
        for r in self.reparacion.get("repos", []):
            if r["nombre"] == repo:
                return r
        return None

    def credencial(self, nombre: str) -> dict | None:
        return next((c for c in self.credenciales if c["nombre"] == nombre), None)

    @property
    def credencial_github(self) -> dict | None:
        """La credencial con la que el supervisor habla con la API de GitHub."""
        return next((c for c in self.credenciales if c.get("uso") == "github"), None)

    @property
    def workflow_reparacion(self) -> Workflow | None:
        archivo = self.reparacion.get("workflow")
        return self.workflow(self.general["repo_supervisor"], archivo) if archivo else None


_LISTAS = ("grupos", "inputs", "inputs_lista", "inputs_uno", "inputs_ignorados", "inputs_manuales",
           "cadencias", "horario", "compuertas", "extractor_nuevo", "titulos_previos", "puede_pedir", "espera_libre")
DIAS = {"lun": 0, "mar": 1, "mie": 2, "jue": 3, "vie": 4, "sab": 5, "dom": 6}
_HORA = re.compile(r"^(\*|[01]\d|2[0-3]):[0-5]\d$")


def cargar(ruta: str | Path | None = None) -> Cadenas:
    with open(ruta or POR_OMISION, "rb") as f:
        crudo = tomllib.load(f)
    workflows = []
    for w in crudo.get("workflows", []):
        w = dict(w)
        for k in _LISTAS:
            if k in w:
                w[k] = tuple(w[k])
        workflows.append(Workflow(**w))
    cadenas = Cadenas(
        general=crudo["general"],
        invariantes=crudo.get("invariantes", {}),
        politica=crudo.get("politica", {}),
        firmas=crudo.get("firmas", {}),
        workflows=tuple(workflows),
        datos=tuple(crudo.get("datos", [])),
        repos=tuple(r["nombre"] for r in crudo.get("repos", [])),
        web=crudo.get("web", {}),
        reparacion=crudo.get("reparacion", {}),
        workers=tuple(crudo.get("workers", [])),
        credenciales=tuple(crudo.get("credenciales", [])),
    )
    validar(cadenas)
    return cadenas


def validar(c: Cadenas) -> None:
    """Falla con un mensaje claro si lo declarado se contradice."""
    vistos = set()
    for w in c.workflows:
        if w.clave in vistos:
            raise ValueError(f"cadenas.toml: {w.repo}/{w.archivo} está dos veces")
        vistos.add(w.clave)
        declarados = set(w.inputs)
        for nombre in ("inputs_lista", "inputs_uno", "inputs_ignorados", "inputs_manuales"):
            sobran = set(getattr(w, nombre)) - declarados
            if sobran:
                raise ValueError(f"cadenas.toml: {w.archivo}: {nombre} nombra inputs no declarados: {sorted(sobran)}")
        for cad in w.cadencias:
            sobran = set(cad.get("inputs", {})) - declarados
            if sobran:
                raise ValueError(f"cadenas.toml: {w.archivo}: una cadencia usa inputs no declarados: {sorted(sobran)}")
        for cita in w.horario:
            # Los días van por nombre: un número significa otra cosa en cada cron (en Cloudflare el 1 es domingo).
            malos = [d for d in cita.get("dias", ["*"]) if d != "*" and d not in DIAS]
            if malos:
                raise ValueError(f"cadenas.toml: {w.archivo}: días de horario desconocidos: {malos} (se escriben {sorted(DIAS)})")
            if not _HORA.match(str(cita.get("hora", ""))):
                raise ValueError(f"cadenas.toml: {w.archivo}: hora de horario inválida: {cita.get('hora')!r}")
            if "dia_mes" in cita and not (isinstance(cita["dia_mes"], int) and 1 <= cita["dia_mes"] <= 28):
                raise ValueError(f"cadenas.toml: {w.archivo}: dia_mes va de 1 a 28")
            sobran = set(cita.get("inputs", {})) - declarados
            if sobran:
                raise ValueError(f"cadenas.toml: {w.archivo}: un horario usa inputs no declarados: {sorted(sobran)}")
        for otro in w.espera_libre:
            if c.workflow(w.repo, otro) is None:
                raise ValueError(f"cadenas.toml: {w.archivo}: espera_libre nombra `{otro}`, que no está declarado en {w.repo}")
        for pedido in w.puede_pedir:
            repo, _, archivo = pedido.rpartition("/")
            destino = c.workflow(repo or w.repo, archivo)
            if destino is None or destino.solo_grupo:
                raise ValueError(f"cadenas.toml: {w.archivo}: puede_pedir nombra `{pedido}`, que no es un workflow "
                                 f"vigilado que el supervisor lance")
        for comp in w.compuertas:
            if "paso" not in comp:
                raise ValueError(f"cadenas.toml: {w.archivo}: una compuerta no dice su paso")
            if "firma" in comp:
                re.compile(comp["firma"])
        for t in w.titulos_previos:
            re.compile(t["patron"])
    for lista in ("transitorias", "deterministas", "credenciales"):
        for patron in c.firmas.get(lista, []):
            re.compile(patron)
    for patron in c.reparacion.get("pasos_protegidos", []):
        re.compile(patron)
    if c.reparacion:
        if c.workflow_reparacion is None:
            raise ValueError("cadenas.toml: [reparacion] nombra un workflow que no está declarado en este repo")
        if c.reparacion.get("scripts_con_poder", "lista") not in ("lista", "cerrado"):
            raise ValueError("cadenas.toml: [reparacion].scripts_con_poder es `lista` o `cerrado`")
        for r in c.reparacion.get("repos", []):
            if r["nombre"] == c.general["repo_supervisor"]:
                raise ValueError("cadenas.toml: el supervisor no se repara a sí mismo")
            if any(r["nombre"] == d["repo"] for d in c.datos):
                raise ValueError(f"cadenas.toml: {r['nombre']} es un repo de datos: el agente no lo toca")
    for worker in c.workers:
        for clave in ("nombre", "salud", "token_env"):
            if not worker.get(clave):
                raise ValueError(f"cadenas.toml: un Worker no declara `{clave}`")
        if not str(worker["salud"]).startswith("https://"):
            raise ValueError(f"cadenas.toml: {worker['nombre']}: `salud` tiene que ser una URL https")
    for cred in c.credenciales:
        for clave in ("nombre", "que", "necesita", "guardar"):
            if not cred.get(clave):
                raise ValueError(f"cadenas.toml: una credencial no declara `{clave}`")
        if c.credencial(cred["nombre"]) is not cred:
            raise ValueError(f"cadenas.toml: la credencial {cred['nombre']} está dos veces")
    if sum(1 for cred in c.credenciales if cred.get("uso") == "github") > 1:
        raise ValueError("cadenas.toml: solo una credencial puede ser la de GitHub (`uso = \"github\"`)")
    for d in c.datos:
        for pr in d.get("prs", []):
            re.compile(pr["rama"])
            for lado in ("vigilancia", "revisor"):
                if lado not in pr:
                    raise ValueError(f"cadenas.toml: un PR de {d['repo']} no declara {lado}")
