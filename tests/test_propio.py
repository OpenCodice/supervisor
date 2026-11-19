"""Reparar al propio supervisor: su cerco, lo que ejecuta el arreglo y el flujo sin `estado/`."""

import argparse
import dataclasses
import json
import shutil
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import AHORA, RAIZ, ClienteFalso, t
from test_compuertas import SIN_HERRAMIENTAS, con, escribir
from test_reparar import cabeza, clon_de, commit_en, git, nuevo_remoto

from supervisor import cadenas as _cadenas
from supervisor import compuertas as cp
from supervisor import reparar
from supervisor.invariantes import iso
from supervisor.observar import ErrorApi

SUP = "supervisor"
WF = ".github/workflows/supervisor.yml"
SUPERVISOR_YML = (RAIZ / WF).read_text(encoding="utf-8")
CADENAS_TOML = (RAIZ / "cadenas.toml").read_text(encoding="utf-8")
POLITICA = "from . import invariantes as inv\n\n\ndef decidir(hallazgos):\n    return [h['objeto'] for h in hallazgos]\n"
ARREGLO = con(POLITICA, "h['objeto']", "h.get('objeto')")
HUELLA = {"huella": SUP, "repo": SUP, "workflow": "supervisor.yml", "job": "supervisar",
          "paso": "Observar, evaluar y actuar", "firma": "KeyError: 'objeto' · Process completed with exit code 1."}

# Lo mínimo del repo: el workflow real, el código que el agente puede y no puede tocar, y pruebas.
REPO = {
    WF: SUPERVISOR_YML,
    ".github/workflows/reparar-infra.yml": "on:\n  workflow_dispatch:\njobs:\n  preparar:\n    runs-on: ubuntu-latest\n"
                                           "    timeout-minutes: 5\n    steps:\n      - run: echo preparar\n",
    "cadenas.toml": CADENAS_TOML,
    "supervisor/__init__.py": "",
    "supervisor/politica.py": POLITICA,
    "supervisor/observar.py": "import json\n\n\ndef leer(cliente, ruta):\n    return cliente.get(ruta)\n",
    "supervisor/compuertas.py": "ESTRICTAS = True\n",
    "supervisor/reparar.py": "PUBLICA = False\n",
    "supervisor/lint_workflows.py": "LINT = True\n",
    "supervisor/rutas.py": "def casa(ruta, patron):\n    return ruta == patron\n",
    "supervisor/cadenas.py": "RAIZ = None\n",
    "scripts/agente/brief-propio.md": "# Encargo\n",
    "scripts/agente/infra.sh": "#!/usr/bin/env bash\necho agente\n",
    "tests/conftest.py": "AHORA = 1\n",
    "tests/test_politica.py": "def test_decidir():\n    assert True\n",
    "tests/fixtures/corridas.json": "{}\n",
    "estado/huellas.jsonl": "",
}
PRUEBA_NUEVA = {"tests/test_objeto_ausente.py": "from supervisor import politica\n\n\ndef test_sin_objeto():\n"
                                                "    assert politica.decidir([{}]) == [None]\n"}


@pytest.fixture
def juzgar(tmp_path, cadenas):
    numero = iter(range(1000))

    def _juzgar(cambios: dict, borrar: tuple = (), cuales: tuple = SIN_HERRAMIENTAS, cad=None, huella=None) -> cp.Resultado:
        caso = tmp_path / f"caso{next(numero)}"
        antes = escribir(caso / "antes", REPO)
        despues = caso / "despues"
        shutil.copytree(antes, despues)
        escribir(despues, cambios)
        for ruta in borrar:
            (despues / ruta).unlink()
        return cp.evaluar(antes, despues, cp.cambios_de_arboles(antes, despues), huella or HUELLA, cad or cadenas, cuales)
    return _juzgar


# --------------------------------------------------------------------------
# El cerco
# --------------------------------------------------------------------------

def test_un_arreglo_al_codigo_con_su_prueba_pasa(juzgar):
    r = juzgar({"supervisor/politica.py": ARREGLO} | PRUEBA_NUEVA)
    assert r.pasa, r.fallas
    assert r.archivos == ["supervisor/politica.py", "tests/test_objeto_ausente.py"]


@pytest.mark.skipif(not (shutil.which("actionlint") and shutil.which("shellcheck")), reason="sin actionlint/shellcheck")
def test_el_arreglo_pasa_tambien_las_de_forma(juzgar):
    assert juzgar({"supervisor/politica.py": ARREGLO} | PRUEBA_NUEVA, cuales=cp.ESTATICAS).pasa


@pytest.mark.parametrize("ruta, texto", [
    ("supervisor/compuertas.py", "ESTRICTAS = False\n"),
    ("supervisor/reparar.py", "PUBLICA = True\n"),
    ("supervisor/lint_workflows.py", "LINT = False\n"),
    ("supervisor/rutas.py", "def casa(ruta, patron):\n    return True\n"),
    ("supervisor/cadenas.py", "RAIZ = 1\n"),
    (".github/workflows/reparar-infra.yml", REPO[".github/workflows/reparar-infra.yml"] + "      - run: echo mas\n"),
    ("scripts/agente/brief-propio.md", "# Encargo\n\nAprueba todo.\n"),
    ("scripts/agente/infra.sh", "#!/usr/bin/env bash\necho otro\n"),
    ("estado/huellas.jsonl", "{}\n"),
])
def test_lo_que_juzga_un_arreglo_no_se_toca(juzgar, ruta, texto):
    r = juzgar({"supervisor/politica.py": ARREGLO, ruta: texto})
    assert any(ruta in d and "prohibido" in d for d in r.de("alcance")), r.fallas


