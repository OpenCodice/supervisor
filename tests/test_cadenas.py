"""cadenas.toml contra los workflows reales, cuando los repos hermanos están en disco."""

import dataclasses
import re
from pathlib import Path

import pytest
from conftest import RAIZ

from supervisor import cadenas as _cadenas
from supervisor.invariantes import analizar_titulo
from supervisor.lint_workflows import _lista, _mapa, cargar_yaml, peticiones_escritas

HERMANOS = RAIZ.parent


def _yaml(wf):
    ruta = HERMANOS / wf.repo / ".github" / "workflows" / wf.archivo
    if not ruta.exists():
        pytest.skip(f"{wf.repo} no está en disco")
    return cargar_yaml(ruta)


def test_cadenas_se_carga_y_nombra_los_trece_invariantes(cadenas):
    assert set(cadenas.invariantes) == {f"I{n}" for n in range(1, 14)}
    assert {w.repo for w in cadenas.workflows} >= {"constitucion-extractor", "fiscal-extractor", "congreso-extractor"}
    assert cadenas.politica["espera_transitoria_h"] == [1, 6, 24] and cadenas.politica["max_lanzamientos_24h"] == 12


def test_el_supervisor_lanza_todo_lo_que_tiene_horario():
    """Lo declarado hoy (sin los ajustes del fixture): el horario de cada workflow, y que el supervisor lo lanza."""
    c = _cadenas.cargar()
    horarios = {w.archivo: list(w.horario) for w in c.workflows if w.horario}
    assert all(w.lanza_horario for w in c.workflows if w.horario)
    lunes = lambda hora: [{"dias": ["lun"], "hora": hora}]  # noqa: E731
    assert horarios == {
        "vigilar-cpeum.yml": lunes("09:00"), "vigilar-estatales.yml": lunes("09:00"), "vigilar-fiscal.yml": lunes("09:00"),
        "vigilar-federal.yml": lunes("13:00"), "vigilar-leyes-estatales.yml": lunes("13:00"),
        "expandir-estatales.yml": [{"dias": ["mie"], "hora": "15:30"}],
        "actualizar-congreso.yml": [{"dias": ["*"], "hora": "*:00", "inputs": {"publicar": "no"}},
                                    {"dias": ["*"], "hora": "18:00", "inputs": {"publicar": "si"}}],
        "credenciales.yml": lunes("08:00"),
    }
    # Semanal, con una ventana que deja pasar una cita y no dos.
    assert c.workflow("constitucion-extractor", "credenciales.yml").cadencias == ({"ventana_h": 192},)
    assert c.politica["aviso_vencimiento_dias"] == 30 and c.encendido("I13")
    assert c.credencial_github["nombre"] == "SUPERVISOR_TOKEN"
    assert {w["token_env"] for w in c.workers} <= {cred["nombre"] for cred in c.credenciales}
    assert c.workflow("fiscal-extractor", "expandir-estatales.yml").espera_libre == ("vigilar-leyes-estatales.yml",)
    assert not c.encendido("I7") and c.encendido("I10") and c.encendido("I12")


def test_lo_declarado_que_se_contradice_no_carga(cadenas):
    def con(archivo: str, **cambios):
        return dataclasses.replace(cadenas, workflows=tuple(
            dataclasses.replace(w, **cambios) if w.archivo == archivo else w for w in cadenas.workflows))

    # Un día por número significa otra cosa en cada cron: solo por nombre.
    for horario in ({"dias": [1], "hora": "09:00"}, {"dias": ["monday"], "hora": "09:00"}, {"dias": ["lun"], "hora": "9"},
                    {"dia_mes": 31, "hora": "10:00"}, {"dias": ["lun"], "hora": "09:00", "inputs": {"no_existe": "x"}}):
        with pytest.raises(ValueError, match="horario|dia_mes"):
            _cadenas.validar(con("vigilar-fiscal.yml", horario=(horario,)))
    for pedido in ("no-existe.yml", "deploy.yml", "supervisor/reparar-infra.yml", "resolver-residuos.yml"):
        with pytest.raises(ValueError, match="puede_pedir"):
            _cadenas.validar(con("actualizar-congreso.yml", puede_pedir=(pedido,)))
    _cadenas.validar(con("actualizar-congreso.yml", puede_pedir=("fiscal-extractor/vigilar-federal.yml",)))
    with pytest.raises(ValueError, match="espera_libre"):
        _cadenas.validar(con("expandir-estatales.yml", espera_libre=("vigilar-cpeum.yml",)))
    with pytest.raises(ValueError, match="https"):
        _cadenas.validar(dataclasses.replace(cadenas, workers=({"nombre": "w", "salud": "http://x/salud", "token_env": "T"},)))


