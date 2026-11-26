import dataclasses
from datetime import datetime, timedelta, timezone

import pytest
from conftest import AHORA, con_horario, corrida, falla, job, t, titulo

from supervisor import invariantes as inv

FE = "fiscal-extractor"
VLE = "vigilar-leyes-estatales.yml"


def _wf(cadenas, repo=FE, archivo=VLE):
    return cadenas.workflow(repo, archivo)


def _solo(hallazgos, invariante, contiene=""):
    return [h for h in hallazgos if h.invariante == invariante and contiene in h.objeto]


def test_gramatica_del_titulo(cadenas):
    wf = _wf(cadenas)
    c = {"display_title": "vigilar-leyes-estatales docs=[] estados=[jalisco colima] lote=[8] revisar=[jalisco/codigo-civil] origen=[reparar]"}
    assert inv.inputs_de(c, wf) == {"docs": "", "estados": "jalisco colima", "lote": "8",
                                    "revisar": "jalisco/codigo-civil", "origen": "reparar"}
    # Un input que el workflow ya no declara (títulos de corridas viejas) se ignora.
    viejo = {"display_title": "vigilar-leyes-estatales reintento=[true] docs=[] estados=[colima] lote=[8] revisar=[] origen=[salud]"}
    assert inv.inputs_de(viejo, wf)["estados"] == "colima" and "reintento" not in inv.inputs_de(viejo, wf)
    # Los títulos anteriores al contrato, y los de push o schedule, son inputs desconocidos.
    assert inv.inputs_de({"display_title": "Vigilar leyes estatales"}, wf) is None
    assert inv.inputs_de({"display_title": "arreglo"}, wf) is None
    assert inv.inputs_de({"display_title": "vigilar-federal docs=[]"}, wf) is None
    assert inv.analizar_titulo("x k=[a] k_dos=[]") == ("x", {"k": "a", "k_dos": ""})
    assert inv.analizar_titulo("x k=[a]] ") is None


def test_titulos_previos_de_congreso(cadenas):
    wf = _wf(cadenas, "congreso-extractor", "actualizar-congreso.yml")
    assert inv.inputs_de({"display_title": "Actualizar congreso-mexicano (solo pedir)"}, wf)["publicar"] == "no"
    assert inv.inputs_de({"display_title": "Actualizar congreso-mexicano"}, wf)["publicar"] == "si"
    assert inv.inputs_de({"display_title": "actualizar-congreso legislatura=[66] publicar=[no] origen=[scheduler]"}, wf)["publicar"] == "no"


def test_abarca_y_unir(cadenas):
    wf = _wf(cadenas)

    def i(**k):
        return {x: k.get(x, wf.valor_omision(x)) for x in wf.inputs}

    assert inv.abarca(i(), i(estados="jalisco"), wf)                    # vacío = todo
    assert not inv.abarca(i(estados="jalisco"), i(), wf)
    assert inv.abarca(i(estados="jalisco colima"), i(estados="colima"), wf)
    assert not inv.abarca(i(estados="jalisco"), i(estados="colima"), wf)
    assert inv.abarca(i(estados="jalisco", lote="3"), i(estados="jalisco"), wf)   # ignorados
    assert not inv.abarca(i(estados="jalisco"), i(revisar="jalisco/codigo-civil"), wf)
    assert inv.abarca(None, None, wf) and inv.abarca(i(), None, wf)
    assert not inv.abarca(None, i(), wf) and not inv.abarca(i(estados="jalisco"), None, wf)

    assert inv.unir([i(estados="nuevo-leon"), i(estados="colima")], wf)["estados"] == "colima nuevo-leon"
    assert inv.unir([i(estados="nuevo-leon"), i()], wf)["estados"] == ""
    assert inv.unir([i(estados="nuevo-leon"), None], wf) is None
    assert inv.para_lanzar(i(estados="colima", lote="8", origen="salud"), wf) == {"estados": "colima"}


def test_foto_sana_no_tiene_hallazgos(cadenas, foto_sana):
    assert inv.evaluar(foto_sana(), cadenas, AHORA) == []


def test_i1_sin_corrida_exitosa_en_la_ventana(cadenas, foto_sana):
    wf = _wf(cadenas)
    vieja = corrida(VLE, hace_h=9 * 24, titulo=titulo(wf))
    parcial = corrida(VLE, hace_h=5, titulo=titulo(wf, estados="jalisco"))
    foto = foto_sana((FE, vieja), (FE, parcial), sin=((FE, VLE),))
    [h] = _solo(inv.evaluar(foto, cadenas, AHORA), "I1")
    assert (h.repo, h.workflow) == (FE, VLE) and h.en_vuelo is None
    assert inv.para_lanzar(h.inputs, wf) == {}
    # Con una completa en curso, sigue rota pero ya hay quien la resuelva.
    foto = foto_sana((FE, vieja), (FE, corrida(VLE, None, hace_h=0.2, titulo=titulo(wf))), sin=((FE, VLE),))
    [h] = _solo(inv.evaluar(foto, cadenas, AHORA), "I1")
    assert h.en_vuelo is not None


def test_i1_congreso_exige_publicar_si_cada_26_h(cadenas, foto_sana):
    wf = _wf(cadenas, "congreso-extractor", "actualizar-congreso.yml")
    foto = foto_sana(("congreso-extractor", corrida(wf.archivo, hace_h=1, titulo=titulo(wf, publicar="no"))),
                     ("congreso-extractor", corrida(wf.archivo, hace_h=30, titulo=titulo(wf, publicar="si"))),
                     sin=(("congreso-extractor", wf.archivo),))
    [h] = _solo(inv.evaluar(foto, cadenas, AHORA), "I1")
    assert h.inputs["publicar"] == "si" and "publicar=si" in h.objeto


def test_i1_usa_la_ultima_exitosa_fuera_de_la_foto(cadenas, foto_sana):
    foto = foto_sana(sin=(("constitucion-extractor", "credenciales.yml"),))
    assert _solo(inv.evaluar(foto, cadenas, AHORA), "I1", "credenciales")
    foto["repos"]["constitucion-extractor"]["ultima_exitosa"]["credenciales.yml"] = corrida("credenciales.yml", hace_h=5 * 24)
    assert not _solo(inv.evaluar(foto, cadenas, AHORA), "I1", "credenciales")


CON_HORARIO = ("vigilar-fiscal.yml", "vigilar-cpeum.yml", "vigilar-estatales.yml", "vigilar-federal.yml",
               "vigilar-leyes-estatales.yml", "expandir-estatales.yml", "actualizar-congreso.yml", "credenciales.yml")


def _citas(foto, cadenas, cuando: str) -> dict:
    """{workflow: hora de la cita} de lo que toca lanzar por horario en ese instante."""
    hallazgos = inv.i1(foto, con_horario(cadenas, *CON_HORARIO), inv.instante(cuando))
    return {h.workflow.removesuffix(".yml"): h.extra["horario"][5:16] for h in hallazgos}


# 2026-11-23 es lunes; el 22, domingo; el 25, miércoles; el 1 de diciembre, martes.
LUNES_9 = {"vigilar-fiscal": "11-23T09:00", "vigilar-cpeum": "11-23T09:00", "vigilar-estatales": "11-23T09:00"}
LUNES_13 = {"vigilar-federal": "11-23T13:00", "vigilar-leyes-estatales": "11-23T13:00"}
CREDENCIALES = {"credenciales": "11-23T08:00"}