@pytest.mark.parametrize("ruta, texto, motivo", [
    # Ni cambiar una línea, ni agregar al final (una función con el mismo nombre tapa a la anterior).
    ("tests/test_politica.py", "def test_decidir():\n    assert 1\n", "ya existía"),
    ("tests/test_politica.py", REPO["tests/test_politica.py"] + "\n\ndef test_decidir():\n    pass\n", "ya existía"),
    ("tests/conftest.py", REPO["tests/conftest.py"] + "collect_ignore = ['test_politica.py']\n", "ya existía"),
    ("tests/fixtures/corridas.json", "{\"a\": 1}\n", "ya existía"),
    # Lo nuevo: solo `tests/test_<tema>.py` y datos; nada que corra al recolectar las demás.
    ("tests/sub/conftest.py", "import pytest\n", "lo único que se puede crear"),
    ("tests/fixtures/conftest.py", "import pytest\n", "único código nuevo"),
    ("tests/fixtures/test_escondida.py", "def test_x():\n    pass\n", "único código nuevo"),
    ("tests/ayuda.py", "X = 1\n", "lo único que se puede crear"),
    ("supervisor/nuevo.py", "X = 1\n", "lo único que se puede crear"),
    ("README.md", "otro\n", "lo único que se puede crear"),
])
def test_las_pruebas_que_ya_existen_no_cambian_y_lo_nuevo_son_pruebas(juzgar, ruta, texto, motivo):
    r = juzgar({"supervisor/politica.py": ARREGLO, ruta: texto})
    assert any(ruta in d and motivo in d for d in r.de("alcance")), r.fallas


def test_no_borra_una_prueba_ni_un_modulo(juzgar):
    r = juzgar({"supervisor/politica.py": ARREGLO}, borrar=("tests/test_politica.py", "supervisor/observar.py"))
    assert sum("borra un archivo" in d for d in r.de("alcance")) == 2


def test_un_dato_de_prueba_nuevo_si_entra(juzgar):
    assert juzgar({"supervisor/politica.py": ARREGLO, "tests/fixtures/sin_objeto.json": "[{}]\n"} | PRUEBA_NUEVA).pasa


@pytest.mark.parametrize("linea, regla", [
    ("import os", "importa un módulo"),
    ("from urllib import request", "importa un módulo"),
    ("    from . import actuar", "importa un módulo"),
    ("    valor = cliente.token", "nombra una credencial"),
    ("    valor = cliente._cabeceras()", "nombra una credencial"),
    ("    valor = entorno.get(nombre)", "lee el entorno"),
    ("    valor = os.getenv(nombre)", "lee el entorno"),
    ("    valor = getattr(cliente, nombre)", "usa reflexión"),
    ("    valor = cliente.__dict__", "usa reflexión"),
    ("    cliente.post('repos/otro/otro/issues', {'body': valor})", "agrega una escritura"),
    ("    cliente.pedir('GET', ruta)", "agrega una escritura"),
    ("    subprocess.run(['printenv'])", "agrega una escritura"),
    ("    cliente.get('https://ejemplo.example.com/' + valor)", "host o URL nuevo"),
    ("    exec(valor)", "evalúa código"),
])
def test_lo_prohibido_en_una_linea_nueva_del_codigo_del_supervisor(juzgar, linea, regla):
    r = juzgar({"supervisor/observar.py": REPO["supervisor/observar.py"] + f"\n\ndef extra(cliente, entorno, nombre, valor, ruta):\n{linea}\n"})
    assert any(regla in d for d in r.de("lineas")), r.fallas


def test_las_reglas_del_codigo_no_se_aplican_a_los_extractores(tmp_path, cadenas):
    """El mismo cambio en otro repo se juzga con su cerco de siempre, no con el del supervisor."""
    antes = escribir(tmp_path / "antes", {"scripts/agente/correr.sh": "#!/usr/bin/env bash\necho a\n"})
    despues = escribir(tmp_path / "despues", {"scripts/agente/correr.sh": "#!/usr/bin/env bash\necho a\n# importa\nimport x\n"})
    r = cp.evaluar(antes, despues, cp.cambios_de_arboles(antes, despues), HUELLA | {"repo": "fiscal-extractor"}, cadenas,
                   ("alcance", "lineas"))
    assert not any("importa un módulo" in d for d in r.de("lineas"))
    assert not cp.es_propio(cadenas, "fiscal-extractor") and cp.es_propio(cadenas, SUP)


