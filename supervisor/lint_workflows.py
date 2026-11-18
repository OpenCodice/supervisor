#!/usr/bin/env python3
"""Lint de los workflows de un repo: lo que actionlint no mira.

    python lint_workflows.py <raíz del repo> [--hermanos <dir>] [--repo DUEÑO/NOMBRE] [--lanzador]

Por cada job:
- rutas: calcula qué directorios existen (cada `actions/checkout` con su
  `path`, `repository` y `sparse-checkout`; cada `git clone … <destino>`) y
  exige que todo script invocado por ruta caiga en uno de ellos y exista en ese
  repo. Solo opina de los repos que tiene en disco: el propio y, con
  `--hermanos`, los que estén en esa carpeta.
- un workflow no lanza: ni los workflows ni los scripts de `scripts/` contienen
  `gh workflow run`, `gh run rerun`, un `dispatches` de la API (con `gh api` o
  `curl`) ni las llamadas equivalentes de `actions/github-script`. Lo que tiene
  que correr después se pide (salida `pedir*`) y lo lanza el supervisor. Exento
  solo el lanzador: el repo que se llama `supervisor`, o `--lanzador`.
- peticiones: cada petición escrita en un `run:` (JSON literal o el objeto de un
  `jq -n`) nombra un workflow que existe, admite `workflow_dispatch` y declara
  esos inputs, y no trae `origen` (lo pone el supervisor). Un workflow con
  salidas `pedir*` termina en un job `estado` que depende de todos los demás,
  corre siempre y sube el artefacto `cadena`. En el lanzador, lo mismo vale
  para cada `gh workflow run X -f k=v`.
- needs: cada `needs.J.outputs.K` y `needs.J.result` nombra un job que está en
  `needs` y un output declarado.
- tiempos: todo job con `runs-on` declara `timeout-minutes`, y ningún paso
  tiene un límite mayor que el de su job.

Sale con 1 si hay hallazgos, uno por línea: `archivo:línea: mensaje`.
Es independiente del resto del paquete: otros repos lo corren desde un clon.
"""

from __future__ import annotations

import argparse
import fnmatch
import itertools
import os
import posixpath
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("lint_workflows.py necesita PyYAML: pip install pyyaml")

EXTENSIONES = (".sh", ".py", ".mjs", ".js", ".cjs")
TEMPORAL = "/home/runner/work/_temp"


# --------------------------------------------------------------------------
# YAML con números de línea
# --------------------------------------------------------------------------

class Texto(str):
    """Escalar con la línea donde empieza; en un bloque (`|`), su contenido empieza una después."""
    linea: int
    bloque: bool

    def linea_de(self, posicion: int) -> int:
        return self.linea + (1 if self.bloque else 0) + self.count("\n", 0, posicion)


class Mapa(dict):
    linea: int
    lineas: dict


class Lista(list):
    linea: int


def _construir(nodo):
    if isinstance(nodo, yaml.ScalarNode):
        texto = Texto(nodo.value)
        texto.linea = nodo.start_mark.line + 1
        texto.bloque = nodo.style in ("|", ">")
        return texto
    if isinstance(nodo, yaml.SequenceNode):
        lista = Lista(_construir(n) for n in nodo.value)
        lista.linea = nodo.start_mark.line + 1
        return lista
    mapa = Mapa()
    mapa.linea = nodo.start_mark.line + 1
    mapa.lineas = {}
    for clave, valor in nodo.value:
        # La clave se guarda como texto: `on` no debe volverse True.
        mapa[str(clave.value)] = _construir(valor)
        mapa.lineas[str(clave.value)] = clave.start_mark.line + 1
    return mapa


def cargar_yaml(ruta: Path):
    with open(ruta, encoding="utf-8") as f:
        nodo = yaml.compose(f, Loader=yaml.SafeLoader)
    return _construir(nodo) if nodo is not None else Mapa()


def _mapa(valor) -> dict:
    return valor if isinstance(valor, dict) else {}


def _lista(valor) -> list:
    if isinstance(valor, list):
        return valor
    return [] if valor is None or isinstance(valor, dict) else [valor]


def _textos(valor):
    """Todos los escalares de un árbol."""
    if isinstance(valor, str):
        yield valor
    elif isinstance(valor, dict):
        for v in valor.values():
            yield from _textos(v)
    elif isinstance(valor, list):
        for v in valor:
            yield from _textos(v)


def _entero(valor) -> int | None:
    try:
        return int(str(valor))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Repos en disco
# --------------------------------------------------------------------------

@dataclass
class Repos:
    raiz: Path
    propio: str
    hermanos: Path | None = None

    def en_disco(self, slug: str | None) -> Path | None:
        if not slug:
            return None
        if slug.lower() == self.propio.lower():
            return self.raiz
        if self.hermanos is not None:
            candidato = self.hermanos / slug.split("/")[-1]
            if candidato.is_dir() and candidato.resolve() != self.raiz.resolve():
                return candidato
        return None

    @property
    def espacio(self) -> str:
        nombre = self.propio.split("/")[-1]
        return f"/home/runner/work/{nombre}/{nombre}"


