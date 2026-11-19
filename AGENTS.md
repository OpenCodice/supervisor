# supervisor — guía para agentes

El reconciliador de OpenCodice y el único lanzador de sus corridas: un job de ~1 minuto, sin LLM, que
cada 15 minutos observa el estado de las cadenas de la organización y lanza lo que toca (por horario,
porque una corrida lo pidió, o porque falta). Lo lanza a él el Worker `scheduler`
(`constitucion-extractor/scheduler/`), que no lanza nada más; `supervisor.yml` tiene además un cron de
respaldo. El contexto del proyecto está en el `AGENTS.md` de la raíz del espacio de trabajo.

## Reglas

- **El supervisor no decide nada sobre texto legal y no fusiona PR.** El reconciliador solo lanza
  workflows (`workflow_dispatch`), cancela corridas atoradas, cierra un PR automático atorado y borra su
  rama (cerrar nunca publica), y abre o comenta issues en este repo. El único que fusiona es
  `revisar-pr.yml` de los extractores; el supervisor a lo más lo lanza. No escribe en ningún repo de datos.
  Lo único que escribe fuera de aquí es `reparar-infra.yml`: un arreglo de infraestructura en un
  extractor, dentro del alcance de `[reparacion]` (ver «El agente de infraestructura»).
- **Las fallas de datos no son suyas.** Un paso que falla a propósito por los datos o por el extractor
  (las compuertas de `cadenas.toml`) se clasifica `datos` y se deja a los agentes de las vigilancias. No
  toca topes de reparación ni de expansión.
- **Único lanzador.** Ningún workflow de la organización lanza a otro ni a sí mismo: lo exige
  `lint_workflows.py` en cada repo, y solo este está exento. El supervisor nunca lanza un workflow cuyo
  grupo de concurrency ya tiene una corrida en espera (GitHub guarda una sola: la nueva cancelaría a la
  que espera), ni relanza lo que ya tiene una corrida posterior equivalente (en curso, en espera o
  exitosa). Lo que no puede lanzar todavía no se pierde: sigue siendo un hallazgo en la vuelta siguiente.
- **Que no se cicle.** Reintentos con espera (1 h → 6 h → 24 h para lo transitorio), tope de
  lanzamientos por corrida y cortacircuitos de 12 lanzamientos por workflow en 24 h (lo lanzado por
  horario no cuenta: va una vez por cita). Si tocas la
  política, la prueba de idempotencia (la misma foto con el estado resultante no repite acciones) tiene
  que seguir en verde.
- **`invariantes.py` y `politica.py` son puros.** Sin red, sin disco, sin reloj: reciben la foto, lo
  declarado y `ahora`. Todo lo que toca la API vive en `observar.py` (leer) y `actuar.py` (escribir).
- **Nada por nombre en el código.** Qué workflows existen, sus grupos, ventanas, inputs y compuertas
  sale de `cadenas.toml`. `tests/test_cadenas.py` lo compara con los YAML reales cuando los repos
  hermanos están en disco.
- **Solo biblioteca estándar** en lo que corre cada 15 minutos. PyYAML se usa nada más en
  `lint_workflows.py`, en `compuertas.py` (que solo corre en `reparar-infra.yml`) y en las pruebas.
  `politica.py` no importa `compuertas.py` ni `reparar.py`.
- **`estado/` solo crece** y lo escriben los workflows. No se edita a mano ni se le hace commit desde una
  corrida local: para probar, `--estado <otra carpeta>`. `huellas.jsonl` y `latido.json` son solo del
  reconciliador; `reparar-infra.yml` solo agrega líneas a `acciones.jsonl` (por eso `.gitattributes` une
  las líneas de los dos al rebasar), y el reconciliador las lee en su vuelta siguiente.
- Git: se trabaja en `main`, commits en español, sin coautoría de asistentes. `git pull --ff-only`
  antes de editar: el workflow hace push de `estado/` cada vez que algo cambia.

## Contrato con los workflows vigilados

- **`run-name`.** La API no devuelve los inputs de un dispatch; `display_title` sí. Cada workflow
  encadenado declara `run-name: "<archivo sin .yml> k1=[v1] k2=[v2] … origen=[x]"`, con todos sus
  inputs en el orden en que se declaran. Gramática: `^[a-z0-9-]+( [a-z_]+=\[[^\]]*\])*$`. Una corrida
  cuyo título no la cumple (anterior al contrato, push, schedule) tiene inputs desconocidos: solo se
  relanza, sin inputs, si el workflow lo declara seguro (`relanzar_sin_inputs`).