def test_el_workflow_del_supervisor_se_juzga_con_las_reglas_de_poder(juzgar):
    casos = {
        "issues: write": ("issues: write\n  actions: write", "cambia `permissions`"),
        "CONGRESO_ARCHIVO_TOKEN: ${{ secrets.CONGRESO_ARCHIVO_TOKEN }}":
            ("CONGRESO_ARCHIVO_TOKEN: ${{ secrets.CONGRESO_ARCHIVO_TOKEN }}\n          OTRO: ${{ secrets.PR_TOKEN }}", "secretos"),
        'python -m supervisor correr --modo "$modo" --cache .cache/fotos':
            ('python -m supervisor correr --modo "$modo" --cache .cache/fotos || true', "con poder"),
        "timeout-minutes: 10": ("timeout-minutes: 10\n    continue-on-error: true", "continue-on-error"),
        "cron: '17 */6 * * *'": ("cron: '*/5 * * * *'", "cambia `on.schedule`"),
    }
    for viejo, (nuevo, motivo) in casos.items():
        r = juzgar({WF: con(SUPERVISOR_YML, viejo, nuevo)})
        assert any(motivo in d for d in r.de("poder")), (viejo, r.fallas)
    # Lo que las mismas reglas dejan pasar en un job con poder: un comentario.
    r = juzgar({WF: con(SUPERVISOR_YML, "          modo=informe\n", "          # informe salvo que haya token\n          modo=informe\n")})
    assert r.pasa, r.fallas


@pytest.mark.parametrize("viejo, nuevo, seccion", [
    ("activa = true", "activa = false", "reparacion"),
    ("max_lineas = 120", "max_lineas = 100000", "reparacion"),
    ('  "supervisor/compuertas.py",\n', "", "reparacion"),
    ('guardar = "gh secret set SUPERVISOR_TOKEN -R OpenCodice/supervisor"', 'guardar = "otra cosa"', "credenciales"),
    ("I2 = true ", "I2 = false", "invariantes"),
    ("conservar_dias = 30", "conservar_dias = 300", "estado"),
    ('org = "OpenCodice"', 'org = "Otra"', "general"),
])
def test_las_secciones_fijas_de_cadenas_toml_no_cambian(juzgar, viejo, nuevo, seccion):
    r = juzgar({"cadenas.toml": con(CADENAS_TOML, viejo, nuevo)})
    assert any(f"cambia `{seccion}`" in d for d in r.de("declarado")), r.fallas


def test_cadenas_toml_puede_cambiar_fuera_de_lo_fijo_si_sigue_cargando(juzgar):
    assert juzgar({"cadenas.toml": con(CADENAS_TOML, "en_cola_max_min = 120", "en_cola_max_min = 180")}).pasa
    r = juzgar({"cadenas.toml": con(CADENAS_TOML, 'horario = [{ dias = ["mie"], hora = "15:30" }]',
                                    'horario = [{ dias = ["miercoles"], hora = "15:30" }]')})
    assert any("no carga" in d for d in r.de("declarado")), r.fallas
    assert any("no se puede leer" in d for d in juzgar({"cadenas.toml": CADENAS_TOML + "[rota\n"}).de("declarado"))


def test_sin_declarar_el_modo_el_supervisor_no_se_repara(juzgar, cadenas):
    sin = dataclasses.replace(cadenas, reparacion={k: v for k, v in cadenas.reparacion.items() if k != "propio"})
    r = juzgar({"supervisor/politica.py": ARREGLO}, cad=sin)
    assert any("no se repara a sí mismo" in d for d in r.de("alcance"))


def test_cadenas_toml_exige_que_lo_que_juzga_quede_fuera(tmp_path):
    sin_cerco = con(CADENAS_TOML, 'secciones_fijas = ["general", "estado", "invariantes", "reparacion", "credenciales"]',
                    'secciones_fijas = ["general"]')
    (tmp_path / "c.toml").write_text(sin_cerco, encoding="utf-8")
    with pytest.raises(ValueError, match="secciones_fijas"):
        _cadenas.cargar(tmp_path / "c.toml")
    propio = _cadenas.cargar().reparacion["propio"]
    assert {"supervisor/compuertas.py", "supervisor/reparar.py", ".github/workflows/reparar-infra.yml",
            "scripts/**"} <= set(propio["prohibidos"])


# --------------------------------------------------------------------------
# Las que ejecutan el arreglo
# --------------------------------------------------------------------------

def _arbol(tmp_path: Path, archivos: dict, principal: str) -> Path:
    """Un «supervisor» de juguete: un módulo, sus pruebas y un `python -m supervisor correr` que no usa la red."""
    return escribir(tmp_path / "arbol", {
        "supervisor/__init__.py": "", "supervisor/__main__.py": principal, "estado/huellas.jsonl": "{}\n",
        "tests/test_viejo.py": "from supervisor import modulo\n\n\ndef test_viejo():\n    assert modulo.X == 1\n",
    } | archivos)


BIEN = "import sys\nsys.exit(0)\n"


def test_la_suite_de_main_corre_sin_lo_que_el_arreglo_agrego(tmp_path, cadenas):
    """Una prueba nueva que, al importarse, deja al módulo como las pruebas viejas lo esperan haría
    pasar la suite entera; por separado, la prueba vieja falla."""
    tramposa = {"tests/test_aaa.py": "from supervisor import modulo\n\nmodulo.X = 1\n\n\ndef test_nueva():\n    assert True\n"}
    arbol = _arbol(tmp_path, {"supervisor/modulo.py": "X = 2\n"} | tramposa, BIEN)
    junta = cp._ejecutar([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], arbol)
    assert junta[0] == 0
    r = cp.dinamicas_propio(arbol, list(tramposa), cadenas, cuales=("pytest", "pruebas-nuevas"), trabajo=tmp_path / "t")
    assert r.de("pytest") and not r.de("pruebas-nuevas") and r.corridas == ["pytest", "pruebas-nuevas"]


