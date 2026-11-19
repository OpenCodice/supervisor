import json
import shutil
import subprocess
from datetime import timedelta

from conftest import AHORA, RAIZ, t

from supervisor.estado import Estado
from supervisor.invariantes import iso


def _lineas(carpeta, nombre):
    return [json.loads(l) for l in (carpeta / nombre).read_text().splitlines()]


def test_huellas_es_una_foto_y_la_bitacora_solo_crece(tmp_path):
    e = Estado(tmp_path)
    assert not e.cambiado and not e.guardar()
    assert e.anotar_huella({"huella": "a1", "estado": "abierta", "issue": None})
    assert not e.anotar_huella({"huella": "a1", "estado": "abierta"})       # sin cambios no se escribe
    e.anotar_accion({"cuando": iso(AHORA), "accion": "lanzar"})
    assert e.guardar()
    e = Estado(tmp_path)
    assert e.anotar_huella({"huella": "a1", "estado": "resuelta"})
    assert e.anotar_huella({"huella": "b2", "estado": "abierta"})
    e.anotar_accion({"cuando": iso(AHORA), "accion": "agotada"})
    e.guardar()
    # Una línea por huella, la vigente, en el orden en que aparecieron.
    assert [(l["huella"], l["estado"]) for l in _lineas(tmp_path, "huellas.jsonl")] == [("a1", "resuelta"), ("b2", "abierta")]
    assert [l["accion"] for l in _lineas(tmp_path, "acciones.jsonl")] == ["lanzar", "agotada"]
    assert not list(tmp_path.glob(".*.tmp"))
    e = Estado(tmp_path)
    assert e.huellas["a1"]["estado"] == "resuelta" and len(e.acciones) == 2


def test_latido_a_lo_mas_una_vez_al_dia_sin_novedades(tmp_path):
    e = Estado(tmp_path)
    latido = {"cuando": iso(AHORA), "invariantes": {"I1": "ok"}, "huellas_abiertas": 0}
    assert e.anotar_latido(latido, AHORA)
    e.guardar()
    e = Estado(tmp_path)
    assert not e.anotar_latido(latido | {"cuando": iso(AHORA + timedelta(hours=5))}, AHORA + timedelta(hours=5))
    assert not e.cambiado
    assert e.anotar_latido(latido, AHORA + timedelta(hours=25))
    assert e.anotar_latido(latido | {"invariantes": {"I1": "rota:1"}}, AHORA + timedelta(hours=26))
    e.guardar()
    assert json.loads((tmp_path / "latido.json").read_text())["invariantes"] == {"I1": "rota:1"}


def _escribir(carpeta, nombre, registros):
    (carpeta / nombre).write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in registros))


def test_compactar_migra_el_archivo_que_solo_crecia(tmp_path):
    """El formato anterior: una línea por cada cambio de la huella. Queda la vigente de cada una."""
    cita = {"huella": "c1", "invariante": "I1", "objeto": "congreso-extractor/actualizar-congreso.yml@x"}
    falla = {"huella": "f1", "invariante": "I2", "firma": "exit code 1", "paso": "Bajar"}
    _escribir(tmp_path, "huellas.jsonl", [
        cita | {"estado": "abierta"}, falla | {"estado": "abierta"},
        cita | {"estado": "resuelta", "resuelta": t(1)}, falla | {"estado": "resuelta", "resuelta": t(1)},
        {"huella": "v1", "invariante": "I2", "firma": "x", "estado": "abierta"}])
    e = Estado(tmp_path)
    assert e.compactar(AHORA, 30) == (1, 0) and e.guardar()
    # La cita resuelta no dejó rastro y sale; la falla de un paso se conserva su ventana.
    assert [(l["huella"], l["estado"]) for l in _lineas(tmp_path, "huellas.jsonl")] == [("f1", "resuelta"), ("v1", "abierta")]
    e = Estado(tmp_path)
    assert e.compactar(AHORA, 30) == (0, 0) and not e.cambiado


