"""Vueltas completas (invariantes → política → acciones → estado) contra un cliente falso."""

from datetime import timedelta

from conftest import AHORA, con_horario, corrida, falla, job, t, titulo

from supervisor import invariantes as inv
from supervisor import politica as pol
from supervisor.invariantes import instante, iso

FE = "fiscal-extractor"
CE = "constitucion-extractor"
VLE = "vigilar-leyes-estatales.yml"
DESTINO = f"{FE}/{VLE}"
REPARAR = "supervisor/reparar-infra.yml"


def _acciones(resultado, invariante=None):
    return [(d.hallazgo.invariante if d.hallazgo else None, d.accion) for d in resultado["decisiones"]
            if d.accion != "nada" and (invariante is None or (d.hallazgo and d.hallazgo.invariante == invariante))]


# (a) exit 127: determinista; la primera vez se reintenta, a la segunda va al agente.
def test_exit_127_reintento_y_despues_agente(cadenas, foto_sana, real, mundo):
    wf = cadenas.workflow(FE, VLE)
    primera = real("fiscal-extractor#44246222455", display_title=titulo(wf, origen="continuar"))
    r = mundo.vuelta(foto_sana((FE, primera)), instante("2026-11-18T06:45:00Z"))
    [d] = mundo.de(r, "I2")
    assert (d.clase, d.accion) == ("determinista", "lanzar")
    assert mundo.cliente.lanzamientos() == [(DESTINO, {"origen": "supervisor"})]
    assert mundo.cliente.llamadas[0][2]["ref"] == "main"

    segunda = real("fiscal-extractor#44247025218", display_title=titulo(wf, origen="supervisor"))
    r = mundo.vuelta(foto_sana((FE, primera), (FE, segunda)), instante("2026-11-18T08:15:00Z"))
    [d] = mundo.de(r, "I2")
    assert (d.clase, d.accion) == ("determinista", "agente")
    assert d.hallazgo.corridas == [44246222455, 44247025218]
    # No se relanza otra vez la vigilancia: se lanza al agente de infraestructura.
    assert mundo.cliente.lanzamientos()[1:] == [(REPARAR, {"huella": d.huella, "origen": "supervisor"})]
    reg = r["estado"].huellas[d.huella]
    assert reg["estado"] == "en-reparacion" and reg["issue"] == 1 and reg["corridas"] == [44246222455, 44247025218]
    assert reg["paso"] == "Reintentar las leyes con el extractor reparado"
    assert [m for m, ruta, _ in mundo.cliente.llamadas if ruta == "repos/OpenCodice/supervisor/issues"] == ["POST"]

    # Las vueltas siguientes no repiten el issue ni el lanzamiento.
    r = mundo.vuelta(foto_sana((FE, primera), (FE, segunda)), instante("2026-11-18T08:30:00Z"))
    assert _acciones(r, "I2") == [("I2", "esperar")] and mundo.cliente.issues == 1
    assert len(mundo.cliente.lanzamientos()) == 2

    # Cuando una corrida posterior termina bien, la huella se cierra y su issue también.
    buena = real("fiscal-extractor#44253703004", display_title=titulo(wf))
    r = mundo.vuelta(foto_sana((FE, primera), (FE, segunda), (FE, buena)), instante("2026-11-18T09:40:00Z"))
    assert r["estado"].huellas[d.huella]["estado"] == "resuelta"
    assert ("PATCH", "repos/OpenCodice/supervisor/issues/1", {"state": "closed"}) in mundo.cliente.llamadas


# (b) cancelada en espera: se lanza la unión de lo que se perdió; si la que la reemplazó lo abarca, nada.
def test_cancelada_en_espera_lanza_la_union(cadenas, foto_sana, real, mundo):
    wf = cadenas.workflow(FE, VLE)
    perdida = real("fiscal-extractor#44232039713", display_title=titulo(wf, estados="nuevo-leon", origen="ronda"))
    reemplazo = real("fiscal-extractor#44234246814", display_title=titulo(wf, estados="jalisco", origen="reparar"))
    otra = corrida(VLE, "cancelled", hace_h=6, titulo=titulo(wf, estados="colima"), jobs=[])
    r = mundo.vuelta(foto_sana((FE, perdida), (FE, reemplazo), (FE, otra)))
    assert _acciones(r) == [("I3", "lanzar")]
    assert mundo.cliente.lanzamientos() == [(DESTINO, {"estados": "colima nuevo-leon", "origen": "supervisor"})]


def test_cancelada_en_espera_abarcada_no_hace_nada(cadenas, foto_sana, real, mundo):
    wf = cadenas.workflow(FE, VLE)
    perdida = real("fiscal-extractor#44232039713", display_title=titulo(wf, estados="nuevo-leon"))
    completa = real("fiscal-extractor#44234246814", display_title=titulo(wf))
    r = mundo.vuelta(foto_sana((FE, perdida), (FE, completa)))
    assert _acciones(r) == [] and mundo.cliente.llamadas == []


# (c) PR automático abierto sin corrida en curso: revisar-pr; a la segunda, cerrarlo y relanzar su vigilancia.
def test_pr_abierto_revisar_y_despues_cerrar(cadenas, foto_sana, mundo):
    pr = {"number": 77, "head_ref": "reforma/leyes-estatales", "labels": ["automatico"], "created_at": t(3),
          "updated_at": t(3), "cross": False}
    con_pr = lambda: foto_sana(datos={"leyes-estatales": {"prs": [pr]}})  # noqa: E731

    r = mundo.vuelta(con_pr())
    assert _acciones(r) == [("I5", "revisar-pr")]
    assert mundo.cliente.lanzamientos() == [
        (f"{FE}/revisar-pr.yml", {"pr": "77", "repo": "OpenCodice/leyes-estatales", "prefijo": "reforma/"})]

    # Mientras su resultado puede estar en camino, se espera.
    r = mundo.vuelta(con_pr(), AHORA + timedelta(minutes=15))
    assert _acciones(r) == [("I5", "esperar")]

    r = mundo.vuelta(con_pr(), AHORA + timedelta(minutes=45))
    assert _acciones(r) == [("I5", "cerrar-pr")]
    rutas = [(m, ruta) for m, ruta, _ in mundo.cliente.llamadas[1:]]
    assert ("PATCH", "repos/OpenCodice/leyes-estatales/pulls/77") in rutas
    assert ("DELETE", "repos/OpenCodice/leyes-estatales/git/refs/heads/reforma/leyes-estatales") in rutas
    # Nunca fusiona ni escribe contenido en el repo de datos.
    assert not any("merge" in ruta or "/contents/" in ruta for _, ruta in rutas)

    # Ya sin PR: queda debiéndose la vigilancia que lo abrió.
    r = mundo.vuelta(foto_sana(), AHORA + timedelta(minutes=60))
    assert [d.accion for d in r["decisiones"] if d.accion != "nada"] == ["lanzar"]
    assert mundo.cliente.lanzamientos()[-1] == (DESTINO, {"origen": "supervisor"})
    antes = len(mundo.cliente.llamadas)
    r = mundo.vuelta(foto_sana(), AHORA + timedelta(minutes=75))
    assert len(mundo.cliente.llamadas) == antes


def test_pr_fusionado_tras_revisar_se_da_por_resuelto(foto_sana, mundo):
    pr = {"number": 77, "head_ref": "reforma/leyes-estatales", "labels": ["automatico"], "created_at": t(3),
          "updated_at": t(3), "cross": False}
    r = mundo.vuelta(foto_sana(datos={"leyes-estatales": {"prs": [pr]}}))
    [d] = mundo.de(r, "I5")
    r = mundo.vuelta(foto_sana(), AHORA + timedelta(minutes=45))
    [resuelta] = [x for x in r["decisiones"] if x.huella == d.huella]
    # Resuelta sin rastro: lo que se hizo queda en la bitácora y la huella sale del estado.
    assert resuelta.registro["estado"] == "resuelta" and d.huella not in r["estado"].huellas
    assert len(mundo.cliente.llamadas) == 1


# (d) vigilancia sin corrida exitosa en 8 días.
def test_cadencia_vencida_lanza_y_no_insiste_antes_de_tiempo(cadenas, foto_sana, mundo):
    wf = cadenas.workflow(FE, "vigilar-fiscal.yml")
    vieja = corrida(wf.archivo, hace_h=8 * 24 + 2, titulo=titulo(wf))
    foto = foto_sana((FE, vieja), sin=((FE, wf.archivo),))
    def accion(ahora):
        [d] = mundo.de(mundo.vuelta(foto, ahora), "I1", "vigilar-fiscal")
        return d.accion

    assert accion(AHORA) == "lanzar"
    assert mundo.cliente.lanzamientos() == [(f"{FE}/vigilar-fiscal.yml", {"origen": "supervisor"})]
    # Si la corrida lanzada no sirvió, el siguiente intento por cadencia espera 1 h, luego 6 h.
    assert accion(AHORA + timedelta(minutes=50)) == "esperar"
    assert accion(AHORA + timedelta(minutes=65)) == "lanzar"
    assert accion(AHORA + timedelta(hours=5)) == "esperar"
    assert accion(AHORA + timedelta(hours=7, minutes=10)) == "lanzar"
    assert [l for l in mundo.cliente.lanzamientos() if "vigilar-fiscal" in l[0]] == \
        [(f"{FE}/vigilar-fiscal.yml", {"origen": "supervisor"})] * 3


