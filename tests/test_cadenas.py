"""cadenas.toml contra los workflows reales, cuando los repos hermanos están en disco."""

import re
from pathlib import Path

import pytest
from conftest import RAIZ

from supervisor.invariantes import analizar_titulo
from supervisor.lint_workflows import _lista, _mapa, cargar_yaml

HERMANOS = RAIZ.parent


def _yaml(wf):
    ruta = HERMANOS / wf.repo / ".github" / "workflows" / wf.archivo
    if not ruta.exists():
        pytest.skip(f"{wf.repo} no está en disco")
    return cargar_yaml(ruta)


def test_cadenas_se_carga_y_nombra_los_once_invariantes(cadenas):
    assert set(cadenas.invariantes) == {f"I{n}" for n in range(1, 12)}
    assert {w.repo for w in cadenas.workflows} >= {"constitucion-extractor", "fiscal-extractor", "congreso-extractor"}
    assert all(not w.lanza_horario for w in cadenas.workflows)
    assert cadenas.politica["espera_transitoria_h"] == [1, 6, 24] and cadenas.politica["max_lanzamientos_24h"] == 12


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
