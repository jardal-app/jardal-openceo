# systemd del core (TASK-2026-00370, diseño v2 §16.4)

Unidad de **usuario** con linger (corre como `develop`, sin sudo;
`loginctl show-user develop` ya dice `Linger=yes`). Decisión del agente,
reversible por el Owner: evita la unidad de sistema con `User=develop`
y mantiene las credenciales de `~/.codex` y `~/.claude`.

## Archivos

- `openceo-core-host.service`: `Restart=on-failure`, `RestartSec=10`,
  `StartLimitIntervalSec=600`, `StartLimitBurst=5`,
  `OnFailure=openceo-alert.service`, `ExecStopPost=record-exit.sh`,
  `RestartPreventExitStatus=3` (candado tomado no reintenta).
- `openceo-alert.service`: oneshot que llama a
  `scripts/openceo-alert.sh` (spool `.runtime/alerts.log`, syslog y
  `OPENCEO_ALERT_WEBHOOK` opcional; nunca toca el core).
- `scripts/record-exit.sh`: escribe `.runtime/last-exit.json`
  (`service_result`, `exit_code`, `exit_status`, fecha; umask 077).

## Migrar (sin dos instancias)

```bash
scripts/openceo-systemd-migrate.sh install   # para procesos sueltos, enlaza, enable --now, verifica
scripts/openceo-systemd-migrate.sh status    # estado de la unidad
scripts/openceo-systemd-migrate.sh uninstall # disable --now y retira los enlaces (el core queda parado)
```

`install` enlaza (symlink) los ficheros del repo en
`~/.config/systemd/user/` para que el repo sea la fuente de verdad.
Antes verifica `Linger` y falla con el comando exacto si falta.
La unidad es la única forma de correr el core (TASK-2026-00510): no hay
arranque por script. `scripts/openceo-core-host.sh start|stop|restart|status|logs`
delega siempre en `systemctl --user`/`journalctl` y falla con el comando de
instalación si la unidad no está instalada; `foreground` es el `ExecStart`
de la unidad. Tras `uninstall` el core queda parado hasta reinstalarla.

## Webhook de alerta

```bash
systemctl --user edit openceo-alert.service   # [Service] Environment=OPENCEO_ALERT_WEBHOOK=https://...
```