- **Input `origen`.** El supervisor lanza con `origen=supervisor`.
- **Job final `estado`.** Último job, con `needs` de todos los demás e `if: always()`. Su nombre es
  `estado pendiente=[true|false]`: se lee en la misma llamada que lista los jobs. `true` = terminé bien
  mi parte y queda otra tanda con los mismos inputs (I6). Además sube el artefacto `cadena`
  (`cadena.json` = `toJSON(needs)`): las salidas y el resultado de cada job.
- **Peticiones.** Una salida de job cuyo nombre empieza por `pedir`, no vacía, es una petición o una
  lista de ellas: `{"workflow": "<archivo.yml>", "inputs": {…}, "repo": "<otro repo de la organización>"}`,
  con valores de cadena y sin `origen`. Significa «cuando esta corrida termine tiene que empezar una
  corrida de ese workflow que abarque esos inputs». Solo vale hacia un workflow declarado y que esté en
  el `puede_pedir` de quien pide.

## Lo que lanza y cuándo

- **Por horario (I1).** `horario` de cada workflow, con `lanza_horario = true`. Vale la cita más
  reciente y solo ella (lo atrasado no se acumula), durante `tolerancia_horario_min`; pasada, lo retoma
  la cadencia (`cadencias`). La corrida de una cita es la que hace todo lo de esa cita (no una parcial
  ni una de prueba), creada desde su hora o anterior y todavía en espera. Cada cita es una huella
  (`horario-<hora>`), así que no se lanza dos veces. Los días van por nombre (`lun`…`dom`): un número
  significa otra cosa en cada cron. Un workflow con `espera_libre` se lanza por horario solo cuando los
  workflows que nombra no tienen corridas vivas ni PR automático abierto, y su cita no caduca hasta la
  siguiente (la ronda de expansión con la cadena de leyes estatales); las rondas que se piden no esperan.
  También cuenta como ocupada la cadena que el propio supervisor acaba de lanzar y la foto todavía no
  trae: en esta misma vuelta (por eso estas citas se deciden al final, después de todo lo demás) o
  dentro de `gracia_lanzamiento_min` sin que su corrida aparezca (`politica._ocupada_fuera_de_la_foto`).
- **Por petición (I12).** `observar.py` baja el artefacto `cadena` de cada corrida terminada de un
  workflow `encadenado` o con `puede_pedir` (una llamada por repo para listarlos y una descarga por
  corrida, que se guarda: cada corrida se baja una sola vez; `max_cadenas_por_vuelta` acota la primera
  vez). Atendida = hay una corrida del workflow pedido que abarca los inputs y empezó después de que
  terminó la que pide (en el mismo grupo de concurrency basta con que se haya creado después: corren de
  una en una). Las pendientes al mismo workflow se unen como en I3. Si la cadena pedida tiene su PR
  automático abierto, espera a que se fusione. Una petición hacia un workflow no declarado, no
  permitida, con inputs que el destino no declara o mal formada es un hallazgo de clase `rechazada`:
  constancia en un issue, sin lanzar. No caduca mientras su corrida esté en la foto (`dias_foto`);
  después la retoma la vuelta completa de la cadencia.
- **La tanda siguiente (I6).** `estado pendiente=[true]` de una corrida exitosa que ninguna posterior
  abarca. No se lanza mientras el PR automático de esa cadena siga abierto; si no se fusiona, lo
  resuelve I5.
- **La web (I8).** `deploy.yml` (workflow_dispatch) cuando `main` de algún repo de datos (`[[datos]]`,
  también `congreso-mexicano`) es posterior al inicio del último despliegue exitoso, pasado
  `web_margen_min`. Si un despliegue posterior a ese commit falla, I8 ya no insiste: la falla tiene
  dueño (I2).
- **I7** (extractor nuevo ⇒ vigilancia) queda apagada: quien publica un arreglo del extractor pide su
  vigilancia con los inputs justos.
- **I10.** `GET /salud` de cada `[[workers]]`, con el token del entorno (`CONGRESO_ARCHIVO_TOKEN`, que
  `supervisor.yml` pasa si el secret existe; si el Worker lo rechaza, es un hallazgo de I13). Un sitio con pendientes cuya más antigua pasa de
  `worker_pendiente_max_h` y sin un éxito en ese tiempo es un hallazgo `determinista`, con huella por
  sitio: constancia en un issue. Sin el token, I10 sale `sin-observar`.
