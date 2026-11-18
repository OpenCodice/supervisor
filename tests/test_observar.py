import dataclasses
import io
import json
import zipfile
from datetime import timedelta

import pytest
from conftest import AHORA, FIXTURES, corrida, t, titulo

from supervisor import huella
from supervisor import invariantes as inv
from supervisor.observar import Cliente, ErrorApi, LimiteDeUso, _jobs_de, peticiones_de, salud_de, tomar_foto, vencimiento


class Red:
    """Transporte falso: responde según la URL y anota lo que se le pidió."""

    def __init__(self, rutas):
        self.rutas = rutas
        self.pedidos = []

    def __call__(self, metodo, url, cabeceras, cuerpo):
        self.pedidos.append((metodo, url, cabeceras, cuerpo))
        for fragmento, respuesta in self.rutas:
            if fragmento in url:
                if callable(respuesta):
                    respuesta = respuesta(url)
                estado, cab, datos = respuesta
                return estado, cab, datos if isinstance(datos, bytes) else json.dumps(datos).encode()
        return 404, {}, b'{"message":"Not Found"}'


def test_paginacion_por_link():
    red = Red([("page=2", (200, {}, {"workflow_runs": [{"id": 2}]})),
               ("/runs", (200, {"link": '<https://api.github.com/repos/o/r/actions/runs?page=2>; rel="next"'},
                          {"workflow_runs": [{"id": 1}]}))])
    cliente = Cliente("t", transporte=red)
    assert cliente.paginas("repos/o/r/actions/runs", "workflow_runs") == [{"id": 1}, {"id": 2}]
    assert red.pedidos[0][2]["Authorization"] == "Bearer t"
    assert "per_page=100" in red.pedidos[0][1]


def test_limite_de_uso_espera_acotada_y_falla_clara():
    respuestas = iter([(429, {"retry-after": "5"}, {}), (200, {}, {"ok": True})])
    dormidas = []
    cliente = Cliente("t", transporte=Red([("", lambda url: next(respuestas))]), dormir=dormidas.append)
    assert cliente.get("x") == {"ok": True}
    assert dormidas == [6.0]

    agotado = Red([("", (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "2000"}, {"message": "rate limit"}))])
    cliente = Cliente("t", transporte=agotado, dormir=dormidas.append, reloj=lambda: 1000.0, espera_max=90)
    with pytest.raises(LimiteDeUso, match="límite de uso"):
        cliente.get("x")
    assert len(agotado.pedidos) == 1

    # Un 403 que no es de límite de uso es un error normal, sin espera.
    with pytest.raises(ErrorApi) as e:
        Cliente("t", transporte=Red([("", (403, {}, {"message": "Resource not accessible"}))])).get("x")
    assert not isinstance(e.value, LimiteDeUso) and e.value.estado == 403


def test_el_log_se_baja_sin_mandar_el_token_al_almacen():
    red = Red([("/logs", (302, {"location": "https://almacen.example/log.txt"}, b"")),
               ("almacen.example", (200, {}, b"2026-11-18T06:37:30.1Z hola\n"))])
    assert Cliente("secreto", transporte=red).texto("repos/o/r/actions/jobs/1/logs").endswith("hola\n")
    assert "Authorization" in red.pedidos[0][2] and "Authorization" not in red.pedidos[1][2]


def _zip(needs) -> bytes:
    cuerpo = io.BytesIO()
    with zipfile.ZipFile(cuerpo, "w") as z:
        z.writestr("cadena.json", needs if isinstance(needs, str) else json.dumps(needs))
    return cuerpo.getvalue()


