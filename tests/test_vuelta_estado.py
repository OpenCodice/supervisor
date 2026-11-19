"""Una vuelta completa de `correr` le pasa a la política las huellas del estado."""

from conftest import AHORA

from supervisor.__main__ import correr
from supervisor.actuar import Actor
from supervisor.estado import Estado


def test_la_vuelta_pasa_las_huellas_del_estado_a_la_politica(cadenas, foto_sana, mundo):
    estado = Estado(mundo.carpeta)
    r = correr(foto_sana(), cadenas, estado, AHORA, Actor(mundo.cliente, cadenas, "informe"))
    assert r["decisiones"] == []