- **I13.** Las credenciales de `[[credenciales]]`. La del propio supervisor (`uso = "github"`): `Cliente`
  guarda la cabecera `github-authentication-token-expiration` de la primera respuesta que la API acepta
  (ninguna llamada de más; sin cabecera, el token no vence) y `tomar_foto` la deja en
  `foto["credenciales"]["github"]` junto con lo que la API le negó: lecturas de esta vuelta y, de
  `acciones.jsonl`, la última acción de cada clase sobre cada repo si fue un 401/403
  (`invariantes.rechazos_de_acciones`; `rechazo_vigente_h`). Hallazgos, uno por credencial y problema
  (`SUPERVISOR_TOKEN`, `SUPERVISOR_TOKEN/permisos`, `SUPERVISOR_TOKEN/ausente`,
  `CONGRESO_ARCHIVO_TOKEN@congreso-archivo`), de clase `credencial`: solo constancia en un issue
  (`politica._credencial`), con el texto de `[[credenciales]]`, repetida cada `aviso_repeticion_dias`;
  nunca un lanzamiento ni el agente. La base del de vencimiento es la fecha: otra fecha dentro del
  umbral vuelve a avisar, y una lejana (o ninguna) lo resuelve y cierra el issue. Solo una vuelta con el
  secret y con respuesta de la API da algo por resuelto (`politica._se_ve`). `presente_env` nombra la
  variable con que `supervisor.yml` dice si el secret existe (`ACTUA`); en una máquina no está, y el
  token se evalúa igual. Si la API rechaza el token en todo con 401, `correr` termina en 1.
- **Sin observar.** `invariantes.sin_observar` dice qué invariante no pudo mirar qué (un repo que el
  token no ve, un repo de datos, un Worker); en el latido sale `sin-observar` con su motivo, nunca `ok`.

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

La firma se saca en cada vuelta, con el código de hoy, del log crudo del paso: del encabezado
`##[group]Run` del paso que falló a su `##[error]`. El caché de corridas terminadas guarda ese log
crudo, no la cola ya limpia, y su nombre lleva una versión (`_VERSION_CACHE` de `observar.py`): si
cambias cómo se recorta o se limpia el log, súbela. Una huella que cambia de una vuelta a otra hace que
el reintento nunca escale.

| Clase | Regla | Tratamiento |
|---|---|---|
| `datos` | el paso es una compuerta de `cadenas.toml` | nada |
| `credencial` | la firma casa con `[firmas].credenciales` («Credencial por renovar», de `credenciales.yml`) | constancia en un issue con el error tal cual, repetida por cada corrida que vuelve a fallar; ni reintento ni agente. Los hallazgos de I13 llevan la misma clase |
| `transitoria` | la firma casa con `[firmas].transitorias` | reintentos a 1 h, 6 h y 24 h; a la cuarta, `determinista` |
| `determinista` | casa con `[firmas].deterministas`, o la huella se repitió tras su reintento | un reintento; si repite, `agente` |
| `desconocida` | lo demás | un reintento a 1 h; si repite, `determinista` |

La acción `agente` lanza `reparar-infra.yml -f huella=<h>` y deja constancia en el issue de la huella.

## El agente de infraestructura

Una huella `determinista` de un workflow de `constitucion-extractor`, `fiscal-extractor` o
`congreso-extractor` la intenta arreglar un agente de Claude Code. Riesgo propio: **lo que escribe corre
después con los secretos de la organización**, y lo que lee (logs con texto de sitios y PDF externos) no
es confiable. El diseño supone que al agente se le puede engañar y lo contiene con código.

**Flujo** (`reparar-infra.yml`; la lógica, en `supervisor/reparar.py` y `supervisor/compuertas.py`):

1. `preparar` (determinista; `SUPERVISOR_TOKEN` solo para leer): la huella sale de
   `estado/huellas.jsonl`; baja de sus dos últimas corridas la lista de jobs y pasos y las últimas 300
   líneas del paso que falló; arma el brief con eso, el cuerpo del issue **si lo escribió el propio
   supervisor** (nunca comentarios) y lo que pasó con los intentos anteriores. Anota `intento-iniciado`
   en `acciones.jsonl` antes del agente: uno que se cae también cuenta.
