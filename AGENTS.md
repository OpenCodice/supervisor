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
- **`estado/` lo escriben los workflows.** No se edita a mano ni se le hace commit desde una corrida
  local: para probar, `--estado <otra carpeta>`. `huellas.jsonl` es una foto (una línea por huella, la
  vigente) y, como `latido.json`, es solo del reconciliador; `acciones.jsonl` es la bitácora, a la que
  `reparar-infra.yml` solo agrega líneas (por eso `.gitattributes` une las líneas de los dos al rebasar,
  y solo en ese archivo). En cada vuelta `Estado.compactar` recorta lo que ya nadie lee: las acciones
  anteriores a `[estado].conservar_dias` (30), las huellas resueltas que no son la falla de un paso y
  no dejaron rastro (una cita del horario, una petición: de inmediato) y las demás resueltas pasada la
  ventana, salvo que la bitácora todavía guarde algo de su reparación. El recorte se aplica al leer y
  se escribe al guardar, así que una línea vieja que un merge devuelva al archivo no llega a ninguna
  decisión. `cadenas.validar` no deja que la ventana sea menor que las que la política cuenta en la
  bitácora. El historial de git conserva todo.
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
  (`horario-<hora>`) mientras su corrida no aparece, así que no se lanza dos veces; con la corrida a la
  vista se resuelve y sale del estado (su lanzamiento queda en la bitácora). Los días van por nombre (`lun`…`dom`): un número
  significa otra cosa en cada cron. Un workflow con `espera_libre` se lanza por horario solo cuando los
  workflows que nombra no tienen corridas vivas ni PR automático abierto, y su cita no caduca hasta la
  siguiente (la ronda de expansión con la cadena de leyes estatales); las rondas que se piden no esperan.
  Su lanzamiento por cadencia espera igual (lleva el mismo motivo en `extra["ocupada"]`). También cuenta
  como ocupada la cadena que el propio supervisor acaba de lanzar y la foto todavía no trae: en esta
  misma vuelta (por eso lo que I1 lanza de estos workflows se decide al final, después de todo lo demás)
  o dentro de `gracia_lanzamiento_min` sin que su corrida aparezca
  (`politica._ocupada_fuera_de_la_foto`).
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
- **I10.** `GET /salud` de cada `[[workers]]`, con el token de su `token_env` (`supervisor.yml` pasa
  `CONGRESO_ARCHIVO_TOKEN` y `EGRESS_MX_TOKEN` si existen; sin el token, I10 sale `sin-observar` para
  ese Worker; si el Worker lo rechaza, es un hallazgo de I13). Lo que contesta se lee según su `forma`
  (`observar.salud_de`): `cola` (`congreso-archivo`), por sitio, con hallazgo `determinista` y huella
  por sitio si tiene pendientes cuya más antigua pasa de `worker_pendiente_max_h` y ningún éxito en
  ese tiempo; `salida` (`egress-mx`), `{"ok": true, "pais": "MX", "colo": …}`, con hallazgo si no dice
  `ok` o si `pais` no es el declarado (un Worker que dejó de salir desde México no sirve aunque
  conteste). Con el token y sin un `/salud` legible (404, 5xx, sin red, otra forma) el hallazgo es
  `<worker>/salud`. Estos tres hallazgos del Worker entero llevan tolerancia: la política
  (`politica._worker`) espera `worker_sin_salud_max_h` (6) seguidas, contadas desde que la huella se
  abrió, antes de dejar constancia en un issue; si entre tanto contesta bien, la huella se resuelve
  sin rastro y la cuenta empieza de cero. Así un despliegue, o un `/salud` que la versión desplegada
  todavía no tiene (404), no abre un issue; mientras espera, el latido dice `rota`, no `ok`.
- **I13.** Las credenciales de `[[credenciales]]`. La del propio supervisor (`uso = "github"`): `Cliente`
  guarda la cabecera `github-authentication-token-expiration` de la primera respuesta que la API acepta
  (ninguna llamada de más; sin cabecera, el token no vence) y `tomar_foto` la deja en
  `foto["credenciales"]["github"]` junto con lo que la API le negó: lecturas de esta vuelta y, de
  `acciones.jsonl`, la última acción de cada clase sobre cada repo si fue un 401/403
  (`invariantes.rechazos_de_acciones`; `rechazo_vigente_h`). Hallazgos, uno por credencial y problema
  (`SUPERVISOR_TOKEN`, `SUPERVISOR_TOKEN/permisos`, `SUPERVISOR_TOKEN/ausente`,
  `CONGRESO_ARCHIVO_TOKEN@congreso-archivo`, `EGRESS_MX_TOKEN@egress-mx`), de clase `credencial`: solo constancia en un issue
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

