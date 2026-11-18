import json
from datetime import timedelta

import pytest
from conftest import AHORA, corrida, t

from supervisor.observar import Cliente, ErrorApi, LimiteDeUso, tomar_foto


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


def _api(cadenas, corridas_por_repo, jobs=None, privados=()):
    def listar(repo):
        def responder(url):
            if "status=success" in url:
                return 200, {}, {"workflow_runs": []}
            return 200, {}, {"workflow_runs": corridas_por_repo.get(repo, [])}
        return responder
    rutas = [(f"/repos/OpenCodice/{r}/actions/runs/{i}/jobs", (200, {}, {"jobs": j})) for (r, i), j in (jobs or {}).items()]
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