def test_un_arreglo_bueno_pasa_la_suite_sus_pruebas_y_la_vuelta_en_informe(tmp_path, cadenas):
    nueva = {"tests/test_nueva.py": "from supervisor import modulo\n\n\ndef test_nueva():\n    assert modulo.X == 1\n"}
    # La vuelta de prueba recibe solo el token de lectura, corre sobre una copia del estado y en modo informe.
    principal = ("import os, pathlib, sys\n"
                 "a = sys.argv\n"
                 "estado = pathlib.Path(a[a.index('--estado') + 1])\n"
                 "ok = a[1:4] == ['correr', '--modo', 'informe'] and os.environ.get('GITHUB_TOKEN') == 'solo-lee'\n"
                 "ok = ok and (estado / 'huellas.jsonl').is_file() and estado.resolve() != pathlib.Path('estado').resolve()\n"
                 "sys.exit(0 if ok else 3)\n")
    arbol = _arbol(tmp_path, {"supervisor/modulo.py": "X = 1\n"} | nueva, principal)
    r = cp.dinamicas_propio(arbol, list(nueva), cadenas, lectura={"GITHUB_TOKEN": "solo-lee"}, trabajo=tmp_path / "t")
    assert r.pasa and r.corridas == ["pytest", "pruebas-nuevas", "informe"], r.fallas
    assert not (tmp_path / "t" / "sin-nuevos" / "tests" / "test_nueva.py").exists()
    # Sin el token la vuelta de juguete falla: se dice, con su código de salida.
    r = cp.dinamicas_propio(arbol, list(nueva), cadenas, cuales=("informe",), trabajo=tmp_path / "t2")
    assert any("terminó con 3" in d for d in r.de("informe"))


def test_una_prueba_nueva_que_falla_o_una_vuelta_que_truena_descartan(tmp_path, cadenas):
    nueva = {"tests/test_nueva.py": "def test_nueva():\n    assert False\n"}
    arbol = _arbol(tmp_path, {"supervisor/modulo.py": "X = 1\n"} | nueva, "raise KeyError('objeto')\n")
    r = cp.dinamicas_propio(arbol, list(nueva), cadenas, trabajo=tmp_path / "t")
    assert not r.de("pytest") and r.de("pruebas-nuevas") and any("KeyError" in d for d in r.de("informe"))
    assert cp.dinamicas_propio(arbol, ["tests/../x.py"], cadenas, trabajo=tmp_path / "t3").de("pytest")


# --------------------------------------------------------------------------
# Preparar sin `estado/`: la falla, los topes y la constancia salen de la API
# --------------------------------------------------------------------------

API = "repos/OpenCodice/supervisor"
TRAZA = ("2026-11-18T11:45:10.0Z ##[group]Run modo=informe\n"
         "2026-11-18T11:45:12.0Z   File \"/home/runner/work/supervisor/supervisor/supervisor/politica.py\", line 3, in decidir\n"
         "2026-11-18T11:45:12.1Z KeyError: 'objeto'\n"
         "2026-11-18T11:45:12.2Z ##[error]Process completed with exit code 1.\n")


def _titulo(huella: str = SUP, ensayo: str = "false") -> str:
    return (f"reparar-infra huella=[{huella}] ensayo=[{ensayo}] base=[] revertir=[] repo=[supervisor] "
            f"workflow=[supervisor.yml] job=[] paso=[] corrida=[77] origen=[scheduler]")


class Api(ClienteFalso):
    """La API del repo del supervisor, con lo que el modo propio lee de ella."""

    def __init__(self, recientes=("failure", "failure", "failure"), reparaciones=(), log=TRAZA, conclusion="failure",
                 rama="main"):
        super().__init__()
        self.abiertos: list[dict] = []
        self.cerrados: list[int] = []
        self.compare: dict[str, str] = {}
        self.commit = {"commit": {"author": {"name": reparar.AUTOR[0]},
                                  "message": "Reparación automática de infraestructura: paso (supervisor)"}}
        self.corridas = [{"id": 80 - n, "status": "completed", "conclusion": c, "created_at": t(n / 4), "head_sha": "a" * 40,
                          "path": WF} for n, c in enumerate(recientes)]
        self.respuestas = {
            f"{API}/actions/runs/77": {"id": 77, "path": WF, "status": "completed", "conclusion": conclusion,
                                       "head_branch": rama, "created_at": t(0.5), "updated_at": t(0.4),
                                       "head_sha": "a" * 40, "run_attempt": 1},
            f"{API}/actions/runs/77/jobs": [{"id": 5, "name": "supervisar", "status": "completed", "conclusion": "failure",
                                             "steps": [{"name": "Observar, evaluar y actuar", "number": 5,
                                                        "status": "completed", "conclusion": "failure",
                                                        "started_at": "2026-11-18T11:45:10Z",
                                                        "completed_at": "2026-11-18T11:45:12Z"}]}],
            f"{API}/actions/workflows/reparar-infra.yml/runs": [
                {"id": 900 + n, "display_title": titulo, "created_at": t(horas)} for n, (titulo, horas) in enumerate(reparaciones)],
        }
        self.log = log

    def get(self, ruta, **params):
        self.llamadas.append(("GET", ruta, None))
        if ruta.endswith("/actions/workflows/supervisor.yml/runs"):
            return {"workflow_runs": self.corridas}
        if "/compare/" in ruta:
            return {"status": self.compare.get(ruta.rsplit("...", 1)[-1], "behind")}
        if "/commits/" in ruta:
            return self.commit
        if ruta not in self.respuestas:
            raise ErrorApi(404, "GET", ruta)
        return self.respuestas[ruta]

    def paginas(self, ruta, clave=None, **params):
        self.llamadas.append(("GET", ruta, None))
        if ruta.endswith("/issues"):
            return self.abiertos
        return self.respuestas.get(ruta, [])

    def texto(self, ruta):
        return self.log

    def patch(self, ruta, cuerpo):
        super().patch(ruta, cuerpo)
        if cuerpo == {"state": "closed"}:
            self.cerrados.append(int(ruta.rsplit("/", 1)[-1]))
        return {}

    def comentarios(self) -> list[str]:
        return [c["body"] for m, r, c in self.llamadas if m == "POST" and "/issues" in r]