@pytest.mark.parametrize("cuando, toca", [
    # Lunes es lunes: ni el domingo ni el martes a esa hora (en el cron de Cloudflare el 1 es domingo).
    ("2026-11-22T09:07:00Z", {"actualizar-congreso": "11-22T09:00"}),
    ("2026-11-24T09:07:00Z", {"actualizar-congreso": "11-24T09:00"}),
    # Las credenciales, los lunes a las 08:00, antes de las vigilancias.
    ("2026-11-23T07:59:00Z", {"actualizar-congreso": "11-23T07:00"}),
    ("2026-11-23T08:59:00Z", {"actualizar-congreso": "11-23T08:00"} | CREDENCIALES),
    ("2026-11-23T09:00:00Z", LUNES_9 | {"actualizar-congreso": "11-23T09:00"} | CREDENCIALES),
    # El tic puede llegar tarde o faltar: vale el primero dentro de la tolerancia (180 min).
    ("2026-11-23T09:47:00Z", LUNES_9 | {"actualizar-congreso": "11-23T09:00"} | CREDENCIALES),
    ("2026-11-23T12:00:00Z", LUNES_9 | {"actualizar-congreso": "11-23T12:00"}),
    ("2026-11-23T12:01:00Z", {"actualizar-congreso": "11-23T12:00"}),
    ("2026-11-23T13:02:00Z", LUNES_13 | {"actualizar-congreso": "11-23T13:00"}),
    ("2026-11-23T16:01:00Z", {"actualizar-congreso": "11-23T16:00"}),
    # Congreso: cada hora en el minuto 0 «solo pedir», y a las 18:00 la que publica.
    ("2026-11-23T17:59:00Z", {"actualizar-congreso": "11-23T17:00"}),
    ("2026-11-23T18:14:00Z", {"actualizar-congreso": "11-23T18:00"}),
    ("2026-11-23T19:00:00Z", {"actualizar-congreso": "11-23T19:00"}),
    # La ronda, los miércoles a las 15:30.
    ("2026-11-25T15:29:00Z", {"actualizar-congreso": "11-25T15:00"}),
    ("2026-11-25T15:31:00Z", {"actualizar-congreso": "11-25T15:00", "expandir-estatales": "11-25T15:30"}),
    ("2026-12-01T10:10:00Z", {"actualizar-congreso": "12-01T10:00"}),
])
def test_tabla_de_horarios(cadenas, foto_sana, cuando, toca):
    foto = foto_sana()
    # La ronda de la semana anterior ya corrió: sin esto su cita seguiría pendiente (espera_libre no caduca).
    ronda = cadenas.workflow(FE, "expandir-estatales.yml")
    for dia in ("2026-11-18", "2026-11-25", "2026-12-30"):
        foto["repos"][FE]["corridas"].append(corrida(ronda.archivo, titulo=titulo(ronda)) | {"created_at": f"{dia}T15:31:00Z"})
    foto["repos"][FE]["corridas"] = [c for c in foto["repos"][FE]["corridas"] if c["created_at"] < cuando]
    assert _citas(foto, cadenas, cuando) == toca


@pytest.mark.parametrize("cuando, cita", [
    ("2026-12-01T10:10:00Z", "12-01T10:00"), ("2026-12-01T13:30:00Z", None), ("2026-12-02T10:10:00Z", None),
    ("2027-01-01T10:00:00Z", "01-01T10:00"),
])
def test_un_horario_por_dia_del_mes(cadenas, foto_sana, cuando, cita):
    mensual = dataclasses.replace(cadenas, workflows=tuple(
        dataclasses.replace(w, lanza_horario=True, cadencias=(), horario=({"dia_mes": 1, "hora": "10:00"},))
        if w.archivo == "credenciales.yml" else w for w in cadenas.workflows))
    citas = [h.extra["horario"][5:16] for h in inv.i1(foto_sana(), mensual, inv.instante(cuando)) if "horario" in h.extra]
    assert citas == ([cita] if cita else [])


def test_congreso_publica_a_las_18_y_el_resto_solo_pide(cadenas, foto_sana):
    con = con_horario(cadenas, "actualizar-congreso.yml")
    wf = con.workflow("congreso-extractor", "actualizar-congreso.yml")
    [h] = inv.i1(foto_sana(), con, inv.instante("2026-11-23T18:05:00Z"))
    assert inv.para_lanzar(h.inputs, wf) == {} and h.inputs["publicar"] == "si"
    [h] = inv.i1(foto_sana(), con, inv.instante("2026-11-23T19:05:00Z"))
    assert inv.para_lanzar(h.inputs, wf) == {"publicar": "no"}


def test_i1_por_horario_solo_con_el_interruptor_y_una_vez_por_cita(cadenas, foto_sana):
    lunes = datetime(2026, 11, 23, 9, 20, tzinfo=timezone.utc)
    wf = _wf(cadenas, FE, "vigilar-fiscal.yml")
    con = con_horario(cadenas, "vigilar-fiscal.yml")
    foto = foto_sana()
    [h] = _solo(inv.evaluar(foto, con, lunes), "I1")
    assert h.base == "horario-2026-11-23T09:00:00Z" and h.workflow == "vigilar-fiscal.yml"
    # Ya hay una corrida creada a su hora: nada.
    foto["repos"][FE]["corridas"].append(corrida("vigilar-fiscal.yml", None, titulo=titulo(wf)) | {"created_at": "2026-11-23T09:00:10Z"})
    assert not _solo(inv.evaluar(foto, con, lunes), "I1")
    # Pasada la tolerancia ya no se lanza por horario (lo cubre la cadencia).
    assert not _solo(inv.evaluar(foto_sana(), con, lunes + timedelta(hours=5)), "I1")
    assert not _solo(inv.evaluar(foto_sana(), con_horario(cadenas), lunes), "I1")


def test_i1_horario_que_corrida_cuenta_como_la_de_esa_cita(cadenas, foto_sana):
    lunes = datetime(2026, 11, 23, 13, 5, tzinfo=timezone.utc)
    wf = _wf(cadenas)
    con = con_horario(cadenas, VLE)

    def toca(*extra):
        return bool(_solo(inv.evaluar(foto_sana(*[(FE, c) for c in extra]), con, lunes), "I1"))

    creada = lambda **k: {"created_at": "2026-11-23T13:01:00Z"} | k  # noqa: E731
    assert toca()
    # Una corrida parcial (un estado, pedida por otra) no es la vigilancia de la semana; una de prueba, tampoco.
    assert toca(corrida(VLE, None, titulo=titulo(wf, estados="jalisco")) | creada())
    assert not toca(corrida(VLE, None, titulo=titulo(wf)) | creada())
    assert not toca(corrida(VLE, "failure", titulo=titulo(wf, lote="3", origen="supervisor")) | creada())
    # Una completa que ya esperaba su turno desde antes hará ese trabajo: no se le forma otra detrás.
    assert not toca(corrida(VLE, None, status="pending", titulo=titulo(wf)) | {"created_at": "2026-11-23T11:00:00Z"})
    assert toca(corrida(VLE, None, titulo=titulo(wf)) | {"created_at": "2026-11-23T11:00:00Z"})
    # Congreso: la que publica no cuenta como la «solo pedir» de la hora, ni al revés.
    cg = _wf(cadenas, "congreso-extractor", "actualizar-congreso.yml")
    con = con_horario(cadenas, cg.archivo)
    foto = foto_sana(("congreso-extractor", corrida(cg.archivo, None, titulo=titulo(cg, publicar="si")) | {"created_at": "2026-11-23T13:00:30Z"}))
    assert _solo(inv.evaluar(foto, con, lunes), "I1")
    foto = foto_sana(("congreso-extractor", corrida(cg.archivo, None, titulo=titulo(cg, publicar="no")) | {"created_at": "2026-11-23T13:00:30Z"}))
    assert not _solo(inv.evaluar(foto, con, lunes), "I1")


