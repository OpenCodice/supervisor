import json
from datetime import timedelta

from conftest import AHORA

from supervisor.estado import Estado
from supervisor.invariantes import iso


def test_solo_se_agrega_y_vale_la_ultima_linea(tmp_path):
    e = Estado(tmp_path)
    assert not e.cambiado and not e.guardar()
    assert e.anotar_huella({"huella": "a1", "estado": "abierta", "issue": None})
    assert not e.anotar_huella({"huella": "a1", "estado": "abierta"})       # sin cambios no hay línea
    e.anotar_accion({"cuando": iso(AHORA), "accion": "lanzar"})
    assert e.guardar()
    e = Estado(tmp_path)
    assert e.anotar_huella({"huella": "a1", "estado": "resuelta"})
    e.guardar()
    lineas = (tmp_path / "huellas.jsonl").read_text().splitlines()
    assert [json.loads(l)["estado"] for l in lineas] == ["abierta", "resuelta"]
    e = Estado(tmp_path)
    assert e.huellas["a1"]["estado"] == "resuelta" and len(e.acciones) == 1


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