2. `agente` (`permissions: {}`): `claude -p` con `Bash Read Edit Write Glob Grep`, sin WebFetch ni
   WebSearch, 100 turnos y 45 minutos, con un **usuario sin sudo, sin docker y sin acceso al directorio
   del runner** (`scripts/agente/infra.sh`), y `CLAUDE_CODE_OAUTH_TOKEN` como único secreto en su
   entorno. Para probar descargas por México hay una puerta local (`proxy_mx.py` de fiscal-extractor)
   que corre con un tercer usuario: `EGRESS_MX_TOKEN` le llega por un archivo que borra al arrancar, y un
   paso comprueba, con el usuario del agente, que no puede leer su entorno ni su memoria; si no se puede
   garantizar, el agente no corre. Entrega su cambio en el árbol de trabajo (sus commits no cuentan) y
   un `veredicto.json`: `arreglo`, `no-es-infra` o `no-puedo`.
3. `compuertas` (sin ningún secreto): la entrega son datos, un tar con lo que cambió. Se pone sobre un
   clon limpio del commit de partida y de ahí sale `arreglo.patch`. Primero las compuertas que solo
   leen; al final, con otro usuario y sobre una copia, las que ejecutan código del arreglo (`pytest`,
   `node --test`, `wrangler deploy --dry-run`).
4. `juez` (solo `CLAUDE_CODE_OAUTH_TOKEN`; `Read Glob Grep Bash` sobre una copia de solo lectura): otro
   agente que no ve ni el veredicto ni la sesión del primero. Tres preguntas cerradas; solo (sí, no,
   no) deja pasar. Un veredicto ausente o mal formado descarta.
5. `publicar` (determinista; `SUPERVISOR_TOKEN`): aplica el parche con `git apply` sobre un clon limpio
   de main (si no aplica limpio, descarta), repite las compuertas que solo leen, hace el commit
   «Reparación automática de infraestructura: <paso> (<huella>)» con autoría `Claude (agente OpenCodice)`, lo sube con
   rebase y tres intentos, anota `arreglo-publicado` y relanza la corrida que falló con sus inputs (con
   la regla del único lanzador; si el grupo está ocupado, la lanza el reconciliador después). En
   cualquier otro final anota `descartado` con su motivo. Comenta el resultado en el issue.
6. El reconciliador **vigila el arreglo** (`politica._vigilar`): en una corrida posterior al commit, el
   paso de la huella pasa → `resuelta`; falla igual, el workflow ya no arranca, falla antes del paso en
   algo que el commit tocó, o pasan 12 h sin resultado → `revertir`: `reparar-infra.yml -f revertir=<sha>`
   hace `git revert` de ese commit (solo del que este workflow publicó para esa huella; si ya no es un
   revert limpio, falla cerrado: `reversion-fallida` y la huella queda `agotada`). Un arreglo revertido
   cuenta su intento, y doble si rompió algo anterior; la siguiente sesión recibe su parche fallido.

**Interruptor**: `[reparacion].activa` de `cadenas.toml`. Apagado, la política no lanza al agente y una
huella determinista solo deja su constancia (`agotada`, con ese motivo); al encenderlo, la toma. Un
ensayo a mano no depende de él.

**Topes**: 2 intentos por huella (caducan a los 28 días), 3 sesiones al día, una sola `reparar-infra` a
la vez. Una huella fuera del alcance (este repo, las webs, los repos de datos) queda `agotada` sin
gastar sesión. El supervisor no se repara a sí mismo.

**Alcance** (`[reparacion]` de `cadenas.toml`): workflows salvo `revisar-pr.yml`, `scripts/lanzar_*.sh`
(hoy no existe ninguno), `scripts/agente/*.sh` salvo `cerrar*.sh` y `publicar_registro.sh`, y los Workers `scheduler/` y
`archivo/`. Solo puede crear pruebas (`tests/test_infra_*.py`, `*.test.mjs` junto al Worker). Nunca
`extractor/`, `congreso/`, una prueba existente, las compuertas de fidelidad, `verificaciones/`,
`pendientes.json`, `certs/` ni `egress-mx/`. Un repo, 4 archivos, 120 líneas.

**Compuertas** (`supervisor/compuertas.py`; todas obligatorias, y lo que no se puede comprobar falla):