def test_i1_la_ronda_espera_a_que_la_cadena_quede_libre(cadenas, foto_sana):
    """espera_libre: la cita no caduca a los 180 minutos y lleva el motivo mientras la cadena esté ocupada."""
    con = con_horario(cadenas, "expandir-estatales.yml")
    ronda, wf = _wf(cadenas, FE, "expandir-estatales.yml"), _wf(cadenas)
    miercoles = datetime(2026, 11, 25, 15, 35, tzinfo=timezone.utc)
    viva_ = (FE, corrida(VLE, None, hace_h=1, titulo=titulo(wf, estados="jalisco")))
    [h] = _solo(inv.evaluar(foto_sana(viva_), con, miercoles), "I1")
    assert h.base == "horario-2026-11-25T15:30:00Z" and "en curso o en espera" in h.extra["ocupada"]
    # Dos días después sigue pendiente; con la cadena libre ya no lleva motivo de espera.
    [h] = _solo(inv.evaluar(foto_sana(viva_), con, miercoles + timedelta(days=2)), "I1")
    assert h.extra.get("ocupada")
    [h] = _solo(inv.evaluar(foto_sana(), con, miercoles + timedelta(days=2)), "I1")
    assert h.base == "horario-2026-11-25T15:30:00Z" and "ocupada" not in h.extra
    # Un PR automático abierto de esa cadena también la ocupa.
    [h] = _solo(inv.evaluar(foto_sana(datos={"leyes-estatales": {"prs": [_pr()]}}), con, miercoles), "I1")
    assert "PR #77" in h.extra["ocupada"]
    # Una ronda pedida (`estado=`) no es la de la semana; la de la semana, sí.
    pedida = corrida(ronda.archivo, titulo=titulo(ronda, estado="colima")) | {"created_at": "2026-11-25T16:00:00Z"}
    assert _solo(inv.evaluar(foto_sana((FE, pedida)), con, miercoles + timedelta(hours=1)), "I1")
    semanal = corrida(ronda.archivo, titulo=titulo(ronda)) | {"created_at": "2026-11-25T16:00:00Z"}
    assert not _solo(inv.evaluar(foto_sana((FE, semanal)), con, miercoles + timedelta(hours=1)), "I1")
    # A la cita siguiente, la anterior se da por perdida: solo cuenta la más reciente.
    [h] = _solo(inv.evaluar(foto_sana(), con, miercoles + timedelta(days=7)), "I1")
    assert h.base == "horario-2026-12-02T15:30:00Z"


def test_ultima_cita_de_congreso(cadenas):
    wf = _wf(cadenas, "congreso-extractor", "actualizar-congreso.yml")
    cita, entrada = inv._ultima_cita(wf.horario, datetime(2026, 11, 18, 18, 7, tzinfo=timezone.utc))
    assert cita.hour == 18 and entrada["inputs"]["publicar"] == "si"
    cita, entrada = inv._ultima_cita(wf.horario, datetime(2026, 11, 18, 19, 7, tzinfo=timezone.utc))
    assert cita.hour == 19 and entrada["inputs"]["publicar"] == "no"
    ronda = _wf(cadenas, FE, "expandir-estatales.yml")
    cita, _ = inv._ultima_cita(ronda.horario, datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc))
    assert (cita.weekday(), cita.hour, cita.minute) == (2, 15, 30)


def test_i2_falla_sin_corrida_posterior(cadenas, foto_sana, real):
    wf = _wf(cadenas)
    c = real("fiscal-extractor#44246222455", display_title=titulo(wf, origen="continuar"))
    [h] = _solo(inv.evaluar(foto_sana((FE, c)), cadenas, AHORA), "I2")
    assert h.base == "44246222455" and h.repeticiones == 1 and h.relanzable
    assert h.falla["paso"] == "Reintentar las leyes con el extractor reparado"
    assert h.cuando == "2026-11-18T06:37:33Z"


def test_i2_una_exitosa_posterior_que_abarca_la_cierra(cadenas, foto_sana, real):
    wf = _wf(cadenas)
    mala = real("fiscal-extractor#44246222455", display_title=titulo(wf, estados="jalisco"))
    buena = real("fiscal-extractor#44253703004", display_title=titulo(wf))
    assert not _solo(inv.evaluar(foto_sana((FE, mala), (FE, buena)), cadenas, AHORA), "I2")
    # Una exitosa de otro estado no la abarca.
    otra = real("fiscal-extractor#44253703004", display_title=titulo(wf, estados="colima"))
    assert _solo(inv.evaluar(foto_sana((FE, mala), (FE, otra)), cadenas, AHORA), "I2")
    # Las anteriores al contrato (inputs desconocidos) se dan por superadas por cualquier exitosa posterior.
    assert not _solo(inv.evaluar(foto_sana((FE, real("fiscal-extractor#44246222455")),
                                           (FE, real("fiscal-extractor#44253703004"))), cadenas, AHORA), "I2")


def test_i2_cuenta_la_racha_de_la_misma_huella(cadenas, foto_sana, real):
    foto = foto_sana((FE, real("fiscal-extractor#44246222455")), (FE, real("fiscal-extractor#44247025218")),
                     sin=((FE, VLE),))
    [h] = _solo(inv.evaluar(foto, cadenas, AHORA), "I2")
    assert h.base == "44247025218" and h.repeticiones == 2 and h.corridas == [44246222455, 44247025218]
    assert h.inputs is None and h.relanzable    # las vigilancias se relanzan sin inputs


def test_i2_las_pruebas_a_mano_no_se_relanzan(cadenas, foto_sana):
    wf = _wf(cadenas, FE, "expandir-estatales.yml")
    c = corrida(wf.archivo, "failure", titulo=titulo(wf, ensayo="true"), jobs=falla("ronda", "Elegir el estado", "boom"))
    assert not _solo(inv.evaluar(foto_sana((FE, c)), cadenas, AHORA), "I2")


def test_i2_inputs_desconocidos_sin_relanzamiento_seguro(cadenas, foto_sana):
    c = corrida("actualizar-congreso.yml", "failure", titulo="Algo sin gramática", jobs=falla("actualizar", "Padrón", "boom"))
    [h] = _solo(inv.evaluar(foto_sana(("congreso-extractor", c)), cadenas, AHORA), "I2")
    assert h.inputs is None and not h.relanzable


def test_i3_cancelada_en_espera(cadenas, foto_sana, real):
    wf = _wf(cadenas)
    perdida = real("fiscal-extractor#44232039713", display_title=titulo(wf, estados="nuevo-leon", origen="ronda"))
    assert perdida["jobs"] == []
    reemplazo = real("fiscal-extractor#44234246814", display_title=titulo(wf, estados="jalisco", origen="reparar"))
    [h] = _solo(inv.evaluar(foto_sana((FE, perdida), (FE, reemplazo)), cadenas, AHORA), "I3")
    assert h.corridas == [44232039713] and inv.para_lanzar(h.inputs, wf) == {"estados": "nuevo-leon"}

    # Dos perdidas: se lanza una sola corrida con la unión.
    otra = corrida(VLE, "cancelled", hace_h=3, titulo=titulo(wf, estados="colima", lote="3"), jobs=[])
    [h] = _solo(inv.evaluar(foto_sana((FE, perdida), (FE, reemplazo), (FE, otra)), cadenas, AHORA), "I3")
    assert inv.para_lanzar(h.inputs, wf) == {"estados": "colima nuevo-leon"}

    # La que la reemplazó es completa: abarca lo que iba a hacer.
    completa = real("fiscal-extractor#44234246814", display_title=titulo(wf))
    assert not _solo(inv.evaluar(foto_sana((FE, perdida), (FE, completa)), cadenas, AHORA), "I3")


