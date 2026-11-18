"""La política alrededor de reparar-infra: lanzar al agente con topes, vigilar el arreglo y revertirlo."""

import json
from datetime import timedelta

from conftest import AHORA, corrida, falla, job, titulo

from supervisor.invariantes import iso

FE = "fiscal-extractor"
VLE = "vigilar-leyes-estatales.yml"
REPARAR = "supervisor/reparar-infra.yml"
PASO = "Reintentar las leyes con el extractor reparado"
LOG = ["bash: /home/runner/work/fiscal-extractor/fiscal-extractor/scripts/lanzar_vigilancia_estatal.sh: No such file or directory",
       "##[error]Process completed with exit code 127."]
COMMIT = "c" * 40


def rota(hace_h: float, wf, log=LOG, paso=PASO, **extra) -> dict:
    return corrida(VLE, "failure", hace_h=hace_h, titulo=titulo(wf),
                   jobs=[job("planear"), job("reparar", "failure", paso, list(log))], **extra)


def minutos(n: float):
    return AHORA + timedelta(minutes=n)


def anotar(mundo, accion: str, huella: str, cuando, **detalle) -> None:
    """Lo que reparar-infra deja en acciones.jsonl."""
    with open(mundo.carpeta / "acciones.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"cuando": iso(cuando), "accion": accion, "huella": huella, "detalle": detalle,
                            "resultado": "ok", "corrida_reparar": 1}) + "\n")


def reparaciones(mundo) -> list:
    """Lanzamientos de reparar-infra (con el paso de las horas la foto también pide otras cadencias)."""
    return [l for l in mundo.cliente.lanzamientos() if l[0] == REPARAR]


def huella_de(r, mundo):
    [d] = mundo.de(r, "I2")
    return d


def hasta_el_agente(cadenas, foto_sana, mundo):
    """Dos corridas seguidas con la misma falla determinista: la política lanza reparar-infra."""
    wf = cadenas.workflow(FE, VLE)
    base = [(FE, rota(5, wf)), (FE, rota(3, wf))]
    d = huella_de(mundo.vuelta(foto_sana(*base)), mundo)
    assert (d.clase, d.accion) == ("determinista", "agente")
    assert mundo.cliente.lanzamientos() == [(REPARAR, {"huella": d.huella, "origen": "supervisor"})]
    return wf, base, d.huella


def con_arreglo(cadenas, foto_sana, mundo, lanzada: bool = True):
    """…y reparar-infra publicó un arreglo: la huella queda `arreglo-publicado`."""
    wf, base, h = hasta_el_agente(cadenas, foto_sana, mundo)
    anotar(mundo, "intento-iniciado", h, minutos(5), repo=FE)
    anotar(mundo, "arreglo-publicado", h, minutos(20), repo=FE, workflow=VLE, commit=COMMIT,
           archivos=[f".github/workflows/{VLE}"], prueba={"lanzada": lanzada, "vence": iso(minutos(20 + 12 * 60))})
    r = mundo.vuelta(foto_sana(*base), minutos(30))
    reg = r["estado"].huellas[h]
    assert reg["estado"] == "arreglo-publicado" and reg["arreglo"]["commit"] == COMMIT and reg["intentos_agente"] == 1
    assert len(mundo.cliente.lanzamientos()) == 1
    return wf, base, h


def prueba(wf, conclusion: str, jobs, a_los_min: float = 25, **extra) -> tuple:
    return (FE, corrida(VLE, conclusion, hace_h=-a_los_min / 60, titulo=titulo(wf, origen="supervisor"), jobs=jobs, **extra))


# --------------------------------------------------------------------------
# Lanzar al agente
# --------------------------------------------------------------------------

def test_agente_lanza_reparar_infra_y_espera_su_resultado(cadenas, foto_sana, mundo):
    wf, base, h = hasta_el_agente(cadenas, foto_sana, mundo)
    reg = mundo.vuelta(foto_sana(*base), minutos(15))["estado"].huellas[h]
    assert (reg["estado"], reg["sesiones"], reg["issue"]) == ("en-reparacion", 1, 1)
    # Idempotencia: la misma foto no vuelve a lanzar ni a comentar.
    llamadas = len(mundo.cliente.llamadas)
    for m in (30, 45, 60):
        r = mundo.vuelta(foto_sana(*base), minutos(m))
        assert huella_de(r, mundo).accion == "esperar" and not r["guardado"]
    assert len(mundo.cliente.llamadas) == llamadas