def test_la_cadencia_no_se_salta_la_espera_de_una_falla(cadenas, foto_sana, real, mundo):
    """Zacatecas caído y, además, la ventana vencida: manda la espera de la transitoria."""
    archivo = "vigilar-estatales.yml"
    primera = real("constitucion-extractor#43881425855")
    foto = foto_sana((CE, primera), sin=((CE, archivo),))
    fin1 = instante(primera["updated_at"])
    r = mundo.vuelta(foto, fin1 + timedelta(minutes=20))
    assert [d.accion for d in mundo.de(r, "I2") + mundo.de(r, "I1", archivo)] == ["esperar", "esperar"]
    assert mundo.cliente.llamadas == []
    r = mundo.vuelta(foto, fin1 + timedelta(minutes=61))
    assert mundo.de(r, "I2")[0].accion == "lanzar" and mundo.de(r, "I1", archivo)[0].accion == "nada"
    assert len(mundo.cliente.lanzamientos()) == 1


# (e) el job `estado pendiente=[true]` sin corrida posterior.
def test_trabajo_pendiente_declarado(cadenas, foto_sana, mundo):
    wf = cadenas.workflow(FE, "vigilar-federal.yml")
    pendiente = corrida(wf.archivo, hace_h=1, titulo=titulo(wf, docs="lgs cff"),
                        jobs=[job("cerrar"), job("estado pendiente=[true]")])
    r = mundo.vuelta(foto_sana((FE, pendiente)))
    assert _acciones(r) == [("I6", "lanzar")]
    assert mundo.cliente.lanzamientos() == [(f"{FE}/vigilar-federal.yml", {"docs": "lgs cff", "origen": "supervisor"})]


# (f) regla del único lanzador: con una corrida en espera en el grupo no se lanza nada.
def test_grupo_con_corrida_en_espera_no_lanza(cadenas, foto_sana, mundo):
    wf = cadenas.workflow(FE, VLE)
    rota = corrida(VLE, "failure", hace_h=3, titulo=titulo(wf, estados="jalisco"),
                   jobs=falla("planear", "Planear", "KeyError: 'jalisco'"))
    en_curso = corrida(VLE, None, hace_h=1, titulo=titulo(wf, estados="colima"))
    en_espera = corrida(VLE, None, hace_h=0.5, status="pending", titulo=titulo(wf, estados="sonora"))
    r = mundo.vuelta(foto_sana((FE, rota), (FE, en_curso), (FE, en_espera)))
    [d] = mundo.de(r, "I2")
    assert d.accion == "esperar" and str(en_espera["id"]) in d.motivo
    assert mundo.cliente.llamadas == []
    # Sin la que espera, sí: la nueva espera su turno detrás de la que corre.
    r = mundo.vuelta(foto_sana((FE, rota), (FE, en_curso)))
    assert mundo.cliente.lanzamientos() == [(DESTINO, {"estados": "jalisco", "origen": "supervisor"})]


def test_el_grupo_es_por_repo_y_lo_comparten_dos_workflows(cadenas, foto_sana, mundo):
    wf = cadenas.workflow("congreso-extractor", "actualizar-congreso.yml")
    rota = corrida(wf.archivo, "failure", hace_h=3, titulo=titulo(wf, publicar="si"),
                   jobs=falla("actualizar", "Padrón", "ValueError: padrón vacío"))
    residuos = corrida("resolver-residuos.yml", None, hace_h=0.2, status="pending", titulo="resolver-residuos")
    r = mundo.vuelta(foto_sana(("congreso-extractor", rota), ("congreso-extractor", residuos),
                               sin=(("congreso-extractor", wf.archivo),)))
    [d] = mundo.de(r, "I2")
    assert d.accion == "esperar" and mundo.cliente.llamadas == []


def test_dos_hallazgos_no_lanzan_dos_veces_el_mismo_workflow(cadenas, foto_sana, real, mundo):
    wf = cadenas.workflow(FE, VLE)
    perdida = real("fiscal-extractor#44232039713", display_title=titulo(wf, estados="nuevo-leon"))
    vieja = corrida(VLE, hace_h=9 * 24, titulo=titulo(wf))
    r = mundo.vuelta(foto_sana((FE, perdida), (FE, vieja), sin=((FE, VLE),)))
    assert sorted(_acciones(r)) == [("I1", "esperar"), ("I3", "lanzar")]
    assert len(mundo.cliente.lanzamientos()) == 1


# (g) cortacircuitos: más de 12 lanzamientos en 24 h a un workflow.
def test_cortacircuitos(cadenas, foto_sana, mundo):
    from supervisor.estado import Estado
    wf = cadenas.workflow(FE, VLE)
    estado = Estado(mundo.carpeta)
    for n in range(12):
        estado.anotar_accion({"cuando": iso(AHORA - timedelta(hours=n + 1)), "huella": "x", "accion": "lanzar",
                              "detalle": {"repo": FE, "workflow": VLE, "inputs": {}}, "resultado": "ok"})
    estado.guardar()
    pendiente = corrida(VLE, hace_h=1, titulo=titulo(wf), jobs=[job("cerrar"), job("estado pendiente=[true]")])
    r = mundo.vuelta(foto_sana((FE, pendiente)))
    assert _acciones(r, "I6") == [("I6", "esperar")]
    assert mundo.cliente.lanzamientos() == []
    [ciclo] = [d for d in r["decisiones"] if d.accion == "agotada"]
    assert "cortacircuitos" in ciclo.motivo and mundo.cliente.issues == 1
    # El aviso del ciclo no se repite en cada vuelta.
    mundo.vuelta(foto_sana((FE, pendiente)), AHORA + timedelta(minutes=15))
    assert mundo.cliente.issues == 1
    # Pasadas 24 h de los lanzamientos, vuelve a lanzar y el ciclo se cierra.
    r = mundo.vuelta(foto_sana((FE, pendiente)), AHORA + timedelta(hours=14))
    assert _acciones(r, "I6") == [("I6", "lanzar")]


# (h) una compuerta de datos en rojo no es asunto del supervisor.
def test_falla_de_compuerta_de_datos_se_ignora(cadenas, foto_sana, mundo):
    wf = cadenas.workflow(FE, VLE)
    rota = corrida(VLE, "failure", hace_h=3, titulo=titulo(wf),
                   jobs=falla("cerrar", "Validar invariantes (gate)", "Invariantes ROTOS — no mergear"))
    r = mundo.vuelta(foto_sana((FE, rota)))
    [d] = mundo.de(r, "I2")
    assert (d.clase, d.accion) == ("datos", "nada")
    assert r["estado"].huellas[d.huella]["estado"] == "datos"
    assert mundo.cliente.llamadas == []
    assert r["latido"]["invariantes"]["I2"] == "ok"


def test_compuerta_con_firma_solo_si_casa(cadenas, foto_sana, mundo):
    no_confirmadas = corrida("impacto.yml", "failure", hace_h=3, evento="push", jobs=falla(
        "impacto", "Resultado", "Hay correcciones que no quedaron confirmadas como mejora: las vigilancias no las publican."))
    r = mundo.vuelta(foto_sana((FE, no_confirmadas)))
    assert mundo.de(r, "I2")[0].clase == "datos"
    tronada = corrida("impacto.yml", "failure", hace_h=3, evento="push", jobs=falla(
        "impacto", "Resultado", "No se pudo medir el impacto (impacto_corpus.py salió con código «3»)."))
    r = mundo.vuelta(foto_sana((CE, tronada)))
    assert mundo.de(r, "I2")[0].accion == "lanzar"


# (i) transitoria: esperas de 1 h, 6 h y 24 h; a la cuarta ya no es transitoria.
def test_transitoria_respeta_las_esperas(cadenas, foto_sana, real, mundo):
    archivo = "vigilar-estatales.yml"
    primera = real("constitucion-extractor#43881425855")
    fin1 = instante(primera["updated_at"])
    sin = ()

    def decision(foto, ahora):
        [d] = mundo.de(mundo.vuelta(foto, ahora), "I2")
        return d

    d = decision(foto_sana((CE, primera), sin=sin), fin1 + timedelta(minutes=30))
    assert (d.clase, d.accion, d.proxima) == ("transitoria", "esperar", iso(fin1 + timedelta(hours=1)))
    assert mundo.cliente.lanzamientos() == []
    d = decision(foto_sana((CE, primera), sin=sin), fin1 + timedelta(minutes=61))
    assert d.accion == "lanzar"
    assert mundo.cliente.lanzamientos() == [(f"{CE}/{archivo}", {"origen": "supervisor"})]

    segunda = real("constitucion-extractor#43883374036")
    fin2 = instante(segunda["updated_at"])
    dos = foto_sana((CE, primera), (CE, segunda), sin=sin)
    assert decision(dos, fin2 + timedelta(hours=5, minutes=50)).accion == "esperar"
    assert decision(dos, fin2 + timedelta(hours=6, minutes=1)).accion == "lanzar"

    def copia(n, horas):
        c = real("constitucion-extractor#43883374036")
        momento = fin2 + timedelta(hours=horas)
        return c | {"id": 43883374036 + n, "created_at": iso(momento), "run_started_at": iso(momento),
                    "updated_at": iso(momento + timedelta(minutes=10))}

    tercera = copia(1, 7)
    fin3 = instante(tercera["updated_at"])
    tres = foto_sana((CE, primera), (CE, segunda), (CE, tercera), sin=sin)
    assert decision(tres, fin3 + timedelta(hours=23)).accion == "esperar"
    assert decision(tres, fin3 + timedelta(hours=24, minutes=1)).accion == "lanzar"
    assert len(mundo.cliente.lanzamientos()) == 3

    cuarta = copia(2, 33)
    cuatro = foto_sana((CE, primera), (CE, segunda), (CE, tercera), (CE, cuarta), sin=sin)
    d = decision(cuatro, instante(cuarta["updated_at"]) + timedelta(minutes=5))
    assert (d.clase, d.accion) == ("determinista", "agente")
    assert [destino for destino, _ in mundo.cliente.lanzamientos()] == [f"{CE}/{archivo}"] * 3 + [REPARAR]