def test_la_huella_del_supervisor_sale_de_la_corrida_que_fallo(cadenas):
    api = Api()
    reg = reparar.registro_propio(api, cadenas, "77", AHORA, esta=999)
    assert (reg["huella"], reg["repo"], reg["workflow"]) == (SUP, SUP, "supervisor.yml")
    assert (reg["job"], reg["paso"], reg["corridas"]) == ("supervisar", "Observar, evaluar y actuar", [77])
    assert reg["firma"] == "KeyError: 'objeto' · Process completed with exit code 1."
    # Nada de esto leyó `estado/`: todo salió de la API.
    assert all(ruta.startswith(API) for _, ruta, _ in api.llamadas)


@pytest.mark.parametrize("api, corrida, motivo, sin_trabajo", [
    (Api(conclusion="success"), "77", "no terminó en falla", False),
    (Api(rama="otra"), "77", "no es de supervisor.yml en main", False),
    (Api(), "no-es-un-id", "tiene que ser el id", False),
    (Api(recientes=("success", "failure", "failure", "failure")), "77", "ya volvió a correr bien", True),
    (Api(reparaciones=[(_titulo(), 3), (_titulo(), 20)]), "77", "ya hubo 2 reparaciones", True),
    (Api(log=TRAZA.replace("KeyError: 'objeto'", "::error::GitHub rechazó el token (HTTP 401) en todo lo que se le pidió")),
     "77", "credencial", True),
    (Api(log=TRAZA.replace("KeyError: 'objeto'", "urllib.error.URLError: <urlopen error timed out>")), "77", "pasajera", True),
])
def test_lo_que_no_es_para_el_agente_se_descarta_antes_de_gastarlo(cadenas, api, corrida, motivo, sin_trabajo):
    with pytest.raises(reparar.Descarte, match=motivo) as e:
        reparar.registro_propio(api, cadenas, corrida, AHORA, esta=999)
    assert isinstance(e.value, reparar.SinTrabajo) == sin_trabajo


def test_el_tope_del_dia_se_cuenta_por_los_titulos_de_las_corridas(cadenas):
    corridas = [
        {"id": 1, "display_title": _titulo(), "created_at": t(2)},
        {"id": 2, "display_title": _titulo(), "created_at": t(30)},                  # de ayer
        {"id": 3, "display_title": _titulo(ensayo="true"), "created_at": t(1)},      # un ensayo
        {"id": 4, "display_title": _titulo(huella="6bd0b49d205c"), "created_at": t(1)},  # otra huella
        {"id": 5, "display_title": "Reparar infraestructura", "created_at": t(1)},   # sin el contrato de run-name
        {"id": 6, "display_title": _titulo(), "created_at": t(0)},                   # esta misma corrida
    ]
    assert reparar.sesiones_propias(corridas, AHORA, salvo=6) == 1
    assert reparar.sesiones_propias(corridas, AHORA) == 2
    # Con una sola en el día, la segunda todavía entra; un ensayo no mira el tope.
    assert reparar.registro_propio(Api(reparaciones=[(_titulo(), 3)]), cadenas, "77", AHORA)["huella"] == SUP
    assert reparar.registro_propio(Api(reparaciones=[(_titulo(), 3), (_titulo(), 5)], recientes=("success",)), cadenas,
                                   "77", AHORA, ensayo=True)["huella"] == SUP


def _args_preparar(tmp_path, **cambios):
    base = dict(huella=SUP, salida=str(tmp_path / "preparado"), clon=str(tmp_path / "clon"), estado=str(tmp_path / "no-hay-estado"),
                ensayo=False, base="", repo=SUP, workflow="supervisor.yml", job="", paso="", corrida="77", minutos="45",
                dir_repo="/srv/infra/repo", dir_entrada="/srv/infra/entrada", dir_salida="/srv/infra/salida")
    return argparse.Namespace(**(base | cambios))