def test_i3_no_confunde_cancelada_con_jobs(cadenas, foto_sana):
    wf = _wf(cadenas)
    c = corrida(VLE, "cancelled", titulo=titulo(wf, estados="colima"), jobs=[job("planear", "cancelled", "Planear")])
    hallazgos = inv.evaluar(foto_sana((FE, c)), cadenas, AHORA)
    assert not _solo(hallazgos, "I3") and _solo(hallazgos, "I2")


def test_i4_atoradas(cadenas, foto_sana):
    wf = _wf(cadenas)
    en_cola = corrida(VLE, None, hace_h=3, status="queued", titulo=titulo(wf))
    [h] = _solo(inv.evaluar(foto_sana((FE, en_cola)), cadenas, AHORA), "I4")
    assert h.base == str(en_cola["id"])
    # Esperar turno detrás de una corrida en curso no es un atasco.
    turno = corrida(VLE, None, hace_h=3, status="pending", titulo=titulo(wf, estados="colima"))
    delante = corrida(VLE, None, hace_h=4, titulo=titulo(wf))
    assert not _solo(inv.evaluar(foto_sana((FE, turno), (FE, delante)), cadenas, AHORA), "I4")
    assert _solo(inv.evaluar(foto_sana((FE, turno)), cadenas, AHORA), "I4")
    eterna = corrida(VLE, None, hace_h=wf.max_min / 60 + 1, titulo=titulo(wf))
    assert _solo(inv.evaluar(foto_sana((FE, eterna)), cadenas, AHORA), "I4")


def _pr(numero=77, rama="reforma/leyes-estatales", hace_h=3.0, etiquetas=("automatico", "verificado")):
    return {"number": numero, "head_ref": rama, "labels": list(etiquetas), "created_at": t(hace_h),
            "updated_at": t(hace_h), "cross": False}


def test_i5_pr_automatico_abierto(cadenas, foto_sana):
    foto = foto_sana(datos={"leyes-estatales": {"prs": [_pr(), _pr(78, hace_h=0.5), _pr(79, etiquetas=("otra",)),
                                                        _pr(80, rama="reforma/jalisco-deteccion-automatica")]}})
    hallazgos = _solo(inv.evaluar(foto, cadenas, AHORA), "I5")
    assert [h.extra["pr"] for h in hallazgos] == [77, 80]
    assert hallazgos[0].extra["revisor"] == {"repo": FE, "archivo": "revisar-pr.yml"}
    assert hallazgos[0].extra["inputs_revisor"] == {"pr": "77", "repo": "OpenCodice/leyes-estatales", "prefijo": "reforma/"}
    assert hallazgos[1].extra["revisor"]["repo"] == "constitucion-extractor"
    # Con su vigilancia viva, todavía puede fusionarlo ella.
    wf = _wf(cadenas)
    foto = foto_sana((FE, corrida(VLE, None, hace_h=3.2, titulo=titulo(wf))),
                     datos={"leyes-estatales": {"prs": [_pr()]}})
    [h] = _solo(inv.evaluar(foto, cadenas, AHORA), "I5")
    assert h.en_vuelo is not None


def test_i5_congreso_usa_su_etiqueta_y_su_revisor(cadenas, foto_sana):
    foto = foto_sana(datos={"congreso-mexicano": {"prs": [_pr(5, "datos/2026-11-18-1800", etiquetas=("verificado",))]}})
    [h] = _solo(inv.evaluar(foto, cadenas, AHORA), "I5")
    assert h.extra["inputs_revisor"] == {"pr": "5"} and h.extra["vigilancia"]["inputs"] == {"publicar": "si"}


def test_i6_trabajo_pendiente(cadenas, foto_sana):
    wf = _wf(cadenas)
    pendiente = corrida(VLE, hace_h=1, titulo=titulo(wf, estados="jalisco"),
                        jobs=[job("cerrar"), job("estado pendiente=[true]")])
    [h] = _solo(inv.evaluar(foto_sana((FE, pendiente)), cadenas, AHORA), "I6")
    assert inv.para_lanzar(h.inputs, wf) == {"estados": "jalisco"}
    # Con una corrida posterior que la abarca (aunque siga en cola), nada.
    siguiente = corrida(VLE, None, hace_h=0.5, status="pending", titulo=titulo(wf))
    assert not _solo(inv.evaluar(foto_sana((FE, pendiente), (FE, siguiente)), cadenas, AHORA), "I6")
    # pendiente=[false] o sin job estado (anteriores al contrato): nada.
    assert not _solo(inv.evaluar(foto_sana(), cadenas, AHORA), "I6")
    # Una exitosa posterior que no la abarca (otro estado) no borra lo que quedó pendiente.
    otra = corrida(VLE, hace_h=0.5, titulo=titulo(wf, estados="colima"), jobs=[job("cerrar"), job("estado pendiente=[false]")])
    [h] = _solo(inv.evaluar(foto_sana((FE, pendiente), (FE, otra)), cadenas, AHORA), "I6")
    assert h.base == str(pendiente["id"])
    assert {pendiente["id"], otra["id"]} <= set(inv.necesitan_jobs(foto_sana((FE, pendiente), (FE, otra)), cadenas)[FE])
    # Mientras el PR automático de esa cadena siga abierto, la tanda siguiente espera (el hallazgo lo dice).
    foto = foto_sana((FE, pendiente), datos={"leyes-estatales": {"prs": [_pr(hace_h=0.2)]}})
    [h] = _solo(inv.evaluar(foto, cadenas, AHORA), "I6")
    assert h.extra["pr_abierto"] == ["leyes-estatales", 77]
    # El PR de otra cadena (las constituciones) no la detiene.
    foto = foto_sana((FE, pendiente), datos={"leyes-estatales": {"prs": [_pr(80, "reforma/jalisco-deteccion-automatica", 0.2)]}})
    assert not _solo(inv.evaluar(foto, cadenas, AHORA), "I6")[0].extra


def test_i7_extractor_nuevo(cadenas, foto_sana):
    foto = foto_sana()
    foto["repos"][FE]["extractor"] = {"sha": "b" * 40, "fecha": t(1.5)}
    # Sin impacto medido y con menos de una hora… aquí ya pasó: se lanza.
    hallazgos = _solo(inv.evaluar(foto, cadenas, AHORA), "I7")
    assert sorted(h.workflow for h in hallazgos) == ["vigilar-federal.yml", VLE]
    # Con impacto.yml todavía corriendo se espera a su veredicto.
    foto["repos"][FE]["corridas"].append(corrida("impacto.yml", None, hace_h=1.4, evento="push"))
    assert not _solo(inv.evaluar(foto, cadenas, AHORA), "I7")
    # Una vigilancia completa creada después del commit ya lo trae.
    foto = foto_sana((FE, corrida(VLE, None, hace_h=1, titulo=titulo(_wf(cadenas)))))
    foto["repos"][FE]["extractor"] = {"sha": "b" * 40, "fecha": t(1.5)}
    assert [h.workflow for h in _solo(inv.evaluar(foto, cadenas, AHORA), "I7")] == ["vigilar-federal.yml"]
    # Commit anterior a la última exitosa: nada.
    foto = foto_sana()
    foto["repos"][FE]["extractor"] = {"sha": "b" * 40, "fecha": t(100)}
    assert not _solo(inv.evaluar(foto, cadenas, AHORA), "I7")