| Compuerta | Qué exige |
|---|---|
| `alcance`, `tamano` | solo rutas declaradas; nada se borra; lo nuevo son pruebas; dentro del tope |
| `yaml` | se lee, sin claves repetidas ni `<<` |
| `poder` | cada workflow tocado, **leído como datos** antes y después: idénticos `on` (se admite un input nuevo opcional con valor por omisión), `permissions`, `concurrency`, `environment`, `runs-on`, cada `uses`, el conjunto de jobs y de pasos, y los secretos que nombra cada job y cada paso; los pasos que son compuertas (los de `cadenas.toml` y `pasos_protegidos`), idénticos enteros; ningún `timeout-minutes` mayor a 355 |
| `poder`, en un job con poder | un job tiene poder si nombra un secreto, su token no es de solo lectura, llama a un workflow reutilizable, o un job con poder depende de él. Ahí no cambian `if`, `env`, `with` ni los pasos, y de un `run:` solo: en una línea que el lint de partida marcó como rota, la carpeta desde la que invoca al mismo script del repo (y el lint del resultado la resuelve a un checkout de este repo) o los campos de su `gh workflow run`; comillas; comentarios; `set -e…`; `mkdir -p`. Seis líneas por paso; ninguna se quita |
| `lineas` | en cualquier línea añadida: ni `curl`/`wget`/`nc`/`ssh`/`scp`, ni un host o URL que no estuviera en ese archivo, ni `base64`/`eval`, ni desactivar TLS, ni `\|\| true`/`continue-on-error`/`set +e`/`exit 0`, ni el nombre de un secreto o token, ni `env`/`printenv`/`set -x`/`GITHUB_ENV`, ni intérpretes en línea, ni instalar algo (salvo `pip install -r`), ni `sudo`, ni `git push`/`gh api`, ni módulos de red o `fetch(` |
| `contenido` | toca el workflow del paso de la huella o un archivo que ese paso invoca (resuelto con el lint), y nada más |
| `worker` | ni bindings, ni rutas, ni `workers_dev`, ni nombres de `env.`, ni `fetch` nuevos; los crons solo si coinciden entre `wrangler.jsonc` y el código |
| `lint`, `actionlint`, `shell`, `node` | sin hallazgos nuevos frente al árbol de partida: un arreglo que haga que un workflow lance otra corrida no entra (`lint_workflows.py`, `actionlint` 1.7.12 con `shellcheck`, `bash -n`, `node --check`) |
| `pytest`, `node-test`, `wrangler` | en verde |

Son estrictas a propósito: casi todos los jobs de los extractores tienen poder, así que un arreglo
legítimo que cambie un `if`, un permiso o agregue un paso ahí queda `agotada` con su motivo. Es el
residuo que este diseño acepta. Si agregas una regla, agrega su parche tramposo a
`tests/test_compuertas.py`; el arreglo real del exit 127 (`tests/fixtures/exit127/`) tiene que seguir
pasando.

**Ensayo**: `-f ensayo=true` hace todo y no publica ni anota nada (ni el intento); el parche, las
compuertas y los veredictos quedan como artefactos. Con `-f base=<sha>` parte de ese commit del repo
afectado, para reproducir una falla ya arreglada, y la huella se puede dar entera por inputs si no está
en `estado/`:

```sh
gh workflow run reparar-infra.yml -R OpenCodice/supervisor -f ensayo=true \
  -f huella=6bd0b49d205c -f base=99eff99 -f repo=fiscal-extractor \
  -f workflow=vigilar-leyes-estatales.yml -f job=reparar \
  -f paso='Reintentar las leyes con el extractor reparado' -f corrida=44246222455
```

**Estado**: una huella pasa por `abierta` → `en-reparacion` → `arreglo-publicado` → `resuelta`, o →
`revertido` → otra sesión o `agotada`. `reparar-infra.yml` anota en `acciones.jsonl`
`intento-iniciado`, `arreglo-publicado` (repo, commit, archivos, prueba), `descartado` (motivo),
`revertido` y `reversion-fallida`.

## Probar

```sh
.venv/bin/python -m pytest -q
actionlint
shellcheck scripts/agente/*.sh
.venv/bin/python supervisor/lint_workflows.py .
```

Las pruebas de las compuertas de forma se saltan si faltan `actionlint`, `shellcheck` o `node`.

Una corrida real de solo lectura: `python -m supervisor correr --modo informe --estado <carpeta temporal>`
con `GH_TOKEN`. No lances el modo `actuar` desde una máquina: ejecuta de verdad.
