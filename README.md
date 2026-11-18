# supervisor

El reconciliador de [OpenCodice](https://github.com/OpenCodice). Cada 15 minutos mira el estado real de
las cadenas (corridas, PR, commits) y lo compara con lo que debería ser cierto; lo que falta, lo relanza.

Antes cada corrida era responsable de lanzar a la siguiente. Si ese último paso no corría (un script que
no estaba donde se le buscaba, una corrida cancelada mientras esperaba, un job saltado), el trabajo
pendiente seguía existiendo y nadie lo sabía. Aquí la pregunta no es «¿qué acaba de pasar?» sino «¿qué
debería ser cierto ahora?»:

| # | Invariante | Si está rota |
|---|---|---|
| I1 | Cada workflow tiene una corrida exitosa dentro de su ventana | lo lanza |
| I2 | Toda falla tiene dueño | reintenta con espera; si la huella se repite, lanza al agente de infraestructura |
| I3 | Nada se pierde en la cola | lanza la unión de los inputs de las corridas canceladas en espera |
| I4 | Nada se atora | cancela y relanza |
| I5 | Ningún PR automático queda abierto | lanza `revisar-pr`; si sigue abierto, lo cierra y relanza su vigilancia |
| I6 | El trabajo pendiente declarado (`estado pendiente=[true]`) continúa | lanza la siguiente tanda |
| I7 | Un extractor nuevo se prueba contra su corpus | lanza la vigilancia |
| I8 | La web está al día con los repos de datos | lanza el despliegue |
| I9 | Los relojes viven (apagada) | constancia |
| I10 | Los Workers avanzan (apagada) | — |
| I11 | Ningún workflow lleva tres corridas seguidas en rojo | lanza al agente de infraestructura |

El supervisor **no decide nada sobre texto legal y no fusiona PR**: lanza workflows, cancela corridas
atoradas, cierra un PR automático atorado (cerrar no publica nada) y deja constancia en issues de este
repo. Las fallas de una compuerta de datos las reconoce y no las toca.

## Cuando reintentar no basta

Una falla que se repite igual después de sus reintentos no es mala suerte: algo de la infraestructura
está roto (una ruta que apunta a otro checkout, un `gh workflow run` con un input que no existe).
`reparar-infra.yml` se la da a un agente de Claude Code, y código determinista decide si su arreglo
entra:

1. El agente trabaja con un usuario sin privilegios y un solo secreto (el suyo). Recibe la huella, el
   log recortado del paso que falló y nada escrito por terceros. Entrega archivos, no commits.
2. Compuertas sin ningún secreto comparan cada workflow tocado **como datos** antes y después: no
   pueden cambiar los disparadores, los permisos, los secretos de ningún paso, las acciones ni el
   conjunto de jobs; en un job que corre con secretos casi nada puede cambiar; ninguna línea nueva puede
   traer red, un host nuevo, ni esconder una falla; y el arreglo tiene que tocar el archivo del paso que
   falló.
3. Otro agente, que no ve el razonamiento del primero, contesta tres preguntas cerradas.
4. Si todo pasa, el arreglo se publica en main del extractor, se relanza la corrida que falló, y el
   reconciliador la vigila: si el paso vuelve a fallar igual (o no hay resultado en 12 horas), **lo
   revierte solo**.

El agente solo puede tocar workflows y scripts de orquestación de los tres extractores, nunca el
extractor, las compuertas de fidelidad, los repos de datos ni este repo. Dos intentos por huella y tres
sesiones al día; lo que no se pueda arreglar así queda `agotada` con su motivo en un issue. Para
ensayarlo sin publicar nada: `gh workflow run reparar-infra.yml -f ensayo=true -f huella=<huella>`. El
detalle (alcance, cada compuerta, cómo se aísla el agente) está en `AGENTS.md`.

## Piezas

- `cadenas.toml` — lo declarado: workflows, grupos de concurrency, ventanas, horarios, inputs que se
  pueden unir, pasos que son compuertas de datos, topes y firmas.
- `supervisor/observar.py` — API de GitHub → una «foto» JSON (solo lectura).
- `supervisor/invariantes.py` — foto + cadenas + ahora → hallazgos. Puro.
- `supervisor/huella.py` — firma normalizada de una falla y huella estable.
- `supervisor/politica.py` — hallazgo + estado + ahora → acción. Puro.
- `supervisor/actuar.py` — ejecuta la acción y la anota.
- `supervisor/estado.py` — `estado/huellas.jsonl`, `estado/acciones.jsonl`, `estado/latido.json`.
- `supervisor/lint_workflows.py` — lint independiente de workflows, para cualquier repo.
- `supervisor/reparar.py` — la parte determinista de `reparar-infra.yml`: preparar, anotar, publicar,
  revertir.
- `supervisor/compuertas.py` — las compuertas del arreglo del agente.
- `scripts/agente/` — `infra.sh` (lanza a los dos agentes, aislados) y sus briefs.

## Correrlo

```sh
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q

# Solo lectura contra la organización: dice lo que haría.
export GH_TOKEN="$(gh auth token)"
.venv/bin/python -m supervisor correr --modo informe --estado /tmp/estado-de-prueba

# Guardar una foto y reproducir una decisión sin red.
.venv/bin/python -m supervisor foto --salida /tmp/foto.json
.venv/bin/python -m supervisor correr --modo informe --foto /tmp/foto.json --ahora 2026-11-18T12:00:00Z --estado /tmp/estado-de-prueba
```

`correr` solo necesita la biblioteca estándar. Sin `--estado` escribe en `estado/` de este repo, que es
el estado real: para probar, apunta a otra carpeta. El modo `actuar` ejecuta de verdad (lanza, cancela,
cierra PR) con el token de `GITHUB_TOKEN`/`GH_TOKEN`; en producción solo lo usa `supervisor.yml` cuando
existe el secret `SUPERVISOR_TOKEN`.

## El lint de workflows

```sh
git clone -q --depth 1 https://github.com/OpenCodice/supervisor.git /tmp/supervisor
python /tmp/supervisor/supervisor/lint_workflows.py <raíz del repo> [--hermanos <carpeta con otros repos>]
```

Sale distinto de 0 con hallazgos, uno por línea `archivo:línea: mensaje`. Necesita PyYAML. Comprueba lo
que `actionlint` no mira:

- **Rutas.** Por cada job calcula qué directorios existen (cada `actions/checkout` con su `path`,
  `repository` y `sparse-checkout`; cada `git clone … <destino>`) y exige que todo script invocado por
  ruta caiga en uno de ellos y exista en ese repo. Solo opina de los repos que tiene en disco: el
  propio y, con `--hermanos`, los que estén en esa carpeta. Sin el otro repo en disco todavía señala
  el caso que rompió la cadena el 2026-11-18: un script de este repo buscado en el checkout de otro
  cuando el job tiene este repo clonado en otra ruta.
- **Lanzamientos.** Cada `gh workflow run X -f k=v` (en workflows y en `scripts/**/*.sh`) apunta a un
  workflow que existe, admite `workflow_dispatch` y declara ese input.
- **`needs`.** Cada `needs.J.outputs.K` y `needs.J.result` nombra un job que está en `needs` y un
  output declarado (también los de un workflow reutilizable local).
- **Tiempos.** Todo job con `runs-on` declara `timeout-minutes` y ningún paso tiene un límite mayor
  que el de su job.

## Estado

`estado/` solo crece. De `huellas.jsonl` vale la última línea de cada huella; `acciones.jsonl` es la
bitácora de lo que el supervisor hizo (y de lo que hizo `reparar-infra.yml` con cada huella); `latido.json` es la última evaluación. Se hace commit solo cuando
algo cambió (sin novedades, el latido se reescribe una vez al día).

## Licencia

MIT. Ver `LICENSE`.