def test_i8_web_atrasada(cadenas, foto_sana):
    foto = foto_sana(datos={"leyes-federales": {"main": {"sha": "c" * 40, "fecha": t(0.5)}}})
    [h] = _solo(inv.evaluar(foto, cadenas, AHORA), "I8")
    assert h.base == "c" * 40 and h.workflow == "deploy.yml"
    # Dentro del margen todavía puede llegar el aviso de la vigilancia.
    foto = foto_sana(datos={"leyes-federales": {"main": {"sha": "c" * 40, "fecha": t(0.1)}}})
    assert not _solo(inv.evaluar(foto, cadenas, AHORA), "I8")
    # Lo que no se ve no se evalúa.
    foto = foto_sana(datos={"leyes-federales": {"main": {"sha": "c" * 40, "fecha": t(0.5)}}})
    foto["repos"]["constitucion-web"] = {"observable": False, "corridas": [], "ultima_exitosa": {}}
    assert not _solo(inv.evaluar(foto, cadenas, AHORA), "I8")
    assert inv.sin_observar(foto, cadenas)["I8"] == "constitucion-web: sin acceso"


def test_i8_mira_todos_los_repos_de_datos_y_no_insiste_tras_un_despliegue_fallido(cadenas, foto_sana):
    W = "constitucion-web"
    # congreso-mexicano también alimenta la web: sin el aviso de los workflows, basta su commit.
    foto = foto_sana(datos={"congreso-mexicano": {"main": {"sha": "9" * 40, "fecha": t(0.5)}}})
    [h] = _solo(inv.evaluar(foto, cadenas, AHORA), "I8")
    assert "congreso-mexicano" in h.motivo and h.inputs == {} and h.en_vuelo is None
    assert {d["repo"] for d in cadenas.datos} == {"constitucion-mexicana", "leyes-estatales", "fiscal-mexicano",
                                                 "leyes-federales", "congreso-mexicano", "historico"}
    # Con un despliegue en curso posterior, ya hay quien la resuelva.
    foto["repos"][W]["corridas"].append(corrida("deploy.yml", None, hace_h=0.05))
    assert _solo(inv.evaluar(foto, cadenas, AHORA), "I8")[0].en_vuelo
    # Un despliegue posterior al commit que falló es de I2 (con sus esperas): I8 no lo relanza en bucle.
    foto = foto_sana((W, corrida("deploy.yml", "failure", hace_h=0.2, jobs=falla("deploy", "Deploy (Worker + assets)", "boom"))),
                     datos={"congreso-mexicano": {"main": {"sha": "9" * 40, "fecha": t(0.5)}}})
    hallazgos = inv.evaluar(foto, cadenas, AHORA)
    assert not _solo(hallazgos, "I8") and [h.workflow for h in _solo(hallazgos, "I2")] == ["deploy.yml"]
    # Si el que falló es anterior al commit, el commit nuevo sí pide su despliegue.
    foto = foto_sana((W, corrida("deploy.yml", "failure", hace_h=0.8, jobs=falla("deploy", "Deploy (Worker + assets)", "boom"))),
                     datos={"congreso-mexicano": {"main": {"sha": "9" * 40, "fecha": t(0.5)}}})
    assert _solo(inv.evaluar(foto, cadenas, AHORA), "I8")


def test_i11_rojo_cronico_en_workflows_sin_dueno(cadenas, foto_sana):
    rojas = [(FE, corrida("tests.yml", "failure", hace_h=h, evento="push", titulo="un commit",
                          jobs=falla("pytest", "Run python -m pytest -q", "1 failed, 332 passed in 12.3s")))
             for h in (3, 2, 1)]
    [h] = _solo(inv.evaluar(foto_sana(*rojas), cadenas, AHORA), "I11")
    assert h.objeto == "fiscal-extractor/tests.yml" and h.repeticiones == 3 and not h.relanzable
    assert not _solo(inv.evaluar(foto_sana(*rojas[:2]), cadenas, AHORA), "I11")
    verde = (FE, corrida("tests.yml", hace_h=0.5, evento="push"))
    assert not _solo(inv.evaluar(foto_sana(*rojas, verde), cadenas, AHORA), "I11")


def test_i9_e_i10_declaradas(cadenas, foto_sana):
    assert {"I9", "I10"} <= set(inv.INVARIANTES)
    foto = foto_sana()
    foto["repos"]["supervisor"] = {"observable": True, "corridas": [], "ultima_exitosa": {}}
    [h] = inv.i9(foto, cadenas, AHORA)
    assert h.objeto == "scheduler-mudo"
    foto["repos"]["supervisor"]["corridas"].append(corrida("supervisor.yml", titulo="supervisor origen=[scheduler]"))
    assert inv.i9(foto, cadenas, AHORA) == []


def test_necesitan_jobs_no_pide_lo_ya_superado(cadenas, foto_sana, real):
    def sin_jobs(c):
        c.pop("jobs", None)
        return c
    superada = sin_jobs(real("fiscal-extractor#44246222455"))
    buena = sin_jobs(real("fiscal-extractor#44253703004"))
    foto = foto_sana((FE, superada), (FE, buena), sin=((FE, VLE),))
    pedir = inv.necesitan_jobs(foto, cadenas)[FE]
    assert 44246222455 not in pedir
    assert 44253703004 in pedir          # la última exitosa de un workflow encadenado: su job `estado`
    foto = foto_sana((FE, sin_jobs(real("fiscal-extractor#44246222455"))), sin=((FE, VLE),))
    assert 44246222455 in inv.necesitan_jobs(foto, cadenas)[FE]


def _sitio(pendientes=12, antigua=7.0, exito=8.0):
    return {"pendientes": pendientes, "pendiente_mas_antigua": t(antigua) if antigua is not None else None,
            "ultimo_exito": t(exito) if exito is not None else None, "en_espera_hasta": None, "bloqueos_seguidos": 0}


def test_i10_un_sitio_con_pendientes_viejas_y_sin_exito_reciente(cadenas, foto_sana):
    foto = foto_sana()
    foto["workers"] = {"congreso-archivo": {"observable": True, "sitios": {
        "atorado.gob.mx": _sitio(), "nunca.gob.mx": _sitio(exito=None),
        "avanza.gob.mx": _sitio(exito=0.5),           # pendientes viejas, pero sigue bajando
        "reciente.gob.mx": _sitio(antigua=2, exito=9),   # todavía dentro de las 6 h
        "vacio.gob.mx": _sitio(pendientes=0, antigua=None, exito=30)}}}
    hallazgos = inv.i10(foto, cadenas, AHORA)
    assert [h.objeto for h in hallazgos] == ["congreso-archivo/atorado.gob.mx", "congreso-archivo/nunca.gob.mx"]
    assert all(not h.relanzable and h.repo == "congreso-extractor" for h in hallazgos)
    assert hallazgos[0].base == "atorado.gob.mx" and "sin ningún éxito" in hallazgos[1].motivo
    # Sin poder leer /salud no hay hallazgos, pero tampoco está «ok».
    foto["workers"] = {"congreso-archivo": {"observable": False, "motivo": "falta CONGRESO_ARCHIVO_TOKEN en el entorno"}}
    foto["motivos"] = {"congreso-archivo": "falta CONGRESO_ARCHIVO_TOKEN en el entorno"}
    assert inv.i10(foto, cadenas, AHORA) == []
    assert inv.sin_observar(foto, cadenas)["I10"] == (
        "congreso-archivo: falta CONGRESO_ARCHIVO_TOKEN en el entorno; egress-mx: sin acceso")