def test_desconocida_un_reintento_a_la_hora_y_despues_agente(cadenas, foto_sana, mundo):
    wf = cadenas.workflow(FE, "vigilar-fiscal.yml")
    sin = ((FE, wf.archivo),)

    def rota(hace_h):
        return corrida(wf.archivo, "failure", hace_h=hace_h, titulo=titulo(wf),
                       jobs=falla("vigilar", "Descargar fuentes y reconstruir el corpus", "HTTP Error 403: Forbidden"))

    una = foto_sana((FE, rota(0.6)), sin=sin)
    assert mundo.de(mundo.vuelta(una), "I2")[0].accion == "esperar"
    [d] = mundo.de(mundo.vuelta(una, AHORA + timedelta(minutes=40)), "I2")
    assert (d.clase, d.accion) == ("desconocida", "lanzar")
    dos = foto_sana((FE, rota(3)), (FE, rota(1)), sin=sin)
    [d] = mundo.de(mundo.vuelta(dos, AHORA + timedelta(minutes=40)), "I2")
    assert (d.clase, d.accion) == ("determinista", "agente")


# Visto el 2026-11-18 en congreso (corridas 44304671081 y 44317033566): la que publica falla siempre en el
# mismo paso y, entre un intento y otro, la horaria «solo pedir» termina bien. Esa no corta la racha.
def test_una_exitosa_de_otros_inputs_no_corta_la_racha(cadenas, foto_sana, mundo):
    wf = cadenas.workflow("congreso-extractor", "actualizar-congreso.yml")
    sin = (("congreso-extractor", wf.archivo),)
    paso = "Agente — leer las páginas que el parser no entendió (1)"

    def rota(hace_h):
        return corrida(wf.archivo, "failure", hace_h=hace_h, titulo=titulo(wf, publicar="si"),
                       jobs=falla("actualizar", paso, "el agente no dejó su veredicto"))

    def buena(hace_h, publicar):
        return ("congreso-extractor", corrida(wf.archivo, hace_h=hace_h, titulo=titulo(wf, publicar=publicar),
                                              jobs=[job("actualizar"), job("estado pendiente=[false]")]))

    def decision(*corridas):
        foto = foto_sana(*corridas, sin=sin)
        [h] = [h for h in inv.evaluar(foto, cadenas, AHORA) if h.invariante == "I2"]
        return h, mundo.de(mundo.vuelta(foto), "I2")[0]

    primera, segunda = ("congreso-extractor", rota(4)), ("congreso-extractor", rota(1.5))
    h, d = decision(primera, buena(3, "no"), segunda, buena(0.5, "no"))
    assert h.repeticiones == 2 and len(h.corridas) == 2
    assert (d.clase, d.accion) == ("determinista", "agente")
    # Una exitosa que sí publica entre las dos es otra cosa: la falla empieza de nuevo.
    h, _ = decision(primera, buena(3, "si"), segunda)
    assert h.repeticiones == 1


# (j) idempotencia: la misma foto con el estado resultante no repite acciones.
def test_idempotencia(cadenas, foto_sana, real, mundo):
    wf = cadenas.workflow(FE, VLE)
    foto = foto_sana(
        (FE, real("fiscal-extractor#44246222455", display_title=titulo(wf))),
        (FE, real("fiscal-extractor#44232039713", display_title=titulo(wf, revisar="jalisco/codigo-civil"))),
        (FE, corrida("tests.yml", "failure", hace_h=3, evento="push", jobs=falla("pytest", "pytest", "1 failed"))),
        (FE, corrida("tests.yml", "failure", hace_h=2, evento="push", jobs=falla("pytest", "pytest", "1 failed"))),
        (FE, corrida("tests.yml", "failure", hace_h=1, evento="push", jobs=falla("pytest", "pytest", "1 failed"))),
        datos={"leyes-federales": {"main": {"sha": "c" * 40, "fecha": t(0.5)},
                                   "prs": [{"number": 9, "head_ref": "actualizacion/leyes-federales",
                                            "labels": ["automatico"], "created_at": t(4), "updated_at": t(4),
                                            "cross": False}]}})
    r = mundo.vuelta(foto)
    hechas = sorted(a for _, a in _acciones(r) if a != "esperar")
    assert hechas == ["agente", "desplegar-web", "lanzar", "revisar-pr"]
    llamadas = len(mundo.cliente.llamadas)
    lineas = (mundo.carpeta / "acciones.jsonl").read_text().count("\n")
    huellas = (mundo.carpeta / "huellas.jsonl").read_text()
    assert lineas == 4

    # La primera repetición solo cambia el latido (ya no hay acciones previstas); las demás, nada.
    for minutos in (0, 10, 20):
        r = mundo.vuelta(foto, AHORA + timedelta(minutes=minutos))
        assert [a for _, a in _acciones(r) if a != "esperar"] == []
        assert not r["guardado"] or minutos == 0
    assert len(mundo.cliente.llamadas) == llamadas
    assert (mundo.carpeta / "acciones.jsonl").read_text().count("\n") == lineas
    assert (mundo.carpeta / "huellas.jsonl").read_text() == huellas


def test_modo_informe_no_ejecuta_ni_anota_acciones(cadenas, foto_sana, real, mundo):
    foto = foto_sana((FE, real("fiscal-extractor#44246222455")), sin=((FE, VLE),))
    r = mundo.vuelta(foto, modo="informe")
    assert ("I2", "lanzar") in _acciones(r)
    assert mundo.cliente.llamadas == []
    assert not (mundo.carpeta / "acciones.jsonl").exists()
    # La huella observada sí queda, sin contar un lanzamiento que no ocurrió.
    [d] = mundo.de(r, "I2")
    assert r["estado"].huellas[d.huella]["lanzamientos"] == 0
    # Y en la vuelta siguiente lo vuelve a proponer.
    assert ("I2", "lanzar") in _acciones(mundo.vuelta(foto, AHORA + timedelta(minutes=15), modo="informe"))


def test_un_dispatch_que_falla_se_anota_y_se_reintenta_con_tope(cadenas, foto_sana, real, mundo):
    mundo.cliente.falla_en = "/dispatches"
    foto = foto_sana((FE, real("fiscal-extractor#44246222455")))
    ahora = instante("2026-11-18T07:00:00Z")
    r = mundo.vuelta(foto, ahora)
    [d] = mundo.de(r, "I2")
    assert r["estado"].acciones[-1]["resultado"].startswith("error")
    assert r["estado"].huellas[d.huella]["errores"] == 1 and r["estado"].huellas[d.huella]["lanzamientos"] == 0
    assert mundo.de(mundo.vuelta(foto, ahora + timedelta(minutes=10)), "I2")[0].accion == "esperar"
    for horas in (1.1, 2.2):
        assert mundo.de(mundo.vuelta(foto, ahora + timedelta(hours=horas)), "I2")[0].accion == "lanzar"
    [d] = mundo.de(mundo.vuelta(foto, ahora + timedelta(hours=3.3)), "I2")
    assert d.accion == "agotada" and "sin que aparezca" in d.motivo
    assert mundo.de(mundo.vuelta(foto, ahora + timedelta(hours=4.4)), "I2")[0].accion == "nada"


def test_corrida_atorada_se_cancela_y_se_relanza(cadenas, foto_sana, mundo):
    wf = cadenas.workflow(FE, "vigilar-fiscal.yml")
    eterna = corrida(wf.archivo, None, hace_h=wf.max_min / 60 + 1, titulo=titulo(wf))
    r = mundo.vuelta(foto_sana((FE, eterna)))
    assert _acciones(r) == [("I4", "cancelar-y-relanzar")]
    assert mundo.cliente.llamadas[0][:2] == ("POST", f"repos/OpenCodice/{FE}/actions/runs/{eterna['id']}/cancel")
    assert mundo.cliente.lanzamientos() == [(f"{FE}/vigilar-fiscal.yml", {"origen": "supervisor"})]


def test_lo_que_no_se_ve_no_se_da_por_resuelto(cadenas, foto_sana, real, mundo):
    foto = foto_sana((FE, real("fiscal-extractor#44246222455")))
    r = mundo.vuelta(foto, instante("2026-11-18T07:00:00Z"))
    [d] = mundo.de(r, "I2")
    ciega = foto_sana()
    ciega["repos"][FE] = {"observable": False, "corridas": [], "ultima_exitosa": {}}
    r = mundo.vuelta(ciega, instante("2026-11-18T07:15:00Z"))
    assert r["estado"].huellas[d.huella]["estado"] == "abierta"
    assert r["latido"]["sin_observar"] == [FE]


def test_las_corridas_solo_pedir_de_congreso_tienen_su_propio_grupo(cadenas, foto_sana, mundo):
    wf = cadenas.workflow("congreso-extractor", "actualizar-congreso.yml")
    assert wf.grupo_de({"publicar": "no"}) == "actualizar-congreso-pedir" and wf.grupo_de(None) == "actualizar-congreso"
    rota = corrida(wf.archivo, "failure", hace_h=3, titulo=titulo(wf, publicar="si"),
                   jobs=falla("actualizar", "Padrón", "ValueError: padrón vacío"))
    pidiendo = corrida(wf.archivo, None, hace_h=0.1, status="pending", titulo=titulo(wf, publicar="no"))
    r = mundo.vuelta(foto_sana(("congreso-extractor", rota), ("congreso-extractor", pidiendo),
                               sin=(("congreso-extractor", wf.archivo),)))
    assert mundo.de(r, "I2")[0].accion == "lanzar"
    assert mundo.cliente.lanzamientos() == [("congreso-extractor/actualizar-congreso.yml", {"origen": "supervisor"})]