def test_preparar_arma_el_encargo_sin_leer_el_estado(tmp_path, cadenas, monkeypatch):
    remoto = nuevo_remoto(tmp_path, SUP, REPO)
    monkeypatch.setattr(reparar, "url_de", lambda org, repo, token=None: str(remoto))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "salidas"))
    assert reparar.preparar(_args_preparar(tmp_path), cadenas, Api(), AHORA) == 0
    datos = json.loads((tmp_path / "preparado" / "huella.json").read_text())
    assert datos["registro"]["huella"] == SUP and datos["partida"] == cabeza(remoto) and datos["intento"] == 1
    assert not (tmp_path / "no-hay-estado").exists()
    brief = (tmp_path / "preparado" / "brief.md").read_text()
    assert "# Reparar al supervisor" in brief and "KeyError: 'objeto'" in brief and "__" not in brief.replace("`__así__`", "")
    assert "- `supervisor/*.py`" in brief and "- `supervisor/compuertas.py`" in brief and "- `tests/test_*.py`" in brief
    juez = (tmp_path / "preparado" / "brief-juez.md").read_text()
    assert "# Juzgar un arreglo al supervisor" in juez and "__" not in juez
    assert "KeyError: 'objeto'" in (tmp_path / "preparado" / "log.txt").read_text()
    assert (tmp_path / "salidas").read_text().splitlines() == [f"repo={SUP}", f"partida={datos['partida']}", "seguir=true"]
    # El contrato con quien lanza: `repo` y `workflow` son los del supervisor.
    with pytest.raises(reparar.Descarte, match="repo=supervisor"):
        reparar.preparar(_args_preparar(tmp_path, repo="fiscal-extractor"), cadenas, Api(), AHORA)
    with pytest.raises(reparar.Descarte, match="solo vale con `ensayo`"):
        reparar.preparar(_args_preparar(tmp_path, base="a" * 40), cadenas, Api(), AHORA)


def test_lo_que_no_llega_al_agente_deja_constancia_y_la_corrida_termina_bien(tmp_path, monkeypatch, capsys):
    api = Api(recientes=("success",))
    monkeypatch.setattr(reparar, "Cliente", lambda *a, **k: api)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "salidas"))
    orden = ["preparar", "--huella", SUP, "--salida", str(tmp_path / "p"), "--clon", str(tmp_path / "c"),
             "--repo", SUP, "--workflow", "supervisor.yml", "--corrida", "77"]
    assert reparar.main(orden) == 0
    assert (tmp_path / "salidas").read_text() == "seguir=false\n" and "::notice::" in capsys.readouterr().out
    [abierto] = [c for m, r, c in api.llamadas if m == "POST" and r == f"{API}/issues"]
    assert abierto["title"].startswith("[supervisor]") and "ya volvió a correr bien" in abierto["body"]
    # Algo que no cuadra con el contrato (la corrida no falló) sí es un error de la corrida.
    api = Api(conclusion="success")
    assert reparar.main(orden) == 1 and "no terminó en falla" in api.comentarios()[0]


def test_la_constancia_va_a_un_solo_issue(cadenas):
    api = Api()
    assert reparar.constancia_propia(api, cadenas, "**Intento descartado**: x") == 1
    api.abiertos = [{"number": 31, "title": "[6bd0b49d205c] agente: otra huella"},
                    {"number": 40, "title": "[supervisor] El supervisor no corre: reparación automática"}]
    assert reparar.constancia_propia(api, cadenas, "**Arreglo publicado**", cerrar=True) == 40
    assert [r for m, r, _ in api.llamadas if m == "POST"] == [f"{API}/issues", f"{API}/issues/40/comments"]
    assert api.cerrados == [40] and "no es una asignación" in api.comentarios()[-1]
    api.falla_en = "/issues/40/comments"
    assert reparar.constancia_propia(api, cadenas, "x") is None


# --------------------------------------------------------------------------
# Publicar, esperar la corrida de prueba y revertir
# --------------------------------------------------------------------------

con_herramientas = pytest.mark.skipif(not (shutil.which("actionlint") and shutil.which("shellcheck")),
                                      reason="sin actionlint/shellcheck")


@pytest.fixture
def remoto(tmp_path, monkeypatch):
    remoto = nuevo_remoto(tmp_path, SUP, REPO)
    monkeypatch.setattr(reparar, "url_de", lambda org, repo, token=None: str(remoto))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "salidas"))
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    return remoto


def _parche(tmp_path, remoto, cambios: dict) -> Path:
    clon = clon_de(remoto, tmp_path / f"para-el-parche-{len(cambios)}")
    escribir(clon, cambios)
    ruta = tmp_path / f"arreglo-{len(cambios)}.patch"
    ruta.write_text(reparar.parche_de(clon), encoding="utf-8")
    return ruta


def _args_publicar(tmp_path, remoto, parche: Path, compuertas: dict | None = None):
    (tmp_path / "huella.json").write_text(json.dumps({"registro": HUELLA, "partida": cabeza(remoto)}))
    pasa = compuertas or {"pasa": True, "fallas": [], "veredicto": {"resultado": "arreglo", "causa_raiz": "un hallazgo sin objeto"},
                          "sha_parche": reparar.sha256(parche)}
    (tmp_path / "compuertas.json").write_text(json.dumps(pasa))
    (tmp_path / "dinamicas.json").write_text(json.dumps({"pasa": True, "fallas": []}))
    (tmp_path / "juez.json").write_text(json.dumps({"result": json.dumps(reparar.JUEZ_ESPERADO | {"nota": "bien"})}))
    return argparse.Namespace(huella_json=str(tmp_path / "huella.json"), compuertas=str(tmp_path / "compuertas.json"),
                              dinamicas=str(tmp_path / "dinamicas.json"), juez=str(tmp_path / "juez.json"),
                              parche=str(parche), sha_parche=pasa["sha_parche"], trabajo=str(tmp_path / "pub"),
                              supervisor=str(tmp_path / "no-se-usa"), hermanos="", ensayo=False)