def _salida(**cambios):
    return {"observable": True, "salida": {"ok": True, "permitidos": 12, "lista_leida": True, "colo": "MEX", "pais": "MX"} | cambios}


def test_i10_el_worker_que_descarga_desde_mexico_tiene_que_salir_desde_mexico(cadenas, foto_sana):
    foto = foto_sana()
    foto["workers"] = {"egress-mx": _salida()}
    assert inv.i10(foto, cadenas, AHORA) == []
    # Contesta, pero desde otro país (o no se pudo saber desde dónde): no sirve.
    for cambios, donde in (({"colo": "IAD", "pais": None}, "el centro de datos IAD"),
                           ({"colo": None, "pais": None}, "un centro de datos que no se pudo saber")):
        foto["workers"] = {"egress-mx": _salida(**cambios)}
        [h] = inv.i10(foto, cadenas, AHORA)
        assert (h.objeto, h.base, h.repo) == ("egress-mx/pais", "pais", "constitucion-extractor")
        assert donde in h.motivo and "no desde MX" in h.motivo and h.extra["espera_h"] == 6 and h.cuando is None
    foto["workers"] = {"egress-mx": _salida(ok=False)}
    assert [h.objeto for h in inv.i10(foto, cadenas, AHORA)] == ["egress-mx/ok"]


def test_i10_un_worker_que_no_contesta_su_salud_es_un_hallazgo_con_tolerancia(cadenas, foto_sana):
    foto = foto_sana()
    foto["workers"] = {"egress-mx": {"observable": False, "motivo": "GET /salud: HTTP 404", "sin_salud": True},
                       "congreso-archivo": {"observable": False, "motivo": "GET /salud: OSError", "sin_salud": True}}
    hallazgos = inv.i10(foto, cadenas, AHORA)
    assert [h.objeto for h in hallazgos] == ["congreso-archivo/salud", "egress-mx/salud"]
    assert "HTTP 404" in hallazgos[1].motivo and all(h.extra["espera_h"] == 6 and not h.relanzable for h in hallazgos)
    # El motivo de la constancia no lleva el código, que puede cambiar de una vuelta a otra.
    assert "404" not in hallazgos[1].extra["constancia_motivo"]
    # Un token rechazado o ausente no es esto: lo primero es de I13 y lo segundo queda sin observar.
    foto["workers"] = {"egress-mx": {"observable": False, "motivo": "GET /salud: HTTP 401", "rechazo": 401},
                       "congreso-archivo": {"observable": False, "motivo": "falta CONGRESO_ARCHIVO_TOKEN en el entorno"}}
    assert inv.i10(foto, cadenas, AHORA) == []
    assert [h.objeto for h in inv.i13(foto, cadenas, AHORA)] == ["EGRESS_MX_TOKEN@egress-mx"]


# ---- I12: toda petición se atiende ----

def _pide(wf, peticiones, hace_h=2.0, dura_h=0.5, invalidas=None, **inputs):
    c = corrida(wf.archivo, hace_h=hace_h, dura_h=dura_h, titulo=titulo(wf, **inputs),
                jobs=[job("reparar"), job("estado pendiente=[false]")])
    c["peticiones"] = [{"job": "reparar", "salida": "pedir_vigilancia", "repo": None} | p for p in peticiones]
    if invalidas:
        c["peticiones_invalidas"] = invalidas
    return c


def _pet(archivo, **inputs):
    return {"workflow": archivo, "inputs": inputs}


def test_i12_peticion_sin_corrida_posterior(cadenas, foto_sana):
    wf = _wf(cadenas)
    pide = _pide(wf, [_pet(VLE, estados="jalisco")])
    [h] = _solo(inv.evaluar(foto_sana((FE, pide)), cadenas, AHORA), "I12")
    assert (h.repo, h.workflow, h.relanzable) == (FE, VLE, True) and h.corridas == [pide["id"]]
    assert inv.para_lanzar(h.inputs, wf) == {"estados": "jalisco"} and h.cuando == pide["updated_at"]
    # Corridas sin artefacto `cadena` (anteriores al contrato) o sin peticiones: nada.
    assert not _solo(inv.evaluar(foto_sana(), cadenas, AHORA), "I12")


def test_i12_atendida_por_una_corrida_que_abarca_y_empezo_despues(cadenas, foto_sana):
    wf, ronda = _wf(cadenas), _wf(cadenas, FE, "expandir-estatales.yml")

    def pendiente(pide, *otras):
        return bool(_solo(inv.evaluar(foto_sana((FE, pide), *[(FE, c) for c in otras]), cadenas, AHORA), "I12"))

    pide = _pide(wf, [_pet(VLE, estados="jalisco")])        # creada hace 2 h, terminó hace 1.5 h
    # Una completa abarca la de un estado; no hace falta que haya terminado, basta con que haya empezado.
    assert not pendiente(pide, corrida(VLE, None, hace_h=1, titulo=titulo(wf)))
    assert not pendiente(pide, corrida(VLE, None, hace_h=1, status="pending", titulo=titulo(wf, estados="jalisco colima")))
    assert not pendiente(pide, corrida(VLE, "failure", hace_h=1, titulo=titulo(wf), jobs=falla("planear", "Planear", "x")))
    # La de otro estado no la abarca; la que se canceló esperando nunca empezó; la anterior no cuenta.
    assert pendiente(pide, corrida(VLE, hace_h=1, titulo=titulo(wf, estados="colima")))
    assert pendiente(pide, corrida(VLE, "cancelled", hace_h=1, titulo=titulo(wf), jobs=[]))
    assert pendiente(pide, corrida(VLE, hace_h=5, titulo=titulo(wf)))
    # En el mismo grupo de concurrency, la creada mientras la que pide corría empezó después de que terminó.
    assert not pendiente(pide, corrida(VLE, None, hace_h=1.8, titulo=titulo(wf)))
    # En otro grupo no hay ese orden: la que empezó antes de que terminara no leyó lo que la petición deja.
    pide_ronda = _pide(wf, [_pet(ronda.archivo, redeclarar="jalisco")])
    assert pendiente(pide_ronda, corrida(ronda.archivo, hace_h=1.8, titulo=titulo(ronda, redeclarar="jalisco")))
    assert not pendiente(pide_ronda, corrida(ronda.archivo, None, hace_h=1.4, titulo=titulo(ronda, redeclarar="jalisco")))
    assert pendiente(pide_ronda, corrida(ronda.archivo, None, hace_h=1.4, titulo=titulo(ronda, redeclarar="colima")))


