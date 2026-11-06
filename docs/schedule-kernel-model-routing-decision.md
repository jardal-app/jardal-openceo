# Decisión model_routing: excepción externa, datos no ejecución (ticket #583)

> TEMPORAL. El ticket ARCH lo transcribe a `docs/ARCHITECTURE_REFERENCE_2026.md` y lo borra.
> Cierra la aceptación 5 del objetivo schedule-kernel
> (§7 de `docs/TASK-2026-00441-schedule-kernel-discovery.md`).
> Hechos verificados en HEAD `d8ce93d` el 2026-11-06.

## Decisión

**Excepción externa (datos, no ejecución).** Se confirma el veredicto del §7:
el cron de `model_routing` queda fuera del kernel, no se toca y no genera
ticket de migración.

Razones (las 4 del §7, re-verificadas en HEAD):

1. **Frontera de proceso.** El refresco corre como proceso aparte lanzado por
   cron (hecho 1) y no debe depender de que el core esté vivo; al revés, el
   core consume ficheros aunque el refresco falle (hechos 7–8: degradado y
   fail-open).
2. **No es un «timer propio» en el sentido de TASK-2026-00440**, que apunta a
   bucles `asyncio` aislados dentro del core. Es un lote de calendario fijo
   sin intención en Mongo ni despertar por CRUD (hechos 1, 9).
3. **Ya está declarado externo** en ARCHITECTURE_REFERENCE (hecho 6).
4. **El lado consumidor ya es reactivo y por fichero** (hechos 7–8).
5. **Migrar no compra nada y cuesta red + cron en el core** (§Por qué no se
   migra).

## Hechos verificados (HEAD `d8ce93d`)

1. **Dos líneas de crontab de usuario**, generadas por
   `model_routing/schedule.py:41-57` (`render_crontab_block`):
   `15 2 * * * … update --benchmarks` (`:53`) y
   `20 */6 * * * … update --providers` (`:54`). Cada línea lanza
   `{python} {cli} update …` como **proceso aparte** con su log en el state
   dir. `schedule.py` está **idéntico desde `cf405f5`**
   (`git diff cf405f5..HEAD` vacío en todo `openceo-core/model_routing/`).
2. **El CLI define ese `update`** (`model_routing/cli.py:107-110`):
   `--providers` = Cron B 6 h, `--benchmarks` = Cron A 24 h;
   `_run_update` (`:422-427`) llama a `run_update`.
3. **Exclusión mutua en proceso**, no en cron: `run_update`
   (`model_routing/refresh.py:516-562`) toma `flock LOCK_EX | LOCK_NB`
   (`:533`) sobre `update.lock` y lo suelta (`:562`); el docstring
   (`schedule.py:44-46`) y el comentario (`:52`) prohíben el wrapper externo
   porque se autobloquearía.
4. **Qué escribe y dónde.** `refresh.py:1-9`: Cron B publica catálogo +
   rankings, Cron A adjunta benchmarks; un refresco fallido deja las
   instantáneas anteriores byte-idénticas (T12). Instantáneas
   `catalog.json`/`rankings.json` en `~/.config/free-router/`
   (`paths.py:51-52,71-73,93-98`); lock, log y alertas en
   `~/.local/state/free-router/` (`paths.py:59-60,76-77`).
   Precisión sobre el §7: el §7 agrupaba todo bajo `~/.config/`; el lock/log
   van al state dir.
5. **La activación en el host pertenece a TASK-2026-00299**
   (`schedule.py:6-7`). Instalación no verificada: `crontab -l` devuelve
   `crontabs/develop/: fopen: Permission denied`, igual que en el
   descubrimiento. No afecta al veredicto.
6. **ARCH ya lo declara externo** (`docs/ARCHITECTURE_REFERENCE_2026.md:1172`):
   «Daily benchmark research/update remains external, with no new periodic
   polling». Texto idéntico en `cf405f5` (`:1169`); el diff hasta HEAD no toca
   esa línea, solo se desplazó por ediciones ajenas.
7. **Consumidor revisores, una lectura por ronda.**
   `review_wiring.load_round_snapshot` (`:266-304`) lee el rankings congelado;
   su llamador es el ensamblado «Pool plus routing for one dev-review round»
   (`:1295,1313`). Sin instantánea usable, orden degradada sobre el catálogo
   vivo o `RoundBlocked` ruidoso: reactivo por fichero, sin wake del
   productor.
8. **Consumidor CEO, piggyback fail-open.** `_router_reasons_for`
   (`ceo/loop.py:130-166`) lee las alertas con `list_alerts` (`:146`):
   «one file read piggybacked on an existing review, never a new poll»
   (`:133-136`); se mezcla en la revisión programada (`:473`) y en la
   reactiva (`:490`). Nota: las líneas se movieron respecto al §7
   (`:329-365` en `cf405f5`) por la migración del CEO al kernel (#575:
   función compartida por loop y agenda); semántica intacta.
9. **El contrato del kernel no le aplica.** `schedule_kernel/agenda.py`
   exige `next_due` («One indexed query; the nearest CLAIMABLE due»),
   `claim_due` atómico y `wake` en proceso; `model_routing` no tiene
   intención en Mongo ni despertar por CRUD, solo calendario fijo 6 h/24 h.
   El diseño del kernel excluyó a propósito el lenguaje cron (§5.6).
10. **Alcance del kernel: execution wake-ups.** ARCH §Internal Scheduler
    (`:1107`) dice que «one kernel controls all execution wake-ups» y que
    dispatcher, Horizon, research y CEO convergen en él (`:1154`). El
    refresco de `model_routing` es un lote de datos, no un wake-up de
    ejecución.

## Línea para ARCH

Colocación: §Internal Scheduler, al final del párrafo de convergencia
(tras «…converge into that single global kernel; no new isolated timer
(TASK-2026-00440).»). Texto exacto a transcribir (inglés, como ARCH):

```text
The free-router refresh (`openceo-core/model_routing/`, user crontab `15 2 * * *` benchmarks / `20 */6 * * *` providers) stays external: a fixed-calendar data batch publishing file snapshots, not an execution wake-up, so it never enters the kernel; consumers read the snapshots reactively (reviewers once per round, CEO alerts piggybacked on existing reviews).
```

## Por qué no se migra (esquema descartado, sin seguimiento)

Migrar significaría: una agenda que despierte cada 6 h/24 h por calendario
(el kernel no tiene lenguaje cron, §5.6), ejecute fetches de red de minutos
(catálogos de proveedores, Artificial Analysis, LMArena) dentro de un carril
del core, y publique ficheros que hoy escriben un proceso aislado con su
propio flock. A cambio de acoplar la disponibilidad del refresco a la vida
del core y de añadir al kernel su primer trabajo por lotes de red, ningún
consumidor gana nada: ninguno necesita un wake, todos leen ficheros cuando
ya tienen trabajo. Se descarta sin ticket de seguimiento.

## Fuera de alcance

Transcripción ARCH, aprovisionamiento del Bearer de wake, cambios de código
o tests. Docs-only: no se corrió la suite ni modelos; solo lectura de código
y `git diff`/`crontab -l`.