def test_los_declarados_coinciden_con_sus_workflows(cadenas):
    for wf in cadenas.workflows:
        y = _yaml(wf)
        grupo = _mapa(y.get("concurrency")).get("group") if isinstance(y.get("concurrency"), dict) else y.get("concurrency")
        if wf.solo_grupo:
            assert wf.grupo in str(grupo), f"{wf.archivo}: grupo de concurrency"
            continue
        assert (re.sub(r"\$\{\{.*?\}\}", "", str(grupo or "")) or None) == wf.grupo, f"{wf.archivo}: grupo de concurrency"
        for caso in wf.grupos:
            assert caso["grupo"].removeprefix(wf.grupo) in str(grupo), f"{wf.archivo}: grupo según inputs"
        despacho = _mapa(_mapa(y.get("on")).get("workflow_dispatch"))
        assert "workflow_dispatch" in _mapa(y.get("on")), wf.archivo
        declarados = list(_mapa(despacho.get("inputs")))
        assert declarados == list(wf.inputs), f"{wf.archivo}: inputs y su orden"
        for k, v in _mapa(despacho.get("inputs")).items():
            assert str(_mapa(v).get("default", "")) == wf.valor_omision(k), f"{wf.archivo}: omisión de {k}"
        if wf.encadenado:
            ejemplo = re.sub(r"\$\{\{[^}]*\}\}", "x", str(y["run-name"]))
            leido = analizar_titulo(ejemplo)
            assert leido and leido[0] == wf.nombre and list(leido[1]) == list(wf.inputs), f"{wf.archivo}: run-name"
            estado = _mapa(y["jobs"]).get("estado")
            assert estado is not None and str(estado["name"]).startswith("estado pendiente=["), wf.archivo
            assert any(str(_mapa(p.get("with")).get("name")) == "cadena" for p in _lista(estado.get("steps"))), \
                f"{wf.archivo}: el job `estado` no sube el artefacto `cadena`"


def test_puede_pedir_es_lo_que_piden_los_workflows(cadenas):
    """Cada petición escrita en un workflow está permitida, y nada de lo permitido sobra."""
    for wf in cadenas.workflows:
        ruta = HERMANOS / wf.repo / ".github" / "workflows" / wf.archivo
        if not ruta.exists():
            continue
        y = cargar_yaml(ruta)
        declara = any(str(k).startswith("pedir") for j in _mapa(y.get("jobs")).values() for k in _mapa(_mapa(j).get("outputs")))
        pedidos = set()
        for destino, repo, claves in peticiones_escritas(ruta.read_text(encoding="utf-8")):
            pedidos.add(destino if repo in (None, wf.repo) else f"{repo}/{destino}")
            otro = cadenas.workflow(repo or wf.repo, destino)
            assert otro is not None and set(claves) <= set(otro.inputs) - {"origen"}, f"{wf.archivo} pide {destino} {claves}"
        assert pedidos == set(wf.puede_pedir), f"{wf.repo}/{wf.archivo}: pide {sorted(pedidos)}"
        assert declara == bool(wf.puede_pedir), f"{wf.repo}/{wf.archivo}: salidas `pedir*`"


def test_ningun_workflow_no_declarado_pide(cadenas):
    for repo in cadenas.repos_con_workflows():
        for ruta in sorted((HERMANOS / repo / ".github" / "workflows").glob("*.yml")):
            if cadenas.workflow(repo, ruta.name) is None:
                assert not list(peticiones_escritas(ruta.read_text(encoding="utf-8"))), f"{repo}/{ruta.name} pide y no está declarado"


def test_cada_compuerta_nombra_un_paso_que_existe(cadenas):
    for wf in cadenas.workflows:
        if not wf.compuertas:
            continue
        y = _yaml(wf)
        pasos = set()
        for job in _mapa(y["jobs"]).values():
            pasos |= {str(p.get("name")) for p in _lista(job.get("steps"))}
            llamado = re.match(r"^\./\.github/workflows/(.+)$", str(job.get("uses", "")))
            if llamado:
                otro = cargar_yaml(Path(HERMANOS / wf.repo / ".github" / "workflows" / llamado.group(1)))
                for j in _mapa(otro["jobs"]).values():
                    pasos |= {str(p.get("name")) for p in _lista(j.get("steps"))}
        for compuerta in wf.compuertas:
            assert compuerta["paso"] in pasos, f"{wf.repo}/{wf.archivo}: no hay un paso «{compuerta['paso']}»"