El issue de una huella dice quién la tiene, con una etiqueta de estado además de `supervisor`:
`en-reparacion` (hay una sesión del agente), `arreglo-en-prueba` (publicado, se espera su corrida de
prueba), `esperando-reintento` (un intento se descartó o se revirtió, o la huella revivió),
`agotada` (no hay más intentos automáticos por ahora) y `necesita-persona` (clase `credencial`: lo
único que el sistema no hace solo). Cada intento descartado deja además un comentario con su motivo
y con lo que sigue. Arriba del cuerpo, el bloque «En qué va» lleva la situación (en progreso, en
espera, detenida), el motivo de la última decisión, hasta cuándo se espera el siguiente paso y los
intentos del agente gastados; el reconciliador lo reescribe solo cuando cambia. Todo esto es para quien lee: el reconciliador trabaja desde `estado/`, no
desde los issues, y nada se asigna a nadie.

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
3. `compuertas` (sin ningún secreto; el token del job solo lee): la entrega son datos, un tar con lo que cambió. Se pone sobre un
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

**Topes**: 2 intentos por huella (caducan a los 28 días, o empiezan de cero al revivir), 3 sesiones al
día, una sola `reparar-infra` a la vez. Una huella fuera del alcance (este repo, las webs, los repos de datos) queda `agotada` sin
gastar sesión. A este repo no llega por una huella: tiene su propio modo (ver «Reparar al propio
supervisor»).

**Revivir** (`politica._agotar_agente`). Una huella que agotó al agente (sus intentos, tres sesiones
que no llegaron a empezar, o el agente la devolvió) no queda así para siempre: mientras siga
apareciendo, vuelve a él con los intentos en cero cuando cambia algo que hace plausible otro resultado
o, si no, a los `revivir_dias` (7). Al agotarse guarda en `agotamiento` cuándo fue y dos versiones que
trae la foto (`foto["versiones"]`, de `observar._versiones`): la del lado del supervisor (un resumen de
`compuertas.py`, `reparar.py`, `lint_workflows.py`, `scripts/agente/` y `[reparacion]`, del árbol desde
el que corre la vuelta) y la del repo afectado (los sha, en main, del workflow de la huella y de los
archivos que el agente puede tocar ahí que no son workflows; una lectura por carpeta, y solo mientras
haya huellas agotadas). Solo cuenta como cambio lo que se vio las dos veces. La acción `revivir` deja
una línea en la bitácora y un comentario en el issue que la huella ya tiene (no abre otro); pone
`revivida` (desde ahí cuenta `intentos_de`) y suma `revividas`, con tope `revividas_por_huella` (3). No
revive una huella cuyo arreglo no se pudo revertir (sigue en main), ni una fuera del alcance. Los
intentos siguen caducando a los 28 días, como antes.

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
| `poder`, en un job con poder | un job tiene poder si nombra un secreto, su token no es de solo lectura, llama a un workflow reutilizable, o un job con poder depende de él. Ahí no cambian `env`, `with` ni los pasos, y de un `run:` solo: en una línea que el lint de partida marcó como rota, la carpeta desde la que invoca al mismo script del repo (y el lint del resultado la resuelve a un checkout de este repo) o los campos de su `gh workflow run`; comillas; comentarios; `set -e…`; `mkdir -p`. Seis líneas por paso; ninguna se quita. Un `if:` de paso solo se estrecha (ver abajo); el de un job no cambia |
| `lineas` | en cualquier línea añadida: ni `curl`/`wget`/`nc`/`ssh`/`scp`, ni un host o URL que no estuviera en ese archivo, ni `base64`/`eval`, ni desactivar TLS, ni `\|\| true`/`continue-on-error`/`set +e`/`exit 0`, ni el nombre de un secreto o token, ni `env`/`printenv`/`set -x`/`GITHUB_ENV`, ni intérpretes en línea, ni instalar algo (salvo `pip install -r`), ni `sudo`, ni `git push`/`gh api`, ni módulos de red o `fetch(` |
| `contenido` | toca el workflow del paso de la huella o un archivo que ese paso invoca (resuelto con el lint), y nada más |
| `worker` | ni bindings, ni rutas, ni `workers_dev`, ni nombres de `env.`, ni `fetch` nuevos; los crons solo si coinciden entre `wrangler.jsonc` y el código |
| `lint`, `actionlint`, `shell`, `node` | sin hallazgos nuevos frente al árbol de partida: un arreglo que haga que un workflow lance otra corrida no entra (`lint_workflows.py`, `actionlint` 1.7.12 con `shellcheck`, `bash -n`, `node --check`) |
| `pytest`, `node-test`, `wrangler` | en verde |