def _api(cadenas, corridas_por_repo, jobs=None, privados=(), cadenas_de=None):
    def listar(repo):
        def responder(url):
            if "status=success" in url:
                return 200, {}, {"workflow_runs": []}
            return 200, {}, {"workflow_runs": corridas_por_repo.get(repo, [])}
        return responder
    rutas = [(f"/repos/OpenCodice/{r}/actions/runs/{i}/jobs", (200, {}, {"jobs": j})) for (r, i), j in (jobs or {}).items()]
    # Artefactos `cadena`: {(repo, corrida): needs}; el id del artefacto es el de la corrida más mil.
    for (r, i), needs in (cadenas_de or {}).items():
        rutas.append((f"/repos/OpenCodice/{r}/actions/artifacts/{i + 1000}/zip", (302, {"location": f"https://almacen.example/zip/{i}"}, b"")))
        rutas.append((f"almacen.example/zip/{i}", (200, {}, _zip(needs))))
    for repo in cadenas.repos_con_workflows():
        rutas.append((f"/repos/OpenCodice/{repo}/actions/artifacts?", (200, {}, {"artifacts": [
            {"id": i + 1000, "name": "cadena", "expired": False, "created_at": t(1), "workflow_run": {"id": i}}
            for (r, i) in (cadenas_de or {}) if r == repo]})))
    rutas.append(("/jobs?", (200, {}, {"jobs": []})))
    rutas += [("/actions/jobs/", (302, {"location": "https://almacen.example/l"}, b"")),
              ("almacen.example", (200, {}, b"2026-11-18T10:00:01.0Z bash: x.sh: No such file or directory\n"
                                            b"2026-11-18T10:00:01.1Z ##[error]Process completed with exit code 127.\n"))]
    for repo in cadenas.repos_con_workflows():
        if repo in privados:
            continue
        rutas.append((f"/repos/OpenCodice/{repo}/actions/workflows/", listar(repo)))
        rutas.append((f"/repos/OpenCodice/{repo}/actions/runs", listar(repo)))
        rutas.append((f"/repos/OpenCodice/{repo}/commits?", (200, {}, [{"sha": "e" * 40, "commit": {"committer": {"date": t(50)}}}])))
    for d in cadenas.datos:
        rutas.append((f"/repos/OpenCodice/{d['repo']}/commits/main",
                      (200, {}, {"sha": "d" * 40, "commit": {"committer": {"date": t(30)}}})))
        rutas.append((f"/repos/OpenCodice/{d['repo']}/pulls", (200, {}, [
            {"number": 4, "head": {"ref": "reforma/leyes-estatales", "repo": {"full_name": "OpenCodice/x"}},
             "base": {"repo": {"full_name": "OpenCodice/x"}}, "labels": [{"name": "automatico"}],
             "created_at": t(1), "updated_at": t(1)}] if d["repo"] == "leyes-estatales" else [])))
    return Red(rutas)


def test_foto_pide_jobs_y_log_solo_de_lo_que_hace_falta(cadenas, tmp_path):
    rota = corrida("vigilar-fiscal.yml", "failure", hace_h=2, ident=11)
    superada = corrida("vigilar-federal.yml", "failure", hace_h=5, ident=12)
    buena = corrida("vigilar-federal.yml", hace_h=1, ident=13)
    verde = corrida("tests.yml", hace_h=1, ident=14, evento="push")
    jobs = {("fiscal-extractor", 11): [{
        "id": 501, "name": "vigilar", "status": "completed", "conclusion": "failure",
        "started_at": "2026-11-18T10:00:00Z", "completed_at": "2026-11-18T10:00:02Z", "runner_name": "sobra",
        "steps": [{"name": "Descargar", "number": 2, "status": "completed", "conclusion": "failure",
                   "started_at": "2026-11-18T10:00:01Z", "completed_at": "2026-11-18T10:00:01Z"}]}],
        ("fiscal-extractor", 13): [{"id": 502, "name": "estado pendiente=[true]", "status": "completed",
                                    "conclusion": "success", "steps": [{"name": "Run true", "conclusion": "success"}]}]}
    red = _api(cadenas, {"fiscal-extractor": [rota, superada, buena, verde]}, jobs, privados=("constitucion-web",))
    cliente = Cliente("t", transporte=red)
    foto = tomar_foto(cliente, cadenas, AHORA, cache=tmp_path / "cache")

    por_id = {c["id"]: c for c in foto["repos"]["fiscal-extractor"]["corridas"]}
    assert por_id[11]["jobs"][0]["log"][-1] == "##[error]Process completed with exit code 127."
    assert "runner_name" not in por_id[11]["jobs"][0]
    assert por_id[13]["jobs"][0]["name"] == "estado pendiente=[true]"
    assert "jobs" not in por_id[12] and "jobs" not in por_id[14]
    assert not any("/runs/12/jobs" in u or "/runs/14/jobs" in u for _, u, _, _ in red.pedidos)
    assert foto["repos"]["fiscal-extractor"]["extractor"] == {"sha": "e" * 40, "fecha": t(50)}
    # Un repo que el token no ve (privado) no rompe la foto: queda sin observar.
    assert foto["repos"]["constitucion-web"]["observable"] is False and foto["errores"]
    assert foto["datos"]["leyes-estatales"]["prs"] == [{
        "number": 4, "head_ref": "reforma/leyes-estatales", "labels": ["automatico"],
        "created_at": t(1), "updated_at": t(1), "cross": False}]
    assert foto["llamadas"] == cliente.llamadas
    json.dumps(foto)

    # La segunda foto no vuelve a pedir los jobs ni el log de corridas terminadas.
    red.pedidos.clear()
    tomar_foto(Cliente("t", transporte=red), cadenas, AHORA + timedelta(minutes=15), cache=tmp_path / "cache")
    assert not any("/jobs" in u or "almacen" in u for _, u, _, _ in red.pedidos)