def test_un_intento_descartado_deja_otro_y_al_segundo_se_agota(cadenas, foto_sana, mundo):
    wf, base, h = hasta_el_agente(cadenas, foto_sana, mundo)
    anotar(mundo, "intento-iniciado", h, minutos(5))
    anotar(mundo, "descartado", h, minutos(40), motivo="no pasó las compuertas: [poder] cambia `permissions`")
    r = mundo.vuelta(foto_sana(*base), minutos(45))
    assert r["estado"].huellas[h]["estado"] == "abierta" and "descartado" in r["estado"].huellas[h]["motivo"]
    d = huella_de(mundo.vuelta(foto_sana(*base), minutos(60)), mundo)
    assert d.accion == "agente" and len(reparaciones(mundo)) == 2

    anotar(mundo, "intento-iniciado", h, minutos(65))
    anotar(mundo, "descartado", h, minutos(90), motivo="el juez no lo aprueba")
    mundo.vuelta(foto_sana(*base), minutos(95))
    r = mundo.vuelta(foto_sana(*base), minutos(140))
    d = huella_de(r, mundo)
    assert (d.accion, d.motivo) == ("agotada", "se agotaron los intentos del agente")
    assert r["estado"].huellas[h]["intentos_agente"] == 2 and len(reparaciones(mundo)) == 2
    # Agotada, ya no insiste.
    assert huella_de(mundo.vuelta(foto_sana(*base), minutos(155)), mundo).accion == "nada"


def test_no_es_infra_agota_sin_segundo_intento(cadenas, foto_sana, mundo):
    wf, base, h = hasta_el_agente(cadenas, foto_sana, mundo)
    anotar(mundo, "intento-iniciado", h, minutos(5))
    anotar(mundo, "descartado", h, minutos(40), motivo="el agente dice que no es infraestructura: falta un secreto", definitivo=True)
    r = mundo.vuelta(foto_sana(*base), minutos(45))
    d = huella_de(r, mundo)
    assert d.accion == "agotada" and "no es infraestructura" in d.motivo
    assert huella_de(mundo.vuelta(foto_sana(*base), minutos(90)), mundo).accion == "nada"
    assert len(reparaciones(mundo)) == 1


def test_tope_diario_y_una_sola_reparacion_a_la_vez(cadenas, foto_sana, mundo):
    wf = cadenas.workflow(FE, VLE)
    base = [(FE, rota(5, wf)), (FE, rota(3, wf))]
    # Otra huella, en otro workflow, con las mismas dos fallas.
    wf2 = cadenas.workflow(FE, "vigilar-federal.yml")
    otra = [(FE, corrida(wf2.archivo, "failure", hace_h=h, titulo=titulo(wf2),
                         jobs=falla("cerrar", "Publicar", "bash: x.sh: No such file or directory"))) for h in (5, 3)]
    r = mundo.vuelta(foto_sana(*base, *otra))
    acciones = sorted(d.accion for d in mundo.de(r, "I2"))
    assert acciones == ["agente", "esperar"]
    assert [d.motivo for d in mundo.de(r, "I2") if d.accion == "esperar"] == ["ya hay una reparación de infraestructura en curso"]
    # Con una corrida de reparar-infra viva, tampoco.
    viva = ("supervisor", corrida("reparar-infra.yml", None, hace_h=-0.3, status="in_progress"))
    r = mundo.vuelta(foto_sana(*base, *otra, viva), minutos(40))
    assert sorted(d.accion for d in mundo.de(r, "I2")) == ["esperar", "esperar"]
    assert len(mundo.cliente.lanzamientos()) == 1


def test_tope_de_sesiones_por_dia(cadenas, foto_sana, mundo):
    wf = cadenas.workflow(FE, VLE)
    mundo.carpeta.mkdir(parents=True, exist_ok=True)
    for n in range(3):
        anotar(mundo, "agente", f"otra{n}", minutos(-600 + n), repo="supervisor", workflow="reparar-infra.yml")
    d = huella_de(mundo.vuelta(foto_sana((FE, rota(5, wf)), (FE, rota(3, wf)))), mundo)
    assert (d.accion, d.motivo) == ("esperar", "tope diario de sesiones de agente")
    assert mundo.cliente.lanzamientos() == []