def slug_de(raiz: Path) -> str:
    try:
        url = subprocess.run(["git", "-C", str(raiz), "config", "--get", "remote.origin.url"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        url = ""
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$", url)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return os.environ.get("GITHUB_REPOSITORY") or f"_/{raiz.resolve().name}"


# --------------------------------------------------------------------------
# Expansión de variables (solo lo que se puede saber sin ejecutar)
# --------------------------------------------------------------------------

_EXPRESION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:-|##|#)([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def expandir(texto: str, variables: dict, repos: Repos) -> str | None:
    """El texto con sus variables resueltas, o None si alguna no se conoce."""
    desconocida = False

    def expresion(m):
        nonlocal desconocida
        e = m.group(1)
        if e == "github.workspace":
            return repos.espacio
        if e == "github.repository":
            return repos.propio
        if e == "runner.temp":
            return TEMPORAL
        if e.startswith("env.") and e[4:] in variables:
            return variables[e[4:]]
        desconocida = True
        return ""

    def variable(m):
        nonlocal desconocida
        nombre = m.group(1) or m.group(4)
        valor = variables.get(nombre)
        operador, argumento = m.group(2), m.group(3)
        if operador == ":-" and valor is None:
            valor = expandir(argumento, variables, repos)
        if valor is None:
            desconocida = True
            return ""
        if operador == "#" and argumento == "*/":
            return valor.split("/", 1)[-1]
        if operador == "##" and argumento == "*/":
            return valor.rsplit("/", 1)[-1]
        if operador in ("#", "##"):
            desconocida = True
        return valor

    texto = _EXPRESION.sub(expresion, texto)
    texto = _VARIABLE.sub(variable, texto)
    if desconocida or "$(" in texto or "`" in texto or "$" in texto:
        return None
    return texto


def _sin_comillas(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token.replace('"', "").replace("'", "")


def _prefijo_fijo(texto: str) -> str:
    """Lo que se sabe de una ruta antes de su primera variable, hasta la última barra."""
    corte = re.search(r"\$|`", texto)
    fijo = texto[:corte.start()] if corte else texto
    return fijo[:fijo.rfind("/") + 1]


def _variables_base(repos: Repos) -> dict:
    return {"GITHUB_WORKSPACE": repos.espacio, "GITHUB_REPOSITORY": repos.propio,
            "RUNNER_TEMP": TEMPORAL, "HOME": "/home/runner",
            "GITHUB_REPOSITORY_OWNER": repos.propio.split("/")[0]}


def _con_env(variables: dict, env, repos: Repos) -> dict:
    variables = dict(variables)
    for k, v in _mapa(env).items():
        valor = expandir(str(v), variables, repos)
        if valor is None:
            variables.pop(k, None)
        else:
            variables[k] = valor
    return variables


# --------------------------------------------------------------------------
# Lectura de un `run:` (renglones lógicos, sin heredocs ni comentarios)
# --------------------------------------------------------------------------

_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def renglones(run: Texto):
    """(línea del archivo, texto) de cada renglón lógico de un script."""
    fin_heredoc, pendiente, linea_inicial = None, "", None
    for n, crudo in enumerate(str(run).split("\n")):
        linea = run.linea + (1 if run.bloque else 0) + n
        if fin_heredoc is not None:
            if crudo.strip() == fin_heredoc:
                fin_heredoc = None
            continue
        if not pendiente and crudo.lstrip().startswith("#"):
            continue
        if linea_inicial is None:
            linea_inicial = linea
        if crudo.rstrip().endswith("\\"):
            pendiente += crudo.rstrip()[:-1] + " "
            continue
        texto, pendiente = pendiente + crudo, ""
        m = _HEREDOC.search(texto)
        if m:
            fin_heredoc = m.group(2)
        yield linea_inicial, texto
        linea_inicial = None
    if pendiente:
        yield linea_inicial, pendiente


def _palabras(texto: str) -> list[str]:
    """Las palabras de un comando, hasta el primer separador de comandos."""
    texto = re.split(r"&&|\|\||[;|)]|\s#", texto, maxsplit=1)[0]
    try:
        return shlex.split(texto, posix=False)
    except ValueError:
        return texto.split()


# --------------------------------------------------------------------------
# Directorios de un job
# --------------------------------------------------------------------------

@dataclass
class Directorio:
    ruta: str
    repo: str | None
    linea: int
    # None = árbol completo; lista = patrones de sparse-checkout; "?" = contenido que no se puede saber.
    parcial: object = None
    cono: bool = True


@dataclass
class Job:
    directorios: list = field(default_factory=list)
    comodines: list = field(default_factory=list)
    creados: set = field(default_factory=set)

    def de(self, ruta: str) -> Directorio | None:
        mejor = None
        for d in self.directorios:
            if ruta == d.ruta or ruta.startswith(d.ruta.rstrip("/") + "/"):
                if mejor is None or len(d.ruta) >= len(mejor.ruta):
                    mejor = d
        return mejor

    def creado(self, ruta: str) -> bool:
        return any(ruta == c or ruta.startswith(c.rstrip("/") + "/") for c in self.creados)

    def comodin(self, ruta: str) -> bool:
        return any(ruta.startswith(c) for c in self.comodines)


def _absoluta(ruta: str, cwd: str | None) -> str | None:
    if ruta.startswith("~/"):
        ruta = "/home/runner/" + ruta[2:]
    if not ruta.startswith("/"):
        if cwd is None:
            return None
        ruta = posixpath.join(cwd, ruta)
    return posixpath.normpath(ruta)


_GITHUB = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
_CLONE_CON_VALOR = {"--depth", "-b", "--branch", "--origin", "-o", "-c", "--config", "--reference",
                    "--shallow-since", "--jobs", "-j", "--template", "--separate-git-dir", "--filter"}


def _clonar(job: Job, argumentos: str, variables: dict, cwd: str | None, repos: Repos, linea: int) -> None:
    posicionales, parcial = [], None
    palabras = iter(_palabras(argumentos))
    for p in palabras:
        if p in ("--sparse", "--no-checkout", "-n", "--bare", "--mirror"):
            parcial = "?"
        elif p in _CLONE_CON_VALOR:
            next(palabras, None)
        elif not p.startswith("-"):
            posicionales.append(_sin_comillas(p))
    if not posicionales:
        return
    url = expandir(posicionales[0], variables, repos)
    m = _GITHUB.search(url or "")
    repo = f"{m.group(1)}/{m.group(2)}" if m else None
    if len(posicionales) > 1:
        destino = expandir(posicionales[1], variables, repos)
        if destino is None:
            base = _absoluta(_prefijo_fijo(posicionales[1]) or ".", cwd)
            if base is not None:
                job.comodines.append(base.rstrip("/") + "/")
            return
    elif url is not None:
        destino = re.sub(r"\.git$", "", url.rstrip("/").rsplit("/", 1)[-1])
    else:
        if cwd is not None:
            job.comodines.append(cwd.rstrip("/") + "/")
        return
    ruta = _absoluta(destino, cwd)
    if ruta is None:
        return
    job.directorios.append(Directorio(ruta, repo, linea, parcial))


def _checkout(job: Job, paso: dict, variables: dict, repos: Repos) -> None:
    con = _mapa(paso.get("with"))
    repo = expandir(str(con["repository"]), variables, repos) if "repository" in con else repos.propio
    destino = expandir(str(con["path"]), variables, repos) if "path" in con else "."
    if destino is None:
        base = _absoluta(_prefijo_fijo(str(con["path"])) or ".", repos.espacio)
        job.comodines.append(base.rstrip("/") + "/")
        return
    parcial, cono = None, True
    if "sparse-checkout" in con:
        parcial = [p.strip() for p in str(con["sparse-checkout"]).split("\n") if p.strip()]
        cono = str(con.get("sparse-checkout-cone-mode", "true")).lower() != "false"
    job.directorios.append(Directorio(_absoluta(destino, repos.espacio), repo, paso.linea, parcial, cono))


def _en_sparse(relativa: str, d: Directorio) -> bool:
    for patron in d.parcial:
        limpio = patron.lstrip("/").rstrip("/")
        if relativa == limpio or relativa.startswith(limpio + "/") or fnmatch.fnmatch(relativa, patron.lstrip("/")):
            return True
    # En modo cono, los archivos de la raíz siempre vienen.
    return d.cono and "/" not in relativa


_CREA = [
    re.compile(r"(?<![0-9&])>>?\s*(\"[^\"]+\"|'[^']+'|[^\s;&|()<>]+)"),
    re.compile(r"\btee\s+(?:-a\s+)?(\"[^\"]+\"|'[^']+'|[^\s;&|()<>]+)"),
    re.compile(r"\bmkdir\s+(?:-p\s+)?(\"[^\"]+\"|'[^']+'|[^\s;&|()<>]+)"),
    re.compile(r"(?:\s-o|\s--output|\s-O|\s-D|\s--dir)[ =](\"[^\"]+\"|'[^']+'|[^\s;&|()<>]+)"),
]
_COPIA = re.compile(r"(?:^|&&|;|\|\||\bthen\b|\bdo\b|\()\s*(?:cp|mv|ln|install)\s+([^;&|]+)")


def _anotar_creados(job: Job, texto: str, variables: dict, cwd: str | None, repos: Repos) -> None:
    candidatos = [m.group(1) for patron in _CREA for m in patron.finditer(texto)]
    for m in _COPIA.finditer(texto):
        palabras = [p for p in _palabras(m.group(1)) if not p.startswith("-")]
        if len(palabras) >= 2:
            candidatos.append(palabras[-1])
    for c in candidatos:
        valor = expandir(_sin_comillas(c), variables, repos)
        ruta = _absoluta(valor, cwd) if valor else None
        if ruta and ruta not in ("/dev/null", "/"):
            job.creados.add(ruta)


# --------------------------------------------------------------------------
# Invocaciones de scripts y `cd`
# --------------------------------------------------------------------------

_RUTA = r"(\"[^\"]+\"|'[^']+'|[^\s;&|()<>\"']+)"
_INTERPRETE = re.compile(r"(?<![\w/.$-])(bash|sh|python3?|node|source)\s+((?:-[A-Za-z]+\s+)*)" + _RUTA)
_DIRECTA = re.compile(r"(?:^|&&|\|\||;|\(|\bthen\b|\bdo\b|\belse\b)\s*" + _RUTA)
_CD = re.compile(r"(?:^|&&|\|\||;|\(|\bthen\b|\bdo\b|\belse\b)\s*cd\s+" + _RUTA)
_ASIGNA = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(\"[^\"]*\"|'[^']*'|[^\s;&|()]*)\s*$")
_FOR = re.compile(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s+([^;]+?)\s*;\s*do\b")
_CLONE = re.compile(r"\bgit\s+clone\s+(.*)")
_APILA = re.compile(r"\b(pushd|popd)\b")


def _profundidad(texto: str, hasta: int) -> int:
    return texto.count("(", 0, hasta) - texto.count(")", 0, hasta)


def _cwd_en(texto: str, posicion: int, cwd: str | None, variables: dict, repos: Repos) -> str | None:
    """Directorio de trabajo en una posición del renglón, con los `cd` que la alcanzan."""
    for m in _CD.finditer(texto):
        if m.start(1) >= posicion:
            break
        nivel = _profundidad(texto, m.start(1))
        if nivel > 0:
            # Un `cd` entre paréntesis vale hasta que su paréntesis se cierra.
            cierre, abiertos = len(texto), nivel
            for i in range(m.end(), len(texto)):
                abiertos += (texto[i] == "(") - (texto[i] == ")")
                if abiertos < nivel:
                    cierre = i
                    break
            if posicion > cierre:
                continue
        destino = expandir(_sin_comillas(m.group(1)), variables, repos)
        cwd = _absoluta(destino, cwd) if destino and destino != "-" else None
    return cwd


def _scripts(texto: str):
    """(posición, ruta tal como está escrita) de cada script invocado por ruta en el renglón."""
    vistos = set()
    for m in _INTERPRETE.finditer(texto):
        opciones = m.group(2).split()
        if any(o in ("-c", "-m") or (o.startswith("-") and not o.startswith("--") and o[-1] in "cm")
               for o in opciones):
            continue
        vistos.add(m.start(3))
        yield m.start(3), m.group(3)
    for m in _DIRECTA.finditer(texto):
        if m.start(1) not in vistos and _sin_comillas(m.group(1)).endswith(".sh") and "=" not in m.group(1):
            yield m.start(1), m.group(1)


# --------------------------------------------------------------------------
# Reglas
# --------------------------------------------------------------------------

# Lo que lanza una corrida: solo lo hace el lanzador.
_LANZA = [
    (re.compile(r"\bgh\s+workflow\s+run\b"), "`gh workflow run`"),
    (re.compile(r"\bgh\s+run\s+rerun\b"), "`gh run rerun`"),
    (re.compile(r"\bgh\s+api\b[^\n|;&]*/dispatches\b"), "`gh api …/dispatches`"),
    (re.compile(r"\b(?:curl|wget)\b[^\n|;&]*/dispatches\b"), "una llamada a `…/dispatches`"),
    (re.compile(r"\b(?:createWorkflowDispatch|createDispatchEvent|reRunWorkflow(?:FailedJobs)?)\b"),
     "un lanzamiento con la API de Actions"),
    (re.compile(r"""['"/]dispatches['"]"""), "una llamada a `…/dispatches`"),
]
_OBJETO_PETICION = re.compile(r"""\{\s*["']?workflow["']?\s*:\s*["']([^"'$\s]+)["']""")
_CLAVE = re.compile(r"""(?:^|[{,])\s*["']?([A-Za-z_][A-Za-z0-9_]*)["']?\s*:""")


def _hasta_cierre(texto: str, abre: int) -> str:
    """El objeto que empieza en `texto[abre] == "{"`, hasta su llave de cierre."""
    nivel = 0
    for i in range(abre, len(texto)):
        nivel += (texto[i] == "{") - (texto[i] == "}")
        if nivel == 0:
            return texto[abre:i + 1]
    return texto[abre:]


def peticiones_escritas(texto: str):
    """(workflow, repo o None, claves de inputs) de cada petición escrita en un renglón.

    Reconoce el JSON literal y el objeto de `jq -n`; una clave calculada
    (`($k)`) o un valor con variables no se pueden comprobar y se dejan pasar.
    """
    for m in _OBJETO_PETICION.finditer(texto):
        objeto = _hasta_cierre(texto, m.start())
        repo = re.search(r"""["']?repo["']?\s*:\s*["']([^"'$\s]+)["']""", objeto)
        dentro = re.search(r"""["']?inputs["']?\s*:\s*\{""", objeto)
        claves = _CLAVE.findall(_hasta_cierre(objeto, dentro.end() - 1)) if dentro else []
        yield m.group(1), repo.group(1) if repo else None, claves


class Lint:
    def __init__(self, raiz: Path, hermanos: Path | None = None, propio: str | None = None,
                 lanzador: bool | None = None):
        self.raiz = Path(raiz)
        self.repos = Repos(self.raiz, propio or slug_de(self.raiz), Path(hermanos) if hermanos else None)
        # El único repo que lanza corridas es el supervisor; en los demás, lanzar es un hallazgo.
        self.lanzador = self.repos.propio.split("/")[-1].lower() == "supervisor" if lanzador is None else lanzador
        self.hallazgos: list[tuple[str, int, str]] = []
        self.carpeta = self.raiz / ".github" / "workflows"
        self.workflows: dict[str, Mapa] = {}
        self._ajenos: dict[Path, dict] = {}

    def anotar(self, archivo: str, linea: int, mensaje: str) -> None:
        entrada = (archivo, linea, mensaje)
        if entrada not in self.hallazgos:
            self.hallazgos.append(entrada)

    def correr(self) -> list[tuple[str, int, str]]:
        self.workflows = self._leer(self.carpeta, anotar=True)
        for nombre, wf in self.workflows.items():
            archivo = f".github/workflows/{nombre}"
            jobs = _mapa(wf.get("jobs"))
            for nombre_job, job in jobs.items():
                if not isinstance(job, dict):
                    continue
                self._needs(archivo, nombre_job, job, jobs)
                self._tiempos(archivo, nombre_job, job, jobs.lineas.get(nombre_job, job.linea))
                self._rutas(archivo, nombre_job, job, wf)
                self._no_lanza_en_job(archivo, job)
            self._job_estado(archivo, wf, jobs)
        scripts = self.raiz / "scripts"
        if scripts.is_dir():
            for ruta in sorted(p for p in scripts.rglob("*") if p.is_file() and p.suffix in EXTENSIONES):
                self._script_suelto(ruta)
        self.hallazgos.sort()
        return self.hallazgos

    def _leer(self, carpeta: Path, anotar: bool = False) -> dict[str, Mapa]:
        leidos = {}
        if not carpeta.is_dir():
            return leidos
        for ruta in sorted([*carpeta.glob("*.yml"), *carpeta.glob("*.yaml")]):
            try:
                leidos[ruta.name] = cargar_yaml(ruta)
            except yaml.YAMLError as e:
                if anotar:
                    linea = getattr(getattr(e, "problem_mark", None), "line", 0) + 1
                    self.anotar(f".github/workflows/{ruta.name}", linea, f"YAML inválido: {getattr(e, 'problem', e)}")
        return leidos

    # ---- needs ----

    def _salidas(self, job: dict) -> set | None:
        """Outputs que declara un job; None si no se pueden conocer."""
        usa = job.get("uses")
        if usa is None:
            return set(_mapa(job.get("outputs")))
        m = re.match(r"^\./\.github/workflows/([^@]+)$", str(usa))
        if not m or m.group(1) not in self.workflows:
            return None
        llamado = _mapa(_mapa(self.workflows[m.group(1)].get("on")).get("workflow_call"))
        return set(_mapa(llamado.get("outputs")))

    def _needs(self, archivo: str, nombre: str, job: dict, jobs: dict) -> None:
        necesita = {str(n) for n in _lista(job.get("needs"))}
        for texto in _textos(job):
            for m in re.finditer(r"\bneeds\.([A-Za-z_][\w-]*)\.(?:outputs\.([A-Za-z_][\w-]*)|result\b)", texto):
                otro, salida = m.group(1), m.group(2)
                linea = texto.linea_de(m.start()) if isinstance(texto, Texto) else job.linea
                if otro not in necesita:
                    self.anotar(archivo, linea, f"job `{nombre}`: usa `needs.{otro}` pero `{otro}` no está en sus `needs`")
                    continue
                if salida is None or otro not in jobs:
                    continue
                declaradas = self._salidas(jobs[otro])
                if declaradas is not None and salida not in declaradas:
                    self.anotar(archivo, linea,
                                f"job `{nombre}`: `needs.{otro}.outputs.{salida}` no existe: `{otro}` no declara ese output")

    # ---- tiempos ----

    def _tiempos(self, archivo: str, nombre: str, job: dict, linea: int) -> None:
        # Un job que llama a un workflow reutilizable (`uses:`) no admite `timeout-minutes`.
        if "runs-on" not in job:
            return
        if "timeout-minutes" not in job:
            self.anotar(archivo, linea, f"job `{nombre}`: no declara `timeout-minutes`")
            return
        limite = _entero(job["timeout-minutes"])
        if limite is None:
            return
        for paso in _lista(job.get("steps")):
            propio = _entero(_mapa(paso).get("timeout-minutes"))
            if propio is not None and propio > limite:
                self.anotar(archivo, paso.lineas["timeout-minutes"],
                            f"job `{nombre}`: el paso «{self._nombre(paso)}» tiene `timeout-minutes: {propio}`, "
                            f"mayor que el del job ({limite})")

    @staticmethod
    def _nombre(paso: dict) -> str:
        return str(paso.get("name") or paso.get("id") or paso.get("uses") or "sin nombre")

    # ---- rutas y lanzamientos ----

    def _rutas(self, archivo: str, nombre: str, job: dict, wf: dict) -> None:
        repos = self.repos
        variables = _con_env(_con_env(_variables_base(repos), wf.get("env"), repos), job.get("env"), repos)
        por_omision = (_mapa(_mapa(job.get("defaults")).get("run")).get("working-directory")
                       or _mapa(_mapa(wf.get("defaults")).get("run")).get("working-directory"))
        estado = Job()
        pasos = [p for p in _lista(job.get("steps")) if isinstance(p, dict)]

        # Lo que el job crea por su cuenta (artefactos, cachés, archivos que escribe) no es de ningún repo.
        for paso in pasos:
            vars_paso = _con_env(variables, paso.get("env"), repos)
            usa = str(paso.get("uses", ""))
            if usa and not usa.startswith("actions/checkout@"):
                for linea in str(_mapa(paso.get("with")).get("path", "")).split("\n"):
                    valor = expandir(linea.strip(), vars_paso, repos) if linea.strip() else None
                    ruta = _absoluta(valor, repos.espacio) if valor else None
                    if ruta:
                        estado.creados.add(ruta)
            if isinstance(paso.get("run"), str):
                for _, texto in renglones(paso["run"]):
                    _anotar_creados(estado, texto, vars_paso, None, repos)

        for paso in pasos:
            vars_paso = _con_env(variables, paso.get("env"), repos)
            usa = str(paso.get("uses", ""))
            if usa.startswith("actions/checkout@"):
                _checkout(estado, paso, vars_paso, repos)
                continue
            run = paso.get("run")
            if not isinstance(run, str):
                continue
            directorio = paso.get("working-directory", por_omision)
            if directorio is None:
                cwd = repos.espacio
            else:
                valor = expandir(str(directorio), vars_paso, repos)
                cwd = _absoluta(valor, repos.espacio) if valor else None
            self._script(archivo, nombre, paso, run, estado, vars_paso, cwd)

    def _script(self, archivo: str, nombre: str, paso: dict, run: Texto, estado: Job,
                variables: dict, cwd: str | None) -> None:
        repos = self.repos
        variables = dict(variables)
        bucles: list[tuple[str, list[str]]] = []
        for linea, texto in renglones(run):
            m = _FOR.search(texto)
            if m:
                valores = [expandir(_sin_comillas(p), variables, repos) for p in m.group(2).split()]
                bucles.append((m.group(1), valores if all(v is not None for v in valores) else []))
                variables.pop(m.group(1), None)
            de_un_renglon = bool(m) and re.search(r";\s*done\b", texto) is not None
            if re.match(r"^\s*done\b", texto) and bucles:
                bucles.pop()
            m = _ASIGNA.match(texto)
            if m:
                valor = expandir(_sin_comillas(m.group(2)), variables, repos)
                if valor is None:
                    variables.pop(m.group(1), None)
                else:
                    variables[m.group(1)] = valor
                continue
            if _APILA.search(texto):
                cwd = None

            # Con un bucle de valores literales, el renglón se evalúa una vez por valor.
            nombres = [n for n, valores in bucles if valores]
            combinaciones = itertools.product(*[v for _, v in bucles if v]) if nombres else [()]
            for combinacion in combinaciones:
                locales = variables | dict(zip(nombres, combinacion))
                _anotar_creados(estado, texto, locales, cwd, repos)
                for m in _CLONE.finditer(texto):
                    _clonar(estado, m.group(1), locales, _cwd_en(texto, m.start(), cwd, locales, repos), repos, linea)
                for posicion, escrita in _scripts(texto):
                    valor = expandir(_sin_comillas(escrita), locales, repos)
                    if valor is None or not valor.endswith(EXTENSIONES):
                        continue
                    ruta = _absoluta(valor, _cwd_en(texto, posicion, cwd, locales, repos))
                    if ruta is not None:
                        self._comprobar(archivo, linea, nombre, paso, _sin_comillas(escrita), ruta, estado)
                self._lanzamientos(archivo, linea, texto, locales)
            if de_un_renglon:
                bucles.pop()
            cwd = self._cwd_final(texto, cwd, variables)

    def _cwd_final(self, texto: str, cwd: str | None, variables: dict) -> str | None:
        for m in _CD.finditer(texto):
            if _profundidad(texto, m.start(1)) > 0:
                continue
            destino = expandir(_sin_comillas(m.group(1)), variables, self.repos)
            cwd = _absoluta(destino, cwd) if destino and destino != "-" else None
        return cwd

    def _comprobar(self, archivo: str, linea: int, nombre: str, paso: dict, escrita: str, ruta: str,
                   estado: Job) -> None:
        repos = self.repos
        donde = f"job `{nombre}`, paso «{self._nombre(paso)}»: `{escrita}`"
        d = estado.de(ruta)
        if d is None:
            if estado.creado(ruta) or estado.comodin(ruta):
                return
            if ruta == repos.espacio or ruta.startswith(repos.espacio + "/"):
                self.anotar(archivo, linea, f"{donde} cae en el workspace, pero el job no hace checkout ahí antes de este paso")
            elif ruta.startswith("/tmp/") and ruta.count("/") >= 3:
                clon = "/".join(ruta.split("/")[:3])
                if not estado.creado(clon):
                    self.anotar(archivo, linea, f"{donde}: ningún checkout ni `git clone` del job crea `{clon}` antes de este paso")
            return
        if d.repo is None or d.parcial == "?" or estado.creado(ruta):
            return
        relativa = posixpath.relpath(ruta, d.ruta)
        en_disco = repos.en_disco(d.repo)
        visible = d.ruta.replace(repos.espacio, "$GITHUB_WORKSPACE")
        if en_disco is None:
            # Del otro repo no se sabe nada; pero si el archivo es de este repo y el job lo tiene
            # clonado en otra parte, la ruta apunta al directorio equivocado.
            propios = [o for o in estado.directorios if o.repo and o.repo.lower() == repos.propio.lower()]
            if propios and (repos.raiz / relativa).is_file():
                otra = propios[0].ruta.replace(repos.espacio, "$GITHUB_WORKSPACE")
                self.anotar(archivo, linea,
                            f"{donde}: `{visible}` es un checkout de {d.repo}, no de este repo; "
                            f"`{relativa}` es de este repo, que en este job está en `{otra}`")
            return
        if isinstance(d.parcial, list) and not _en_sparse(relativa, d):
            self.anotar(archivo, linea, f"{donde} queda fuera del `sparse-checkout` de `{visible}` (línea {d.linea})")
            return
        # Con comodines basta con que algo case.
        existe = any(en_disco.glob(relativa)) if re.search(r"[*?\[]", relativa) else (en_disco / relativa).exists()
        if not existe:
            self.anotar(archivo, linea,
                        f"{donde}: `{relativa}` no existe en {d.repo} (en este job, `{visible}` es un checkout de ese repo)")

    # ---- un workflow no lanza ----

    def _no_lanza(self, archivo: str, linea: int, texto: str) -> None:
        if self.lanzador:
            return
        for patron, que in _LANZA:
            if patron.search(texto):
                self.anotar(archivo, linea, f"{que}: un workflow no lanza corridas; lo que tiene que correr después "
                                            f"se pide con una salida `pedir*` y lo lanza el supervisor")
                return

    def _no_lanza_en_job(self, archivo: str, job: dict) -> None:
        """Lo que no es un `run:` (el script de `actions/github-script`, por ejemplo)."""
        for paso in _lista(job.get("steps")):
            guion = _mapa(_mapa(paso).get("with")).get("script")
            if isinstance(guion, Texto):
                for n, renglon in enumerate(str(guion).split("\n")):
                    self._no_lanza(archivo, guion.linea + (1 if guion.bloque else 0) + n, renglon)

    # ---- peticiones y lanzamientos: el destino existe y declara esos inputs ----

    def _destino(self, archivo: str, linea: int, como: str, destino: str, repo: str, claves: list[str]) -> None:
        raiz = self.repos.en_disco(repo)
        if raiz is None:
            return
        if raiz == self.raiz:
            disponibles = self.workflows
        else:
            disponibles = self._ajenos.setdefault(raiz, self._leer(raiz / ".github" / "workflows"))
        if destino not in disponibles:
            self.anotar(archivo, linea, f"{como}: ese workflow no existe en {repo}")
            return
        disparadores = disponibles[destino].get("on")
        if isinstance(disparadores, dict):
            tiene = "workflow_dispatch" in disparadores
            declarados = set(_mapa(_mapa(disparadores.get("workflow_dispatch")).get("inputs")))
        else:
            tiene = "workflow_dispatch" in [str(x) for x in _lista(disparadores)]
            declarados = set()
        if not tiene:
            self.anotar(archivo, linea, f"{como}: ese workflow no tiene `workflow_dispatch`")
            return
        for clave in claves:
            if clave and "$" not in clave and clave not in declarados:
                self.anotar(archivo, linea, f"{como}: {destino} no declara el input `{clave}`")

    def _peticiones(self, archivo: str, linea: int, texto: str) -> None:
        for destino, repo, claves in peticiones_escritas(texto):
            if not destino.endswith((".yml", ".yaml")):
                continue
            como = f"petición de `{destino}`"
            if "origen" in claves:
                self.anotar(archivo, linea, f"{como}: no lleva `origen`; lo pone el supervisor")
            slug = self.repos.propio if repo is None else (repo if "/" in repo else f"{self.repos.propio.split('/')[0]}/{repo}")
            self._destino(archivo, linea, como, destino, slug, [c for c in claves if c != "origen"])

    def _lanzamientos(self, archivo: str, linea: int, texto: str, variables: dict) -> None:
        self._no_lanza(archivo, linea, texto)
        self._peticiones(archivo, linea, texto)
        if not self.lanzador:
            return
        for m in re.finditer(r"\bgh\s+workflow\s+run\s+(.*)", texto):
            palabras = [_sin_comillas(p) for p in _palabras(m.group(1))]
            destino, repo, campos = None, self.repos.propio, []
            it = iter(palabras)
            for p in it:
                if p in ("-R", "--repo"):
                    repo = expandir(next(it, ""), variables, self.repos)
                elif p.startswith("--repo="):
                    repo = expandir(p.split("=", 1)[1], variables, self.repos)
                elif p in ("-f", "-F", "--field", "--raw-field"):
                    campos.append(next(it, ""))
                elif p in ("-r", "--ref"):
                    next(it, None)
                elif not p.startswith("-") and destino is None:
                    destino = p
            destino = expandir(destino, variables, self.repos) if destino else None
            if not destino or not destino.endswith((".yml", ".yaml")) or repo is None:
                continue
            for clave in [c.split("=", 1)[0] for c in campos] or [""]:
                como = f"`gh workflow run {destino} -f {clave}=…`" if clave else f"`gh workflow run {destino}`"
                self._destino(archivo, linea, como, destino, repo, [clave] if clave else [])

    def _script_suelto(self, ruta: Path) -> None:
        archivo = ruta.relative_to(self.raiz).as_posix()
        variables = _variables_base(self.repos)
        try:
            contenido = ruta.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        texto = Texto(contenido)
        texto.linea, texto.bloque = 1, False
        if ruta.suffix != ".sh":
            for n, renglon in enumerate(contenido.split("\n"), 1):
                if not renglon.lstrip().startswith(("#", "//")):
                    self._no_lanza(archivo, n, renglon)
            return
        for linea, renglon in renglones(texto):
            m = _ASIGNA.match(renglon)
            if m:
                valor = expandir(_sin_comillas(m.group(2)), variables, self.repos)
                if valor is None:
                    variables.pop(m.group(1), None)
                else:
                    variables[m.group(1)] = valor
                continue
            self._lanzamientos(archivo, linea, renglon, variables)

    # ---- job `estado` ----

    def _job_estado(self, archivo: str, wf: dict, jobs: dict) -> None:
        piden = [n for n, j in jobs.items() if isinstance(j, dict)
                 and any(str(k).startswith("pedir") for k in _mapa(j.get("outputs")))]
        if not piden:
            return
        estado = jobs.get("estado")
        if not isinstance(estado, dict):
            self.anotar(archivo, jobs.lineas.get(piden[0], 1),
                        f"job `{piden[0]}` declara salidas `pedir*`, pero el workflow no tiene el job `estado` "
                        f"que las publica en el artefacto `cadena`")
            return
        linea = jobs.lineas.get("estado", estado.linea)
        faltan = sorted(set(jobs) - {"estado"} - {str(n) for n in _lista(estado.get("needs"))})
        if faltan:
            self.anotar(archivo, linea, f"job `estado`: sus `needs` no incluyen a {', '.join(f'`{f}`' for f in faltan)}; "
                                        f"tiene que depender de todos los demás jobs")
        if "always()" not in str(estado.get("if", "")):
            self.anotar(archivo, linea, "job `estado`: sin `if: always()` no corre cuando otro job falla y sus peticiones se pierden")
        sube = any(str(_mapa(p).get("uses", "")).startswith("actions/upload-artifact@")
                   and str(_mapa(_mapa(p).get("with")).get("name", "")) == "cadena"
                   for p in _lista(estado.get("steps")))
        if not sube:
            self.anotar(archivo, linea, "job `estado`: no sube el artefacto `cadena` (`actions/upload-artifact` con `name: cadena`)")


def main(argv: list[str] | None = None) -> int:
    analizador = argparse.ArgumentParser(description="Lint de rutas, lanzamientos, peticiones, needs y tiempos de los workflows.")
    analizador.add_argument("raiz", help="raíz del repo")
    analizador.add_argument("--hermanos", default=None,
                            help="carpeta con otros repos de la organización, para comprobar rutas que caen en ellos")
    analizador.add_argument("--repo", default=None, help="DUEÑO/NOMBRE de este repo (por omisión, el de su remoto)")
    analizador.add_argument("--lanzador", action="store_true",
                            help="este repo es el lanzador: puede lanzar corridas (por omisión, solo el que se llama `supervisor`)")
    args = analizador.parse_args(argv)
    raiz = Path(args.raiz)
    if not (raiz / ".github" / "workflows").is_dir():
        print(f"{raiz}: no tiene .github/workflows", file=sys.stderr)
        return 2
    hallazgos = Lint(raiz, args.hermanos, args.repo, lanzador=True if args.lanzador else None).correr()
    for archivo, linea, mensaje in hallazgos:
        print(f"{archivo}:{linea}: {mensaje}")
    return 1 if hallazgos else 0


if __name__ == "__main__":
    sys.exit(main())
