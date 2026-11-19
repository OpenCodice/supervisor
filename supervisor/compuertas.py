"""Compuertas deterministas de un arreglo de infraestructura (reparar-infra.yml).

Reciben dos árboles del repo afectado, el de partida y el que dejó el agente,
y la huella que se quería arreglar. Ninguna depende del agente ni de su
veredicto. Todas son obligatorias: lo que no se puede comprobar (falta una
herramienta, un archivo no se deja leer) cuenta como falla.

- alcance    solo rutas declaradas en `[reparacion]` de cadenas.toml; nada se
             borra; lo único que se crea son pruebas; ninguna prueba existente.
- tamano     archivos y líneas dentro del tope.
- yaml       cada YAML tocado se lee, sin claves repetidas ni `<<`.
- poder      cada workflow tocado, leído como datos antes y después: `on`,
             permisos, secretos por job y por paso, `uses`, `runs-on`,
             concurrency, environment y el conjunto de jobs y de pasos,
             idénticos; en un job con poder (secretos o token de escritura, o
             del que depende uno que los tiene) un `run:` solo cambia de las
             formas de `_run_con_poder`, y solo el `if:` del paso de la huella
             se estrecha con una guarda (`_if_mas_estricto`).
- lineas     lo prohibido en cualquier línea añadida: red, hosts nuevos,
             codificación, TLS, esconder una falla, secretos, entorno,
             intérpretes en línea, instalar paquetes, git o gh hacia fuera.
- contenido  el arreglo toca el archivo del paso de la huella o uno que ese
             paso invoca, y nada más.
- worker     en un Worker: ni bindings, ni rutas, ni nombres de `env.`, ni
             `fetch` nuevos; los crons coinciden entre wrangler.jsonc y el código.
- lint, actionlint, shell, node    sin hallazgos nuevos frente a la partida.
- pytest, node-test, wrangler      las que ejecutan código del arreglo: van al
             final, sin secretos y con otro usuario.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import lint_workflows as lint
from .cadenas import Cadenas

ESTATICAS = ("alcance", "tamano", "yaml", "poder", "lineas", "contenido", "worker",
             "lint", "actionlint", "shell", "node")
DINAMICAS = ("pytest", "node-test", "wrangler")
WORKFLOWS = ".github/workflows/"


@dataclass
class Cambio:
    ruta: str
    tipo: str                      # A (nuevo) | M (modificado) | D (borrado)
    antes: str | None = None
    despues: str | None = None
    # Enlace simbólico, binario, o cambio de permisos: nada de eso es un arreglo.
    raro: str | None = None

    @property
    def anadidas(self) -> list[str]:
        """Líneas que el arreglo escribe: las que no están iguales en la partida."""
        a = (self.antes or "").split("\n")
        d = (self.despues or "").split("\n")
        nuevas = []
        for tag, _, _, j1, j2 in difflib.SequenceMatcher(None, a, d, autojunk=False).get_opcodes():
            if tag in ("replace", "insert"):
                nuevas.extend(d[j1:j2])
        return nuevas

    @property
    def lineas(self) -> int:
        a = (self.antes or "").split("\n")
        d = (self.despues or "").split("\n")
        n = 0
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, d, autojunk=False).get_opcodes():
            if tag != "equal":
                n += (i2 - i1) + (j2 - j1)
        return n


@dataclass
class Resultado:
    fallas: list[dict] = field(default_factory=list)
    corridas: list[str] = field(default_factory=list)
    archivos: list[str] = field(default_factory=list)
    lineas: int = 0

    @property
    def pasa(self) -> bool:
        return not self.fallas

    def falla(self, compuerta: str, detalle: str) -> None:
        entrada = {"compuerta": compuerta, "detalle": " ".join(str(detalle).split())[:600]}
        if entrada not in self.fallas:
            self.fallas.append(entrada)

    def de(self, compuerta: str) -> list[str]:
        return [f["detalle"] for f in self.fallas if f["compuerta"] == compuerta]

    def como_dict(self) -> dict:
        return {"pasa": self.pasa, "corridas": self.corridas, "fallas": self.fallas,
                "archivos": self.archivos, "lineas": self.lineas}


# --------------------------------------------------------------------------
# Árboles y rutas
# --------------------------------------------------------------------------

def casa(ruta: str, patron: str) -> bool:
    """Glob de rutas: `*` no cruza `/`, `**` sí."""
    partes, i = [], 0
    while i < len(patron):
        if patron.startswith("**/", i):
            partes.append("(?:.*/)?")
            i += 3
        elif patron.startswith("**", i):
            partes.append(".*")
            i += 2
        elif patron[i] == "*":
            partes.append("[^/]*")
            i += 1
        elif patron[i] == "?":
            partes.append("[^/]")
            i += 1
        else:
            partes.append(re.escape(patron[i]))
            i += 1
    return re.fullmatch("".join(partes), ruta) is not None


def _leer(ruta: Path) -> tuple[str | None, str | None]:
    """(texto, por qué no se puede tratar como texto)."""
    if ruta.is_symlink():
        return None, "es un enlace simbólico"
    if not ruta.is_file():
        return None, "no es un archivo regular"
    try:
        return ruta.read_bytes().decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "no es texto UTF-8"


def cambio(antes: Path, despues: Path, ruta: str) -> Cambio | None:
    """El cambio de una ruta entre los dos árboles; None si quedó igual."""
    a, d = antes / ruta, despues / ruta
    hay_a, hay_d = a.exists() or a.is_symlink(), d.exists() or d.is_symlink()
    if not hay_a and not hay_d:
        return None
    if hay_a and not hay_d:
        return Cambio(ruta, "D", antes=_leer(a)[0])
    texto_d, raro_d = _leer(d)
    if not hay_a:
        return Cambio(ruta, "A", despues=texto_d, raro=raro_d)
    texto_a, raro_a = _leer(a)
    raro = raro_d or raro_a
    if not raro and os.access(a, os.X_OK) != os.access(d, os.X_OK):
        raro = "cambia sus permisos"
    if texto_a == texto_d and not raro:
        return None
    return Cambio(ruta, "M", antes=texto_a, despues=texto_d, raro=raro)


def cambios_de_arboles(antes: Path, despues: Path) -> list[Cambio]:
    """Todo lo que difiere entre dos carpetas (sin `.git`)."""
    rutas = set()
    for raiz in (antes, despues):
        for carpeta, dirs, archivos in os.walk(raiz):
            dirs[:] = [d for d in dirs if d != ".git"]
            for nombre in archivos:
                rutas.add((Path(carpeta) / nombre).relative_to(raiz).as_posix())
    return [c for c in (cambio(antes, despues, r) for r in sorted(rutas)) if c is not None]


def cambios_de_git(antes: Path, despues: Path) -> list[Cambio]:
    """Lo que `despues` (un clon con el arreglo encima) difiere de su HEAD, que es `antes`.

    Lo ignorado por .gitignore no cuenta: tampoco entra al parche.
    """
    salida = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(despues), "status", "--porcelain=v1", "-z", "-uall",
         "--no-renames"], capture_output=True, check=True).stdout.decode("utf-8", "surrogateescape")
    rutas = sorted({entrada[3:] for entrada in salida.split("\0") if len(entrada) > 3})
    return [c for c in (cambio(antes, despues, r) for r in rutas) if c is not None]


# --------------------------------------------------------------------------
# alcance · tamano
# --------------------------------------------------------------------------

_PRUEBA = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|conftest\.py$|[^/]*\.test\.[cm]?js$)")


def _alcance(r: Resultado, cambios: list[Cambio], cadenas: Cadenas, repo: str) -> None:
    conf, alcance = cadenas.reparacion, cadenas.alcance(repo)
    if alcance is None:
        r.falla("alcance", f"{repo} está fuera del alcance del agente de infraestructura")
        return
    if not cambios:
        r.falla("alcance", "el arreglo no cambia ningún archivo")
    for c in cambios:
        prohibido = next((p for p in conf.get("prohibidos", []) if casa(c.ruta, p)), None)
        if c.raro:
            r.falla("alcance", f"{c.ruta}: {c.raro}")
        elif prohibido:
            r.falla("alcance", f"{c.ruta}: prohibido ({prohibido})")
        elif c.tipo == "D":
            r.falla("alcance", f"{c.ruta}: borra un archivo")
        elif c.tipo == "A":
            if not any(casa(c.ruta, p) for p in alcance.get("nuevos", [])):
                r.falla("alcance", f"{c.ruta}: lo único que se puede crear son pruebas ({', '.join(alcance.get('nuevos', []))})")
        elif _PRUEBA.search(c.ruta):
            r.falla("alcance", f"{c.ruta}: es una prueba que ya existía")
        elif not any(casa(c.ruta, p) for p in alcance.get("permitidos", [])):
            r.falla("alcance", f"{c.ruta}: fuera de lo que el arreglo puede tocar")


def _tamano(r: Resultado, cambios: list[Cambio], cadenas: Cadenas) -> None:
    conf = cadenas.reparacion
    r.archivos = [c.ruta for c in cambios]
    r.lineas = sum(c.lineas for c in cambios)
    if len(cambios) > conf.get("max_archivos", 4):
        r.falla("tamano", f"toca {len(cambios)} archivos (tope: {conf.get('max_archivos', 4)})")
    if r.lineas > conf.get("max_lineas", 120):
        r.falla("tamano", f"cambia {r.lineas} líneas (tope: {conf.get('max_lineas', 120)})")


# --------------------------------------------------------------------------
# yaml
# --------------------------------------------------------------------------

def _yaml_raro(nodo, vistos: set | None = None) -> str | None:
    """Claves repetidas, `<<` o etiquetas: GitHub y PyYAML no las leen igual."""
    vistos = set() if vistos is None else vistos
    if id(nodo) in vistos:
        return None
    vistos.add(id(nodo))
    if isinstance(nodo, yaml.MappingNode):
        claves = [k.value for k, _ in nodo.value]
        repetidas = [k for k, n in Counter(claves).items() if n > 1]
        if repetidas:
            return f"clave repetida `{repetidas[0]}` (línea {nodo.start_mark.line + 1})"
        if "<<" in claves:
            return f"usa `<<` (línea {nodo.start_mark.line + 1})"
        for k, v in nodo.value:
            if not isinstance(k, yaml.ScalarNode):
                return f"clave que no es un texto (línea {k.start_mark.line + 1})"
            problema = _yaml_raro(v, vistos)
            if problema:
                return problema
    elif isinstance(nodo, yaml.SequenceNode):
        for v in nodo.value:
            problema = _yaml_raro(v, vistos)
            if problema:
                return problema
    elif nodo.tag.startswith("!") and not nodo.tag.startswith("tag:yaml.org"):
        return f"etiqueta {nodo.tag} (línea {nodo.start_mark.line + 1})"
    return None


def _yaml(r: Resultado, cambios: list[Cambio]) -> None:
    for c in cambios:
        if not c.ruta.endswith((".yml", ".yaml")) or c.despues is None:
            continue
        try:
            nodo = yaml.compose(c.despues, Loader=yaml.SafeLoader)
        except yaml.YAMLError as e:
            r.falla("yaml", f"{c.ruta}: YAML inválido: {getattr(e, 'problem', e)}")
            continue
        if not isinstance(nodo, yaml.MappingNode):
            r.falla("yaml", f"{c.ruta}: no es un mapa")
            continue
        problema = _yaml_raro(nodo)
        if problema:
            r.falla("yaml", f"{c.ruta}: {problema}")


def _datos(texto: str | None):
    """Un workflow como datos, con las claves como texto y la línea de cada valor."""
    try:
        nodo = yaml.compose(texto or "", Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return None
    dato = lint._construir(nodo) if nodo is not None else None
    return dato if isinstance(dato, dict) else None


# --------------------------------------------------------------------------
# poder
# --------------------------------------------------------------------------

_mapa, _lista = lint._mapa, lint._lista
# `with:` de una acción: lo único que un arreglo puede cambiar.
_WITH_LIBRE = {"path", "sparse-checkout", "sparse-checkout-cone-mode", "name", "key", "restore-keys",
               "python-version", "node-version", "retention-days", "if-no-files-found", "fetch-depth"}
_JOB_FIJO = ("runs-on", "permissions", "uses", "with", "secrets", "concurrency", "environment", "container",
             "services", "strategy", "name", "needs", "continue-on-error")
_RAIZ_FIJO_SIEMPRE = ("permissions", "concurrency", "name", "run-name", "defaults")


def secretos(nodo) -> frozenset:
    """Secretos y tokens que nombra un trozo del workflow."""
    vistos = set()
    for t in lint._textos(nodo):
        vistos |= {f"secrets.{m}" for m in re.findall(r"\bsecrets\.([A-Za-z_]\w*)", t)}
        if re.search(r"\bgithub\.token\b", t):
            vistos.add("github.token")
        # El objeto entero (`toJSON(secrets)`) o una clave calculada (`secrets[x]`).
        for objeto in re.findall(r"(?i)\btojson\s*\(\s*(github|secrets)\s*\)|\b(github|secrets)\s*\[", t):
            vistos.add(f"{objeto[0] or objeto[1]}.*")
    return frozenset(vistos)


def _solo_lectura(permisos) -> bool:
    if permisos is None:
        return False
    if isinstance(permisos, dict):
        return all(str(v) in ("read", "none") for v in permisos.values())
    return str(permisos) == "read-all"


def con_poder(wf: dict, job: dict) -> bool:
    """¿Corre el job con algo que valga la pena robar o con lo que se pueda escribir?"""
    if "uses" in job or "secrets" in job:
        return True
    if secretos(wf.get("env")) or secretos(job):
        return True
    # Con un token de escritura no hace falta nombrarlo: lo usan el checkout y cualquier acción.
    return not _solo_lectura(job.get("permissions", wf.get("permissions")))


def restringidos(wf: dict) -> set[str]:
    """Jobs con poder, y los jobs de los que esos dependen (sus salidas llegan a ellos)."""
    jobs = {k: j for k, j in _mapa(wf.get("jobs")).items() if isinstance(j, dict)}
    marcados = {k for k, j in jobs.items() if con_poder(wf, j)}
    pendientes = list(marcados)
    while pendientes:
        for previo in map(str, _lista(jobs[pendientes.pop()].get("needs"))):
            if previo in jobs and previo not in marcados:
                marcados.add(previo)
                pendientes.append(previo)
    return marcados


def nombre_de_paso(paso: dict) -> str:
    """El nombre con que GitHub muestra el paso (y con el que queda en la huella)."""
    if paso.get("name"):
        return str(paso["name"])
    if paso.get("uses"):
        return f"Run {paso['uses']}"
    return "Run " + str(paso.get("run", "")).strip().split("\n")[0]


def _claves_de_pasos(pasos: list) -> list[str]:
    claves, vistas = [], Counter()
    for p in pasos:
        base = str(p.get("name") or p.get("id") or p.get("uses") or "run:" + str(p.get("run", "")).strip().split("\n")[0])
        vistas[base] += 1
        claves.append(base if vistas[base] == 1 else f"{base}#{vistas[base]}")
    return claves


def _protegido(nombre: str, cadenas: Cadenas, repo: str, archivo: str) -> bool:
    wf = cadenas.workflow(repo, archivo)
    if wf is not None and any(c["paso"] == nombre for c in wf.compuertas):
        return True
    return any(re.search(p, nombre) for p in cadenas.reparacion.get("pasos_protegidos", []))


def _on(a, d) -> str | None:
    if a == d:
        return None
    if not isinstance(a, dict) or not isinstance(d, dict) or set(a) != set(d):
        return "cambia los disparadores"
    for k in a:
        if k != "workflow_dispatch" and a[k] != d[k]:
            return f"cambia `on.{k}`"
    wa, wd = _mapa(a.get("workflow_dispatch")), _mapa(d.get("workflow_dispatch"))
    if {k: v for k, v in wa.items() if k != "inputs"} != {k: v for k, v in wd.items() if k != "inputs"}:
        return "cambia `on.workflow_dispatch`"
    ia, id_ = _mapa(wa.get("inputs")), _mapa(wd.get("inputs"))
    for k, v in ia.items():
        if id_.get(k) != v:
            return f"cambia o quita el input `{k}`"
    for k in set(id_) - set(ia):
        nuevo = id_[k]
        if (not isinstance(nuevo, dict) or "default" not in nuevo
                or str(nuevo.get("required", "false")).lower() == "true"
                or str(nuevo.get("type", "string")) not in ("string", "boolean", "number")):
            return f"el input nuevo `{k}` no es opcional con valor por omisión"
    return None


_COMILLAS = str.maketrans("", "", "\"'")
_RUTA_SEGURA = re.compile(r"[A-Za-z0-9_$/.~-]+")
_LINEA_INOCUA = re.compile(r"\s*(#.*)?")
# Sin `set -x`: la traza escribe en el log valores derivados de un secreto, que GitHub no enmascara.
_LINEA_SEGURA = re.compile(r"\s*(set -[euo]+( pipefail)?|mkdir -p [A-Za-z0-9_$/.\"-]+)\s*")


def _misma_invocacion(vieja: str, nueva: str, antes: Path) -> bool:
    """La misma línea, donde solo cambia la carpeta desde la que se llama a un script del repo."""
    pv, pn = vieja.split(), nueva.split()
    if len(pv) != len(pn):
        return False
    distintas = [(a.translate(_COMILLAS), b.translate(_COMILLAS)) for a, b in zip(pv, pn) if a != b]
    if not distintas:
        return False
    for a, b in distintas:
        if not _RUTA_SEGURA.fullmatch(b) or ".." in b:
            return False
        partes = a.split("/")
        # El sufijo más largo de la ruta vieja que es un archivo del repo: ese es el script.
        script = next(("/".join(partes[i:]) for i in range(len(partes))
                       if partes[i] and (antes / "/".join(partes[i:])).is_file()), None)
        if script is None or "/" not in script or not b.endswith("/" + script):
            return False
    return True


_CAMPO = re.compile(r"[\"']?([A-Za-z_][A-Za-z0-9_-]*)=(.*)", re.S)
_WORKFLOW = re.compile(r"[\"']?[A-Za-z0-9_.-]+\.ya?ml[\"']?")


def _mismo_lanzamiento(vieja: str, nueva: str, antes: Path) -> bool:
    """El mismo `gh workflow run`, al que solo se le quitan campos, se le renombra la clave de
    alguno (`-f clave=valor`, con el mismo valor) o se le corrige un archivo de workflow que no existe."""
    pv, pn = vieja.split(), nueva.split()
    if "gh workflow run" not in " ".join(pv) or "gh workflow run" not in " ".join(pn) or pv == pn:
        return False
    i = 0
    for palabra in pn:
        # Cada palabra nueva es una de las viejas, en orden; las que se saltan son las que se quitan.
        while i < len(pv):
            vieja_p, i = pv[i], i + 1
            a, b = _CAMPO.fullmatch(vieja_p), _CAMPO.fullmatch(palabra)
            # El archivo solo se corrige si el que estaba escrito no existe.
            otro_archivo = (_WORKFLOW.fullmatch(vieja_p) and _WORKFLOW.fullmatch(palabra)
                            and not (antes / WORKFLOWS / vieja_p.translate(_COMILLAS)).is_file())
            if vieja_p == palabra or (a and b and a.group(2) == b.group(2)) or otro_archivo:
                break
            if not (vieja_p in ("-f", "-F", "--field", "--raw-field") or _CAMPO.fullmatch(vieja_p)):
                return False        # solo se quitan campos
        else:
            return False
    return all(p in ("-f", "-F", "--field", "--raw-field") or _CAMPO.fullmatch(p) for p in pv[i:])


def _run_con_poder(r: Resultado, donde: str, archivo: str, ra, rd, rotas: set[int], positivas: set[int],
                   con_hallazgo: set[int], antes: Path, tope: int) -> None:
    """Lo que puede cambiar en el `run:` de un job con poder.

    Una línea que el lint de partida marcó como rota puede cambiar solo: la
    carpeta desde la que invoca al mismo script de este repo (y el lint del
    resultado tiene que resolverla a un checkout de este repo), o los campos
    de su `gh workflow run` (quitar uno, renombrar su clave, corregir el
    archivo), y el lint ya no la marca. Fuera de eso: comillas, comentarios,
    `set -e…` y `mkdir -p`. Nada se quita.
    """
    la, ld = str(ra).split("\n"), str(rd).split("\n")
    base_a = ra.linea + (1 if ra.bloque else 0)
    base_d = rd.linea + (1 if rd.bloque else 0)
    cambiadas = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, la, ld, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        cambiadas += max(i2 - i1, j2 - j1)
        if tag == "delete":
            if not all(_LINEA_INOCUA.fullmatch(x) for x in la[i1:i2]):
                r.falla("poder", f"{donde}: quita líneas de un `run:` que corre con poder")
        elif tag == "insert":
            for x in ld[j1:j2]:
                if not (_LINEA_INOCUA.fullmatch(x) or _LINEA_SEGURA.fullmatch(x)):
                    r.falla("poder", f"{donde}: agrega `{x.strip()[:80]}` a un `run:` que corre con poder")
        elif i2 - i1 != j2 - j1:
            # Cada línea vieja tiene que seguir ahí (salvo comillas), en orden; lo de en medio es lo agregado.
            j = j1
            for vieja in la[i1:i2]:
                socio = next((x for x in range(j, j2) if ld[x].translate(_COMILLAS) == vieja.translate(_COMILLAS)), None)
                if socio is None:
                    if not _LINEA_INOCUA.fullmatch(vieja):
                        r.falla("poder", f"{donde}: reescribe un `run:` que corre con poder")
                    continue
                agregadas, j = ld[j:socio], socio + 1
                for x in agregadas:
                    if not (_LINEA_INOCUA.fullmatch(x) or _LINEA_SEGURA.fullmatch(x)):
                        r.falla("poder", f"{donde}: agrega `{x.strip()[:80]}` a un `run:` que corre con poder")
            for x in ld[j:j2]:
                if not (_LINEA_INOCUA.fullmatch(x) or _LINEA_SEGURA.fullmatch(x)):
                    r.falla("poder", f"{donde}: agrega `{x.strip()[:80]}` a un `run:` que corre con poder")
        else:
            for k in range(i2 - i1):
                vieja, nueva = la[i1 + k], ld[j1 + k]
                if vieja.translate(_COMILLAS) == nueva.translate(_COMILLAS):
                    continue
                if _LINEA_INOCUA.fullmatch(vieja) and _LINEA_INOCUA.fullmatch(nueva):
                    continue
                if base_a + i1 + k not in rotas:
                    r.falla("poder", f"{donde}: cambia `{vieja.strip()[:80]}`, que corre con poder y el lint de "
                                     f"partida no marcó como rota")
                elif _mismo_lanzamiento(vieja, nueva, antes):
                    if base_d + j1 + k in con_hallazgo:
                        r.falla("poder", f"{donde}: el lint sigue marcando `{nueva.strip()[:80]}`")
                elif not _misma_invocacion(vieja, nueva, antes):
                    r.falla("poder", f"{donde}: en una línea que corre con poder solo puede cambiar la carpeta "
                                     f"desde la que se invoca al mismo script del repo, o los campos de su "
                                     f"`gh workflow run`")
                elif base_d + j1 + k not in positivas:
                    r.falla("poder", f"{donde}: el lint no resuelve `{nueva.strip()[:80]}` a un checkout de este repo")
    if cambiadas > tope:
        r.falla("poder", f"{donde}: cambia {cambiadas} líneas de un `run:` que corre con poder (tope: {tope})")


_ID = r"[A-Za-z_][A-Za-z0-9_-]*"
_COMPARACION = re.compile(
    rf"(?:steps\.(?P<paso>{_ID})\.(?:outputs\.{_ID}|outcome|conclusion)"
    rf"|needs\.(?P<job>{_ID})\.(?:result|outputs\.{_ID})"
    rf"|inputs\.(?P<input>{_ID}))"
    r"[ \t]*(?:==|!=)[ \t]*(?:'[A-Za-z0-9_ .:/-]*'|true|false)")


def _expresion(valor) -> str | None:
    """El texto de un `if:` sin sus `${{ }}` exteriores; None si no es una sola expresión."""
    if not isinstance(valor, str):
        return None
    texto = valor.strip()
    if texto.startswith("${{") and texto.endswith("}}"):
        texto = texto[3:-2].strip()
    # Con un `${{ }}` a medias GitHub no evalúa una condición: arma un texto, que siempre es verdadero.
    return None if not texto or "${{" in texto or "}}" in texto else texto


def _alternativa_suelta(expresion: str) -> bool | None:
    """¿Tiene un `||` fuera de paréntesis? None si los paréntesis o las comillas no cuadran."""
    nivel, i, suelta = 0, 0, False
    while i < len(expresion):
        ch = expresion[i]
        if ch == "'":
            i = expresion.find("'", i + 1)
            if i < 0:
                return None
        elif ch in "([":
            nivel += 1
        elif ch in ")]":
            nivel -= 1
            if nivel < 0:
                return None
        elif nivel == 0 and expresion.startswith("||", i):
            suelta = True
        i += 1
    return suelta if nivel == 0 else None


def _guarda(texto: str, pasos_previos: set[str], needs: set[str], inputs: set[str]) -> str | None:
    """Por qué `texto` no es una guarda admisible; None si lo es."""
    if not texto:
        return "la guarda está vacía"
    for parte in texto.split("&&"):
        m = _COMPARACION.fullmatch(parte.strip())
        if m is None:
            return (f"`{parte.strip()[:80]}` no es una comparación `==`/`!=` de `steps.<id>.outputs.<nombre>`, "
                    f"`steps.<id>.outcome`, `steps.<id>.conclusion`, `needs.<job>.result`, "
                    f"`needs.<job>.outputs.<nombre>` o `inputs.<nombre>` con un literal")
        if m.group("paso") and m.group("paso") not in pasos_previos:
            return f"`steps.{m.group('paso')}` no es el `id` de un paso anterior del mismo job"
        if m.group("job") and m.group("job") not in needs:
            return f"`needs.{m.group('job')}` no está en el `needs` del job"
        if m.group("input") and m.group("input") not in inputs:
            return f"`inputs.{m.group('input')}` no es un input declarado"
    return None


def _if_mas_estricto(viejo, nuevo, pasos_previos: set[str], needs: set[str], inputs: set[str]) -> str | None:
    """Por qué el `if:` nuevo de un paso con poder no es el viejo con una guarda más; None si lo es.

    Admite `<guarda> && <viejo>` o `<viejo> && <guarda>`, con el viejo letra
    por letra (entre paréntesis si trae un `||` suelto, que si no absorbería a
    la guarda), y en un paso sin `if:` la guarda sola. La guarda es una
    conjunción de comparaciones de un contexto que el arreglo no escribe con
    un literal: sin funciones de estado (`always()` quita el `success()`
    implícito), sin `||` y sin `!`, el resultado es `viejo && algo`: el paso
    corre en un subconjunto de los casos en que corría. Estrechar no salta
    una compuerta (esas quedan idénticas) ni añade un caso en que un paso
    corra.

    Lo que no cubre: el paso estrechado puede dejar de correr donde antes
    corría, y lo que venga después lo ve saltado en vez de hecho. Quien
    consume una salida suya recibe un valor nulo: lo normal es que se salte o
    falle, pero una condición que el nulo satisface (`== ''`, `== '0'`,
    `!= 'x'`) sigue corriendo sin ella. Eso lo juzga el segundo agente, y la
    corrida de prueba revierte el arreglo si el paso de la huella no pasa.
    Por eso quien llama solo lo admite en el paso de la huella: el que deja
    de correr es el que ya fallaba, no otro que hacía su trabajo.
    """
    e_nuevo = _expresion(nuevo)
    if e_nuevo is None:
        return "el `if:` nuevo no es una sola expresión"
    if viejo is None:
        return _guarda(e_nuevo, pasos_previos, needs, inputs)
    e_viejo = _expresion(viejo)
    suelta = _alternativa_suelta(e_viejo) if e_viejo is not None else None
    if suelta is None:
        return "el `if:` de partida no se puede leer como una sola expresión"
    motivo = "la condición anterior no queda intacta con una guarda antepuesta o pospuesta con `&&`"
    for intacto in ([f"({e_viejo})"] if suelta else [e_viejo, f"({e_viejo})"]):
        guardas = []
        if e_nuevo.endswith(intacto) and e_nuevo[:-len(intacto)].rstrip().endswith("&&"):
            guardas.append(e_nuevo[:-len(intacto)].rstrip()[:-2])
        if e_nuevo.startswith(intacto) and e_nuevo[len(intacto):].lstrip().startswith("&&"):
            guardas.append(e_nuevo[len(intacto):].lstrip()[2:])
        for guarda in guardas:
            motivo = _guarda(guarda.strip(), pasos_previos, needs, inputs)
            if motivo is None:
                return None
    return motivo


def _tiempos(r: Resultado, archivo: str, wf: dict, antes: dict, tope: int) -> None:
    def revisar(donde: str, nuevo, viejo) -> None:
        if nuevo is None or nuevo == viejo:
            return
        valor = lint._entero(nuevo)
        if valor is None or valor > tope:
            r.falla("poder", f"{archivo}: {donde}: `timeout-minutes: {nuevo}` (tope: {tope})")

    for k, job in _mapa(wf.get("jobs")).items():
        previo = _mapa(_mapa(antes.get("jobs")).get(k))
        revisar(f"job `{k}`", _mapa(job).get("timeout-minutes"), previo.get("timeout-minutes"))
        viejos = {c: p for c, p in zip(_claves_de_pasos(_lista(previo.get("steps"))), _lista(previo.get("steps")))}
        pasos = [p for p in _lista(_mapa(job).get("steps")) if isinstance(p, dict)]
        for clave, paso in zip(_claves_de_pasos(pasos), pasos):
            revisar(f"job `{k}`, paso «{clave}»", paso.get("timeout-minutes"),
                    _mapa(viejos.get(clave)).get("timeout-minutes"))


def _poder_de_workflow(r: Resultado, c: Cambio, cadenas: Cadenas, repo: str, antes: Path,
                       lint_antes: "_Lint | None", lint_despues: "_Lint | None", ifs: list[str],
                       huella: dict) -> None:
    archivo, nombre = c.ruta, c.ruta.rsplit("/", 1)[-1]
    if c.tipo != "M":
        r.falla("poder", f"{archivo}: no se crean ni se borran workflows")
        return
    a, d = _datos(c.antes), _datos(c.despues)
    if a is None or d is None:
        r.falla("poder", f"{archivo}: no se puede leer como datos")
        return
    conf = cadenas.reparacion
    rotas = {linea for arch, linea, _ in (lint_antes.hallazgos if lint_antes else []) if arch == archivo}
    con_hallazgo = {linea for arch, linea, _ in (lint_despues.hallazgos if lint_despues else []) if arch == archivo}
    positivas = {i["linea"] for i in (lint_despues.invocaciones if lint_despues else [])
                 if i["archivo"] == archivo and i["propio"]} - con_hallazgo

    problema = _on(a.get("on"), d.get("on"))
    if problema:
        r.falla("poder", f"{archivo}: {problema}")
    del_paso = paso_de_la_huella(a, huella) if nombre == huella.get("workflow") and huella.get("paso") else None
    jobs_a, jobs_d = _mapa(a.get("jobs")), _mapa(d.get("jobs"))
    if set(jobs_a) != set(jobs_d):
        r.falla("poder", f"{archivo}: agrega o quita jobs: {sorted(set(jobs_a) ^ set(jobs_d))}")
        return
    con = restringidos(a) | restringidos(d)
    fijas = set(_RAIZ_FIJO_SIEMPRE) | (set(a) | set(d)) - {"on", "jobs", "env"}
    if con:
        fijas.add("env")
    for k in sorted(fijas):
        if a.get(k) != d.get(k):
            r.falla("poder", f"{archivo}: cambia `{k}`")
    if secretos({k: v for k, v in a.items() if k != "jobs"}) != secretos({k: v for k, v in d.items() if k != "jobs"}):
        r.falla("poder", f"{archivo}: cambia los secretos que nombra fuera de los jobs")
    _tiempos(r, archivo, d, a, conf.get("timeout_max_min", 355))

    for k in jobs_a:
        ja, jd = jobs_a[k], jobs_d[k]
        if not isinstance(ja, dict) or not isinstance(jd, dict):
            if ja != jd:
                r.falla("poder", f"{archivo}: job `{k}`: cambia")
            continue
        restringido = k in con
        claves = set(ja) | set(jd)
        fijas = claves - {"steps", "timeout-minutes"} if restringido else claves & set(_JOB_FIJO)
        for campo in sorted(fijas):
            if ja.get(campo) != jd.get(campo):
                r.falla("poder", f"{archivo}: job `{k}`: cambia `{campo}`")
        sin_pasos = lambda j: {c: v for c, v in j.items() if c != "steps"}  # noqa: E731
        if secretos(sin_pasos(ja)) != secretos(sin_pasos(jd)):
            r.falla("poder", f"{archivo}: job `{k}`: cambia los secretos que nombra")

        pasos_a = [p for p in _lista(ja.get("steps")) if isinstance(p, dict)]
        pasos_d = [p for p in _lista(jd.get("steps")) if isinstance(p, dict)]
        if len(pasos_a) != len(_lista(ja.get("steps"))) or len(pasos_d) != len(_lista(jd.get("steps"))):
            r.falla("poder", f"{archivo}: job `{k}`: tiene pasos que no son un mapa")
            continue
        claves_a, claves_d = _claves_de_pasos(pasos_a), _claves_de_pasos(pasos_d)
        pares, j = [], 0
        for i, clave in enumerate(claves_a):
            while j < len(claves_d) and claves_d[j] != clave:
                j += 1
            if j == len(claves_d):
                r.falla("poder", f"{archivo}: job `{k}`: quita, renombra o cambia de lugar el paso «{clave}»")
                pares = None
                break
            pares.append((i, j))
            j += 1
        if pares is None:
            continue
        for j in sorted(set(range(len(pasos_d))) - {j for _, j in pares}):
            nuevo = pasos_d[j]
            donde = f"{archivo}: job `{k}`, paso nuevo «{claves_d[j]}»"
            if restringido:
                r.falla("poder", f"{donde}: no se agregan pasos a un job con poder")
            elif "uses" in nuevo or secretos(nuevo) or "continue-on-error" in nuevo:
                r.falla("poder", f"{donde}: un paso nuevo no puede usar acciones, secretos ni `continue-on-error`")
        for i, j in pares:
            pa, pd = pasos_a[i], pasos_d[j]
            donde = f"{archivo}: job `{k}`, paso «{claves_a[i]}»"
            if pa == pd:
                continue
            if _protegido(nombre_de_paso(pa), cadenas, repo, nombre) or _protegido(nombre_de_paso(pd), cadenas, repo, nombre):
                r.falla("poder", f"{donde}: es una compuerta y tiene que quedar idéntico")
                continue
            if secretos(pa) != secretos(pd):
                r.falla("poder", f"{donde}: cambia los secretos que nombra")
            if restringido:
                for campo in sorted((set(pa) | set(pd)) - {"run", "timeout-minutes", "if"}):
                    if pa.get(campo) != pd.get(campo):
                        r.falla("poder", f"{donde}: cambia `{campo}` en un job con poder")
                if pa.get("if") != pd.get("if"):
                    disparo = _mapa(d.get("on"))
                    es_el_de_la_huella = del_paso is not None and del_paso[0] == k and del_paso[1] is pa
                    problema = "quita su `if:`" if "if" not in pd else (
                        "solo se estrecha el `if:` del paso de la huella") if not es_el_de_la_huella else _if_mas_estricto(
                        pa.get("if"), pd["if"], {str(p["id"]) for p in pasos_d[:j] if p.get("id")},
                        set(map(str, _lista(jd.get("needs")))),
                        {str(x) for e in ("workflow_dispatch", "workflow_call")
                         for x in _mapa(_mapa(disparo.get(e)).get("inputs"))})
                    if problema:
                        r.falla("poder", f"{donde}: cambia `if` en un job con poder y no solo lo estrecha: {problema}")
                    else:
                        ifs.append(donde)
                if pa.get("run") != pd.get("run"):
                    if not isinstance(pa.get("run"), lint.Texto) or not isinstance(pd.get("run"), lint.Texto):
                        r.falla("poder", f"{donde}: agrega o quita su `run:`")
                    else:
                        _run_con_poder(r, donde, archivo, pa["run"], pd["run"], rotas, positivas, con_hallazgo, antes,
                                       conf.get("max_lineas_paso_con_poder", 6))
                continue
            for campo in ("uses", "continue-on-error", "shell"):
                if pa.get(campo) != pd.get(campo):
                    r.falla("poder", f"{donde}: cambia `{campo}`")
            wa, wd = _mapa(pa.get("with")), _mapa(pd.get("with"))
            for campo in sorted(set(wa) | set(wd)):
                if wa.get(campo) != wd.get(campo) and campo not in _WITH_LIBRE:
                    r.falla("poder", f"{donde}: cambia `with.{campo}`")


def _poder(r: Resultado, cambios: list[Cambio], cadenas: Cadenas, repo: str, antes: Path,
           lint_antes, lint_despues, huella: dict) -> None:
    ifs: list[str] = []
    for c in cambios:
        if c.ruta.startswith(".github/") and c.ruta.endswith((".yml", ".yaml")):
            _poder_de_workflow(r, c, cadenas, repo, antes, lint_antes, lint_despues, ifs, huella)
    tope = cadenas.reparacion.get("max_if_con_poder", 1)
    if len(ifs) > tope:
        r.falla("poder", f"estrecha el `if:` de {len(ifs)} pasos con poder (tope: {tope})")


# --------------------------------------------------------------------------
# lineas
# --------------------------------------------------------------------------

_PROHIBIDO = [
    ("una herramienta de red", re.compile(
        r"(?<![\w./-])(curl|wget|nc|ncat|netcat|ssh|scp|sftp|rsync|telnet|ftp|socat|openssl|dig|nslookup|ping|getent)(?![\w./-])"
        r"|/dev/(tcp|udp)\b")),
    ("codifica u ofusca", re.compile(
        r"base64|\bxxd\b|\bod\s+-|\brev\b|fromCharCode|\batob\b|\bbtoa\b|fromhex|\\x[0-9a-fA-F]{2}|\\u00"
        r"|\bcodecs\b|rot13|Buffer\.from|\bchr\s*\(")),
    ("evalúa código", re.compile(r"\beval\b|\bexec\s*\(|new Function|__import__|\bimportlib\b|\bcompile\s*\(")),
    ("desactiva TLS", re.compile(
        r"--insecure|verify\s*=\s*False|NODE_TLS_REJECT_UNAUTHORIZED|GIT_SSL_NO_VERIFY|sslVerify|PYTHONHTTPSVERIFY"
        r"|no-check-certificate|rejectUnauthorized|check_hostname|CERT_NONE|_create_unverified_context|strict-ssl"
        r"|(?<![\w-])-k(?![\w-])")),
    ("esconde una falla", re.compile(
        r"\|\|\s*(true|:|exit\s+0|echo|printf)(?![\w-])|continue-on-error|set\s+\+e|set\s+\+o\s+(errexit|pipefail)"
        r"|;\s*true\s*$|\bexit\s+0\b")),
    ("nombra un secreto", re.compile(
        r"secrets\.|github\.token|TOKEN|SECRET|PASSW|CREDENTIAL|x-access-token|[Aa]uthorization|Bearer|\.netrc"
        r"|git-credentials|extraheader")),
    ("lee o cambia el entorno", re.compile(
        r"(?:^|[\s;|&(`])(?:env|printenv)(?:\s|$)|export\s+-p|declare\s+-[px]|\bcompgen\b|\btypeset\b|/proc/"
        r"|\benviron\b|process\.env|^\s*(?:set|export)\s*(?:[|>].*)?$|\$\{!|\.git/config|\bset\s+-[a-z]*x"
        r"|GITHUB_ENV|GITHUB_PATH|BASH_ENV|LD_PRELOAD|PROMPT_COMMAND|ACTIONS_(?:RUNTIME|CACHE|ID_TOKEN)")),
    ("desvía la red", re.compile(
        r"(?i)\b(https?|all|no)_proxy\b|CURL_CA_BUNDLE|REQUESTS_CA_BUNDLE|SSL_CERT_(FILE|DIR)|NODE_EXTRA_CA_CERTS")),
]
# Un paquete o un binario nuevo es código que ninguna compuerta lee. Lo único que se admite instalar es
# un archivo de requisitos que ya está en un repo.
_REQUISITOS = re.compile(r"\s*(?:- )?(?:run: )?(?:python3? -m )?pip3? install(?: -q| --quiet)? -r [A-Za-z0-9_$/.\"-]+\s*")
_PROHIBIDO_EN_CODIGO = [
    ("un intérprete en línea", re.compile(
        r"\b(python3?|node|perl|ruby|php|deno|bun|bash|sh|zsh)\s+-(c|e)\b|\b(perl|ruby|php)\b")),
    ("instala o eleva", re.compile(
        r"\b(pip3?|npm|pnpm|yarn|apt|apt-get|brew|gem|cargo|go)\s+(install|i|add|exec|get)\b|\bnpx\b|\bsudo\b"
        r"|\bdocker\b|\bchmod\b|\bchown\b|\bgdb\b|\bstrace\b")),
    ("git o gh hacia fuera", re.compile(
        r"\bgit\s+(push|remote|config|clone|fetch|pull)\b"
        r"|\bgh\s+(api|gist|secret|auth|repo|release|issue|pr|variable|ssh-key|gpg-key|extension|codespace|cache|search|browse)\b")),
    ("red o serialización en Python", re.compile(
        r"\b(socket|urllib|urllib3|requests|httpx|http\.client|http\.server|ftplib|smtplib|telnetlib|xmlrpc|asyncio"
        r"|aiohttp|pickle|marshal|ctypes|webbrowser)\b|\bos\.system\b|\bpopen\b")),
    ("red o carga de código en JavaScript", re.compile(
        r"\bfetch\s*\(|WebSocket|XMLHttpRequest|\bimport\s*\(|\brequire\s*\(|child_process|\bconnect\s*\(")),
]
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>)\]}`,;]+")
_IP = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
_TLD = ("com|net|org|io|dev|mx|app|co|xyz|biz|us|uk|de|fr|es|ru|cn|br|eu|gov|edu|cloud|online|tk|ml|ga|cf|gq|cc|tv"
        "|ws|ly|gg|ai|nl|se|ch|jp|kr|au|ca|nz|za|vn|ph|club|shop|tech|space|website|pw|su|xxx|icu|buzz|site|top")
_DOMINIO = re.compile(rf"(?<![\w.$/@-])(?:[a-z0-9-]+\.)+(?:{_TLD})(?![\w-])", re.I)
_NO_ES_HOST = re.compile(r"^(steps|needs|inputs|github|secrets|env|matrix|runner|vars|job|strategy)\.", re.I)


def hosts(texto: str) -> set[str]:
    """URL, IP y nombres con pinta de dominio que aparecen en un texto."""
    vistos = {m.group(0).rstrip(".") for m in _URL.finditer(texto)}
    vistos |= {m.group(0) for m in _IP.finditer(texto) if m.group(0) != "127.0.0.1"}
    vistos |= {m.group(0).lower() for m in _DOMINIO.finditer(texto) if not _NO_ES_HOST.match(m.group(0))}
    return vistos


def _lineas(r: Resultado, cambios: list[Cambio]) -> None:
    for c in cambios:
        if c.despues is None:
            continue
        conocidos = hosts(c.antes or "")
        for linea in c.anadidas:
            for nombre, patron in _PROHIBIDO + _PROHIBIDO_EN_CODIGO:
                m = patron.search(linea)
                if m and not (nombre == "instala o eleva" and _REQUISITOS.fullmatch(linea)):
                    r.falla("lineas", f"{c.ruta}: una línea nueva {nombre} (`{m.group(0).strip()}`): `{linea.strip()[:100]}`")
            for h in sorted(hosts(linea) - conocidos):
                r.falla("lineas", f"{c.ruta}: host o URL nuevo `{h[:80]}`")


# --------------------------------------------------------------------------
# lint (y lo que el lint sabe de cada invocación)
# --------------------------------------------------------------------------

class _Lint(lint.Lint):
    """El lint de workflows, que además recuerda a qué checkout resolvió cada script invocado."""

    def __init__(self, raiz: Path, hermanos: Path | None, propio: str):
        super().__init__(raiz, hermanos, propio)
        self.invocaciones: list[dict] = []

    def _comprobar(self, archivo, linea, nombre, paso, escrita, ruta, estado) -> None:
        d = estado.de(ruta)
        relativa = os.path.relpath(ruta, d.ruta) if d else None
        propio = bool(d and d.repo and d.repo.lower() == self.repos.propio.lower() and d.parcial != "?"
                      and (self.raiz / relativa).is_file())
        self.invocaciones.append({"archivo": archivo, "linea": linea, "job": nombre, "escrita": escrita,
                                  "ruta": ruta, "relativa": relativa, "propio": propio})
        super()._comprobar(archivo, linea, nombre, paso, escrita, ruta, estado)


def _correr_lint(raiz: Path, hermanos: Path | None, propio: str) -> _Lint | None:
    if not (raiz / ".github" / "workflows").is_dir():
        return None
    l = _Lint(raiz, hermanos, propio)
    l.correr()
    return l


def _sin_posicion(mensaje: str) -> str:
    return re.sub(r"\(línea \d+\)|:\d+:\d+", "", mensaje)


def _lint(r: Resultado, lint_antes, lint_despues) -> None:
    if lint_despues is None:
        return
    previos = Counter((a, _sin_posicion(m)) for a, _, m in (lint_antes.hallazgos if lint_antes else []))
    ahora = Counter((a, _sin_posicion(m)) for a, _, m in lint_despues.hallazgos)
    for (archivo, mensaje), _ in sorted((ahora - previos).items()):
        r.falla("lint", f"{archivo}: {mensaje}")


# --------------------------------------------------------------------------
# contenido
# --------------------------------------------------------------------------

def paso_de_la_huella(wf: dict, huella: dict) -> tuple[str, dict] | None:
    """(clave del job, paso) al que apunta la huella en un workflow leído como datos."""
    from .huella import normalizar_job
    for clave, job in _mapa(wf.get("jobs")).items():
        if not isinstance(job, dict):
            continue
        if huella.get("job") not in (normalizar_job(str(job.get("name") or clave)), clave):
            continue
        for paso in _lista(job.get("steps")):
            if isinstance(paso, dict) and nombre_de_paso(paso) == huella.get("paso"):
                return clave, paso
    return None


_SCRIPT_NOMBRADO = re.compile(r"[A-Za-z0-9_./-]*[A-Za-z0-9_-]\.(?:sh|py|mjs|js)(?![\w.-])")


def _scripts_de(texto: str) -> list[str]:
    """Rutas de script que nombra un script de shell fuera de sus comentarios.

    Con `"$(dirname "$0")/vecino.sh"` o `"$DIR/scripts/x.py"` queda la parte
    literal (`/vecino.sh`, `/scripts/x.py`): basta para hallar el archivo.
    """
    return [m.group(0) for linea in texto.split("\n") if not linea.lstrip().startswith("#")
            for m in _SCRIPT_NOMBRADO.finditer(linea)]


def _archivo_del_repo(antes: Path, escrita: str) -> str | None:
    """El archivo del repo al que apunta una ruta escrita en un script: su sufijo más largo que existe."""
    partes = [p for p in escrita.split("/") if p not in ("", ".")]
    return next(("/".join(partes[i:]) for i in range(len(partes)) if (antes / "/".join(partes[i:])).is_file()), None)


def invocados(antes: Path, lint_antes, archivo: str, paso: dict) -> set[str]:
    """Archivos del repo que el paso invoca, directamente o a través de un script (hasta tres saltos)."""
    run = paso.get("run")
    if not isinstance(run, lint.Texto) or lint_antes is None:
        return set()
    hasta = run.linea + (1 if run.bloque else 0) + str(run).count("\n")
    pendientes = {_archivo_del_repo(antes, i["ruta"]) for i in lint_antes.invocaciones
                  if i["archivo"] == archivo and run.linea <= i["linea"] <= hasta} - {None}
    hallados: set[str] = set()
    for _ in range(3):
        hallados |= pendientes
        siguientes = set()
        for c in pendientes:
            if not c.endswith(".sh"):
                continue
            carpeta = c.rsplit("/", 1)[0] if "/" in c else "."
            for e in _scripts_de(_leer(antes / c)[0] or ""):
                # Un vecino (`comun.sh`, `/comun.sh` tras un `$(dirname "$0")`) o una ruta del repo.
                vecino = "/" not in e.lstrip("/")
                siguientes.add(_archivo_del_repo(antes, f"{carpeta}/{e.lstrip('/')}" if vecino else e))
        pendientes = siguientes - hallados - {None}
    return hallados


def _contenido(r: Resultado, cambios: list[Cambio], cadenas: Cadenas, repo: str, huella: dict, antes: Path,
               lint_antes) -> None:
    alcance = cadenas.alcance(repo) or {}
    archivo = WORKFLOWS + str(huella.get("workflow") or "")
    if not huella.get("workflow") or not (antes / archivo).is_file():
        r.falla("contenido", f"la huella no nombra un workflow de {repo}")
        return
    texto, _ = _leer(antes / archivo)
    wf = _datos(texto) or {}
    hallado = paso_de_la_huella(wf, huella) if huella.get("paso") else None
    if huella.get("paso") and hallado is None:
        r.falla("contenido", f"el paso «{huella['paso']}» del job `{huella.get('job')}` no está en {archivo}")
        return
    rutas = [c.ruta for c in cambios]
    relacionados = {archivo} & set(rutas)
    if hallado is not None:
        _, paso = hallado
        relacionados |= invocados(antes, lint_antes, archivo, paso) & set(rutas)
        donde = f"{paso.get('run', '')}\n{paso.get('working-directory', '')}"
        for w in alcance.get("workers", []):
            if re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", donde):
                relacionados |= {x for x in rutas if x.startswith(w + "/")}
    if not relacionados:
        r.falla("contenido", f"no toca {archivo} ni un archivo que el paso de la huella invoque")
    for c in cambios:
        es_prueba_nueva = c.tipo == "A" and any(casa(c.ruta, p) for p in alcance.get("nuevos", []))
        if c.ruta not in relacionados and not es_prueba_nueva:
            r.falla("contenido", f"{c.ruta}: ni es el archivo del paso de la huella ni lo invoca ese paso")


# --------------------------------------------------------------------------
# worker
# --------------------------------------------------------------------------

def leer_jsonc(texto: str):
    """JSON con comentarios y comas finales (wrangler.jsonc)."""
    limpio, i, en_texto = [], 0, False
    while i < len(texto):
        ch = texto[i]
        if en_texto:
            limpio.append(ch)
            if ch == "\\":
                limpio.append(texto[i + 1:i + 2])
                i += 1
            elif ch == '"':
                en_texto = False
        elif ch == '"':
            en_texto = True
            limpio.append(ch)
        elif texto.startswith("//", i):
            i = texto.find("\n", i)
            if i < 0:
                break
            continue
        elif texto.startswith("/*", i):
            i = texto.find("*/", i)
            if i < 0:
                break
            i += 2
            continue
        else:
            limpio.append(ch)
        i += 1
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(limpio)))


_CRON = re.compile(r"""["'`]((?:[\d*/,A-Za-z-]+ ){4}[\d*/,A-Za-z-]+)["'`]""")
_ENV = re.compile(r"\benv\s*(?:\.\s*([A-Za-z_]\w*)|\[\s*[\"']([^\"']+)[\"']\s*\])")
_FETCH = re.compile(r"\bfetch\s*\(")


def _worker(r: Resultado, cambios: list[Cambio], cadenas: Cadenas, repo: str, antes: Path, despues: Path) -> None:
    for w in (cadenas.alcance(repo) or {}).get("workers", []):
        tocados = [c for c in cambios if c.ruta.startswith(w + "/")]
        if not tocados:
            continue
        for c in tocados:
            if c.tipo == "M" and not c.ruta.endswith((".js", ".mjs", "/wrangler.jsonc")):
                r.falla("worker", f"{c.ruta}: en un Worker solo se tocan su código y wrangler.jsonc")
            if c.ruta.endswith((".js", ".mjs")) and not _PRUEBA.search(c.ruta):
                a, d = c.antes or "", c.despues or ""
                nombres = lambda t: {x or y for x, y in _ENV.findall(t)}  # noqa: E731
                if nombres(d) - nombres(a):
                    r.falla("worker", f"{c.ruta}: usa bindings o secretos nuevos: {sorted(nombres(d) - nombres(a))}")
                if re.search(r"\benv\s*\[\s*[^\"'\s]", d) and not re.search(r"\benv\s*\[\s*[^\"'\s]", a):
                    r.falla("worker", f"{c.ruta}: lee `env[…]` con una clave calculada")
                if len(_FETCH.findall(d)) > len(_FETCH.findall(a)):
                    r.falla("worker", f"{c.ruta}: agrega llamadas a `fetch`")
        try:
            conf_a = leer_jsonc((antes / w / "wrangler.jsonc").read_text(encoding="utf-8"))
            conf_d = leer_jsonc((despues / w / "wrangler.jsonc").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            r.falla("worker", f"{w}/wrangler.jsonc: no se puede leer ({e})")
            continue
        sin_crons = lambda conf: {k: ({x: y for x, y in v.items() if x != "crons"} if k == "triggers" and isinstance(v, dict) else v)  # noqa: E731
                                  for k, v in conf.items()}
        if not isinstance(conf_a, dict) or not isinstance(conf_d, dict) or sin_crons(conf_a) != sin_crons(conf_d):
            r.falla("worker", f"{w}/wrangler.jsonc: solo pueden cambiar los crons")
            continue
        crons = lambda conf: set(_mapa(conf.get("triggers")).get("crons") or [])  # noqa: E731
        literales = lambda raiz: {m for f in sorted((raiz / w).glob("*.js")) for m in _CRON.findall(f.read_text(encoding="utf-8"))}  # noqa: E731
        if crons(conf_a) != crons(conf_d) or literales(antes) != literales(despues):
            if crons(conf_d) != literales(despues):
                r.falla("worker", f"{w}: los crons de wrangler.jsonc {sorted(crons(conf_d))} no coinciden con los "
                                  f"del código {sorted(literales(despues))}")


# --------------------------------------------------------------------------
# De forma: actionlint, shell, node
# --------------------------------------------------------------------------

def _ejecutar(orden: list[str], cwd: Path, limite: int = 300) -> tuple[int, str]:
    try:
        p = subprocess.run(orden, cwd=str(cwd), capture_output=True, text=True, timeout=limite)
    except subprocess.TimeoutExpired:
        return 124, f"no terminó en {limite} s"
    except OSError as e:
        return 127, str(e)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _hallazgos_actionlint(raiz: Path) -> Counter | str:
    # Con los archivos nombrados no hace falta que el árbol sea un repo git.
    archivos = sorted(p.relative_to(raiz).as_posix() for p in (raiz / ".github" / "workflows").glob("*.y*ml"))
    if not archivos:
        return Counter()
    config = ["-config-file", ".github/actionlint.yaml"] if (raiz / ".github" / "actionlint.yaml").is_file() else []
    rc, salida = _ejecutar(["actionlint", "-format", "{{json .}}", *config, *archivos], raiz)
    if rc not in (0, 1):
        return f"actionlint terminó con {rc}: {salida[-300:]}"
    try:
        return Counter((h.get("filepath"), h.get("kind"), _sin_posicion(h.get("message", ""))) for h in json.loads(salida or "[]"))
    except ValueError:
        return f"actionlint no dio JSON: {salida[-300:]}"


def _actionlint(r: Resultado, antes: Path, despues: Path) -> None:
    if not shutil.which("actionlint") or not shutil.which("shellcheck"):
        r.falla("actionlint", "faltan actionlint o shellcheck en esta máquina")
        return
    previos, ahora = _hallazgos_actionlint(antes), _hallazgos_actionlint(despues)
    for lado in (previos, ahora):
        if isinstance(lado, str):
            r.falla("actionlint", lado)
            return
    for (archivo, clase, mensaje), _ in sorted((ahora - previos).items(), key=str):
        r.falla("actionlint", f"{archivo}: [{clase}] {mensaje}")


def _hallazgos_shellcheck(archivo: Path) -> Counter | str:
    rc, salida = _ejecutar(["shellcheck", "-f", "json", archivo.name], archivo.parent)
    try:
        return Counter((h.get("code"), h.get("level"), h.get("message")) for h in json.loads(salida or "[]"))
    except ValueError:
        return f"shellcheck terminó con {rc}: {salida[-300:]}"


def _shell(r: Resultado, cambios: list[Cambio], antes: Path, despues: Path) -> None:
    tocados = [c for c in cambios if c.ruta.endswith(".sh") and c.despues is not None]
    if not tocados:
        return
    if not shutil.which("shellcheck") or not shutil.which("bash"):
        r.falla("shell", "faltan bash o shellcheck en esta máquina")
        return
    for c in tocados:
        rc, salida = _ejecutar(["bash", "-n", c.ruta], despues)
        if rc != 0:
            r.falla("shell", f"{c.ruta}: `bash -n`: {salida[-300:]}")
            continue
        ahora = _hallazgos_shellcheck(despues / c.ruta)
        previos = _hallazgos_shellcheck(antes / c.ruta) if c.tipo == "M" else Counter()
        for lado in (previos, ahora):
            if isinstance(lado, str):
                r.falla("shell", f"{c.ruta}: {lado}")
                break
        else:
            for (codigo, nivel, mensaje), _ in sorted((ahora - previos).items(), key=str):
                r.falla("shell", f"{c.ruta}: SC{codigo} ({nivel}): {mensaje}")


def _node(r: Resultado, cambios: list[Cambio], despues: Path) -> None:
    tocados = [c for c in cambios if c.ruta.endswith((".js", ".mjs")) and c.despues is not None]
    if not tocados:
        return
    if not shutil.which("node"):
        r.falla("node", "falta node en esta máquina")
        return
    for c in tocados:
        rc, salida = _ejecutar(["node", "--check", c.ruta], despues)
        if rc != 0:
            r.falla("node", f"{c.ruta}: `node --check`: {salida[-300:]}")


# --------------------------------------------------------------------------
# Las que ejecutan código del arreglo
# --------------------------------------------------------------------------

def dinamicas(arbol: Path, cambios_rutas: list[str], cadenas: Cadenas, repo: str, prefijo: list[str] | None = None,
              cuales: tuple[str, ...] = DINAMICAS) -> Resultado:
    """pytest del repo y, si se tocó un Worker, sus pruebas y `wrangler deploy --dry-run`.

    `prefijo` antepone la orden que las corre con otro usuario: este proceso
    solo mira el código de salida.
    """
    r, prefijo = Resultado(), prefijo or []
    conf = cadenas.reparacion
    if "pytest" in cuales:
        r.corridas.append("pytest")
        rc, salida = _ejecutar([*prefijo, sys.executable if not prefijo else "python3", "-m", "pytest", "-q",
                                "-p", "no:cacheprovider"], arbol, limite=conf.get("pytest_max_s", 1500))
        # 5 = no hay ninguna prueba que correr.
        if rc not in (0, 5):
            r.falla("pytest", f"pytest terminó con {rc}: {salida[-400:]}")
    for w in (cadenas.alcance(repo) or {}).get("workers", []):
        if not any(x.startswith(w + "/") for x in cambios_rutas):
            continue
        if "node-test" in cuales:
            r.corridas.append("node-test")
            rc, salida = _ejecutar([*prefijo, "node", "--test"], arbol / w, limite=600)
            if rc != 0:
                r.falla("node-test", f"{w}: `node --test` terminó con {rc}: {salida[-400:]}")
        if "wrangler" in cuales:
            r.corridas.append("wrangler")
            rc, salida = _ejecutar([*prefijo, "npx", "--yes", conf.get("wrangler", "wrangler@4.164.0"), "deploy",
                                    "--dry-run"], arbol / w, limite=600)
            if rc != 0:
                r.falla("wrangler", f"{w}: `wrangler deploy --dry-run` terminó con {rc}: {salida[-400:]}")
    return r


# --------------------------------------------------------------------------
# Entrada
# --------------------------------------------------------------------------

def scripts_con_poder(raiz: Path, lint_arbol) -> set[str]:
    """Scripts del repo que algún job con poder invoca."""
    hallados = set()
    if lint_arbol is None:
        return hallados
    for nombre, wf in lint_arbol.workflows.items():
        con = restringidos(wf)
        for i in lint_arbol.invocaciones:
            if i["archivo"] == WORKFLOWS + nombre and i["job"] in con and i["relativa"] and (raiz / i["relativa"]).is_file():
                hallados.add(i["relativa"])
    return hallados


def evaluar(antes: Path, despues: Path, cambios: list[Cambio], huella: dict, cadenas: Cadenas,
            cuales: tuple[str, ...] = ESTATICAS, hermanos: Path | None = None) -> Resultado:
    """Las compuertas que no ejecutan nada del arreglo."""
    antes, despues = Path(antes), Path(despues)
    repo = str(huella.get("repo") or "")
    propio = f"{cadenas.org}/{repo}"
    r = Resultado(corridas=[c for c in ESTATICAS if c in cuales])
    r.archivos = [c.ruta for c in cambios]
    r.lineas = sum(c.lineas for c in cambios)
    if "alcance" in cuales:
        _alcance(r, cambios, cadenas, repo)
    try:
        lint_antes = _correr_lint(antes, hermanos, propio)
        lint_despues = _correr_lint(despues, hermanos, propio)
    except Exception as e:  # noqa: BLE001 — un árbol que el lint no puede leer no pasa
        r.falla("lint", f"el lint no pudo leer el árbol: {type(e).__name__}: {e}")
        return r
    if "alcance" in cuales:
        if cadenas.reparacion.get("scripts_con_poder", "lista") == "cerrado":
            for ruta in sorted(scripts_con_poder(antes, lint_antes) & {c.ruta for c in cambios}):
                r.falla("alcance", f"{ruta}: corre en un job con poder y `scripts_con_poder` está en `cerrado`")
    if "tamano" in cuales:
        _tamano(r, cambios, cadenas)
    if "yaml" in cuales:
        _yaml(r, cambios)
    if "poder" in cuales:
        _poder(r, cambios, cadenas, repo, antes, lint_antes, lint_despues, huella)
    if "lineas" in cuales:
        _lineas(r, cambios)
    if "contenido" in cuales:
        _contenido(r, cambios, cadenas, repo, huella, antes, lint_antes)
    if "worker" in cuales:
        _worker(r, cambios, cadenas, repo, antes, despues)
    if "lint" in cuales:
        _lint(r, lint_antes, lint_despues)
    if "actionlint" in cuales:
        _actionlint(r, antes, despues)
    if "shell" in cuales:
        _shell(r, cambios, antes, despues)
    if "node" in cuales:
        _node(r, cambios, despues)
    return r
