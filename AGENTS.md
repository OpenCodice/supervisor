# supervisor — guía para agentes

El reconciliador de OpenCodice: un job de ~1 minuto, sin LLM, que cada 15 minutos observa el estado de
las cadenas de la organización y relanza lo que falte. Lo lanza el Worker `scheduler`
(`constitucion-extractor/scheduler/`); `supervisor.yml` tiene además un cron de respaldo. El contexto
del proyecto está en el `AGENTS.md` de la raíz del espacio de trabajo.

## Reglas

- **El supervisor no decide nada sobre texto legal y no fusiona PR.** Solo lanza workflows
  (`workflow_dispatch`), cancela corridas atoradas, cierra un PR automático atorado y borra su rama
  (cerrar nunca publica), y abre o comenta issues en este repo. El único que fusiona es `revisar-pr.yml`
  de los extractores; el supervisor a lo más lo lanza. No escribe en ningún repo de datos.
- **Las fallas de datos no son suyas.** Un paso que falla a propósito por los datos o por el extractor
  (las compuertas de `cadenas.toml`) se clasifica `datos` y se deja a los agentes de las vigilancias. No
  toca topes de reparación ni de expansión.
- **Único lanzador.** Nunca lanza un workflow cuyo grupo de concurrency ya tiene una corrida en espera
  (GitHub guarda una sola: la nueva cancelaría a la que espera), ni relanza lo que ya tiene una corrida
  posterior equivalente (en curso, en espera o exitosa).
- **Que no se cicle.** Reintentos con espera (1 h → 6 h → 24 h para lo transitorio), tope de
  lanzamientos por corrida y cortacircuitos de 12 lanzamientos por workflow en 24 h. Si tocas la
  política, la prueba de idempotencia (la misma foto con el estado resultante no repite acciones) tiene
  que seguir en verde.
- **`invariantes.py` y `politica.py` son puros.** Sin red, sin disco, sin reloj: reciben la foto, lo
  declarado y `ahora`. Todo lo que toca la API vive en `observar.py` (leer) y `actuar.py` (escribir).
- **Nada por nombre en el código.** Qué workflows existen, sus grupos, ventanas, inputs y compuertas
  sale de `cadenas.toml`. `tests/test_cadenas.py` lo compara con los YAML reales cuando los repos
  hermanos están en disco.
- **Solo biblioteca estándar** en lo que corre cada 15 minutos. PyYAML se usa nada más en
  `lint_workflows.py` y en las pruebas.
- **`estado/` solo crece** y lo escribe el workflow. No se edita a mano ni se le hace commit desde una
  corrida local: para probar, `--estado <otra carpeta>`.
- Git: se trabaja en `main`, commits en español, sin coautoría de asistentes. `git pull --ff-only`
  antes de editar: el workflow hace push de `estado/` cada vez que algo cambia.

## Contrato con los workflows vigilados

- **`run-name`.** La API no devuelve los inputs de un dispatch; `display_title` sí. Cada workflow
  encadenado declara `run-name: "<archivo sin .yml> k1=[v1] k2=[v2] … origen=[x]"`, con todos sus
  inputs en el orden en que se declaran. Gramática: `^[a-z0-9-]+( [a-z_]+=\[[^\]]*\])*$`. Una corrida
  cuyo título no la cumple (anterior al contrato, push, schedule) tiene inputs desconocidos: solo se
  relanza, sin inputs, si el workflow lo declara seguro (`relanzar_sin_inputs`).
- **Input `origen`.** El supervisor lanza con `origen=supervisor`.
- **Job final `estado`.** Su nombre es `estado pendiente=[true|false]`: se lee en la misma llamada que
  lista los jobs. `true` = terminé bien mi parte y queda otra tanda con los mismos inputs (I6).

## Cómo se agrega un invariante

1. En `supervisor/invariantes.py`, una función `iN(foto, cadenas, ahora) -> list[Hallazgo]` y su
   entrada en `INVARIANTES`. Si necesita algo que la foto no trae, se agrega en `observar.tomar_foto`
   (y, si son jobs, en `necesitan_jobs`, que decide qué corridas se leen a fondo): cuida el número de
   llamadas, corre 96 veces al día.
2. Su interruptor en `[invariantes]` de `cadenas.toml`, y lo que haya que declarar por workflow.
3. En `supervisor/politica.py`, qué acción le toca en `decidir`. Casi todo lo que termina en un
   lanzamiento pasa por `_lanzar`, que ya aplica la espera, el tope, el cortacircuitos y la regla del
   único lanzador. Un hallazgo con `en_vuelo` (ya hay una corrida viva que lo resolvería) no produce
   acción pero mantiene abierta su huella.
4. Pruebas: el invariante con fotos pequeñas en `tests/test_invariantes.py` (parte de `foto_sana`, que
   no tiene hallazgos) y una vuelta completa en `tests/test_politica.py`. Las fotos de prueba salen de
   corridas reales recortadas (`tests/fixtures/`).
5. La fila en la tabla del README.

## Huellas y clases

Una falla se identifica por `(repo, workflow, job, paso, firma)`; la firma es la línea de error del paso
sin lo que cambia de una corrida a otra (rutas temporales, números de corrida, fechas, shas, hosts). Lo
que no es la falla de un paso se identifica por `(invariante, objeto)`.

| Clase | Regla | Tratamiento |
|---|---|---|
| `datos` | el paso es una compuerta de `cadenas.toml` | nada |
| `transitoria` | la firma casa con `[firmas].transitorias` | reintentos a 1 h, 6 h y 24 h; a la cuarta, `determinista` |
| `determinista` | casa con `[firmas].deterministas`, o la huella se repitió tras su reintento | un reintento; si repite, `agente` |
| `desconocida` | lo demás | un reintento a 1 h; si repite, `determinista` |

La acción `agente` hoy solo anota la huella como `pendiente-agente` y abre un issue: el workflow que
repara infraestructura con un agente todavía no existe.

## Probar

```sh
.venv/bin/python -m pytest -q
actionlint
.venv/bin/python supervisor/lint_workflows.py .
```

Una corrida real de solo lectura: `python -m supervisor correr --modo informe --estado <carpeta temporal>`
con `GH_TOKEN`. No lances el modo `actuar` desde una máquina: ejecuta de verdad.