PEDIR = '{"workflow":"vigilar-leyes-estatales.yml","inputs":{"estados":"jalisco"}}'


def test_peticiones_de_las_salidas_pedir():
    needs = {
        "reparar": {"result": "success", "outputs": {
            "pedir_vigilancia": PEDIR, "pedir_vacia": "", "reparado": "true",
            "pedir_varias": json.dumps([{"workflow": "expandir-estatales.yml", "inputs": {"redeclarar": "colima"}},
                                        {"workflow": "deploy.yml", "repo": "OpenCodice/constitucion-web"}])}},
        "cerrar": {"result": "failure", "outputs": {"pedir_rota": "{no es json", "pedir_tipo": '"texto"',
                                                    "pedir_inputs": '{"workflow":"x.yml","inputs":{"a":["b"]}}',
                                                    "pedir_ruta": '{"workflow":"../x.yml"}'}},
        "saltado": {"result": "skipped", "outputs": {}},
    }
    buenas, malas = peticiones_de(needs, "OpenCodice")
    assert buenas == [
        {"job": "reparar", "salida": "pedir_varias", "workflow": "expandir-estatales.yml", "repo": None, "inputs": {"redeclarar": "colima"}},
        {"job": "reparar", "salida": "pedir_varias", "workflow": "deploy.yml", "repo": "constitucion-web", "inputs": {}},
        {"job": "reparar", "salida": "pedir_vigilancia", "workflow": "vigilar-leyes-estatales.yml", "repo": None, "inputs": {"estados": "jalisco"}},
    ]
    assert [(m["salida"], m["motivo"]) for m in malas] == [
        ("pedir_inputs", "el input `a` no es una cadena"), ("pedir_rota", "no es JSON"),
        ("pedir_ruta", "`workflow` no es el nombre de un archivo .yml"), ("pedir_tipo", "no es un objeto JSON")]