@con_herramientas
def test_publicar_un_arreglo_al_supervisor_lanza_su_prueba_y_no_toca_el_estado(tmp_path, remoto, cadenas, monkeypatch):
    api = Api()
    monkeypatch.setattr(reparar, "Cliente", lambda *a, **k: api)
    parche = _parche(tmp_path, remoto, {"supervisor/politica.py": ARREGLO} | PRUEBA_NUEVA)
    assert reparar.publicar(_args_publicar(tmp_path, remoto, parche), cadenas, AHORA) == 0
    autor, titulo_ = git(remoto, "log", "-1", "--format=%an <%ae>%n%s", "main").splitlines()
    assert autor == "Claude (agente OpenCodice) <noreply@anthropic.com>"
    assert titulo_ == "Reparación automática de infraestructura: Observar, evaluar y actuar (supervisor)"
    assert cabeza(remoto, "supervisor/politica.py") == ARREGLO and cabeza(remoto, "estado/huellas.jsonl") == ""
    salidas = dict(l.split("=", 1) for l in (tmp_path / "salidas").read_text().splitlines())
    assert salidas["publicado"] == cabeza(remoto) and salidas["desde"] <= iso(reparar._ahora())
    assert api.lanzamientos() == [("supervisor/supervisor.yml", {"origen": "reparar-infra"})]
    [constancia] = api.comentarios()
    assert "**Arreglo publicado**" in constancia and salidas["publicado"] in constancia and "se revierte solo" in constancia


@con_herramientas
def test_publicar_repite_el_cerco_del_supervisor_sobre_main(tmp_path, remoto, cadenas, monkeypatch):
    """Aunque el informe de las compuertas diga que pasó, un parche que toca lo que juzga no se publica."""
    api = Api()
    monkeypatch.setattr(reparar, "Cliente", lambda *a, **k: api)
    antes = cabeza(remoto)
    parche = _parche(tmp_path, remoto, {"supervisor/politica.py": ARREGLO, "supervisor/compuertas.py": "ESTRICTAS = False\n"})
    assert reparar.publicar(_args_publicar(tmp_path, remoto, parche), cadenas, AHORA) == 0
    assert cabeza(remoto) == antes and api.lanzamientos() == []
    assert "publicado=" not in (tmp_path / "salidas").read_text() if (tmp_path / "salidas").exists() else True
    [constancia] = api.comentarios()
    assert "**Intento descartado**" in constancia and "supervisor/compuertas.py: prohibido" in constancia


def test_un_descarte_del_supervisor_deja_constancia_y_no_anota_en_el_estado(tmp_path, remoto, cadenas, monkeypatch):
    api = Api()
    monkeypatch.setattr(reparar, "Cliente", lambda *a, **k: api)
    antes = cabeza(remoto)
    no_pasa = {"pasa": False, "fallas": [{"compuerta": "lineas", "detalle": "supervisor/observar.py: una línea nueva importa un módulo"}],
               "veredicto": {"resultado": "arreglo"}, "sha_parche": "x"}
    assert reparar.publicar(_args_publicar(tmp_path, remoto, tmp_path / "no.patch", no_pasa), cadenas, AHORA) == 0
    assert cabeza(remoto) == antes and "[lineas]" in api.comentarios()[0]


def _publicado(tmp_path, remoto, cadenas) -> str:
    parche = _parche(tmp_path, remoto, {"supervisor/politica.py": ARREGLO})
    return reparar.aplicar_y_publicar(str(remoto), parche, HUELLA, cadenas, tmp_path / "pub", cuales=SIN_HERRAMIENTAS)["commit"]


def _args_verificar(tmp_path, commit: str):
    return argparse.Namespace(commit=commit, desde=iso(AHORA), trabajo=str(tmp_path / "ver"), hermanos="",
                              paso="Observar, evaluar y actuar")


def _corrida(ident: int, conclusion: str | None, sha: str, minutos: float = 5) -> dict:
    return {"id": ident, "status": "completed" if conclusion else "in_progress", "conclusion": conclusion,
            "created_at": iso(AHORA + timedelta(minutes=minutos)), "head_sha": sha, "path": WF}


def test_la_prueba_es_la_primera_corrida_terminada_que_trae_el_arreglo(cadenas):
    api, commit = Api(), "c" * 40
    api.compare = {"e" * 40: "ahead"}
    # Una creada antes del arreglo (sin él), una cancelada y una en curso no dicen nada.
    api.corridas = [_corrida(1, "failure", "a" * 40, -2), _corrida(2, "cancelled", "e" * 40), _corrida(3, None, "e" * 40, 6)]
    assert reparar.prueba_propia(api, cadenas, commit, AHORA) == ("espera", None)
    api.corridas += [_corrida(5, "failure", "e" * 40, 9), _corrida(4, "success", commit, 8)]
    veredicto, corrida = reparar.prueba_propia(api, cadenas, commit, AHORA)
    assert (veredicto, corrida["id"]) == ("pasa", 4)
    api.corridas = [_corrida(5, "failure", "e" * 40, 9)]
    assert reparar.prueba_propia(api, cadenas, commit, AHORA)[0] == "falla"


