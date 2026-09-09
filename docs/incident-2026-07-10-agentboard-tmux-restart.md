# Incident Report: Agentboard + Tmux Session Wipe

**Date**: 2026-07-10 06:01 UTC  
**Severity**: Medium (data loss: all tmux sessions destroyed)  
**Author**: Claude investigation via Jardal server  
**Host**: `jardal-server` (16 CPU, 32GB RAM)

---

## TL;DR

No hubo reinicio de VM. El servicio `agentboard` se reinició a las 06:01 UTC debido a un `unattended-upgrade` de `apache2-utils`. Como `KillMode=control-group` en el systemd unit, systemd mató al servidor tmux viejo (hijo del proceso agentboard). El nuevo agentboard arrancó con un servidor tmux limpio, perdiendo todas las sesiones.

---

## Timeline

| Hora (UTC) | Evento |
|-------------|--------|
| 06:01:08 | `unattended-upgrade` inicia actualización de `apache2-utils` |
| 06:01:25 | `dpkg` termina la instalación (trigger `man-db`) |
| 06:01:26 | Último log del agentboard viejo (PID `1173974`) |
| 06:01:27 | Systemd registra `ActiveExit` del servicio |
| 06:01:29 | Systemd ejecuta `Restart=always` → nuevo agentboard (PID `2884185`) |
| 06:01:29 | Nuevo tmux server (PID `2884269`), nuevo socket `/tmp/tmux-1002/default` |
| 06:01:29 | Agente detecta 62 sesiones huérfanas, mata ventanas `melany:@28` y `otb:@21` |

---

## Root Cause Analysis

### 1. Trigger: `unattended-upgrade`

```log
Start-Date: 2026-07-10  06:01:08
Commandline: /usr/bin/unattended-upgrade
Upgrade: apache2-utils:amd64 (2.4.58-1ubuntu8.13, 2.4.58-1ubuntu8.15)
End-Date: 2026-07-10  06:01:25
```

El unattended-upgrade disparó un `dpkg` trigger que causó que systemd reiniciara servicios dependientes o ejecutara `daemon-reload`. Esto provocó que el servicio `agentboard` se detuviera y rearrancara.

### 2. Kill Chain: `KillMode=control-group`

El systemd unit del agentboard:

```ini
# /etc/systemd/system/agentboard.service
[Service]
Type=simple
Restart=always
RestartSec=5
KillMode=control-group   # ← MATA TODOS los procesos del cgroup
SendSIGKILL=yes          # ← con SIGKILL
SendSIGHUP=no
MemoryMax=4G
MemoryHigh=3.5G
```

**Mecanismo**: `KillMode=control-group` (el default) significa que cuando systemd detiene el servicio — ya sea por restart, stop, o crash — envía `SIGKILL` a **todos** los procesos en el cgroup, no solo al proceso principal (`ExecStart`). El servidor tmux era un proceso hijo del agentboard, por lo que también fue eliminado.

### 3. Data Loss: Tmux socket destruction

- **Socket viejo**: destruido al morir el proceso tmux
- **Socket nuevo**: creado en `/tmp/tmux-1002/default` a las 06:01:29
- **Sesiones perdidas**: todas las sesiones del servidor tmux anterior (incluyendo `melany`, `otb`, y cualquier otra activa)
- **62 orphan records**: el nuevo agentboard detectó sesiones en su base de datos que ya no existían en tmux

### 4. What did NOT happen

| Hipótesis | Resultado |
|-----------|-----------|
| Reinicio de VM | ❌ Uptime: 170 días |
| OOM kill | ❌ Sin registros de OOM; memoria libre >2GB |
| Kernel panic / crash | ❌ Sin registros en dmesg |
| Error del proceso agentboard | ❌ Sin crash/error logs; estaba funcionando normalmente |
| Intervención manual | ❌ Sin sesiones SSH activas que hayan ejecutado `systemctl restart` |

---

## Evidence

### VM Uptime
```
12:31:51 up 170 days,  9:16,  9 users
```

### Systemd service state
```
ActiveExitTimestamp=Fri 2026-07-10 06:01:27 UTC
InactiveEnterTimestamp=Fri 2026-07-10 06:01:29 UTC
ActiveEnterTimestamp=Fri 2026-07-10 06:01:29 UTC
```

### Agentboard startup state (log del nuevo proceso)
```json
{
  "event": "startup_state",
  "activeSessionCount": 2,
  "windowCount": 1,
  "activeWindows": [
    {"sessionId": "5ffaa988", "name": "melany-2", "window": "melany:@28"},
    {"sessionId": "4d046fac", "name": "otb", "window": "otb:@21"}
  ],
  "tmuxWindows": [{"tmuxWindow": "agentboard:@0", "name": "bash"}]
}
```
→ Detectó 2 sesiones existentes pero no las reconoció como propias → las marcó huérfanas y las mató.

### New tmux socket
```
File: /tmp/tmux-1002/default
Modify: 2026-07-10 06:01:29  ← creado en el reinicio
```

---

## Recommendations

### 1. Cambiar `KillMode` a `process` (prioridad alta)

```ini
# /etc/systemd/system/agentboard.service
[Service]
KillMode=process
```

Esto evita que systemd mate procesos hijos (como el servidor tmux) cuando el servicio se reinicia. Solo el proceso principal recibe la señal.

```bash
sudo systemctl daemon-reload
sudo systemctl restart agentboard
```

### 2. Usar un socket tmux con nombre explícito

En lugar de usar el socket default (`/tmp/tmux-1002/default`), configurar agentboard para usar un socket con nombre fijo:

```
tmux -S /tmp/agentboard-tmux.sock new-session -d -s agentboard
```

Esto aísla el socket y facilita la recuperación.

### 3. Evaluar unattended-upgrades

Si los reinicios inesperados de servicios son inaceptables:

```bash
# Desactivar unattended-upgrades
sudo dpkg-reconfigure unattended-upgrades

# O configurar la ventana de mantenimiento en /etc/apt/apt.conf.d/50unattended-upgrades
```

### 4. Agregar un hook post-restart en agentboard

El código de agentboard podría detectar sesiones tmux preexistentes al arrancar y re-adoptarlas en lugar de marcarlas como huérfanas y matarlas. Esto haría el reinicio transparente.

---

## Related

- Systemd unit: `/etc/systemd/system/agentboard.service`
- Agentboard binary: `/usr/lib/node_modules/@gbasin/agentboard/node_modules/@gbasin/agentboard-linux-x64/bin/agentboard`
- Watched dirs: `/home/develop/.claude/projects`, `/home/develop/.codex/sessions`
- Tmux socket: `/tmp/tmux-1002/default`