def test_fuera_de_alcance_queda_agotada_sin_gastar_sesion(cadenas, foto_sana, mundo):
    rojas = [("constitucion-web", corrida("otro.yml", "failure", hace_h=h, evento="push",
                                          jobs=falla("build", "Construir", "bash: x: command not found"))) for h in (3, 2, 1)]
    r = mundo.vuelta(foto_sana(*rojas))
    [d] = mundo.de(r, "I11")
    assert (d.accion, d.motivo) == ("agotada", "constitucion-web está fuera del alcance del agente de infraestructura")
    assert mundo.cliente.lanzamientos() == [] and mundo.cliente.issues == 1
    # El propio supervisor tampoco se repara a sí mismo.
    rojas = [("supervisor", corrida("tests.yml", "failure", hace_h=h, evento="push", jobs=falla("pruebas", "pytest", "1 failed")))
             for h in (3, 2, 1)]
    [d] = mundo.de(mundo.vuelta(foto_sana(*rojas)), "I11")
    assert d.accion == "agotada" and "fuera del alcance" in d.motivo and mundo.cliente.lanzamientos() == []


# --------------------------------------------------------------------------
# Vigilar el arreglo
# --------------------------------------------------------------------------

def test_la_prueba_pasa_el_paso_y_la_huella_queda_resuelta(cadenas, foto_sana, mundo):
    wf, base, h = con_arreglo(cadenas, foto_sana, mundo)
    # Mientras la prueba corre y todavía no llega al paso: se espera.
    en_curso = prueba(wf, None, [job("planear")], status="in_progress")
    assert huella_de(mundo.vuelta(foto_sana(*base, en_curso), minutos(45)), mundo).accion == "esperar"
    # Pasa el paso de la huella aunque la corrida falle después por otra cosa.
    despues = prueba(wf, "failure", [job("reparar", "success", PASO), job("salud", "failure", "Avisar", ["otra cosa", "##[error]x"])])
    r = mundo.vuelta(foto_sana(*base, despues), minutos(90))
    reg = r["estado"].huellas[h]
    assert reg["estado"] == "resuelta"
    assert ("PATCH", "repos/OpenCodice/supervisor/issues/1", {"state": "closed"}) in mundo.cliente.llamadas
    assert len(reparaciones(mundo)) == 1                                # ni otro agente ni un revert
    # Lo de después es otra huella, con su propio reintento.
    otra = [d for d in mundo.de(r, "I2") if d.huella != h]
    assert [d.hallazgo.falla["paso"] for d in otra] == ["Avisar"]


def test_misma_firma_revierte_y_cuenta_el_intento(cadenas, foto_sana, mundo):
    wf, base, h = con_arreglo(cadenas, foto_sana, mundo)
    igual = prueba(wf, "failure", [job("planear"), job("reparar", "failure", PASO, list(LOG))])
    r = mundo.vuelta(foto_sana(*base, igual), minutos(60))
    d = huella_de(r, mundo)
    assert d.accion == "revertir" and "mismo paso con la misma firma" in d.motivo
    assert reparaciones(mundo)[-1] == (REPARAR, {"huella": h, "revertir": COMMIT, "origen": "supervisor"})
    # Mientras el revert corre no se pide otra vez.
    assert huella_de(mundo.vuelta(foto_sana(*base, igual), minutos(75)), mundo).accion == "esperar"
    assert len(reparaciones(mundo)) == 2

    anotar(mundo, "revertido", h, minutos(80), repo=FE, commit=COMMIT, commit_revert="d" * 40)
    reg = mundo.vuelta(foto_sana(*base, igual), minutos(90))["estado"].huellas[h]
    assert (reg["estado"], reg["intentos_agente"]) == ("revertido", 1) and "arreglo" not in reg
    # Le queda un intento: la siguiente sesión.
    d = huella_de(mundo.vuelta(foto_sana(*base, igual), minutos(125)), mundo)
    assert d.accion == "agente" and reparaciones(mundo)[-1] == (REPARAR, {"huella": h, "origen": "supervisor"})
    assert len(reparaciones(mundo)) == 3