def test_la_foto_trae_las_peticiones_y_cada_cadena_se_baja_una_vez(cadenas, tmp_path):
    FE = "fiscal-extractor"
    wf = cadenas.workflow(FE, "vigilar-leyes-estatales.yml")
    pide = corrida(wf.archivo, hace_h=2, ident=21, titulo=titulo(wf))
    rota = corrida(wf.archivo, hace_h=3, ident=22, titulo=titulo(wf))
    sin_artefacto = corrida(wf.archivo, hace_h=4, ident=23, titulo=titulo(wf))
    en_curso = corrida(wf.archivo, None, hace_h=0.1, ident=24, titulo=titulo(wf))
    ajena = corrida("tests.yml", hace_h=1, ident=25, evento="push")
    red = _api(cadenas, {FE: [pide, rota, sin_artefacto, en_curso, ajena]}, cadenas_de={
        (FE, 21): {"reparar": {"result": "success", "outputs": {"pedir_vigilancia": PEDIR}}},
        (FE, 22): {"reparar": {"result": "success", "outputs": {"pedir_x": "{mal"}}},
        (FE, 24): {}, (FE, 25): {}})
    foto = tomar_foto(Cliente("t", transporte=red), cadenas, AHORA, cache=tmp_path / "cache")
    por_id = {c["id"]: c for c in foto["repos"][FE]["corridas"]}
    assert por_id[21]["peticiones"] == [{"job": "reparar", "salida": "pedir_vigilancia", "workflow": wf.archivo,
                                         "repo": None, "inputs": {"estados": "jalisco"}}]
    assert "peticiones_invalidas" not in por_id[21]
    # Una salida mal formada no truena la vuelta: queda anotada.
    assert por_id[22]["peticiones"] == [] and por_id[22]["peticiones_invalidas"][0]["motivo"] == "no es JSON"
    # Sin artefacto (anterior al contrato, o no llegó al job `estado`), en curso, o de un workflow que no pide: nada.
    assert all("peticiones" not in por_id[i] for i in (23, 24, 25))
    bajadas = [u for _, u, _, _ in red.pedidos if "/zip" in u]
    assert sorted(u.rsplit("/", 2)[-2] for u in bajadas if "api.github.com" in u) == ["1021", "1022"]
    assert not any("Authorization" in cab for _, u, cab, _ in red.pedidos if "almacen.example" in u)
    assert len([u for _, u, _, _ in red.pedidos if f"/{FE}/actions/artifacts?" in u]) == 1
    json.dumps(foto)

    # La segunda vuelta no vuelve a bajar nada de lo ya leído, y las peticiones siguen en la foto.
    red.pedidos.clear()
    otra = tomar_foto(Cliente("t", transporte=red), cadenas, AHORA + timedelta(minutes=15), cache=tmp_path / "cache")
    assert not any("/zip" in u for _, u, _, _ in red.pedidos)
    assert {c["id"]: c.get("peticiones") for c in otra["repos"][FE]["corridas"]}[21] == por_id[21]["peticiones"]


def test_las_cadenas_se_bajan_con_tope_por_vuelta(cadenas, tmp_path):
    FE = "fiscal-extractor"
    wf = cadenas.workflow(FE, "vigilar-fiscal.yml")
    corridas = [corrida(wf.archivo, hace_h=n, ident=30 + n, titulo=titulo(wf)) for n in range(1, 6)]
    red = _api(cadenas, {FE: corridas}, cadenas_de={(FE, c["id"]): {} for c in corridas})
    pocas = dataclasses.replace(cadenas, general=cadenas.general | {"max_cadenas_por_vuelta": 2})
    foto = tomar_foto(Cliente("t", transporte=red), pocas, AHORA, cache=tmp_path / "cache")
    # Primero las más nuevas (ids mayores); las demás, en las vueltas siguientes.
    assert sorted(c["id"] for c in foto["repos"][FE]["corridas"] if "peticiones" in c) == [34, 35]
    assert foto["cadenas_sin_leer"] == 3
    foto = tomar_foto(Cliente("t", transporte=red), pocas, AHORA, cache=tmp_path / "cache")
    assert sorted(c["id"] for c in foto["repos"][FE]["corridas"] if "peticiones" in c) == [32, 33, 34, 35]


