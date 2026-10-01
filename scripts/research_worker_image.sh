#!/usr/bin/env bash
# Prepara el aislamiento con egress filtrado del worker de research (TASK-2026-00062).
#
# Hace tres cosas, todas idempotentes:
#   1. Construye la imagen del worker (iptables + CA; el CLI se monta en solo lectura).
#   2. Crea la red dedicada del worker (no la del host) con una subred conocida.
#   3. Imprime la regla de UFW que hay que anadir para que el worker alcance el broker
#      en el puerto del servicio; **no la aplica** (cambiar el cortafuegos del host es
#      una decision de despliegue, como la regla de ERPNext).
#
# Uso:
#   scripts/research_worker_image.sh            # construye imagen + red
#   scripts/research_worker_image.sh --ufw      # ademas aplica la regla (requiere sudo)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="${RESEARCH_WORKER_IMAGE:-openceo-research-worker:latest}"
NETWORK="${RESEARCH_WORKER_NETWORK:-openceo-research-egress}"
SUBNET="${RESEARCH_WORKER_SUBNET:-172.31.240.0/24}"
GATEWAY="${RESEARCH_WORKER_NETWORK_GATEWAY:-172.31.240.1}"
PORT="${OPENCEO_PORT:-18765}"

docker build -q -t "$IMAGE" -f "$ROOT_DIR/openceo-core/research/runtimes/worker.Dockerfile" "$ROOT_DIR" >/dev/null
echo "imagen $IMAGE lista"

if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
  docker network create --subnet "$SUBNET" --gateway "$GATEWAY" "$NETWORK" >/dev/null
  echo "red $NETWORK creada ($SUBNET, gateway $GATEWAY)"
else
  echo "red $NETWORK ya existe"
fi

RULE="allow from $SUBNET to any port $PORT proto tcp"
if [[ "${1:-}" == "--ufw" ]]; then
  sudo ufw allow from "$SUBNET" to any port "$PORT" proto tcp
  echo "regla ufw aplicada"
else
  echo "para que el worker alcance el broker, aplica: sudo ufw $RULE"
fi

cat <<EOF

Ajustes en openceo-core/.env para activar el filtrado:
  RESEARCH_ISOLATION_BACKEND=container
  RESEARCH_WORKER_NETWORK=$NETWORK
  RESEARCH_WORKER_NETWORK_GATEWAY=$GATEWAY
  RESEARCH_WORKER_EGRESS_HOSTS=opencode.ai
  RESEARCH_WORKER_RUN_AS=$(id -u):$(id -g)
  RESEARCH_BROKER_ALLOWED_IPS=127.0.0.1/32,::1/128,$SUBNET

Sin RESEARCH_WORKER_NETWORK_GATEWAY el backend no puede filtrar y el plan lo declara
(egress_restricted=false) en vez de fingir la frontera.
EOF