def test_falla_antes_del_paso_en_lo_que_toco_revierte_y_cuenta_doble(cadenas, foto_sana, mundo):
    wf, base, h = con_arreglo(cadenas, foto_sana, mundo)
    antes = prueba(wf, "failure", [job("planear", "failure", "Planear", ["yaml: unexpected", "##[error]Process completed with exit code 1."])])
    r = mundo.vuelta(foto_sana(*base, antes), minutos(60))
    d = next(x for x in mundo.de(r, "I2") + [x for x in r["decisiones"] if x.hallazgo is None] if x.huella == h)
    assert d.accion == "revertir" and "antes del paso" in d.motivo
    anotar(mundo, "revertido", h, minutos(80), repo=FE, commit=COMMIT, commit_revert="d" * 40)
    r = mundo.vuelta(foto_sana(*base, antes), minutos(90))
    reg = r["estado"].huellas[h]
    assert (reg["estado"], reg["intentos_agente"], reg["intentos_extra"]) == ("revertido", 2, 1)


def test_ya_no_arranca_revierte(cadenas, foto_sana, mundo):
    wf, base, h = con_arreglo(cadenas, foto_sana, mundo)
    r = mundo.vuelta(foto_sana(*base, prueba(wf, "startup_failure", None)), minutos(60))
    d = next(x for x in r["decisiones"] if x.huella == h)
    assert d.accion == "revertir" and "startup_failure" in d.motivo and d.efecto["reversion"]["doble"] is True


def test_una_falla_de_datos_antes_del_paso_no_revierte(cadenas, foto_sana, mundo):
    wf, base, h = con_arreglo(cadenas, foto_sana, mundo)
    compuerta = wf.compuertas[0]["paso"]
    datos = prueba(wf, "failure", [job("planear", "failure", compuerta, ["x", "##[error]Process completed with exit code 1."])])
    r = mundo.vuelta(foto_sana(*base, datos), minutos(60))
    assert next(x for x in r["decisiones"] if x.huella == h).accion == "esperar"


def test_sin_resultado_en_12_horas_revierte(cadenas, foto_sana, mundo):
    wf, base, h = con_arreglo(cadenas, foto_sana, mundo)
    # Una corrida que termina bien sin llegar a ejecutar el paso no prueba nada.
    sin_paso = prueba(wf, "success", [job("planear"), job("estado pendiente=[false]")])
    r = mundo.vuelta(foto_sana(*base, sin_paso), minutos(11 * 60))
    assert next(x for x in r["decisiones"] if x.huella == h).accion == "esperar"
    r = mundo.vuelta(foto_sana(*base, sin_paso), minutos(20 + 12 * 60 + 1))
    d = next(x for x in r["decisiones"] if x.huella == h)
    assert d.accion == "revertir" and "sin resultado en 12 h" in d.motivo and d.efecto["reversion"]["doble"] is False


def test_a_las_12_horas_una_prueba_viva_todavia_cuenta(cadenas, foto_sana, mundo):
    wf, base, h = con_arreglo(cadenas, foto_sana, mundo)
    viva = prueba(wf, None, [job("planear")], a_los_min=11.5 * 60, status="in_progress")
    r = mundo.vuelta(foto_sana(*base, viva), minutos(20 + 12 * 60 + 1))
    assert next(x for x in r["decisiones"] if x.huella == h).accion == "esperar"


def test_si_nadie_lanzo_la_prueba_la_lanza_el_reconciliador(cadenas, foto_sana, mundo):
    wf, base, h = con_arreglo(cadenas, foto_sana, mundo, lanzada=False)
    d = huella_de(mundo.vuelta(foto_sana(*base), minutos(55)), mundo)
    assert d.accion == "lanzar" and mundo.cliente.lanzamientos()[-1] == (f"{FE}/{VLE}", {"origen": "supervisor"})
    assert huella_de(mundo.vuelta(foto_sana(*base), minutos(70)), mundo).accion == "esperar"
    assert len(mundo.cliente.lanzamientos()) == 2