def test_i12_une_las_peticiones_pendientes_al_mismo_workflow(cadenas, foto_sana):
    wf, ronda = _wf(cadenas), _wf(cadenas, FE, "expandir-estatales.yml")
    a = _pide(ronda, [_pet(VLE, estados="jalisco")], hace_h=3, estado="jalisco")
    b = _pide(wf, [_pet(VLE, estados="colima"), _pet(ronda.archivo, redeclarar="colima")], hace_h=2, estados="colima")
    hallazgos = _solo(inv.evaluar(foto_sana((FE, a), (FE, b)), cadenas, AHORA), "I12")
    [union] = [h for h in hallazgos if h.workflow == VLE]
    assert inv.para_lanzar(union.inputs, wf) == {"estados": "colima jalisco"}
    assert union.corridas == sorted([a["id"], b["id"]]) and union.cuando == b["updated_at"]
    assert [inv.para_lanzar(h.inputs, ronda) for h in hallazgos if h.workflow == ronda.archivo] == [{"redeclarar": "colima"}]
    # Si alguna pide la vuelta completa, la unión es completa.
    c = _pide(ronda, [_pet(VLE)], hace_h=1.2, dura_h=0.1, redeclarar="sonora")
    [union] = [h for h in _solo(inv.evaluar(foto_sana((FE, a), (FE, b), (FE, c)), cadenas, AHORA), "I12") if h.workflow == VLE]
    assert inv.para_lanzar(union.inputs, wf) == {} and len(union.corridas) == 3
    # Lo que no se puede unir (dos estados de la ronda) son dos lanzamientos.
    d = _pide(ronda, [_pet(ronda.archivo, estado="sonora"), _pet(ronda.archivo, estado="puebla")])
    dos = _solo(inv.evaluar(foto_sana((FE, d)), cadenas, AHORA), "I12")
    assert sorted(inv.para_lanzar(h.inputs, ronda)["estado"] for h in dos) == ["puebla", "sonora"]


def test_i12_de_otro_workflow_que_no_es_vigilado(cadenas, foto_sana):
    """reparar-parsers (lo dispara workflow_run) pide la actualización que publica."""
    CG = "congreso-extractor"
    cg = _wf(cadenas, CG, "actualizar-congreso.yml")
    pide = corrida("reparar-parsers.yml", hace_h=1.2, dura_h=0.2, titulo="reparar-parsers run_id=[] origen=[]", evento="workflow_run")
    pide["peticiones"] = [{"job": "publicar", "salida": "pedir_actualizar", "repo": None,
                           "workflow": cg.archivo, "inputs": {"publicar": "si"}}]
    [h] = _solo(inv.evaluar(foto_sana((CG, pide)), cadenas, AHORA), "I12")
    assert (h.repo, h.workflow) == (CG, cg.archivo) and inv.para_lanzar(h.inputs, cg) == {}
    # La «solo pedir» de la hora no la atiende; la que publica, sí.
    hora = corrida(cg.archivo, hace_h=0.5, titulo=titulo(cg, publicar="no"))
    assert _solo(inv.evaluar(foto_sana((CG, pide), (CG, hora)), cadenas, AHORA), "I12")
    publica = corrida(cg.archivo, None, hace_h=0.5, titulo=titulo(cg, publicar="si"))
    assert not _solo(inv.evaluar(foto_sana((CG, pide), (CG, publica)), cadenas, AHORA), "I12")
    # Una corrida saltada (la dispara cada «solo pedir») no trae artefacto ni es una falla.
    saltada = corrida("reparar-parsers.yml", "skipped", hace_h=1, titulo="reparar-parsers run_id=[] origen=[]", evento="workflow_run")
    assert inv.evaluar(foto_sana((CG, saltada)), cadenas, AHORA) == []


def test_i12_lo_que_no_se_atiende_solo_deja_constancia(cadenas, foto_sana):
    wf = _wf(cadenas)
    fiscal = _wf(cadenas, FE, "vigilar-fiscal.yml")
    mal = [{"job": "reparar", "salida": "pedir_x", "motivo": "no es JSON"}]
    casos = [
        (_pide(wf, [_pet("no-existe.yml")]), "no es un workflow declarado"),
        (_pide(wf, [_pet("deploy.yml") | {"repo": "constitucion-web"}]), "puede_pedir"),
        (_pide(wf, [_pet("reparar-infra.yml") | {"repo": "supervisor"}]), "no es un workflow declarado"),
        (_pide(wf, [_pet("vigilar-fiscal.yml")]), "puede_pedir"),                       # declarado, pero no permitido
        (_pide(fiscal, [_pet("vigilar-fiscal.yml")]), "puede_pedir"),                   # quien no puede pedir nada
        (_pide(wf, [_pet(VLE, estado="jalisco")]), "no declara los inputs ['estado']"),
        (_pide(wf, [_pet("expandir-estatales.yml", ensayo="true")]), "prueba a mano"),
        (_pide(wf, [], invalidas=mal), "mal formada (no es JSON)"),
    ]
    for pide, motivo in casos:
        [h] = _solo(inv.evaluar(foto_sana((FE, pide)), cadenas, AHORA), "I12")
        assert not h.relanzable and motivo in h.extra["rechazada"], motivo
        assert h.objeto.startswith(f"{FE}/{pide['path'].rsplit('/', 1)[-1]}#{pide['id']}:reparar.")
    # `origen` lo pone el supervisor: si viene, se ignora.
    [h] = _solo(inv.evaluar(foto_sana((FE, _pide(wf, [_pet(VLE, estados="jalisco", origen="reparar")]))), cadenas, AHORA), "I12")
    assert h.relanzable and inv.para_lanzar(h.inputs, wf) == {"estados": "jalisco"}
    # Una corrida de prueba a mano no pide nada.
    ronda = _wf(cadenas, FE, "expandir-estatales.yml")
    assert not _solo(inv.evaluar(foto_sana((FE, _pide(ronda, [_pet(VLE, estados="x")], ensayo="true"))), cadenas, AHORA), "I12")


def test_i12_espera_al_pr_abierto_de_la_cadena_pedida(cadenas, foto_sana):
    wf = _wf(cadenas)
    foto = foto_sana((FE, _pide(wf, [_pet(VLE)])), datos={"leyes-estatales": {"prs": [_pr(hace_h=0.3)]}})
    [h] = _solo(inv.evaluar(foto, cadenas, AHORA), "I12")
    assert h.extra == {"pr_abierto": ["leyes-estatales", 77]}


def test_sin_observar_dice_que_invariante_no_pudo_mirar_que(cadenas, foto_sana):
    assert inv.sin_observar(foto_sana(), cadenas) == {"I10": "congreso-archivo: sin acceso; egress-mx: sin acceso"}
    foto = foto_sana()
    foto["repos"]["constitucion-web"] = {"observable": False, "corridas": [], "ultima_exitosa": {}}
    foto["motivos"] = {"constitucion-web": "HTTP 404", "fiscal-extractor (artefactos)": "HTTP 403"}
    del foto["datos"]["leyes-federales"]
    ciegos = inv.sin_observar(foto, cadenas)
    assert ciegos["I8"] == "constitucion-web: HTTP 404; leyes-federales: sin acceso"
    assert ciegos["I5"] == "leyes-federales: sin acceso" and ciegos["I11"] == "constitucion-web: HTTP 404"
    assert ciegos["I2"] == ciegos["I4"] == "constitucion-web: HTTP 404"       # deploy.yml también tiene dueño
    assert ciegos["I12"] == "fiscal-extractor (artefactos): HTTP 403"
    assert not {"I6", "I7", "I9"} & set(ciegos)


# ---- I13: las credenciales siguen vigentes ----

def _con_token(foto, **cambios):
    foto["credenciales"]["github"] |= cambios
    return foto


@pytest.mark.parametrize("faltan_dias, avisa", [(400, False), (31, False), (30.5, False), (29.9, True), (1, True),
                                                (0.5, True), (-2, True)])