def test_la_firma_no_depende_de_quien_escribio_el_cache(cadenas, tmp_path):
    """La corrida 44272219102 salía con dos huellas: lo guardado por una versión anterior traía el eco del
    script («bash scripts/agente/leer.sh») dentro de la cola, y la firma se sacaba de lo guardado."""
    log = (FIXTURES / "log_job_actualizar_44272219102.txt").read_bytes()
    paso = {"name": "Agente — leer las páginas que el parser no entendió (1)", "number": 10, "status": "completed",
            "conclusion": "failure", "started_at": "2026-11-18T09:30:42Z", "completed_at": "2026-11-18T09:30:42Z"}
    jobs = [{"id": 134699222777, "name": "actualizar", "status": "completed", "conclusion": "failure",
             "started_at": "2026-11-18T09:17:40Z", "completed_at": "2026-11-18T09:30:47Z", "steps": [paso]}]
    red = Red([("/runs/44272219102/jobs", (200, {}, {"jobs": jobs})),
               ("/actions/jobs/", (302, {"location": "https://almacen.example/l"}, b"")),
               ("almacen.example", (200, {}, log))])
    c = corrida("actualizar-congreso.yml", "failure", ident=44272219102)
    cache = tmp_path / "cache"
    cache.mkdir()
    # Lo que dejó la versión anterior: la cola ya recortada, con el eco sin su color.
    (cache / "congreso-extractor-44272219102-1.json").write_text(json.dumps([jobs[0] | {"log": [
        "##[group]Run bash scripts/agente/leer.sh", "bash scripts/agente/leer.sh", "shell: /usr/bin/bash -e {0}",
        "##[endgroup]", "##[error]Process completed with exit code 1."]}]))

    def firma():
        leidos = _jobs_de(Cliente("t", transporte=red), "OpenCodice", "congreso-extractor", c, 40, cache)
        return inv.falla_de(c | {"jobs": leidos})["firma"]

    assert firma() == "Process completed with exit code 1."
    pedidos = len(red.pedidos)
    assert firma() == "Process completed with exit code 1." and len(red.pedidos) == pedidos      # ya del caché
    # Se guarda el log crudo: la cola (y con ella la firma) se saca con el código de hoy en cada lectura.
    guardado = json.loads((cache / "congreso-extractor-44272219102-1.v2.json").read_text())
    assert "log" not in guardado[0] and any("\x1b[36;1m" in l for l in guardado[0]["log_crudo"])
    assert huella.de_falla("congreso-extractor", "actualizar-congreso.yml", "actualizar", paso["name"], firma()) == "411a8dc49bba"


def test_un_log_que_no_se_pudo_bajar_no_se_guarda(cadenas, tmp_path):
    jobs = [{"id": 7, "name": "vigilar", "status": "completed", "conclusion": "failure",
             "steps": [{"name": "Descargar", "conclusion": "failure"}]}]
    caido = Red([("/runs/11/jobs", (200, {}, {"jobs": jobs})), ("/actions/jobs/", (404, {}, {"message": "Not Found"}))])
    c = corrida("vigilar-fiscal.yml", "failure", ident=11)
    leidos = _jobs_de(Cliente("t", transporte=caido), "OpenCodice", "fiscal-extractor", c, 40, tmp_path)
    assert leidos[0]["log"] == [] and "log_error" in leidos[0] and not list(tmp_path.iterdir())
    # En la vuelta siguiente el log ya está: se lee y entonces sí se guarda.
    red = Red([("/runs/11/jobs", (200, {}, {"jobs": jobs})), ("/actions/jobs/", (302, {"location": "https://almacen.example/l"}, b"")),
               ("almacen.example", (200, {}, b"2026-11-18T10:00:01.0Z ##[error]Process completed with exit code 2.\n"))])
    leidos = _jobs_de(Cliente("t", transporte=red), "OpenCodice", "fiscal-extractor", c, 40, tmp_path)
    assert leidos[0]["log"] == ["##[error]Process completed with exit code 2."] and len(list(tmp_path.iterdir())) == 1


SALUD = {"ahora": "2026-11-18T12:00:00Z", "sitios": {
    "gaceta.diputados.gob.mx": {"pausa_ms": 3000, "pausa_base_ms": 3000, "bloqueos_seguidos": 0, "ultimo_bloqueo": None,
                                "en_espera_hasta": None, "pendientes": 12, "pendiente_mas_antigua": "2026-11-18T03:00:00Z",
                                "ultimo_exito": "2026-11-18T02:00:00Z"},
    "www.senado.gob.mx": {"pausa_ms": 3000, "pausa_base_ms": 3000, "bloqueos_seguidos": 0, "ultimo_bloqueo": None,
                          "en_espera_hasta": None, "pendientes": 0, "pendiente_mas_antigua": None,
                          "ultimo_exito": "2026-11-18T11:58:00Z"}}}


