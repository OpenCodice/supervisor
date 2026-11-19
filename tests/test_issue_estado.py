"""El issue de una huella dice quién la tiene: una etiqueta de estado y, tras cada intento, qué sigue."""

from supervisor import reparar
from supervisor.actuar import ETIQUETA

from conftest import ClienteFalso

REPO = "repos/OpenCodice/supervisor"


class ConIssues(ClienteFalso):
    def __init__(self, abiertos):
        super().__init__()
        self.abiertos = abiertos

    def paginas(self, ruta, clave=None, **params):
        return self.abiertos if ruta.endswith("/issues") else []


def test_el_resultado_llega_al_issue_aunque_el_registro_aun_no_lo_traiga(cadenas):
    cliente = ConIssues([{"number": 7, "title": "[aaaa11112222] agente: x/y.yml#1"},
                         {"number": 8, "title": "[bbbb11112222] agente: x/z.yml#2"}])
    reparar.comentar(cliente, cadenas, {"huella": "bbbb11112222"}, "**Intento descartado**: no pudo", "esperando-reintento")
    assert cliente.llamadas == [
        ("POST", f"{REPO}/issues/8/comments", {"body": "**Intento descartado**: no pudo"}),
        ("PATCH", f"{REPO}/issues/8", {"labels": [ETIQUETA, "esperando-reintento"]})]


def test_sin_issue_no_se_escribe_en_ninguno(cadenas, capsys):
    cliente = ConIssues([{"number": 7, "title": "[aaaa11112222] agente: x/y.yml#1"}])
    reparar.comentar(cliente, cadenas, {"huella": "bbbb11112222"}, "texto", "esperando-reintento")
    assert cliente.llamadas == [] and "no tiene issue" in capsys.readouterr().out


def test_un_descarte_dice_que_sigue(cadenas):
    assert "no hay otro intento automático" in reparar.lo_que_sigue(True, cadenas)
    sigue = reparar.lo_que_sigue(False, cadenas)
    assert "siguiente vuelta" in sigue and str(cadenas.politica.get("intentos_agente_por_huella", 2)) in sigue


def test_la_constancia_lleva_la_etiqueta_de_su_estado(cadenas, foto_sana, mundo):
    from test_politica_reparacion import test_con_el_agente_apagado_solo_queda_constancia_y_al_encenderlo_la_toma as caso  # noqa: F401
    etiquetas = []
    original = mundo.cliente.post

    def post(ruta, cuerpo=None):
        if ruta.endswith("/issues"):
            etiquetas.append(cuerpo["labels"])
        return original(ruta, cuerpo)

    mundo.cliente.post = post
    caso(cadenas, foto_sana, mundo, mundo.carpeta.parent)
    assert etiquetas and etiquetas[0] == [ETIQUETA, "agotada"]
    assert any(m == "PATCH" and (c or {}).get("labels") == [ETIQUETA, "en-reparacion"] for m, _, c in mundo.cliente.llamadas)