def test_compactar_recorta_por_ventana_sin_tocar_lo_que_la_politica_lee(tmp_path):
    dias = lambda n: t(24 * n)  # noqa: E731
    _escribir(tmp_path, "huellas.jsonl", [
        {"huella": "vieja", "invariante": "I2", "firma": "x", "estado": "resuelta", "resuelta": dias(31), "issue": 4},
        {"huella": "reciente", "invariante": "I2", "firma": "x", "estado": "resuelta", "resuelta": dias(29)},
        # Resuelta hace mucho, pero la bitácora todavía guarda un evento de su reparación.
        {"huella": "reparada", "invariante": "I2", "firma": "x", "estado": "resuelta", "resuelta": dias(40),
         "evento_visto": dias(20)},
        {"huella": "agotada", "invariante": "I2", "firma": "x", "estado": "agotada", "ultima": dias(90), "issue": 9},
        {"huella": "con-issue", "invariante": "I9", "estado": "resuelta", "resuelta": dias(2), "issue": 5}])
    _escribir(tmp_path, "acciones.jsonl", [
        {"cuando": dias(45), "accion": "lanzar", "huella": "vieja"},
        {"cuando": dias(31), "accion": "intento-iniciado", "huella": "vieja"},
        {"cuando": dias(20), "accion": "descartado", "huella": "reparada"},
        {"cuando": dias(20), "accion": "descartado", "huella": "reparada"},
        {"cuando": dias(1), "accion": "agente", "huella": "agotada", "resultado": "ok"}])
    e = Estado(tmp_path)
    acciones = e.acciones
    assert e.compactar(AHORA, 30) == (1, 3)
    assert sorted(e.huellas) == ["agotada", "con-issue", "reciente", "reparada"]
    # La lista que ya tenía quien llamó es la recortada, sin la línea repetida.
    assert [a["accion"] for a in acciones] == ["descartado", "agente"]
    e.anotar_accion({"cuando": iso(AHORA), "accion": "lanzar", "huella": "z"})
    assert e.guardar()
    assert [l["accion"] for l in _lineas(tmp_path, "acciones.jsonl")] == ["descartado", "agente", "lanzar"]
    assert len(_lineas(tmp_path, "huellas.jsonl")) == 4


def test_una_linea_vieja_que_vuelve_al_archivo_no_llega_a_la_politica(tmp_path):
    """Si un merge devolviera a la bitácora una línea ya recortada, se recorta otra vez al leer."""
    vigente = {"cuando": t(2), "accion": "lanzar", "huella": "a"}
    _escribir(tmp_path, "acciones.jsonl", [vigente])
    e = Estado(tmp_path)
    assert e.compactar(AHORA, 30) == (0, 0) and not e.cambiado
    with open(tmp_path / "acciones.jsonl", "a") as f:
        f.write(json.dumps({"cuando": t(24 * 60), "accion": "arreglo-publicado", "huella": "a"}) + "\n")
        f.write(json.dumps(vigente, sort_keys=True) + "\n")
    e = Estado(tmp_path)
    e.compactar(AHORA, 30)
    assert e.acciones == [vigente] and e.guardar()
    assert _lineas(tmp_path, "acciones.jsonl") == [vigente]


def test_sin_compactar_solo_se_agrega_a_la_bitacora(tmp_path):
    """reparar-infra anota una línea y no toca nada más, aunque haya qué recortar."""
    _escribir(tmp_path, "huellas.jsonl", [{"huella": "a", "estado": "abierta"}, {"huella": "a", "estado": "resuelta"}])
    _escribir(tmp_path, "acciones.jsonl", [{"cuando": t(24 * 90), "accion": "lanzar"}])
    antes = (tmp_path / "huellas.jsonl").read_text()
    e = Estado(tmp_path)
    e.anotar_accion({"cuando": iso(AHORA), "accion": "intento-iniciado", "huella": "a"})
    assert e.guardar()
    assert (tmp_path / "huellas.jsonl").read_text() == antes
    assert [l["accion"] for l in _lineas(tmp_path, "acciones.jsonl")] == ["lanzar", "intento-iniciado"]