**Estrechar un `if:`** (`compuertas._if_mas_estricto`). En un job con poder, el `if:` del **paso de la
huella** (el que falló; el de cualquier otro paso no cambia), si no es compuerta, puede cambiar solo a
`<guarda> && <viejo>` o `<viejo> && <guarda>`: la condición anterior letra por letra (se admiten los
`${{ }}` exteriores y envolverla en paréntesis, obligatorios si trae un `||` fuera de paréntesis), y un paso
sin `if:` puede ganar la guarda sola. Gramática de la guarda:

```
guarda      = comparacion { "&&" comparacion }
comparacion = referencia ("==" | "!=") literal
referencia  = "steps." id ".outputs." nombre | "steps." id ".outcome" | "steps." id ".conclusion"
            | "needs." job ".result" | "needs." job ".outputs." nombre | "inputs." nombre
literal     = "'" { letra | dígito | "_" | " " | "." | ":" | "/" | "-" } "'" | "true" | "false"
```

El `id` es el de un paso anterior del mismo job, el job está en su `needs` y el input está declarado.
Sin funciones (`always()` quitaría el `success()` implícito), sin `||`, sin `!`, sin paréntesis y sin
`env.`/`secrets.`/`github.`, el resultado es `viejo && algo`: el paso corre en un subconjunto de los
casos en que corría. `max_if_con_poder` (1) queda como interruptor: en 0 no se admite ninguno. Lo que
no cubre: quien consume una salida del paso estrechado la recibe nula, y una condición que el nulo
satisface (`== ''`, `== '0'`, `!= 'x'`) sigue corriendo sin ella. Por eso solo vale en el paso de la
huella: el que deja de correr es el que ya estaba roto, no otro que hacía su trabajo. El juez tiene un
criterio propio para ese parche (la guarda tiene que decir la causa, y el paso no puede ser el que
detectaba o reportaba el problema) y la corrida de prueba revierte el arreglo si el paso no pasa.
Caso real: `tests/fixtures/if_estricto/` (huella `4516547eae19`).

Son estrictas a propósito: casi todos los jobs de los extractores tienen poder, así que un arreglo
legítimo que ahí ensanche o reescriba un `if`, estreche el de otro paso, cambie un permiso o agregue un
paso queda `agotada` con su motivo. Es el residuo que este diseño acepta. Si agregas una regla, agrega su
parche tramposo a `tests/test_compuertas.py`; los arreglos reales del exit 127
(`tests/fixtures/exit127/`) y de la guarda (`tests/fixtures/if_estricto/`) tienen que seguir pasando.

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
`revertido` → otra sesión o `agotada` (→ `abierta` otra vez si revive). `reparar-infra.yml` anota en `acciones.jsonl`
`intento-iniciado`, `arreglo-publicado` (repo, commit, archivos, prueba), `descartado` (motivo),
`revertido` y `reversion-fallida`.

## Reparar al propio supervisor

Si `supervisor.yml` truena (un error en este código o en su workflow), el reconciliador no corre y no
puede observarse: nadie más lanza nada. Lo detecta el Worker `scheduler`, que es quien lo lanza: cuando
las últimas 3 corridas terminadas de `supervisor.yml` fallaron, lanza

```
reparar-infra.yml  huella=supervisor  repo=supervisor  workflow=supervisor.yml
                   corrida=<id de la última corrida fallida>  origen=scheduler
```

`huella=supervisor` (`reparar.PROPIA`) es el modo. Mismo flujo y mismos jobs que arriba, con estas
diferencias (todas porque, roto, el supervisor no puede vigilar nada ni leer o escribir `estado/`):

- **`preparar` no lee `estado/`.** La huella (job, paso, firma) sale de la corrida indicada, por la API
  y con el token del job (el del supervisor puede ser lo que falla): `reparar.registro_propio`. Sin
  gastar agente y dejando constancia, termina bien (`SinTrabajo`, salida `seguir=false`) si
  `supervisor.yml` ya volvió a correr bien, si ya hubo `max_por_dia` (2) corridas de este modo en 24 h
  (se cuentan por el título `huella=[supervisor]` de las corridas de `reparar-infra.yml`; los ensayos no
  cuentan; el Worker limita además por su lado), o si la falla es de una credencial o pasajera. No se
  anota `intento-iniciado`.