def _en(c: dict, cuando: str) -> dict:
    """La misma corrida, creada (y empezada) en ese instante."""
    return c | {"created_at": cuando, "run_started_at": cuando, "updated_at": cuando}


# ---- Peticiones (I12) ----

def _pide(wf, peticiones, hace_h=2.0, dura_h=0.5, **inputs):
    c = corrida(wf.archivo, hace_h=hace_h, dura_h=dura_h, titulo=titulo(wf, **inputs),
                jobs=[job("reparar"), job("estado pendiente=[false]")])
    c["peticiones"] = [{"job": "reparar", "salida": "pedir_vigilancia", "repo": None} | p for p in peticiones]
    return c


def test_peticion_se_lanza_una_vez_y_se_resuelve_cuando_empieza_su_corrida(cadenas, foto_sana, mundo):
    wf = cadenas.workflow(FE, VLE)
    pide = _pide(wf, [{"workflow": VLE, "inputs": {"estados": "jalisco"}}])
    r = mundo.vuelta(foto_sana((FE, pide)))
    assert _acciones(r) == [("I12", "lanzar")]
    assert mundo.cliente.lanzamientos() == [(DESTINO, {"estados": "jalisco", "origen": "supervisor"})]
    [d] = mundo.de(r, "I12")
    # Idempotencia: la misma foto con el estado resultante no vuelve a lanzar.
    for minutos in (5, 15, 25):
        r = mundo.vuelta(foto_sana((FE, pide)), AHORA + timedelta(minutes=minutos))
        assert _acciones(r) == [("I12", "esperar")]
    assert len(mundo.cliente.lanzamientos()) == 1
    # Aparece la corrida pedida (basta con que haya empezado): la huella se cierra.
    atendida = corrida(VLE, None, hace_h=-0.1, titulo=titulo(wf, estados="jalisco", origen="supervisor"))
    r = mundo.vuelta(foto_sana((FE, pide), (FE, atendida)), AHORA + timedelta(minutes=30))
    [resuelta] = [x for x in r["decisiones"] if x.huella == d.huella]
    assert not mundo.de(r, "I12") and resuelta.registro["estado"] == "resuelta" and d.huella not in r["estado"].huellas
    assert len(mundo.cliente.lanzamientos()) == 1


def test_peticion_no_se_pierde_si_el_grupo_tiene_una_corrida_en_espera(cadenas, foto_sana, mundo):
    wf = cadenas.workflow(FE, VLE)
    # Pide la ronda, que es de otro grupo de concurrency.
    ronda = cadenas.workflow(FE, "expandir-estatales.yml")
    pide = _pide(ronda, [{"workflow": VLE, "inputs": {"estados": "jalisco"}}], hace_h=1, dura_h=0.5, estado="jalisco")
    en_curso = corrida(VLE, None, hace_h=2, titulo=titulo(wf, estados="colima"))
    en_espera = corrida(VLE, None, hace_h=1.5, status="pending", titulo=titulo(wf, estados="sonora"))
    r = mundo.vuelta(foto_sana((FE, pide), (FE, en_curso), (FE, en_espera)))
    [d] = mundo.de(r, "I12")
    assert d.accion == "esperar" and str(en_espera["id"]) in d.motivo and mundo.cliente.llamadas == []
    # Horas después sigue pendiente, y en cuanto el lugar de espera queda libre, se lanza.
    r = mundo.vuelta(foto_sana((FE, pide), (FE, en_curso), (FE, en_espera)), AHORA + timedelta(minutes=45))
    assert mundo.de(r, "I12")[0].accion == "esperar"
    ya_corre = en_espera | {"status": "in_progress"}
    r = mundo.vuelta(foto_sana((FE, pide), (FE, ya_corre)), AHORA + timedelta(minutes=60))
    assert mundo.de(r, "I12")[0].accion == "lanzar"
    assert mundo.cliente.lanzamientos() == [(DESTINO, {"estados": "jalisco", "origen": "supervisor"})]


def test_peticiones_unidas_y_las_que_no_se_unen_se_turnan(cadenas, foto_sana, mundo):
    wf, ronda = cadenas.workflow(FE, VLE), cadenas.workflow(FE, "expandir-estatales.yml")
    a = _pide(ronda, [{"workflow": VLE, "inputs": {"estados": "jalisco"}},
                      {"workflow": ronda.archivo, "inputs": {"estado": "puebla"}},
                      {"workflow": ronda.archivo, "inputs": {"estado": "sonora"}}], estado="jalisco")
    b = _pide(wf, [{"workflow": VLE, "inputs": {"estados": "colima"}}], hace_h=1.9, dura_h=0.2, estados="x")
    r = mundo.vuelta(foto_sana((FE, a), (FE, b)))
    # Una sola vigilancia con la unión; de las dos rondas, una ahora y la otra cuando el grupo tenga lugar.
    assert sorted(mundo.cliente.lanzamientos(), key=str) == sorted([
        (DESTINO, {"estados": "colima jalisco", "origen": "supervisor"}),
        (f"{FE}/expandir-estatales.yml", {"estado": "puebla", "origen": "supervisor"})], key=str)
    assert sorted(d.accion for d in mundo.de(r, "I12")) == ["esperar", "lanzar", "lanzar"]


def test_peticion_rechazada_deja_constancia_una_vez_y_no_lanza(cadenas, foto_sana, mundo):
    wf = cadenas.workflow(FE, VLE)
    pide = _pide(wf, [{"workflow": "vigilar-fiscal.yml", "inputs": {}}])
    r = mundo.vuelta(foto_sana((FE, pide)))
    [d] = mundo.de(r, "I12")
    assert (d.accion, d.clase) == ("agotada", "rechazada") and "puede_pedir" in d.motivo
    assert mundo.cliente.lanzamientos() == [] and mundo.cliente.issues == 1
    r = mundo.vuelta(foto_sana((FE, pide)), AHORA + timedelta(minutes=15))
    assert mundo.de(r, "I12")[0].accion == "nada" and mundo.cliente.issues == 1
    assert r["latido"]["invariantes"]["I12"] == "rota:1"
    # Cuando la corrida sale de la ventana de la foto, la constancia se cierra.
    r = mundo.vuelta(foto_sana(), AHORA + timedelta(days=9))
    assert r["estado"].huellas[d.huella]["estado"] == "resuelta"


def test_peticion_y_tanda_pendiente_de_la_misma_corrida_lanzan_una_sola(cadenas, foto_sana, mundo):
    wf = cadenas.workflow(FE, "vigilar-federal.yml")
    c = corrida(wf.archivo, hace_h=1, titulo=titulo(wf), jobs=[job("cerrar"), job("estado pendiente=[true]")])
    c["peticiones"] = [{"job": "reparar", "salida": "pedir_vigilancia", "repo": None, "workflow": wf.archivo, "inputs": {}}]
    r = mundo.vuelta(foto_sana((FE, c)))
    assert sorted(_acciones(r)) == [("I12", "lanzar")] and len(mundo.cliente.lanzamientos()) == 1
    assert mundo.de(r, "I6")[0].accion == "nada"


# ---- I6 sin el job `continuar`: la tanda espera a que su PR esté fusionado ----

def test_la_tanda_siguiente_espera_al_pr_de_la_cadena(cadenas, foto_sana, mundo):
    wf = cadenas.workflow(FE, "vigilar-federal.yml")
    pendiente = corrida(wf.archivo, hace_h=0.5, titulo=titulo(wf), jobs=[job("cerrar"), job("estado pendiente=[true]")])
    pr = {"number": 9, "head_ref": "actualizacion/leyes-federales", "labels": ["automatico"], "created_at": t(0.6),
          "updated_at": t(0.6), "cross": False}
    con_pr = foto_sana((FE, pendiente), datos={"leyes-federales": {"prs": [pr]}})
    r = mundo.vuelta(con_pr)
    [d] = mundo.de(r, "I6")
    assert d.accion == "esperar" and "PR #9 de leyes-federales" in d.motivo and mundo.cliente.llamadas == []
    # Fusionado (ya no está abierto): se lanza la tanda. El commit del PR también pide su despliegue.
    r = mundo.vuelta(foto_sana((FE, pendiente)), AHORA + timedelta(minutes=15))
    assert mundo.de(r, "I6")[0].accion == "lanzar"
    assert mundo.cliente.lanzamientos() == [(f"{FE}/vigilar-federal.yml", {"origen": "supervisor"})]


# ---- Horarios ----

