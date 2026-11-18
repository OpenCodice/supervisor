from datetime import datetime, timedelta, timezone

from conftest import AHORA, corrida, falla, job, t, titulo

from supervisor import invariantes as inv

FE = "fiscal-extractor"
VLE = "vigilar-leyes-estatales.yml"


def _wf(cadenas, repo=FE, archivo=VLE):
    return cadenas.workflow(repo, archivo)


def _solo(hallazgos, invariante, contiene=""):
    return [h for h in hallazgos if h.invariante == invariante and contiene in h.objeto]


def test_gramatica_del_titulo(cadenas):
    wf = _wf(cadenas)
    c = {"display_title": "vigilar-leyes-estatales reintento=[true] docs=[] estados=[jalisco colima] lote=[8] revisar=[jalisco/codigo-civil] origen=[reparar]"}
    assert inv.inputs_de(c, wf) == {"reintento": "true", "docs": "", "estados": "jalisco colima", "lote": "8",
                                    "revisar": "jalisco/codigo-civil", "origen": "reparar"}
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
    assert inv.abarca(i(estados="jalisco", reintento="true", lote="3"), i(estados="jalisco"), wf)   # ignorados
    assert not inv.abarca(i(estados="jalisco"), i(revisar="jalisco/codigo-civil"), wf)
    assert inv.abarca(None, None, wf) and inv.abarca(i(), None, wf)
    assert not inv.abarca(None, i(), wf) and not inv.abarca(i(estados="jalisco"), None, wf)

    assert inv.unir([i(estados="nuevo-leon"), i(estados="colima")], wf)["estados"] == "colima nuevo-leon"
    assert inv.unir([i(estados="nuevo-leon"), i()], wf)["estados"] == ""
    assert inv.unir([i(estados="nuevo-leon"), None], wf) is None
    assert inv.para_lanzar(i(estados="colima", reintento="true", origen="salud"), wf) == {"estados": "colima"}


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
    foto["repos"]["constitucion-extractor"]["ultima_exitosa"]["credenciales.yml"] = corrida("credenciales.yml", hace_h=20 * 24)
    assert not _solo(inv.evaluar(foto, cadenas, AHORA), "I1", "credenciales")


def test_i1_por_horario_solo_con_el_interruptor(cadenas, foto_sana):
    import dataclasses
    lunes = datetime(2026, 11, 23, 9, 20, tzinfo=timezone.utc)
    wf = _wf(cadenas, FE, "vigilar-fiscal.yml")
    encendido = dataclasses.replace(wf, lanza_horario=True, cadencias=())
    con = dataclasses.replace(cadenas, workflows=tuple(encendido if w is wf else dataclasses.replace(w, cadencias=())
                                                       for w in cadenas.workflows))
    foto = foto_sana()
    [h] = _solo(inv.evaluar(foto, con, lunes), "I1")
    assert h.base == "horario-2026-11-23T09:00:00Z" and h.workflow == "vigilar-fiscal.yml"
    # Ya hay una corrida creada a su hora: nada.
    foto["repos"][FE]["corridas"].append(corrida("vigilar-fiscal.yml", None, titulo=titulo(wf)) | {"created_at": "2026-11-23T09:00:10Z"})
    assert not _solo(inv.evaluar(foto, con, lunes), "I1")
    # Pasada la tolerancia ya no se lanza por horario (lo cubre la cadencia).
    assert not _solo(inv.evaluar(foto_sana(), con, lunes + timedelta(hours=5)), "I1")
    sin = dataclasses.replace(cadenas, workflows=tuple(dataclasses.replace(w, cadencias=()) for w in cadenas.workflows))
    assert not _solo(inv.evaluar(foto_sana(), sin, lunes), "I1")


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
    otra = corrida(VLE, "cancelled", hace_h=3, titulo=titulo(wf, estados="colima", reintento="true"), jobs=[])
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