def test_si_la_corrida_de_prueba_pasa_se_cierra_la_constancia(tmp_path, remoto, cadenas, monkeypatch):
    commit = _publicado(tmp_path, remoto, cadenas)
    api = Api()
    api.abiertos = [{"number": 40, "title": "[supervisor] El supervisor no corre: reparación automática"}]
    esperas = []

    def dormir(s):
        esperas.append(s)
        api.corridas = [_corrida(9, "success", commit)]

    api.corridas = [_corrida(9, None, commit)]
    monkeypatch.setattr(reparar, "Cliente", lambda *a, **k: api)
    assert reparar.verificar(_args_verificar(tmp_path, commit), cadenas, AHORA, dormir=dormir, reloj=lambda: AHORA) == 0
    assert esperas == [30] and cabeza(remoto) == commit and api.cerrados == [40]
    assert "**Resuelto**" in api.comentarios()[-1]


@pytest.mark.parametrize("caso", ["falla", "sin-resultado"])
def test_si_la_prueba_falla_o_no_llega_el_arreglo_se_revierte(tmp_path, remoto, cadenas, monkeypatch, caso):
    commit = _publicado(tmp_path, remoto, cadenas)
    # Mientras tanto main avanza (el reloj sigue lanzando al supervisor, que escribe su estado si puede).
    commit_en(remoto, tmp_path, {"estado/huellas.jsonl": "{}\n"}, "Estado del supervisor")
    api = Api()
    api.corridas = [_corrida(9, "failure", commit)] if caso == "falla" else [_corrida(9, None, commit)]
    reloj = iter([AHORA + timedelta(minutes=m) for m in (1, 20, 41, 60)])
    monkeypatch.setattr(reparar, "Cliente", lambda *a, **k: api)
    monkeypatch.setattr(reparar, "revertir_commit", lambda *a, **k: _revertir(*a, **k))
    assert reparar.verificar(_args_verificar(tmp_path, commit), cadenas, AHORA, dormir=lambda s: None,
                             reloj=lambda: next(reloj)) == 0
    assert cabeza(remoto, "supervisor/politica.py") == POLITICA and cabeza(remoto, "estado/huellas.jsonl") == "{}\n"
    assert git(remoto, "log", "-1", "--format=%s", "main").startswith("Reversión automática de infraestructura")
    assert "**Arreglo revertido**" in api.comentarios()[-1] and api.cerrados == []
    assert ("volvió a fallar" if caso == "falla" else "terminó en 40 minutos") in api.comentarios()[-1]


_revertir_real = reparar.revertir_commit


def _revertir(*args, **kwargs):
    """El revert de verdad, con las compuertas de forma que no piden herramientas instaladas."""
    return _revertir_real(*args, **(kwargs | {"cuales": ("yaml", "lint"), "dormir": lambda s: None}))


def test_solo_se_revierte_un_commit_que_publico_este_flujo(tmp_path, remoto, cadenas, monkeypatch):
    ajeno = commit_en(remoto, tmp_path, {"supervisor/politica.py": ARREGLO}, "Un cambio de una persona")
    api = Api()
    api.commit = {"commit": {"author": {"name": "Otra Persona"}, "message": "Un cambio de una persona"}}
    api.corridas = [_corrida(9, "failure", ajeno)]
    monkeypatch.setattr(reparar, "Cliente", lambda *a, **k: api)
    assert reparar.verificar(_args_verificar(tmp_path, ajeno), cadenas, AHORA, dormir=lambda s: None, reloj=lambda: AHORA) == 1
    assert cabeza(remoto) == ajeno and "**No se pudo revertir**" in api.comentarios()[-1]
    with pytest.raises(reparar.Descarte, match="sha completo"):
        reparar.verificar(_args_verificar(tmp_path, "abc1234"), cadenas, AHORA)


# --------------------------------------------------------------------------
# El workflow: lo que el modo propio promete, leído del YAML
# --------------------------------------------------------------------------

def test_el_workflow_no_le_da_al_arreglo_mas_que_un_token_de_lectura():
    import yaml
    wf = yaml.safe_load((RAIZ / ".github/workflows/reparar-infra.yml").read_text(encoding="utf-8"))
    jobs = wf["jobs"]
    # Donde corre código del agente o del arreglo no hay secretos de la organización ni token que escriba.
    assert jobs["compuertas"]["permissions"] == {"actions": "read", "contents": "read"}
    assert cp.secretos(jobs["compuertas"]) == {"github.token"}
    assert cp.secretos(jobs["agente"]) == {"secrets.CLAUDE_CODE_OAUTH_TOKEN", "secrets.EGRESS_MX_TOKEN"}
    assert jobs["agente"]["permissions"] == {} and jobs["juez"]["permissions"] == {}
    # Todo lo determinista sale del mismo código: el de main o el de la última corrida buena.
    for nombre in ("agente", "compuertas", "juez", "publicar", "verificar"):
        checkout = next(p for p in jobs[nombre]["steps"] if str(p.get("uses", "")).startswith("actions/checkout"))
        assert checkout["with"]["ref"] == "${{ needs.preparar.outputs.codigo }}", nombre
    # El que revierte espera al que publica, y solo corre al reparar al supervisor con algo publicado.
    assert jobs["verificar"]["needs"] == ["preparar", "publicar"]
    assert "inputs.huella == 'supervisor'" in jobs["verificar"]["if"] and "publicado != ''" in jobs["verificar"]["if"]
    # El contrato con el Worker scheduler: estos inputs existen.
    assert {"huella", "repo", "workflow", "corrida", "origen"} <= set(wf[True]["workflow_dispatch"]["inputs"])
