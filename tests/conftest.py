"""Piezas comunes: fotos pequeñas armadas a mano o con corridas reales recortadas."""

from __future__ import annotations

import copy
import dataclasses
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from supervisor import cadenas as _cadenas  # noqa: E402
from supervisor.actuar import Actor  # noqa: E402
from supervisor.estado import Estado  # noqa: E402
from supervisor.invariantes import iso  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
AHORA = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def t(horas_antes: float = 0, base: datetime = AHORA) -> str:
    return iso(base - timedelta(hours=horas_antes))


def titulo(wf, **inputs) -> str:
    """run-name del contrato para un workflow declarado en cadenas.toml."""
    return wf.nombre + "".join(f" {k}=[{inputs.get(k, wf.valor_omision(k))}]" for k in wf.inputs)


_ids = iter(range(900_000, 10_000_000))


def corrida(archivo: str, conclusion: str | None = "success", hace_h: float = 1, dura_h: float = 0.1, *,
            status: str | None = None, titulo: str = "Corrida", ident: int | None = None, jobs=None,
            evento: str = "workflow_dispatch", sha: str = "a" * 40) -> dict:
    creada = AHORA - timedelta(hours=hace_h)
    c = {
        "id": ident or next(_ids), "name": archivo, "path": f".github/workflows/{archivo}",
        "status": status or ("completed" if conclusion else "in_progress"), "conclusion": conclusion,
        "display_title": titulo, "created_at": iso(creada), "updated_at": iso(creada + timedelta(hours=dura_h)),
        "run_started_at": iso(creada), "head_sha": sha, "event": evento, "run_attempt": 1,
    }
    if jobs is not None:
        c["jobs"] = jobs
    return c


def job(nombre: str, conclusion: str = "success", paso: str | None = None, log: list[str] | None = None) -> dict:
    pasos = [{"name": "Set up job", "conclusion": "success"}]
    if paso:
        pasos.append({"name": paso, "conclusion": conclusion})
    j = {"id": next(_ids), "name": nombre, "status": "completed", "conclusion": conclusion, "steps": pasos}
    if log is not None:
        j["log"] = log
    return j


def falla(job_nombre: str, paso: str, *lineas: str) -> list[dict]:
    """Jobs de una corrida que falló en ese paso con ese final de log."""
    return [job("planear"), job(job_nombre, "failure", paso, [*lineas, "##[error]Process completed with exit code 1."])]


@pytest.fixture(scope="session")
def cadenas():
    # Las pruebas no dependen de qué interruptores están encendidos hoy en
    # cadenas.toml: I1–I8 e I11 encendidos; I9 e I10 se prueban llamándolos.
    c = _cadenas.cargar()
    c.invariantes.update({k: k not in ("I9", "I10") for k in c.invariantes})
    # Ni de si el agente de infraestructura ya está encendido.
    c.reparacion["activa"] = True
    # Ni de la hora: lo que toca por horario se prueba encendiéndolo (`con_horario`).
    return dataclasses.replace(c, workflows=tuple(dataclasses.replace(w, lanza_horario=False) for w in c.workflows))


def con_horario(cadenas, *archivos: str, sin_cadencias: bool = True):
    """Las mismas cadenas con el horario encendido en esos workflows (y, por omisión, sin cadencias)."""
    return dataclasses.replace(cadenas, workflows=tuple(
        dataclasses.replace(w, lanza_horario=w.archivo in archivos, cadencias=() if sin_cadencias else w.cadencias)
        for w in cadenas.workflows))


@pytest.fixture(scope="session")
def reales() -> dict:
    return json.loads((FIXTURES / "corridas_reales.json").read_text(encoding="utf-8"))


@pytest.fixture
def real(reales):
    def _real(clave: str, **cambios) -> dict:
        return copy.deepcopy(reales[clave]) | cambios
    return _real


@pytest.fixture
def foto_sana(cadenas):
    """Todo en orden: cada workflow con cadencia tiene una corrida exitosa reciente y no hay nada abierto."""
    def _foto(*corridas_extra: tuple[str, dict], datos: dict | None = None, sin: tuple = ()) -> dict:
        foto = {"cuando": iso(AHORA), "org": "OpenCodice", "repos": {}, "datos": {}, "errores": [], "llamadas": 0,
                "credenciales": {"github": {"propio": True, "observado": True, "vence": None, "cabecera": None,
                                            "rechazos": []}}}
        for repo in cadenas.repos_con_workflows():
            foto["repos"][repo] = {"observable": True, "corridas": [], "ultima_exitosa": {}}
        for wf in cadenas.workflows:
            if (wf.repo, wf.archivo) in sin:
                continue
            for cad in wf.cadencias:
                foto["repos"][wf.repo]["corridas"].append(corrida(
                    wf.archivo, hace_h=min(72, cad["ventana_h"] / 2), titulo=titulo(wf, **cad.get("inputs", {})),
                    jobs=[job("vigilar"), job("estado pendiente=[false]")]))
        foto["repos"]["constitucion-web"]["corridas"].append(corrida("deploy.yml", hace_h=1, evento="push"))
        for d in cadenas.datos:
            foto["datos"][d["repo"]] = {"main": {"sha": "d" * 40, "fecha": t(30)}, "prs": []}
        for repo, c in corridas_extra:
            foto["repos"][repo]["corridas"].append(c)
        for repo, cambios in (datos or {}).items():
            foto["datos"][repo] |= cambios
        return foto
    return _foto


class ClienteFalso:
    """Registra lo que el supervisor le pediría a la API."""

    def __init__(self, falla_en: str | None = None):
        self.llamadas: list[tuple] = []
        self.falla_en = falla_en
        self.issues = 0

    def _anotar(self, metodo, ruta, cuerpo=None):
        self.llamadas.append((metodo, ruta, cuerpo))
        if self.falla_en and self.falla_en in ruta:
            from supervisor.observar import ErrorApi
            raise ErrorApi(422, metodo, ruta, "falla simulada")

    def post(self, ruta, cuerpo=None):
        self._anotar("POST", ruta, cuerpo)
        if ruta.endswith("/issues"):
            self.issues += 1
            return {"number": self.issues}
        return None

    def patch(self, ruta, cuerpo):
        self._anotar("PATCH", ruta, cuerpo)
        return {}

    def delete(self, ruta):
        self._anotar("DELETE", ruta)

    def paginas(self, ruta, clave=None, **params):
        return []

    def lanzamientos(self) -> list[tuple[str, dict]]:
        return [(r.split("/actions/workflows/")[0].split("/")[-1] + "/" + r.split("/workflows/")[1].split("/")[0],
                 c["inputs"]) for m, r, c in self.llamadas if r.endswith("/dispatches")]


@pytest.fixture
def mundo(cadenas, tmp_path):
    """Un supervisor en modo actuar contra un cliente falso, con su estado en disco."""
    from supervisor.__main__ import correr

    class Mundo:
        def __init__(self):
            self.cliente = ClienteFalso()
            self.carpeta = tmp_path / "estado"

        def vuelta(self, foto: dict, ahora: datetime = AHORA, modo: str = "actuar"):
            estado = Estado(self.carpeta)
            resultado = correr(foto, cadenas, estado, ahora, Actor(self.cliente, cadenas, modo))
            resultado["guardado"] = estado.guardar()
            resultado["estado"] = estado
            return resultado

        @staticmethod
        def de(resultado, invariante: str, contiene: str = ""):
            return [d for d in resultado["decisiones"]
                    if d.hallazgo is not None and d.hallazgo.invariante == invariante and contiene in d.hallazgo.objeto]

    return Mundo()
