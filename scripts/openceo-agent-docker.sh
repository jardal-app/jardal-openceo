#!/usr/bin/env bash
# Rootless Docker daemon for host-mode builders (TASK-2026-00985).
# Runs as its own user so builder containers reach neither the production
# daemon nor the Owner's home; plan_host_agent points DOCKER_HOST at it.
# Usage: scripts/openceo-agent-docker.sh install|status (both use sudo)
set -euo pipefail

AGENT_USER="openceo-docker"
OWNER_USER="${SUDO_USER:-$(id -un)}"

as_agent() {
  local uid
  uid="$(id -u "$AGENT_USER")"
  sudo -u "$AGENT_USER" -H env XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    PATH=/usr/bin:/usr/sbin:/bin:/sbin "$@"
}

install() {
  command -v newuidmap >/dev/null || sudo apt-get install -y uidmap
  id "$AGENT_USER" >/dev/null 2>&1 || sudo useradd --create-home \
    --shell /usr/sbin/nologin --comment "OpenCEO agent Docker (rootless)" "$AGENT_USER"
  sudo loginctl enable-linger "$AGENT_USER"
  local home uid
  home="$(getent passwd "$AGENT_USER" | cut -d: -f6)"
  uid="$(id -u "$AGENT_USER")"
  for _ in $(seq 1 50); do [[ -d "/run/user/$uid" ]] && break; sleep 0.2; done
  if [[ ! -f "$home/.config/systemd/user/docker.service" ]]; then
    # --force: the rootful production daemon is running on purpose.
    (cd /tmp && as_agent dockerd-rootless-setuptool.sh install --force)
  fi
  sudo -u "$AGENT_USER" mkdir -p "$home/.config/docker" \
    "$home/.config/systemd/user/docker.service.d"
  printf '{\n  "ip": "127.0.0.1"\n}\n' \
    | sudo -u "$AGENT_USER" tee "$home/.config/docker/daemon.json" >/dev/null
  # The runtime dir and socket are recreated on every start: reapply ACLs.
  sudo -u "$AGENT_USER" tee \
    "$home/.config/systemd/user/docker.service.d/openceo-develop-acl.conf" >/dev/null <<EOF
[Service]
ExecStartPost=/bin/sh -c 'for i in \$(seq 1 100); do [ -S "%t/docker.sock" ] && break; sleep 0.1; done; setfacl -m u:${OWNER_USER}:x "%t" && setfacl -m u:${OWNER_USER}:rw "%t/docker.sock"'
EOF
  as_agent systemctl --user daemon-reload
  as_agent systemctl --user enable docker.service >/dev/null
  as_agent systemctl --user restart docker.service
  status
}

status() {
  local uid sock
  uid="$(id -u "$AGENT_USER")"
  sock="/run/user/$uid/docker.sock"
  echo "service: $(as_agent systemctl --user is-active docker.service)"
  echo "socket:  $sock"
  getfacl -p "$sock" 2>/dev/null | grep "^user:${OWNER_USER}:" || echo "no ACL for ${OWNER_USER}"
  docker -H "unix://$sock" info --format \
    'daemon: {{.ServerVersion}} {{range .SecurityOptions}}{{.}} {{end}}'
}

case "${1:-}" in
  install) install ;;
  status) status ;;
  *) echo "Usage: $0 install|status"; exit 1 ;;
esac