def test_congreso_cada_hora_no_acumula_lo_atrasado(cadenas, foto_sana, mundo):
    CG = "congreso-extractor"
    con = con_horario(cadenas, "actualizar-congreso.yml")
    wf = con.workflow(CG, "actualizar-congreso.yml")
    from supervisor.__main__ import correr
    from supervisor.actuar import Actor
    from supervisor.estado import Estado

    def vuelta(foto, cuando):
        estado = Estado(mundo.carpeta)
        r = correr(foto, con, estado, instante(cuando), Actor(mundo.cliente, con, "actuar"))
        estado.guardar()
        return [d for d in r["decisiones"] if d.hallazgo is not None and d.hallazgo.extra.get("horario")]

    lanzadas = lambda: [i for destino, i in mundo.cliente.lanzamientos() if destino == f"{CG}/{wf.archivo}"]  # noqa: E731
    # Faltaron los tics de las 13, 14 y 15 (AHORA son las 12): a las 15:07 se lanza una, la de las 15.
    [d] = vuelta(foto_sana(), "2026-11-18T15:07:00Z")
    assert d.accion == "lanzar" and d.detalle["horario"] == "2026-11-18T15:00:00Z"
    assert lanzadas() == [{"publicar": "no", "origen": "supervisor"}]
    # La misma cita no se lanza dos veces, aunque su corrida tarde en aparecer en la lista.
    [d] = vuelta(foto_sana(), "2026-11-18T15:22:00Z")
    assert d.accion == "esperar" and len(lanzadas()) == 1
    # Con su corrida a la vista ya no hay nada que hacer.
    suya = _en(corrida(wf.archivo, None, titulo=titulo(wf, publicar="no", origen="supervisor")), "2026-11-18T15:07:05Z")
    assert vuelta(foto_sana((CG, suya)), "2026-11-18T15:37:00Z") == []
    # A la hora siguiente toca otra; si la anterior sigue esperando su turno, no se le forma otra detrás.
    en_espera = suya | {"status": "pending"}
    assert vuelta(foto_sana((CG, en_espera)), "2026-11-18T16:07:00Z") == []
    [d] = vuelta(foto_sana((CG, suya)), "2026-11-18T16:07:00Z")
    assert d.accion == "lanzar" and len(lanzadas()) == 2
    # A las 18:00, la que publica.
    [d] = vuelta(foto_sana((CG, suya)), "2026-11-18T18:01:00Z")
    assert d.accion == "lanzar" and lanzadas()[-1] == {"origen": "supervisor"}


def test_las_citas_del_horario_no_se_acumulan_en_el_estado(cadenas, foto_sana, mundo):
    """Cada cita es una huella mientras su corrida no aparece; con ella a la vista, sale del estado."""
    import json
    CG = "congreso-extractor"
    con = con_horario(cadenas, "actualizar-congreso.yml")
    wf = con.workflow(CG, "actualizar-congreso.yml")
    from supervisor.__main__ import correr
    from supervisor.actuar import Actor
    from supervisor.estado import Estado

    def vuelta(foto, cuando):
        estado = Estado(mundo.carpeta)
        correr(foto, con, estado, cuando, Actor(mundo.cliente, con, "actuar"))
        estado.guardar()
        return [json.loads(l) for l in (mundo.carpeta / "huellas.jsonl").read_text().splitlines()]

    corridas = []
    for hora in range(13, 18):
        cita = AHORA.replace(hour=hora)
        [abierta] = vuelta(foto_sana(*corridas), cita + timedelta(minutes=1))
        assert (abierta["invariante"], abierta["estado"], abierta["lanzamientos"]) == ("I1", "abierta", 1)
        suya = _en(corrida(wf.archivo, "success", titulo=titulo(wf, publicar="no", origen="supervisor"),
                           jobs=[job("actualizar"), job("estado pendiente=[false]")]), iso(cita + timedelta(minutes=2)))
        corridas.append((CG, suya))
        assert vuelta(foto_sana(*corridas), cita + timedelta(minutes=16)) == []
    # Lo que se lanzó sigue en la bitácora: de ahí sale que la misma cita no se repita ni cuente para el cortacircuitos.
    acciones = Estado(mundo.carpeta).acciones
    assert len(acciones) == 5 and all(a["detalle"].get("horario") for a in acciones)


def test_lo_lanzado_por_horario_no_abre_el_cortacircuitos(cadenas, foto_sana, mundo):
    from supervisor.estado import Estado
    CG = "congreso-extractor"
    wf = cadenas.workflow(CG, "actualizar-congreso.yml")
    estado = Estado(mundo.carpeta)
    for n in range(24):
        estado.anotar_accion({"cuando": iso(AHORA - timedelta(hours=n, minutes=30)), "huella": f"h{n}", "accion": "lanzar",
                              "detalle": {"repo": CG, "workflow": wf.archivo, "inputs": {"publicar": "no"},
                                          "horario": iso(AHORA - timedelta(hours=n + 1))}, "resultado": "ok"})
    estado.guardar()
    # Veinticuatro lanzamientos por horario en un día no son un ciclo: el reintento de una falla sigue pasando.
    rota = corrida(wf.archivo, "failure", hace_h=3, titulo=titulo(wf, publicar="si"),
                   jobs=falla("actualizar", "Padrón", "ValueError: padrón vacío"))
    r = mundo.vuelta(foto_sana((CG, rota), sin=((CG, wf.archivo),)))
    assert mundo.de(r, "I2")[0].accion == "lanzar" and not [d for d in r["decisiones"] if d.accion == "agotada"]


def test_el_horario_no_espera_a_la_falla_anterior_y_la_cadencia_si(cadenas, foto_sana, real, mundo):
    """Lunes 09:00 con la vigilancia anterior en rojo y esperando su reintento: la de la semana se lanza igual."""
    from supervisor.__main__ import correr
    from supervisor.actuar import Actor
    from supervisor.estado import Estado
    archivo = "vigilar-estatales.yml"
    con = con_horario(cadenas, archivo, sin_cadencias=False)
    primera = real("constitucion-extractor#43881425855") | {
        "created_at": "2026-11-23T08:20:00Z", "run_started_at": "2026-11-23T08:20:00Z", "updated_at": "2026-11-23T08:40:00Z"}
    foto = foto_sana((CE, primera), sin=((CE, archivo),))
    estado = Estado(mundo.carpeta)
    r = correr(foto, con, estado, instante("2026-11-23T09:05:00Z"), Actor(mundo.cliente, con, "actuar"))
    por_base = {d.hallazgo.base: d.accion for d in r["decisiones"] if d.hallazgo and d.hallazgo.workflow == archivo}
    assert por_base["horario-2026-11-23T09:00:00Z"] == "lanzar" and por_base[str(primera["id"])] == "esperar"
    assert [i for destino, i in mundo.cliente.lanzamientos() if destino == f"{CE}/{archivo}"] == [{"origen": "supervisor"}]


def test_la_ronda_semanal_se_lanza_cuando_la_cadena_queda_libre(cadenas, foto_sana, mundo):
    from supervisor.__main__ import correr
    from supervisor.actuar import Actor
    from supervisor.estado import Estado
    con = con_horario(cadenas, "expandir-estatales.yml")
    wf, ronda = con.workflow(FE, VLE), con.workflow(FE, "expandir-estatales.yml")

    def vuelta(foto, cuando):
        estado = Estado(mundo.carpeta)
        r = correr(foto, con, estado, instante(cuando), Actor(mundo.cliente, con, "actuar"))
        estado.guardar()
        return r

    ocupada = (FE, _en(corrida(VLE, None, titulo=titulo(wf)), "2026-11-25T14:00:00Z"))
    r = vuelta(foto_sana(ocupada), "2026-11-25T15:35:00Z")
    [d] = mundo.de(r, "I1")
    assert d.accion == "esperar" and "cuando la cadena quede libre" in d.motivo and mundo.cliente.llamadas == []
    # Una ronda pedida para un estado no espera a la cadena: se lanza aunque esté ocupada.
    pide = _en(_pide(wf, [{"workflow": ronda.archivo, "inputs": {"redeclarar": "jalisco"}}], estados="jalisco"), "2026-11-25T13:00:00Z")
    r = vuelta(foto_sana(ocupada, (FE, pide)), "2026-11-25T15:50:00Z")
    assert mundo.de(r, "I12")[0].accion == "lanzar" and mundo.de(r, "I1")[0].accion == "esperar"
    assert mundo.cliente.lanzamientos() == [(f"{FE}/expandir-estatales.yml", {"redeclarar": "jalisco", "origen": "supervisor"})]
    # Al día siguiente la cadena sigue ocupada: la cita no caducó. El primer tic con la cadena libre, se lanza.
    otra = (FE, _en(corrida(VLE, None, status="pending", titulo=titulo(wf)), "2026-11-26T15:00:00Z"))
    assert mundo.de(vuelta(foto_sana(otra), "2026-11-26T15:35:00Z"), "I1")[0].accion == "esperar"
    r = vuelta(foto_sana(), "2026-11-26T15:50:00Z")
    assert mundo.de(r, "I1")[0].accion == "lanzar"
    assert mundo.cliente.lanzamientos()[-1] == (f"{FE}/expandir-estatales.yml", {"origen": "supervisor"})
    semanal = _en(corrida(ronda.archivo, titulo=titulo(ronda, origen="supervisor")), "2026-11-26T15:50:10Z")
    assert not mundo.de(vuelta(foto_sana((FE, semanal)), "2026-11-26T16:05:00Z"), "I1")
    assert len(mundo.cliente.lanzamientos()) == 2