def test_una_reversion_que_no_es_limpia_queda_agotada(cadenas, foto_sana, mundo):
    wf, base, h = con_arreglo(cadenas, foto_sana, mundo)
    igual = prueba(wf, "failure", [job("planear"), job("reparar", "failure", PASO, list(LOG))])
    mundo.vuelta(foto_sana(*base, igual), minutos(60))
    anotar(mundo, "reversion-fallida", h, minutos(70), repo=FE, commit=COMMIT, motivo="main cambió las mismas líneas")
    r = mundo.vuelta(foto_sana(*base, igual), minutos(75))
    d = huella_de(r, mundo)
    assert d.accion == "agotada" and "no se pudo revertir" in d.motivo
    assert huella_de(mundo.vuelta(foto_sana(*base, igual), minutos(90)), mundo).accion == "nada"
    # Ni con otra corrida fallida le toca otra sesión: el arreglo sigue en main sin revertir.
    otra = prueba(wf, "failure", [job("planear"), job("reparar", "failure", PASO, list(LOG))], a_los_min=100)
    assert huella_de(mundo.vuelta(foto_sana(*base, igual, otra), minutos(150)), mundo).accion == "agotada"
    assert len(reparaciones(mundo)) == 2


def test_un_arreglo_publicado_a_mano_tambien_se_vigila(cadenas, foto_sana, mundo):
    """reparar-infra lanzado a mano: la huella ya se había dado por resuelta y aun así se vigila el commit."""
    wf = cadenas.workflow(FE, VLE)
    una = [(FE, rota(5, wf))]
    h = huella_de(mundo.vuelta(foto_sana(*una)), mundo).huella
    buena = (FE, corrida(VLE, "success", hace_h=-0.1, titulo=titulo(wf), jobs=[job("planear"), job("estado pendiente=[false]")]))
    assert mundo.vuelta(foto_sana(*una, buena), minutos(10))["estado"].huellas[h]["estado"] == "resuelta"
    anotar(mundo, "arreglo-publicado", h, minutos(20), repo=FE, workflow=VLE, commit=COMMIT,
           archivos=[f".github/workflows/{VLE}"], prueba={"lanzada": False})
    assert mundo.vuelta(foto_sana(*una, buena), minutos(30))["estado"].huellas[h]["estado"] == "arreglo-publicado"
    pasa = prueba(wf, "success", [job("reparar", "success", PASO)], a_los_min=40)
    assert mundo.vuelta(foto_sana(*una, buena, pasa), minutos(60))["estado"].huellas[h]["estado"] == "resuelta"
    # Resuelta otra vez, la bitácora ya leída no la reabre.
    r = mundo.vuelta(foto_sana(*una, buena, pasa), minutos(75))
    assert r["estado"].huellas[h]["estado"] == "resuelta" and not r["guardado"]


def test_idempotencia_con_un_arreglo_en_prueba(cadenas, foto_sana, mundo):
    wf, base, h = con_arreglo(cadenas, foto_sana, mundo)
    llamadas = len(mundo.cliente.llamadas)
    huellas = (mundo.carpeta / "huellas.jsonl").read_text()
    for m in (31, 40, 45):
        r = mundo.vuelta(foto_sana(*base), minutos(m))
        assert huella_de(r, mundo).accion == "esperar"
    assert len(mundo.cliente.llamadas) == llamadas
    assert (mundo.carpeta / "huellas.jsonl").read_text() == huellas


def test_los_jobs_de_las_corridas_de_prueba_se_piden_aunque_sigan_en_curso(cadenas, foto_sana):
    from supervisor import invariantes as inv
    wf = cadenas.workflow(FE, VLE)
    vieja = corrida(VLE, "success", hace_h=30, titulo=titulo(wf), ident=11)
    viva = corrida(VLE, None, hace_h=0.2, titulo=titulo(wf), status="in_progress", ident=12)
    foto = foto_sana((FE, vieja), (FE, viva), sin=((FE, VLE),))
    for c in foto["repos"][FE]["corridas"]:
        c.pop("jobs", None)
    assert 12 not in inv.necesitan_jobs(foto, cadenas)[FE]
    pedidos = inv.necesitan_jobs(foto, cadenas, ((FE, VLE, iso(AHORA - timedelta(hours=1))),))[FE]
    assert 12 in pedidos and 11 in inv.necesitan_jobs(foto, cadenas)[FE]
