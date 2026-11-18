from conftest import FIXTURES

from supervisor import huella
from supervisor.invariantes import falla_de
from supervisor.observar import cola_del_paso
from supervisor.politica import clasificar


def _falla(reales, clave):
    return falla_de(reales[clave])


def test_exit_127_misma_huella_en_las_dos_corridas(reales, cadenas):
    a = _falla(reales, "fiscal-extractor#44246222455")
    b = _falla(reales, "fiscal-extractor#44247025218")
    assert a == b
    assert a["job"] == "reparar"
    assert a["paso"] == "Reintentar las leyes con el extractor reparado"
    assert "No such file or directory" in a["firma"] and "exit code 127" in a["firma"]
    assert "/home/runner" not in a["firma"]
    wf = cadenas.workflow("fiscal-extractor", "vigilar-leyes-estatales.yml")
    assert clasificar(a, wf, cadenas) == "determinista"


def test_la_huella_no_depende_de_rutas_temporales_fechas_ni_shas():
    base = ["2026-11-18T06:37:30.4668421Z bash: /home/runner/work/fiscal-extractor/fiscal-extractor/scripts/x.sh: No such file or directory",
            "2026-11-18T06:37:30.4679731Z ##[error]Process completed with exit code 127."]
    otra = ["2026-12-01T10:00:01.1Z bash: /home/runner/work/otro/otro/scripts/x.sh: No such file or directory",
            "2026-12-01T10:00:01.2Z ##[error]Process completed with exit code 127."]
    assert huella.firma(base) == huella.firma(otra)
    con_tmp = ["cp: cannot stat '/home/runner/work/_temp/4c1fb0c1-12db-4ef7-bb13-5530dca303bd/a.json': No such file or directory",
               "##[error]Process completed with exit code 1."]
    con_otro_tmp = ["cp: cannot stat '/home/runner/work/_temp/99999999-aaaa-4ef7-bb13-000000000000/a.json': No such file or directory",
                    "##[error]Process completed with exit code 1."]
    assert huella.firma(con_tmp) == huella.firma(con_otro_tmp)
    assert huella.normalizar("commit 6f72f4f6a52c2b12ff1cb7018b817a359c3fab8f en la corrida 44246222455") == \
        huella.normalizar("commit 00569ff2365aa7c7fb8bbc93414d506382d325a0 en la corrida 44247025218")
    assert huella.firma(base) != huella.firma([base[0], "##[error]Process completed with exit code 1."])


def test_la_firma_conserva_los_codigos_que_distinguen_la_causa(reales, cadenas):
    f = _falla(reales, "fiscal-extractor#44114896498")
    assert f["job"] == "continuar"
    assert "HTTP 422" in f["firma"] and "<host>" in f["firma"]
    assert clasificar(f, cadenas.workflow("fiscal-extractor", "vigilar-leyes-estatales.yml"), cadenas) == "determinista"


def test_zacatecas_es_transitoria_y_el_job_de_matriz_se_normaliza(reales, cadenas):
    a = _falla(reales, "constitucion-extractor#43881425855")
    b = _falla(reales, "constitucion-extractor#43883374036")
    assert a == b and a["job"] == "vigilar"
    assert "Connection refused" in a["firma"]
    assert clasificar(a, cadenas.workflow("constitucion-extractor", "vigilar-estatales.yml"), cadenas) == "transitoria"


def test_cola_del_paso_sobre_un_log_real_del_job(reales):
    log = (FIXTURES / "log_job_reparar_44246222455.txt").read_text(encoding="utf-8")
    corrida = reales["fiscal-extractor#44246222455"]
    reparar = next(j for j in corrida["jobs"] if j["name"] == "reparar")
    paso = next(s for s in reparar["steps"] if s["conclusion"] == "failure")
    cola = cola_del_paso(log, paso, 40)
    assert cola[-1] == "##[error]Process completed with exit code 127."
    assert not any(l.startswith("2026-") for l in cola)
    assert huella.firma(cola) == falla_de(corrida)["firma"]


def test_huella_de_objeto_y_de_falla_son_estables():
    assert huella.de_objeto("I5", "leyes-estatales#12") == huella.de_objeto("I5", "leyes-estatales#12")
    assert huella.de_objeto("I5", "leyes-estatales#12") != huella.de_objeto("I5", "leyes-estatales#13")
    assert huella.de_falla("r", "w.yml", "vigilar (zacatecas)", "p", "f") == huella.de_falla("r", "w.yml", "vigilar (puebla)", "p", "f")
    assert len(huella.de_objeto("I1", "x")) == 12


def test_el_eco_del_script_no_es_la_firma():
    log = "\n".join([
        "2026-11-18T10:00:01.0Z ##[group]Run bash scripts/agente/leer.sh",
        "2026-11-18T10:00:01.1Z \x1b[36;1mbash scripts/agente/leer.sh\x1b[0m",
        "2026-11-18T10:00:01.2Z shell: /usr/bin/bash -e {0}",
        "2026-11-18T10:00:01.3Z ##[endgroup]",
        "2026-11-18T10:00:09.0Z ##[error]Process completed with exit code 1.",
    ])
    assert huella.firma(cola_del_paso(log, None, 40)) == "Process completed with exit code 1."


# La corrida 44272219102 de actualizar-congreso: el paso que falló empieza y termina en el mismo segundo.
PASO_CONGRESO = {"name": "Agente — leer las páginas que el parser no entendió (1)", "conclusion": "failure",
                 "started_at": "2026-11-18T09:30:42Z", "completed_at": "2026-11-18T09:30:42Z"}


def test_la_firma_no_arrastra_el_final_del_paso_anterior():
    log = (FIXTURES / "log_job_actualizar_44272219102.txt").read_text(encoding="utf-8")
    assert huella.firma(cola_del_paso(log, PASO_CONGRESO, 40)) == "Process completed with exit code 1."
    # Si lo último que dijo el paso anterior cae en ese mismo segundo, tampoco entra: cambia de una
    # corrida a otra («254 páginas», «194 páginas») y con él cambiaría la huella.
    pegado = log.replace("2026-11-18T09:29:03.2104444Z 254 páginas", "2026-11-18T09:30:42.9000000Z 254 páginas")
    assert pegado != log
    assert huella.firma(cola_del_paso(pegado, PASO_CONGRESO, 40)) == "Process completed with exit code 1."
    # Sin las horas del paso se parte igual del encabezado del paso que falló.
    assert huella.firma(cola_del_paso(log, None, 40)) == "Process completed with exit code 1."