def test_la_ronda_espera_a_la_vigilancia_que_se_lanza_en_la_misma_vuelta(cadenas, foto_sana, mundo):
    """El 2026-11-18 a las 16:00 una vuelta lanzó la vigilancia pedida y, con la cadena libre en la foto, la ronda."""
    from supervisor.__main__ import correr
    from supervisor.actuar import Actor
    from supervisor.estado import Estado
    con = con_horario(cadenas, "expandir-estatales.yml")
    wf, ronda = con.workflow(FE, VLE), con.workflow(FE, "expandir-estatales.yml")
    destinos = lambda: [destino for destino, _ in mundo.cliente.lanzamientos()]  # noqa: E731

    def vuelta(foto, cuando):
        estado = Estado(mundo.carpeta)
        r = correr(foto, con, estado, instante(cuando), Actor(mundo.cliente, con, "actuar"))
        r["guardado"] = estado.guardar()
        return r

    pide = (FE, _en(_pide(wf, [{"workflow": VLE, "inputs": {"estados": "colima"}}], estados="jalisco"), "2026-11-25T15:00:00Z"))
    r = vuelta(foto_sana(pide), "2026-11-25T15:35:00Z")
    assert mundo.de(r, "I12")[0].accion == "lanzar"
    [d] = mundo.de(r, "I1")
    assert d.accion == "esperar" and "se lanza en esta misma vuelta" in d.motivo
    assert destinos() == [DESTINO]
    # La misma foto otra vez (la corrida lanzada todavía no aparece en la lista): nada nuevo, y la ronda sigue esperando.
    for minutos in (36, 50):
        r = vuelta(foto_sana(pide), f"2026-11-25T15:{minutos}:00Z")
        [d] = mundo.de(r, "I1")
        assert d.accion == "esperar" and "su corrida todavía no aparece" in d.motivo
        assert not [x for x in r["decisiones"] if x.accion in pol.EJECUTABLES]
    assert destinos() == [DESTINO]
    # La vuelta siguiente ya ve la vigilancia viva: la ronda sigue esperando, ahora por la foto.
    viva_ = (FE, _en(corrida(VLE, None, titulo=titulo(wf, estados="colima", origen="supervisor")), "2026-11-25T15:35:20Z"))
    [d] = mundo.de(vuelta(foto_sana(pide, viva_), "2026-11-25T16:05:00Z"), "I1")
    assert d.accion == "esperar" and "en curso o en espera" in d.motivo and destinos() == [DESTINO]
    # Terminó: la cadena está libre y la ronda se lanza, una sola vez.
    hecha = (FE, _en(corrida(VLE, titulo=titulo(wf, estados="colima", origen="supervisor"),
                             jobs=[job("vigilar"), job("estado pendiente=[false]")]), "2026-11-25T15:35:20Z"))
    r = vuelta(foto_sana(pide, hecha), "2026-11-25T16:20:00Z")
    assert mundo.de(r, "I1")[0].accion == "lanzar"
    assert destinos() == [DESTINO, f"{FE}/expandir-estatales.yml"]
    r = vuelta(foto_sana(pide, hecha), "2026-11-25T16:21:00Z")
    assert not [x for x in r["decisiones"] if x.accion in pol.EJECUTABLES] and len(destinos()) == 2


def test_la_ronda_espera_aunque_la_vigilancia_se_decida_despues(cadenas, foto_sana):
    """El orden de evaluación no importa: la cadencia de la vigilancia se decide después que la cita de la ronda."""
    import dataclasses
    con = dataclasses.replace(cadenas, workflows=tuple(
        dataclasses.replace(w, lanza_horario=w.archivo == "expandir-estatales.yml", cadencias=w.cadencias if w.archivo == VLE else ())
        for w in cadenas.workflows))
    ahora = instante("2026-11-25T15:35:00Z")
    foto = foto_sana(sin=((FE, VLE),))
    hallazgos = [h for h in inv.evaluar(foto, con, ahora) if h.workflow in (VLE, "expandir-estatales.yml")]
    assert sorted(h.workflow for h in hallazgos) == ["expandir-estatales.yml", VLE]
    for orden in (hallazgos, hallazgos[::-1]):
        acciones = {d.hallazgo.workflow: (d.accion, d.motivo) for d in pol.decidir(orden, {}, [], foto, con, ahora)}
        assert acciones[VLE][0] == "lanzar"
        assert acciones["expandir-estatales.yml"] == ("esperar", f"se lanza cuando la cadena quede libre: {VLE} se lanza en esta misma vuelta")
    # Un lanzamiento que falló no ocupa la cadena, ni uno de antes de la gracia, ni el de otro workflow.
    ronda = [h for h in hallazgos if h.workflow == "expandir-estatales.yml"]
    libre = foto_sana()
    linea = {"accion": "lanzar", "cuando": "2026-11-25T15:20:00Z", "resultado": "ok", "huella": "x",
             "detalle": {"repo": FE, "workflow": VLE, "inputs": {}}}
    decide = lambda *acciones: pol.decidir(ronda, {}, list(acciones), libre, con, ahora)[0].accion  # noqa: E731
    assert decide(linea) == "esperar"
    assert decide(linea | {"resultado": "error: HTTP 422"}) == "lanzar"
    assert decide(linea | {"cuando": "2026-11-25T15:00:00Z"}) == "lanzar"
    assert decide(linea | {"detalle": {"repo": FE, "workflow": "vigilar-federal.yml"}}) == "lanzar"
    assert decide(linea | {"accion": "cancelar-y-relanzar", "detalle": linea["detalle"] | {"relanzar": False}}) == "lanzar"
    assert decide({"accion": "arreglo-publicado", "cuando": "2026-11-25T15:20:00Z", "resultado": "ok", "huella": "x",
                   "detalle": {"repo": FE, "workflow": VLE, "prueba": {"lanzada": True}}}) == "esperar"


def test_la_cadencia_de_la_ronda_tambien_espera_a_la_cadena(cadenas, foto_sana, mundo):
    """Sin corrida exitosa en su ventana, la ronda se lanza por cadencia solo con la cadena libre, como su cita."""
    wf = cadenas.workflow(FE, VLE)
    sin_ronda = ((FE, "expandir-estatales.yml"),)
    destinos = lambda: [destino for destino, _ in mundo.cliente.lanzamientos() if destino.startswith(FE)]  # noqa: E731
    viva_ = (FE, corrida(VLE, None, hace_h=1, titulo=titulo(wf, estados="jalisco")))
    [d] = mundo.de(mundo.vuelta(foto_sana(viva_, sin=sin_ronda)), "I1", "expandir-estatales.yml")
    assert d.hallazgo.base == "cadencia-0" and d.accion == "esperar"
    assert "cuando la cadena quede libre" in d.motivo and "en curso o en espera" in d.motivo
    # Un PR automático abierto de esa cadena también la ocupa.
    pr = {"number": 77, "head_ref": "reforma/leyes-estatales", "labels": ["automatico"], "created_at": t(1),
          "updated_at": t(1), "cross": False}
    [d] = mundo.de(mundo.vuelta(foto_sana(sin=sin_ronda, datos={"leyes-estatales": {"prs": [pr]}})), "I1", "expandir-estatales.yml")
    assert d.accion == "esperar" and "PR #77" in d.motivo
    # Y la vigilancia que esta misma vuelta lanza por su cadencia, se decida antes o después.
    r = mundo.vuelta(foto_sana(sin=sin_ronda + ((FE, VLE),)))
    assert mundo.de(r, "I1", VLE)[0].accion == "lanzar"
    [d] = mundo.de(r, "I1", "expandir-estatales.yml")
    assert d.accion == "esperar" and "se lanza en esta misma vuelta" in d.motivo
    assert destinos() == [DESTINO]
    # Libre: se lanza, una vez. Las demás cadencias no cambian: quien no declara espera_libre no espera a nadie.
    r = mundo.vuelta(foto_sana(sin=sin_ronda), AHORA + timedelta(hours=2))
    assert mundo.de(r, "I1", "expandir-estatales.yml")[0].accion == "lanzar"
    assert destinos() == [DESTINO, f"{FE}/expandir-estatales.yml"]
    mundo.vuelta(foto_sana(sin=sin_ronda), AHORA + timedelta(hours=2, minutes=1))
    assert len(destinos()) == 2


# ---- I8 sin el aviso de los workflows ----

def test_la_web_se_despliega_sola_y_un_despliegue_roto_no_entra_en_bucle(cadenas, foto_sana, mundo):
    W = "constitucion-web"
    nuevo = {"congreso-mexicano": {"main": {"sha": "9" * 40, "fecha": t(0.5)}}}
    r = mundo.vuelta(foto_sana(datos=nuevo))
    assert _acciones(r) == [("I8", "desplegar-web")]
    assert mundo.cliente.lanzamientos() == [(f"{W}/deploy.yml", {})]
    assert mundo.cliente.llamadas[0][2] == {"ref": "main", "inputs": {}}
    # El despliegue lanzado falla: I8 calla y la falla tiene dueño (I2), que reintenta a la hora y una sola vez.
    roto = corrida("deploy.yml", "failure", hace_h=-0.1, dura_h=0.05, titulo="Deploy a producción",
                   jobs=falla("deploy", "Deploy (Worker + assets)", "✘ [ERROR] A request to the Cloudflare API failed"))
    otro = roto | {"id": roto["id"] + 1, "created_at": t(-1.3), "run_started_at": t(-1.3), "updated_at": t(-1.35)}
    lanzados = []
    for minutos in range(15, 6 * 60, 15):
        # El reintento (a los 75 minutos) también falla: su corrida aparece en la vuelta siguiente.
        foto = foto_sana((W, roto), datos=nuevo) if minutos <= 75 else foto_sana((W, roto), (W, otro), datos=nuevo)
        r = mundo.vuelta(foto, AHORA + timedelta(minutes=minutos))
        assert not mundo.de(r, "I8")
        lanzados += [minutos for d in mundo.de(r, "I2") if d.accion == "lanzar"]
    assert lanzados == [75] and [l for l in mundo.cliente.lanzamientos() if l[0].startswith(W)] == [(f"{W}/deploy.yml", {})] * 2
    # La web está fuera del alcance del agente de infraestructura: queda la constancia, una sola vez.
    [d] = mundo.de(r, "I2")
    assert d.accion == "nada" and r["estado"].huellas[d.huella]["estado"] == "agotada" and mundo.cliente.issues == 1
    # Un commit de datos posterior vuelve a pedir su despliegue.
    mas_nuevo = {"congreso-mexicano": {"main": {"sha": "8" * 40, "fecha": t(-6)}}}
    r = mundo.vuelta(foto_sana((W, roto), (W, otro), datos=mas_nuevo), AHORA + timedelta(hours=6, minutes=20))
    assert mundo.de(r, "I8")[0].accion == "desplegar-web"


