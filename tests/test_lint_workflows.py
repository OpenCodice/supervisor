import subprocess
import textwrap
from pathlib import Path

import pytest
from conftest import RAIZ

from supervisor.lint_workflows import Lint, main

FISCAL = RAIZ.parent / "fiscal-extractor"

CABECERA = """\
on:
  workflow_dispatch:
    inputs:
      estados:
        default: ''
env:
  EXTRACTOR_REPO: https://github.com/OpenCodice/extractor.git
  DATOS: OpenCodice/datos
jobs:
"""


def _repo(tmp_path: Path, jobs: str, nombre: str = "vigilar.yml", otros: dict | None = None) -> Path:
    raiz = tmp_path / "extractor"
    (raiz / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (raiz / "scripts" / "agente").mkdir(parents=True, exist_ok=True)
    (raiz / "scripts" / "lanzar.sh").write_text("#!/usr/bin/env bash\n")
    (raiz / "scripts" / "agente" / "reparar.sh").write_text("#!/usr/bin/env bash\n")
    (raiz / "scripts" / "medir.py").write_text("")
    (raiz / ".github" / "workflows" / nombre).write_text(CABECERA + textwrap.indent(textwrap.dedent(jobs), "  "))
    for archivo, contenido in (otros or {}).items():
        (raiz / ".github" / "workflows" / archivo).write_text(textwrap.dedent(contenido))
    return raiz


def _linea(raiz: Path, fragmento: str, archivo: str = "vigilar.yml") -> int:
    lineas = (raiz / ".github" / "workflows" / archivo).read_text().splitlines()
    return next(n for n, l in enumerate(lineas, 1) if fragmento in l)


def _lint(raiz: Path, hermanos: Path | None = None) -> list[str]:
    return [f"{a}:{l}: {m}" for a, l, m in Lint(raiz, hermanos, "OpenCodice/extractor").correr()]


# El job `reparar` de vigilar-leyes-estatales.yml antes de 6e05c25: el workspace es un checkout del
# repo de datos y el lanzador vive en el clon del extractor.
EXIT_127 = """\
reparar:
  runs-on: ubuntu-latest
  timeout-minutes: 340
  steps:
    - uses: actions/checkout@v5
      with:
        repository: OpenCodice/datos
    - name: Clonar extractor
      run: |
        git clone -q "$EXTRACTOR_REPO" /tmp/extractor
        pip install -q -r /tmp/extractor/requirements.txt
    - name: Publicar
      run: bash /tmp/extractor/scripts/agente/reparar.sh publicar
    - name: Reintentar las leyes con el extractor reparado
      run: bash "$GITHUB_WORKSPACE/scripts/lanzar.sh"
"""


def test_exit_127_script_del_extractor_buscado_en_el_checkout_de_datos(tmp_path):
    raiz = _repo(tmp_path, EXIT_127)
    [hallazgo] = _lint(raiz)
    linea = _linea(raiz, "$GITHUB_WORKSPACE/scripts/lanzar.sh")
    assert hallazgo.startswith(f".github/workflows/vigilar.yml:{linea}: job `reparar`, paso «Reintentar las leyes con el extractor reparado»")
    assert "es un checkout de OpenCodice/datos" in hallazgo and "`/tmp/extractor`" in hallazgo
    # Con el repo de datos en disco no hace falta inferir nada: el archivo no existe ahí.
    (tmp_path / "datos").mkdir()
    [hallazgo] = _lint(raiz, tmp_path)
    assert "`scripts/lanzar.sh` no existe en OpenCodice/datos" in hallazgo
    # El arreglo de 6e05c25.
    arreglado = EXIT_127.replace('"$GITHUB_WORKSPACE/scripts/lanzar.sh"', '"/tmp/extractor/scripts/lanzar.sh"')
    assert _lint(_repo(tmp_path, arreglado), tmp_path) == []


def test_rutas_relativas_cd_subshell_y_working_directory(tmp_path):
    raiz = _repo(tmp_path, """\
        uno:
          runs-on: ubuntu-latest
          timeout-minutes: 10
          env:
            OUT: ${{ github.workspace }}
          steps:
            - uses: actions/checkout@v5
            - run: git clone -q --depth 1 "https://github.com/$DATOS.git" ../repos/datos
            - run: |
                python scripts/medir.py --out "$OUT"
                (cd "$DONDE_SEA" && python scripts/no-se-sabe.py)
                python scripts/falta.py
                cd ../repos/datos
                python "$GITHUB_WORKSPACE/scripts/medir.py" .
                bash scripts/lanzar.sh
            - working-directory: scripts
              run: python medir.py && python -m pytest -q && python -c 'print(1)'
            - run: |
                python - <<'PY'
                import subprocess
                subprocess.run(["bash", "scripts/tampoco.sh"])
                PY
                python3 /tmp/generado.py > /dev/null
            - run: echo 'x' > /tmp/generado.py
        """)
    (tmp_path / "datos").mkdir()
    hallazgos = _lint(raiz, tmp_path)
    assert len(hallazgos) == 2, hallazgos
    assert f"vigilar.yml:{_linea(raiz, 'scripts/falta.py')}:" in hallazgos[0]
    assert "`scripts/falta.py` no existe en OpenCodice/extractor" in hallazgos[0]
    assert f"vigilar.yml:{_linea(raiz, 'bash scripts/lanzar.sh')}:" in hallazgos[1]
    assert "no existe en OpenCodice/datos" in hallazgos[1]


def test_sin_checkout_y_clon_que_nadie_crea(tmp_path):
    raiz = _repo(tmp_path, """\
        uno:
          runs-on: ubuntu-latest
          timeout-minutes: 10
          steps:
            - run: bash scripts/lanzar.sh
            - run: bash /tmp/otro/scripts/lanzar.sh
            - uses: actions/download-artifact@v4
              with:
                path: /tmp/plan
            - run: bash /tmp/plan/correr.sh
        """)
    hallazgos = _lint(raiz)
    assert len(hallazgos) == 2
    assert "el job no hace checkout ahí antes de este paso" in hallazgos[0]
    assert "ningún checkout ni `git clone` del job crea `/tmp/otro`" in hallazgos[1]


def test_sparse_checkout_y_bucle_de_clones(tmp_path):
    raiz = _repo(tmp_path, """\
        uno:
          runs-on: ubuntu-latest
          timeout-minutes: 10
          steps:
            - uses: actions/checkout@v5
            - uses: actions/checkout@v5
              with:
                path: _lanzador
                sparse-checkout: scripts/lanzar.sh
                sparse-checkout-cone-mode: false
            - run: |
                bash "$GITHUB_WORKSPACE/_lanzador/scripts/lanzar.sh"
                bash "$GITHUB_WORKSPACE/_lanzador/scripts/agente/reparar.sh"
            - run: |
                for e in extractor otro; do
                  git clone -q --depth 1 "https://github.com/OpenCodice/$e.git" "/tmp/$e"
                done
            - run: |
                bash "/tmp/${GITHUB_REPOSITORY#*/}/scripts/lanzar.sh"
                bash "/tmp/${GITHUB_REPOSITORY#*/}/scripts/validar.sh"
                for f in scripts/*.sh; do bash -n "$f"; done
        """)
    hallazgos = _lint(raiz)
    assert len(hallazgos) == 2, hallazgos
    assert "queda fuera del `sparse-checkout`" in hallazgos[0] and "reparar.sh" in hallazgos[0]
    assert "`scripts/validar.sh` no existe en OpenCodice/extractor" in hallazgos[1]


def test_gh_workflow_run_apunta_a_un_workflow_y_a_inputs_que_existen(tmp_path):
    raiz = _repo(tmp_path, """\
        uno:
          runs-on: ubuntu-latest
          timeout-minutes: 10
          steps:
            - run: |
                gh workflow run vigilar.yml -R "$GITHUB_REPOSITORY" --ref main -f estados="$E"
                gh workflow run vigilar.yml -R "$GITHUB_REPOSITORY" --ref main -f reintento=true
                gh workflow run no-existe.yml --ref main
                gh workflow run pruebas.yml
                gh workflow run otro.yml -R OpenCodice/ajeno -f lo_que_sea=1
                # gh workflow run comentado.yml
        """, otros={"pruebas.yml": "on: [push]\njobs: {}\n"})
    (raiz / "scripts" / "lanzar.sh").write_text(
        'repo="${GITHUB_REPOSITORY:-OpenCodice/extractor}"\n'
        'gh workflow run vigilar.yml -R "$repo" --ref main -f origen=x "$@"\n')
    hallazgos = _lint(raiz)
    assert [h.split(": ", 1)[1] for h in hallazgos] == [
        "`gh workflow run vigilar.yml -f reintento=…`: vigilar.yml no declara el input `reintento`",
        "`gh workflow run no-existe.yml`: ese workflow no existe en OpenCodice/extractor",
        "`gh workflow run pruebas.yml`: ese workflow no tiene `workflow_dispatch`",
        "`gh workflow run vigilar.yml -f origen=…`: vigilar.yml no declara el input `origen`",
    ]
    assert hallazgos[-1].startswith("scripts/lanzar.sh:2:")


def test_needs_y_outputs(tmp_path):
    raiz = _repo(tmp_path, """\
        planear:
          runs-on: ubuntu-latest
          timeout-minutes: 5
          outputs:
            hay: ${{ steps.p.outputs.hay }}
          steps:
            - run: 'true'
        revisar:
          needs: planear
          uses: ./.github/workflows/revisar.yml
        cerrar:
          needs: [planear, revisar]
          if: ${{ needs.planear.outputs.hay == 'true' && needs.planear.outputs.falta == '' && needs.extraer.result == 'success' }}
          runs-on: ubuntu-latest
          timeout-minutes: 5
          steps:
            - run: echo "${{ needs.revisar.outputs.sha }} ${{ needs.revisar.outputs.otro }} ${{ needs.revisar.result }}"
        estado:
          name: "estado pendiente=[${{ needs.cerrar.outputs.continuar == 'true' && 'true' || 'false' }}]"
          needs: [cerrar]
          if: ${{ always() }}
          runs-on: ubuntu-latest
          timeout-minutes: 5
          permissions: {}
          steps:
            - run: 'true'
        """, otros={"revisar.yml": """\
            on:
              workflow_call:
                outputs:
                  sha:
                    value: ${{ jobs.validar.outputs.sha }}
            jobs:
              validar:
                runs-on: ubuntu-latest
                timeout-minutes: 5
                outputs:
                  sha: x
                steps:
                  - run: 'true'
            """})
    hallazgos = [h.split(": ", 1)[1] for h in _lint(raiz)]
    assert hallazgos == [
        "job `cerrar`: `needs.planear.outputs.falta` no existe: `planear` no declara ese output",
        "job `cerrar`: usa `needs.extraer` pero `extraer` no está en sus `needs`",
        "job `cerrar`: `needs.revisar.outputs.otro` no existe: `revisar` no declara ese output",
        "job `estado`: `needs.cerrar.outputs.continuar` no existe: `cerrar` no declara ese output",
    ]


def test_tiempos_todo_job_con_limite_y_ningun_paso_por_encima(tmp_path):
    raiz = _repo(tmp_path, """\
        sin_limite:
          runs-on: ubuntu-latest
          steps:
            - run: 'true'
        llamado:
          uses: ./.github/workflows/revisar.yml
        holgado:
          runs-on: ubuntu-latest
          timeout-minutes: 300
          steps:
            - name: Agente uno
              timeout-minutes: 300
              run: 'true'
            - name: Agente dos
              timeout-minutes: 120
              run: 'true'
            - name: Agente tres
              timeout-minutes: 301
              run: 'true'
        """, otros={"revisar.yml": "on:\n  workflow_call: {}\njobs:\n  v:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n    steps:\n      - run: 'true'\n"})
    hallazgos = _lint(raiz)
    assert [h.split(": ", 1)[1] for h in hallazgos] == [
        "job `sin_limite`: no declara `timeout-minutes`",
        "job `holgado`: el paso «Agente tres» tiene `timeout-minutes: 301`, mayor que el del job (300)",
    ]
    assert hallazgos[0].startswith(f".github/workflows/vigilar.yml:{_linea(raiz, 'sin_limite:')}:")
    assert hallazgos[1].startswith(f".github/workflows/vigilar.yml:{_linea(raiz, 'timeout-minutes: 301')}:")


def test_como_script_un_hallazgo_por_linea_y_codigo_de_salida(tmp_path, capsys):
    raiz = _repo(tmp_path, EXIT_127)
    assert main([str(raiz), "--repo", "OpenCodice/extractor"]) == 1
    salida = capsys.readouterr().out.strip().splitlines()
    assert len(salida) == 1 and salida[0].startswith(".github/workflows/vigilar.yml:24: ")
    limpio = _repo(tmp_path / "limpio", EXIT_127.replace('"$GITHUB_WORKSPACE/scripts/lanzar.sh"', "/tmp/extractor/scripts/lanzar.sh"))
    assert main([str(limpio), "--repo", "OpenCodice/extractor"]) == 0
    por_ruta = subprocess.run(["python", str(RAIZ / "supervisor" / "lint_workflows.py"), str(raiz), "--repo", "OpenCodice/extractor"],
                              capture_output=True, text=True, cwd=tmp_path)
    assert por_ruta.returncode == 1 and por_ruta.stdout.strip() == salida[0]


def test_el_lint_pasa_sobre_este_repo():
    assert Lint(RAIZ, propio="OpenCodice/supervisor").correr() == []


@pytest.mark.skipif(not (FISCAL / ".git").exists(), reason="fiscal-extractor no está en disco")
def test_aceptacion_fiscal_extractor_antes_de_6e05c25(tmp_path):
    arbol = tmp_path / "fe"
    creado = subprocess.run(["git", "-C", str(FISCAL), "worktree", "add", "--detach", str(arbol), "6e05c25~1"],
                            capture_output=True, text=True)
    if creado.returncode != 0:
        pytest.skip(f"no se pudo sacar 6e05c25~1: {creado.stderr.strip()}")
    try:
        for hermanos in (None, RAIZ.parent):
            rutas = [h for h in _lint_real(arbol, hermanos) if "timeout-minutes" not in h]
            assert len(rutas) == 2, rutas
            assert rutas[0].startswith(".github/workflows/vigilar-leyes-estatales.yml:709: job `reparar`, "
                                       "paso «Reintentar las leyes con el extractor reparado»")
            assert "paso «Dar de alta las leyes que pasaron a LEYES»" in rutas[1]
    finally:
        subprocess.run(["git", "-C", str(FISCAL), "worktree", "remove", "--force", str(arbol)], capture_output=True)


def _lint_real(raiz: Path, hermanos: Path | None) -> list[str]:
    return [f"{a}:{l}: {m}" for a, l, m in Lint(raiz, hermanos, "OpenCodice/fiscal-extractor").correr()]