def test_salud_del_worker_con_su_token_y_sin_el(cadenas):
    worker = cadenas.workers[0]
    red = Red([("workers.dev/salud", (200, {}, SALUD))])
    leido = salud_de(Cliente("token-de-github", transporte=red), worker, {"CONGRESO_ARCHIVO_TOKEN": "del-worker"})
    assert leido["observable"] and leido["sitios"]["gaceta.diputados.gob.mx"] == {
        "pendientes": 12, "pendiente_mas_antigua": "2026-11-18T03:00:00Z", "ultimo_exito": "2026-11-18T02:00:00Z",
        "en_espera_hasta": None, "bloqueos_seguidos": 0}
    # Al Worker va su token, nunca el de GitHub.
    assert red.pedidos[0][2]["Authorization"] == "Bearer del-worker"
    sin = salud_de(Cliente("t", transporte=red), worker, {})
    assert sin == {"observable": False, "motivo": "falta CONGRESO_ARCHIVO_TOKEN en el entorno"} and len(red.pedidos) == 1
    negado = salud_de(Cliente("t", transporte=Red([("/salud", (401, {}, b"no autorizado\n"))])), worker, {"CONGRESO_ARCHIVO_TOKEN": "x"})
    assert negado == {"observable": False, "motivo": "GET /salud: HTTP 401", "rechazo": 401}
    caido = salud_de(Cliente("t", transporte=Red([("/salud", (503, {}, b""))])), worker, {"CONGRESO_ARCHIVO_TOKEN": "x"})
    assert caido == {"observable": False, "motivo": "GET /salud: HTTP 503"}

    def caida(*_):
        raise OSError("sin red")
    assert salud_de(Cliente("t", transporte=caida), worker, {"CONGRESO_ARCHIVO_TOKEN": "x"})["observable"] is False


def test_la_foto_lleva_la_salud_solo_con_i10_encendida(cadenas, tmp_path):
    con = dataclasses.replace(cadenas, invariantes=cadenas.invariantes | {"I10": True})
    red = _api(cadenas, {})
    red.rutas.append(("workers.dev/salud", (200, {}, SALUD)))
    foto = tomar_foto(Cliente("t", transporte=red), con, AHORA, entorno={"CONGRESO_ARCHIVO_TOKEN": "x"})
    assert foto["workers"]["congreso-archivo"]["observable"]
    foto = tomar_foto(Cliente("t", transporte=red), con, AHORA, entorno={})
    assert foto["workers"]["congreso-archivo"]["observable"] is False
    assert foto["motivos"]["congreso-archivo"] == "falta CONGRESO_ARCHIVO_TOKEN en el entorno"
    assert tomar_foto(Cliente("t", transporte=red), cadenas, AHORA, entorno={})["workers"] == {}


# ---- Credenciales (I13) ----

@pytest.mark.parametrize("cabecera, esperado", [
    ("2027-11-20 06:00:00 UTC", "2027-11-20T06:00:00Z"),
    ("2027-11-20 00:00:00 -0600", "2027-11-20T06:00:00Z"),
    ("2027-11-20 08:00:00 +0200", "2027-11-20T06:00:00Z"),
    ("2027-11-20 11:30:00 +05:30", "2027-11-20T06:00:00Z"),
    ("2027-11-20T06:00:00Z", "2027-11-20T06:00:00Z"),
    ("2027-11-20 06:00:00", "2027-11-20T06:00:00Z"),
    (" 2027-02-28 23:59:59 UTC ", "2027-02-28T23:59:59Z"),
])
def test_vencimiento_en_los_formatos_de_la_cabecera(cabecera, esperado):
    assert inv.iso(vencimiento(cabecera)) == esperado


@pytest.mark.parametrize("cabecera", ["", "nunca", "2027-11-20", "20/11/2027 06:00:00 UTC", "2027-13-40 06:00:00 UTC"])
def test_vencimiento_ilegible(cabecera):
    with pytest.raises(ValueError):
        vencimiento(cabecera)