def test_i13_avisa_dentro_del_umbral(cadenas, foto_sana, faltan_dias, avisa):
    vence = inv.iso(AHORA + timedelta(days=faltan_dias))
    hallazgos = inv.i13(_con_token(foto_sana(), vence=vence), cadenas, AHORA)
    assert [h.objeto for h in hallazgos] == (["SUPERVISOR_TOKEN"] if avisa else [])
    if avisa:
        [h] = hallazgos
        assert h.base == vence and not h.relanzable and h.repo == "supervisor" and h.workflow is None
        assert h.extra["titulo"] == f"🔑 SUPERVISOR_TOKEN vence el {vence[:10]}"
        texto = h.extra["constancia"]
        assert "`gh secret set SUPERVISOR_TOKEN -R OpenCodice/supervisor`" in texto
        assert "Actions, Contents, Workflows, Pull requests e Issues" in texto and "opencodice-supervisor" in texto
        assert ("Venció el" if faltan_dias < 0 else "Vence el") in texto


def test_i13_un_token_que_no_vence_no_avisa_y_sin_respuesta_no_es_ok(cadenas, foto_sana):
    assert inv.i13(foto_sana(), cadenas, AHORA) == [] and "I13" not in inv.sin_observar(foto_sana(), cadenas)
    ciega = _con_token(foto_sana(), observado=False, vence=t(-24))
    assert inv.i13(ciega, cadenas, AHORA) == []
    assert inv.sin_observar(ciega, cadenas)["I13"] == "SUPERVISOR_TOKEN: la API no aceptó ninguna petición con el token"
    ilegible = _con_token(foto_sana(), observado=False, cabecera="mañana")
    assert inv.sin_observar(ilegible, cadenas)["I13"] == "SUPERVISOR_TOKEN: fecha de vencimiento ilegible: mañana"
    vieja = foto_sana()
    del vieja["credenciales"]
    assert inv.i13(vieja, cadenas, AHORA) == [] and "I13" in inv.sin_observar(vieja, cadenas)


def test_i13_sin_el_secret_el_supervisor_solo_informa(cadenas, foto_sana):
    foto = _con_token(foto_sana(), propio=False, rechazos=[{"que": "leer las corridas de constitucion-web", "estado": 404, "origen": "foto"}])
    [h] = inv.i13(foto, cadenas, AHORA)
    assert h.objeto == "SUPERVISOR_TOKEN/ausente" and "modo informe" in h.motivo and "nada se lanza" in h.motivo
    assert inv.credenciales_en_breve(foto, cadenas, AHORA) == ["SUPERVISOR_TOKEN: falta; la vuelta usó el token del job"]


def test_i13_permisos_perdidos_al_leer_y_al_actuar(cadenas, foto_sana):
    rechazos = [{"que": "leer las corridas de constitucion-web", "estado": 404, "origen": "foto"},
                {"que": "lanzar o cancelar corridas en congreso-extractor", "estado": 403, "origen": "accion", "cuando": t(1)}]
    [h] = inv.i13(_con_token(foto_sana(), rechazos=rechazos), cadenas, AHORA)
    assert h.objeto == "SUPERVISOR_TOKEN/permisos" and h.extra["titulo"] == "🔑 SUPERVISOR_TOKEN perdió permisos"
    assert "- lanzar o cancelar corridas en congreso-extractor (HTTP 403)" in h.extra["constancia"]
    assert "- leer las corridas de constitucion-web (HTTP 404)" in h.extra["constancia"]
    # La huella no depende de qué se negó: es una por credencial y clase de problema.
    otro = inv.i13(_con_token(foto_sana(), rechazos=rechazos[:1]), cadenas, AHORA)[0]
    assert otro.objeto == h.objeto and otro.base != h.base


def _accion(accion, hace_h, resultado="ok", **detalle):
    return {"cuando": t(hace_h), "accion": accion, "detalle": detalle, "resultado": resultado, "huella": "h"}


NEGADO = ('error: POST https://api.github.com/repos/OpenCodice/congreso-extractor/actions/workflows/actualizar-congreso.yml/'
          'dispatches: HTTP 403 {"message":"Resource not accessible by personal access token","status":"403"}')


def test_rechazos_de_acciones(cadenas):
    de = lambda *acciones: [(r["que"], r["estado"]) for r in inv.rechazos_de_acciones(list(acciones), cadenas, AHORA)]  # noqa: E731
    negada = _accion("lanzar", 5, NEGADO, repo="congreso-extractor", workflow="actualizar-congreso.yml")
    assert de(negada) == [("lanzar o cancelar corridas en congreso-extractor", 403)]
    # Un lanzamiento posterior a ese repo que salió bien lo deja atrás; uno a otro repo, no.
    assert de(negada, _accion("agente", 2, repo="congreso-extractor", workflow="x.yml")) == []
    assert de(negada, _accion("lanzar", 2, repo="fiscal-extractor", workflow="x.yml")) != []
    # Lo viejo ya no cuenta, ni un límite de uso, ni un 422.
    assert de(_accion("lanzar", 200, NEGADO, repo="congreso-extractor")) == []
    assert de(_accion("lanzar", 1, "error: POST x: HTTP 403 API rate limit exceeded", repo="congreso-extractor")) == []
    assert de(_accion("lanzar", 1, "error: POST x: HTTP 422 falla", repo="congreso-extractor")) == []
    # Cerrar un PR y escribir la constancia son otros permisos, en otros repos.
    assert de(_accion("cerrar-pr", 1, "error: PATCH x: HTTP 403 no", repo_datos="leyes-estatales", pr=4),
              _accion("agotada", 1, "error: POST x: HTTP 401 Bad credentials")) == [
        ("escribir issues en supervisor", 401), ("cerrar un PR de leyes-estatales", 403)]
    # Lo que anota reparar-infra no es una acción del token.
    assert de({"cuando": t(1), "accion": "descartado", "huella": "h", "detalle": {"motivo": "HTTP 403"}}) == []


def test_i13_el_worker_rechaza_su_token(cadenas, foto_sana):
    foto = foto_sana()
    foto["workers"] = {"congreso-archivo": {"observable": False, "motivo": "GET /salud: HTTP 401", "rechazo": 401}}
    [h] = inv.i13(foto, cadenas, AHORA)
    assert h.objeto == "CONGRESO_ARCHIVO_TOKEN@congreso-archivo" and "HTTP 401" in h.motivo
    assert "`gh secret set CONGRESO_ARCHIVO_TOKEN -R OpenCodice/supervisor`" in h.extra["constancia"]
    assert "CONGRESO_ARCHIVO_TOKEN: rechazado (HTTP 401) por congreso-archivo" in inv.credenciales_en_breve(foto, cadenas, AHORA)
    # Sin el token en el entorno, o con el Worker caído, no es un rechazo.
    foto["workers"]["congreso-archivo"] = {"observable": False, "motivo": "GET /salud: HTTP 503"}
    assert inv.i13(foto, cadenas, AHORA) == []


def test_credenciales_en_breve(cadenas, foto_sana):
    assert inv.credenciales_en_breve(foto_sana(), cadenas, AHORA) == ["SUPERVISOR_TOKEN: no vence"]
    foto = _con_token(foto_sana(), propio=None, vence="2027-11-20T06:00:00Z")
    assert inv.credenciales_en_breve(foto, cadenas, AHORA) == [
        "el token de esta vuelta (se evalúa como SUPERVISOR_TOKEN): vence el 2027-11-20 06:00 UTC (faltan 366 días)"]