# ---- «Sin observar» no es «ok» ----

def test_el_latido_distingue_sin_observar_de_ok(cadenas, foto_sana, mundo):
    r = mundo.vuelta(foto_sana())
    assert r["latido"]["invariantes"]["I8"] == "ok" and r["latido"]["motivos"] == {}
    assert r["latido"]["invariantes"]["I10"] == "apagada"
    ciega = foto_sana(datos={"leyes-federales": {"main": {"sha": "c" * 40, "fecha": t(0.5)}}})
    ciega["repos"]["constitucion-web"] = {"observable": False, "corridas": [], "ultima_exitosa": {}}
    ciega["motivos"] = {"constitucion-web": "HTTP 404"}
    r = mundo.vuelta(ciega, AHORA + timedelta(minutes=15))
    estados = r["latido"]["invariantes"]
    assert estados["I8"] == estados["I11"] == estados["I2"] == "sin-observar" and estados["I5"] == estados["I6"] == "ok"
    assert r["latido"]["motivos"]["I8"] == "constitucion-web: HTTP 404"
    assert mundo.cliente.llamadas == []
    from supervisor.__main__ import resumen
    texto = resumen(r, ciega, "actuar")
    assert "I8 sin-observar" in texto and "- I8: constitucion-web: HTTP 404" in texto


def test_i10_un_worker_que_no_contesta_espera_su_tolerancia_antes_del_issue(cadenas, foto_sana, mundo):
    """Mientras el /salud de egress-mx no esté desplegado contesta 404: nada de issues hasta pasadas 6 h seguidas."""
    import dataclasses

    from supervisor.__main__ import correr
    from supervisor.actuar import Actor
    from supervisor.estado import Estado
    con = dataclasses.replace(cadenas, invariantes=cadenas.invariantes | {"I10": True})
    archivo = {"observable": True, "sitios": {}}

    def vuelta(egress, horas):
        estado = Estado(mundo.carpeta)
        foto = foto_sana()
        foto["workers"] = {"congreso-archivo": archivo, "egress-mx": egress}
        r = correr(foto, con, estado, AHORA + timedelta(hours=horas), Actor(mundo.cliente, con, "actuar"))
        estado.guardar()
        return r | {"estado": estado}

    sin_salud = {"observable": False, "motivo": "GET /salud: HTTP 404", "sin_salud": True}
    sano = {"observable": True, "salida": {"ok": True, "pais": "MX", "colo": "MEX"}}
    for horas in (0, 0.25, 3, 5.75):
        [d] = mundo.de(vuelta(sin_salud, horas), "I10")
        assert d.accion == "esperar" and d.proxima == iso(AHORA + timedelta(hours=6)) and mundo.cliente.issues == 0
    # Se desplegó: resuelta sin dejar rastro, y la cuenta empieza de cero si vuelve a pasar.
    r = vuelta(sano, 5.9)
    assert not mundo.de(r, "I10") and d.huella not in r["estado"].huellas and r["latido"]["invariantes"]["I10"] == "ok"
    assert mundo.de(vuelta(sin_salud, 7), "I10")[0].accion == "esperar"
    assert mundo.de(vuelta({"observable": False, "motivo": "GET /salud: HTTP 502", "sin_salud": True}, 12.9), "I10")[0].accion == "esperar"
    # Seis horas seguidas sin contestar: constancia, una sola vez aunque el código cambie.
    r = vuelta(sin_salud, 13)
    [d] = mundo.de(r, "I10")
    assert (d.accion, d.clase, d.hallazgo.objeto) == ("agotada", "determinista", "egress-mx/salud") and mundo.cliente.issues == 1
    assert "no contesta su /salud" in d.motivo
    assert mundo.de(vuelta({"observable": False, "motivo": "GET /salud: HTTP 500", "sin_salud": True}, 13.25), "I10")[0].accion == "nada"
    # Sin el token no se sabe: la huella sigue. Cuando contesta, se cierra su issue.
    r = vuelta({"observable": False, "motivo": "falta EGRESS_MX_TOKEN en el entorno"}, 13.5)
    assert r["estado"].huellas[d.huella]["estado"] == "agotada"
    r = vuelta(sano, 14)
    assert r["estado"].huellas[d.huella]["estado"] == "resuelta"
    # Lo mismo cuando contesta pero ya no sale desde México.
    fuera = {"observable": True, "salida": {"ok": True, "pais": None, "colo": "IAD"}}
    assert mundo.de(vuelta(fuera, 15), "I10")[0].accion == "esperar"
    [d] = mundo.de(vuelta(fuera, 21), "I10")
    assert d.accion == "agotada" and "ya no sale desde MX" in d.motivo and mundo.cliente.issues == 2


def test_i10_sin_token_sale_sin_observar_y_con_un_sitio_atorado_deja_constancia(cadenas, foto_sana, mundo):
    import dataclasses

    from supervisor.__main__ import correr
    from supervisor.actuar import Actor
    from supervisor.estado import Estado
    con = dataclasses.replace(cadenas, invariantes=cadenas.invariantes | {"I10": True})

    def vuelta(foto, minutos=0):
        estado = Estado(mundo.carpeta)
        r = correr(foto, con, estado, AHORA + timedelta(minutes=minutos), Actor(mundo.cliente, con, "actuar"))
        estado.guardar()
        return r | {"estado": estado}

    sin_token = foto_sana()
    sin_token["workers"] = {"congreso-archivo": {"observable": False, "motivo": "falta CONGRESO_ARCHIVO_TOKEN en el entorno"}}
    sin_token["motivos"] = {"congreso-archivo": "falta CONGRESO_ARCHIVO_TOKEN en el entorno"}
    r = vuelta(sin_token)
    assert r["latido"]["invariantes"]["I10"] == "sin-observar"
    assert r["latido"]["motivos"] == {"I10": "congreso-archivo: falta CONGRESO_ARCHIVO_TOKEN en el entorno; egress-mx: sin acceso"}

    def con_sitios(pendientes):
        foto = foto_sana()
        foto["workers"] = {"egress-mx": {"observable": True, "salida": {"ok": True, "pais": "MX", "colo": "MEX"}},
                           "congreso-archivo": {"observable": True, "sitios": {
            "gaceta.diputados.gob.mx": {"pendientes": pendientes, "pendiente_mas_antigua": t(7), "ultimo_exito": t(9)},
            "www.senado.gob.mx": {"pendientes": 3, "pendiente_mas_antigua": t(1), "ultimo_exito": t(0.1)}}}}
        return foto

    r = vuelta(con_sitios(12), 15)
    [d] = mundo.de(r, "I10")
    assert (d.accion, d.clase) == ("agotada", "determinista") and d.hallazgo.objeto == "congreso-archivo/gaceta.diputados.gob.mx"
    assert mundo.cliente.issues == 1 and mundo.cliente.lanzamientos() == []
    assert r["latido"]["invariantes"]["I10"] == "rota:1"
    # Las cuentas del sitio cambian en cada vuelta: la constancia no se repite.
    r = vuelta(con_sitios(40), 30)
    assert mundo.de(r, "I10")[0].accion == "nada" and mundo.cliente.issues == 1
    # Si el token deja de estar, la huella no se da por resuelta; cuando el sitio avanza, sí.
    r = vuelta(sin_token, 45)
    assert r["estado"].huellas[d.huella]["estado"] == "agotada"
    sano = con_sitios(0)
    r = vuelta(sano, 60)
    assert r["estado"].huellas[d.huella]["estado"] == "resuelta" and r["latido"]["invariantes"]["I10"] == "ok"


# ---- Una corrida de credenciales.yml que falla por una credencial por vencer: constancia, sin reintento ni agente ----

def test_credencial_por_renovar_deja_constancia_y_no_reintenta_ni_va_al_agente(cadenas, foto_sana, mundo):
    wf = cadenas.workflow(CE, "credenciales.yml")
    sin = ((CE, wf.archivo),)
    error = ("Credencial por renovar: PR_TOKEN vence el 2026-12-08 06:00 UTC, en menos de 30 días. "
             "Cómo: gh secret set PR_TOKEN --org OpenCodice --visibility all")

    def rota(hace_h):
        return (CE, corrida(wf.archivo, "failure", hace_h=hace_h, titulo=titulo(wf, origen="supervisor"),
                            jobs=falla("comprobar", "PR_TOKEN — no vence en los próximos 30 días", f"##[error]{error}")))

    primera = rota(0.2)
    r = mundo.vuelta(foto_sana(primera, sin=sin))
    [d] = mundo.de(r, "I2")
    assert (d.clase, d.accion, d.motivo) == ("credencial", "agotada", error)
    assert mundo.cliente.lanzamientos() == [] and _issues(mundo) == (1, 0, 0)
    assert "2026-12-08 06:00 UTC" in mundo.cliente.llamadas[-1][2]["body"]
    # Sin una corrida exitosa en su ventana, la cadencia tampoco lo relanza: lo vuelve a lanzar su horario.
    [cadencia] = mundo.de(r, "I1", "credenciales")
    assert cadencia.accion == "esperar" and d.huella in cadencia.motivo
    r = mundo.vuelta(foto_sana(primera, sin=sin), AHORA + timedelta(hours=3))
    assert mundo.de(r, "I2")[0].accion == "nada" and _issues(mundo) == (1, 0, 0)

    # La corrida de la semana siguiente falla igual: misma huella, un recordatorio en el mismo issue.
    semana = AHORA + timedelta(days=7)
    otra = (CE, corrida(wf.archivo, "failure", hace_h=-167.8, titulo=titulo(wf, origen="supervisor"),
                        jobs=falla("comprobar", "PR_TOKEN — no vence en los próximos 30 días", f"##[error]{error}")))
    r = mundo.vuelta(foto_sana(primera, otra, sin=sin), semana)
    [otro] = mundo.de(r, "I2")
    assert otro.huella == d.huella and otro.accion == "agotada" and _issues(mundo) == (1, 1, 0)
    assert not [destino for destino, _ in mundo.cliente.lanzamientos() if "credenciales" in destino]

    # Renovada: la corrida siguiente pasa y el issue se cierra.
    buena = (CE, corrida(wf.archivo, hace_h=-168.5, titulo=titulo(wf, origen="supervisor"),
                         jobs=[job("comprobar"), job("estado pendiente=[false]")]))
    r = mundo.vuelta(foto_sana(primera, otra, buena, sin=sin), semana + timedelta(hours=1))
    assert r["estado"].huellas[d.huella]["estado"] == "resuelta" and _issues(mundo) == (1, 2, 1)