def _con_cabeceras(red, cabeceras):
    """La misma API, con esas cabeceras en cada respuesta."""
    def envolver(respuesta):
        def responder(url):
            estado, cab, datos = respuesta(url) if callable(respuesta) else respuesta
            return estado, cab | cabeceras, datos
        return responder
    red.rutas = [(fragmento, envolver(respuesta)) for fragmento, respuesta in red.rutas]
    return red


def test_la_foto_lleva_el_vencimiento_del_token_sin_llamadas_de_mas(cadenas):
    sin_cabecera = tomar_foto(Cliente("t", transporte=_api(cadenas, {})), cadenas, AHORA, entorno={"ACTUA": "true"})
    assert sin_cabecera["credenciales"]["github"] == {
        "propio": True, "observado": True, "vence": None, "cabecera": None, "rechazos": []}
    assert "rechazos" not in sin_cabecera

    red = _con_cabeceras(_api(cadenas, {}), {"github-authentication-token-expiration": "2026-12-10 00:00:00 -0600"})
    foto = tomar_foto(Cliente("t", transporte=red), cadenas, AHORA, entorno={"ACTUA": "true"})
    assert foto["credenciales"]["github"] | {"cabecera": None} == sin_cabecera["credenciales"]["github"] | {
        "vence": "2026-12-10T06:00:00Z"}
    assert foto["llamadas"] == sin_cabecera["llamadas"]
    [h] = inv.i13(foto, cadenas, AHORA)
    assert h.objeto == "SUPERVISOR_TOKEN" and "faltan 21 días" in h.motivo

    # Con el token del job (sin el secret) y en una máquina (sin esa variable).
    assert tomar_foto(Cliente("t", transporte=red), cadenas, AHORA, entorno={"ACTUA": "false"})["credenciales"]["github"]["propio"] is False
    assert tomar_foto(Cliente("t", transporte=red), cadenas, AHORA, entorno={})["credenciales"]["github"]["propio"] is None

    rara = _con_cabeceras(_api(cadenas, {}), {"github-authentication-token-expiration": "pronto"})
    leido = tomar_foto(Cliente("t", transporte=rara), cadenas, AHORA, entorno={"ACTUA": "true"})["credenciales"]["github"]
    assert (leido["observado"], leido["vence"], leido["cabecera"]) == (False, None, "pronto")


def test_la_foto_anota_lo_que_la_api_le_nego_al_token(cadenas):
    red = _api(cadenas, {}, privados=("constitucion-web",))
    red.rutas.insert(0, ("/repos/OpenCodice/fiscal-extractor/actions/runs", (403, {}, {"message": "Resource not accessible by personal access token"})))
    negada = _accion_negada()
    foto = tomar_foto(Cliente("t", transporte=red), cadenas, AHORA, entorno={"ACTUA": "true"}, acciones=[negada])
    leido = foto["credenciales"]["github"]
    assert leido["observado"] and {(r["que"], r["estado"], r["origen"]) for r in leido["rechazos"]} == {
        ("leer las corridas de constitucion-web", 404, "foto"), ("leer las corridas de fiscal-extractor", 403, "foto"),
        ("lanzar o cancelar corridas en congreso-extractor", 403, "accion")}
    assert not inv.token_rechazado(foto)
    assert [h.objeto for h in inv.i13(foto, cadenas, AHORA)] == ["SUPERVISOR_TOKEN/permisos"]


def _accion_negada():
    return {"cuando": t(3), "accion": "lanzar", "huella": "h", "detalle": {"repo": "congreso-extractor", "workflow": "x.yml"},
            "resultado": "error: POST https://api.github.com/x/dispatches: HTTP 403 {\"message\":\"Resource not accessible by personal access token\"}"}


def test_un_token_vencido_no_se_confunde_con_uno_que_no_vence(cadenas):
    muerto = Red([("", (401, {}, {"message": "Bad credentials"}))])
    foto = tomar_foto(Cliente("t", transporte=muerto), cadenas, AHORA, entorno={"ACTUA": "true"})
    leido = foto["credenciales"]["github"]
    assert leido["observado"] is False and leido["vence"] is None and {r["estado"] for r in leido["rechazos"]} == {401}
    assert inv.token_rechazado(foto) and "I13" in inv.sin_observar(foto, cadenas)