def _git(repo, *args):
    orden = ["git", "-c", "user.name=prueba", "-c", "user.email=prueba@example.invalid", "-c", "commit.gpgsign=false",
             "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args]
    return subprocess.run(orden, capture_output=True, text=True, check=True).stdout


def _repo_con_estado(tmp_path, acciones, huellas):
    repo = tmp_path / "repo"
    (repo / "estado").mkdir(parents=True)
    shutil.copy(RAIZ / ".gitattributes", repo / ".gitattributes")
    _escribir(repo / "estado", "acciones.jsonl", acciones)
    _escribir(repo / "estado", "huellas.jsonl", huellas)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _vuelta_y_reparacion_a_la_vez(repo):
    """El reconciliador compacta y anota; mientras, reparar-infra agrega su línea y llega antes a main."""
    _git(repo, "checkout", "-q", "-b", "reparar")
    e = Estado(repo / "estado")
    e.anotar_accion({"cuando": iso(AHORA), "accion": "intento-iniciado", "huella": "f1"})
    e.guardar()
    _git(repo, "commit", "-q", "-am", "reparar-infra")
    _git(repo, "checkout", "-q", "main")
    e = Estado(repo / "estado")
    e.compactar(AHORA, 30)
    e.anotar_huella({"huella": "f1", "invariante": "I2", "firma": "x", "estado": "en-reparacion"})
    e.anotar_accion({"cuando": iso(AHORA), "accion": "agente", "huella": "f1", "resultado": "ok"})
    e.guardar()
    _git(repo, "commit", "-q", "-am", "Estado del supervisor")
    # Lo mismo que hace `Guardar el estado` de supervisor.yml.
    _git(repo, "rebase", "-q", "reparar")
    leido = Estado(repo / "estado")
    leido.compactar(AHORA, 30)
    return leido


def test_el_rebase_del_estado_no_resucita_lo_recortado(tmp_path):
    viejas = [{"cuando": t(24 * (60 - n)), "accion": "lanzar", "huella": f"v{n}"} for n in range(20)]
    recientes = [{"cuando": t(24 * (20 - n)), "accion": "lanzar", "huella": f"r{n}"} for n in range(20)]
    repo = _repo_con_estado(tmp_path, viejas + recientes, [
        {"huella": "f1", "invariante": "I2", "firma": "x", "estado": "abierta"},
        {"huella": "c1", "invariante": "I1", "estado": "resuelta", "resuelta": t(1)}])
    leido = _vuelta_y_reparacion_a_la_vez(repo)
    assert [a["huella"] for a in _lineas(repo / "estado", "acciones.jsonl")] == [
        *[f"r{n}" for n in range(20)], "f1", "f1"]
    assert {a["accion"] for a in leido.acciones if a["huella"] == "f1"} == {"intento-iniciado", "agente"}
    assert [(l["huella"], l["estado"]) for l in _lineas(repo / "estado", "huellas.jsonl")] == [("f1", "en-reparacion")]


def test_si_el_rebase_devuelve_lineas_viejas_la_lectura_las_descarta(tmp_path):
    """Con toda la bitácora fuera de la ventana, el recorte y la línea nueva caen en el mismo trozo
    y `merge=union` conserva los dos lados: lo viejo vuelve al archivo, pero no a la memoria."""
    viejas = [{"cuando": t(24 * (60 - n)), "accion": "arreglo-publicado", "huella": "f1"} for n in range(3)]
    repo = _repo_con_estado(tmp_path, viejas, [{"huella": "f1", "invariante": "I2", "firma": "x", "estado": "abierta"}])
    leido = _vuelta_y_reparacion_a_la_vez(repo)
    assert sorted(a["accion"] for a in leido.acciones) == ["agente", "intento-iniciado"]
    assert leido.guardar() or len(_lineas(repo / "estado", "acciones.jsonl")) == 2
    assert sorted(a["accion"] for a in _lineas(repo / "estado", "acciones.jsonl")) == ["agente", "intento-iniciado"]