# ---- I13: una credencial por vencer deja constancia, la recuerda cada semana y se cierra al renovarla ----

def _vence(foto, cuando, **cambios):
    foto["credenciales"]["github"] |= {"vence": cuando} | cambios
    return foto


def _issues(mundo):
    """(abiertos, comentarios, cerrados) que el supervisor ha escrito en su repo."""
    rutas = [(m, r, c) for m, r, c in mundo.cliente.llamadas if r.startswith("repos/OpenCodice/supervisor/issues")]
    return (sum(1 for m, r, _ in rutas if m == "POST" and r.endswith("/issues")),
            sum(1 for m, r, _ in rutas if m == "POST" and r.endswith("/comments")),
            sum(1 for m, r, c in rutas if m == "PATCH" and c == {"state": "closed"}))


def test_i13_constancia_recordatorio_semanal_y_cierre_al_renovar(cadenas, foto_sana, mundo):
    vence = iso(AHORA + timedelta(days=20))
    r = mundo.vuelta(_vence(foto_sana(), vence))
    [d] = mundo.de(r, "I13")
    assert (d.accion, d.clase, d.hallazgo.objeto) == ("agotada", "credencial", "SUPERVISOR_TOKEN")
    assert r["latido"]["invariantes"]["I13"] == "rota:1" and mundo.cliente.lanzamientos() == []
    [(_, _, abierto)] = [x for x in mundo.cliente.llamadas if x[1] == "repos/OpenCodice/supervisor/issues"]
    assert abierto["title"] == f"[{d.huella}] 🔑 SUPERVISOR_TOKEN vence el {vence[:10]}"
    assert "gh secret set SUPERVISOR_TOKEN -R OpenCodice/supervisor" in abierto["body"] and "faltan 20 días" in abierto["body"]
    assert "Issues" in abierto["body"] and "no es una asignación" in abierto["body"]

    # La misma foto, y las de los días siguientes, no repiten el aviso ni cambian su huella.
    anotada = r["estado"].huellas[d.huella]
    r = mundo.vuelta(_vence(foto_sana(), vence), AHORA + timedelta(minutes=15))
    assert mundo.de(r, "I13")[0].accion == "nada" and _issues(mundo) == (1, 0, 0)
    assert r["estado"].huellas[d.huella] == anotada
    r = mundo.vuelta(_vence(foto_sana(), vence), AHORA + timedelta(days=6, hours=23))
    assert _issues(mundo) == (1, 0, 0) and r["estado"].huellas[d.huella] == anotada

    # A la semana, un recordatorio en el mismo issue, con los días que quedan.
    r = mundo.vuelta(_vence(foto_sana(), vence), AHORA + timedelta(days=7))
    assert mundo.de(r, "I13")[0].accion == "agotada" and _issues(mundo) == (1, 1, 0)
    assert "faltan 13 días" in mundo.cliente.llamadas[-1][2]["body"]
    mundo.vuelta(_vence(foto_sana(), vence), AHORA + timedelta(days=8))
    assert _issues(mundo) == (1, 1, 0)

    # Una vuelta que no supo nada del token (o que corrió sin el secret) no lo da por renovado.
    r = mundo.vuelta(_vence(foto_sana(), None, observado=False), AHORA + timedelta(days=9))
    assert r["estado"].huellas[d.huella]["estado"] == "agotada" and r["latido"]["invariantes"]["I13"] == "sin-observar"
    r = mundo.vuelta(_vence(foto_sana(), None, propio=False), AHORA + timedelta(days=9, minutes=15), modo="informe")
    assert r["estado"].huellas[d.huella]["estado"] == "agotada" and _issues(mundo) == (1, 1, 0)

    # Renovado: la fecha cambió y queda lejos. El issue se cierra solo.
    r = mundo.vuelta(_vence(foto_sana(), iso(AHORA + timedelta(days=375))), AHORA + timedelta(days=10))
    assert r["estado"].huellas[d.huella]["estado"] == "resuelta" and _issues(mundo) == (1, 2, 1)
    assert r["latido"]["invariantes"]["I13"] == "ok"


def test_i13_renovado_por_poco_tiempo_vuelve_a_avisar_con_la_fecha_nueva(foto_sana, mundo):
    r = mundo.vuelta(_vence(foto_sana(), iso(AHORA + timedelta(days=3))))
    [d] = mundo.de(r, "I13")
    r = mundo.vuelta(_vence(foto_sana(), iso(AHORA + timedelta(days=25))), AHORA + timedelta(days=1))
    assert mundo.de(r, "I13")[0].accion == "agotada" and _issues(mundo) == (1, 1, 0)
    assert "faltan 24 días" in mundo.cliente.llamadas[-1][2]["body"] and mundo.de(r, "I13")[0].huella == d.huella


def test_i13_un_permiso_perdido_deja_constancia_y_se_cierra_cuando_vuelve(cadenas, foto_sana, mundo):
    negado = [{"que": "lanzar o cancelar corridas en congreso-extractor", "estado": 403, "origen": "accion", "cuando": t(1)}]
    r = mundo.vuelta(_vence(foto_sana(), None, rechazos=negado))
    [d] = mundo.de(r, "I13")
    assert (d.accion, d.hallazgo.objeto) == ("agotada", "SUPERVISOR_TOKEN/permisos")
    assert _issues(mundo) == (1, 0, 0) and mundo.cliente.lanzamientos() == []
    mundo.vuelta(_vence(foto_sana(), None, rechazos=negado), AHORA + timedelta(minutes=15))
    assert _issues(mundo) == (1, 0, 0)
    r = mundo.vuelta(foto_sana(), AHORA + timedelta(minutes=30))
    assert r["estado"].huellas[d.huella]["estado"] == "resuelta" and _issues(mundo) == (1, 1, 1)


def test_i13_un_aviso_que_no_se_pudo_escribir_no_se_reintenta_en_cada_vuelta(foto_sana, mundo):
    mundo.cliente.falla_en = "/issues"
    foto = lambda: _vence(foto_sana(), iso(AHORA + timedelta(days=5)))  # noqa: E731
    mundo.vuelta(foto())
    mundo.vuelta(foto(), AHORA + timedelta(minutes=15))
    assert len(mundo.cliente.llamadas) == 1
    mundo.cliente.falla_en = None
    r = mundo.vuelta(foto(), AHORA + timedelta(hours=6))
    assert mundo.de(r, "I13")[0].accion == "agotada" and _issues(mundo) == (2, 0, 0)


def test_i13_sin_el_secret_es_un_hallazgo_y_el_worker_que_rechaza_su_token_tambien(cadenas, foto_sana, mundo):
    from supervisor.__main__ import resumen
    foto = _vence(foto_sana(), None, propio=False)
    r = mundo.vuelta(foto, modo="informe")
    [d] = mundo.de(r, "I13")
    assert d.hallazgo.objeto == "SUPERVISOR_TOKEN/ausente" and r["latido"]["invariantes"]["I13"] == "rota:1"
    assert mundo.cliente.llamadas == []
    texto = resumen(r, foto, "informe", cadenas)
    assert "modo informe y nada se lanza ni se relanza" in texto and "- SUPERVISOR_TOKEN: falta; la vuelta usó el token del job" in texto

    foto = foto_sana()
    foto["workers"] = {"congreso-archivo": {"observable": False, "motivo": "GET /salud: HTTP 401", "rechazo": 401}}
    r = mundo.vuelta(foto, AHORA + timedelta(minutes=15))
    [d] = mundo.de(r, "I13")
    assert (d.accion, d.hallazgo.objeto) == ("agotada", "CONGRESO_ARCHIVO_TOKEN@congreso-archivo") and _issues(mundo) == (1, 0, 0)
    # El Worker caído no es el Worker aceptando el token: la huella sigue abierta hasta que conteste bien.
    foto["workers"] = {"congreso-archivo": {"observable": False, "motivo": "GET /salud: OSError"}}
    r = mundo.vuelta(foto, AHORA + timedelta(minutes=30))
    assert r["estado"].huellas[d.huella]["estado"] == "agotada"
    foto["workers"] = {"congreso-archivo": {"observable": True, "sitios": {}}}
    r = mundo.vuelta(foto, AHORA + timedelta(minutes=45))
    assert r["estado"].huellas[d.huella]["estado"] == "resuelta" and _issues(mundo) == (1, 1, 1)