- **Quién juzga.** Las compuertas, el encargo y lo que publica y revierte salen del código de main de
  esta corrida, nunca del árbol que entrega el agente. Si el código de main ni siquiera se deja
  importar (eso mismo puede ser la falla), el paso «Código con el que se juzga» toma el del commit de
  la última corrida buena de `supervisor.yml`, comprobando que es un antepasado de main, y todos los
  jobs siguientes hacen checkout de ese commit (`needs.preparar.outputs.codigo`).
- **Cerco** (`[reparacion.propio]`; `compuertas._alcance_propio`): puede modificar `supervisor/*.py`,
  `.github/workflows/supervisor.yml` y `cadenas.toml`, y crear `tests/test_<tema>.py` y datos en
  `tests/fixtures/`. No puede tocar lo que juzga un arreglo: `supervisor/compuertas.py`, `reparar.py`,
  `lint_workflows.py`, `rutas.py`, `cadenas.py`, `reparar-infra.yml`, `tests.yml`, `scripts/**`,
  `estado/**`. **Ninguna prueba ni dato de prueba que ya exista cambia**, ni una línea ni agregándole
  al final (una función con el mismo nombre tapa a la anterior, y un `conftest.py` cambia a todas);
  tampoco se crea un `conftest.py` ni un `.py` que no sea `tests/test_<tema>.py`. Nada se borra. Mismo
  tope de archivos y líneas.
- **`cadenas.toml`** (`compuertas._declarado`): leído como datos antes y después, `secciones_fijas`
  (todas las tablas menos `workflows`) quedan idénticas, no se agrega ni se quita ninguna tabla, y el
  resultado tiene que cargar con el validador de quien juzga. Apagar un invariante, subir un tope o
  cambiar a dónde se manda un token no es un arreglo.
- **`supervisor.yml`**: las mismas reglas de `poder` que cualquier workflow. Su único job tiene poder,
  así que en la práctica solo admite comillas, comentarios, `set -e…` y `mkdir -p`.
- **Líneas** (`compuertas._lineas_propias`, además de la lista de siempre): una línea nueva de
  `supervisor/*.py` no importa nada (`import`, `from … import`), no nombra tokens, secretos,
  credenciales ni cabeceras, no lee el entorno, no usa reflexión (`getattr`, `vars`, `__x__`) y no
  agrega escrituras ni peticiones propias (`.post(`, `.patch(`, `.delete(`, `.pedir(`, `subprocess`).
  Ese código corre cada 15 minutos con `SUPERVISOR_TOKEN`.
- **Lo que ejecuta el arreglo** (`compuertas.dinamicas_propio`; con otro usuario, sin secretos): (1) la
  suite tal como está en main, en una copia **sin nada de lo que el arreglo agregó**, para que una
  prueba nueva no pueda cambiar lo que miden las viejas; (2) sus pruebas nuevas, aparte; (3) **una
  vuelta real en modo informe** con el arreglo, sobre una copia de `estado/`, que tiene que terminar
  bien. Para esa vuelta, y solo para ella, recibe el token del job `compuertas`, que solo lee
  (`actions: read`, `contents: read`): el modo informe solo hace GET y escribe en la carpeta de
  `--estado`. Lo que ese token no alcance a ver sale `sin-observar`, no falla.
- **Después de publicar** (`reparar.publicar_propio`): el commit va a main de este repo con la autoría
  de siempre, se lanza `supervisor.yml` (`origen=reparar-infra`) y el job **`verificar`** espera la
  primera corrida terminada de `supervisor.yml` que ya trae el commit (`reparar.prueba_propia`; una
  cancelada no cuenta). Si termina bien, cierra la constancia. Si falla, o ninguna termina en
  `verificar_max_min` (40), hace `git revert` del commit (solo de uno con la autoría y el título de
  este flujo). No pasa por `acciones.jsonl` ni por `politica._vigilar`.
- **Constancia**: un solo issue `[supervisor] El supervisor no corre: reparación automática`, con un
  comentario por cada final (sin reparación, descartado, publicado, resuelto, revertido, no se pudo
  revertir); se cierra al resolverse.

Un ensayo sirve igual: `-f huella=supervisor -f ensayo=true -f repo=supervisor -f workflow=supervisor.yml
-f corrida=<id>` (y `-f base=<sha>` para reproducir una falla ya arreglada) hace todo y no publica.

Lo que este modo no cubre: una falla que solo aparece al actuar (la vuelta de prueba es en modo
informe; ahí la red es el revert), un arreglo que necesite tocar lo prohibido o un `import` nuevo, y
una falla de `reparar-infra.yml` mismo (I11 deja constancia; no se repara a sí mismo).

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
