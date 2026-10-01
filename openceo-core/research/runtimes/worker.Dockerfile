# Imagen del worker de research (TASK-2026-00062).
#
# Por que existe: bwrap acredita aislamiento de ficheros, pero **no** puede filtrar el
# egress por destino. El unico backend que si puede es un contenedor con su propia red,
# donde el propio worker aplica una lista blanca de salida (`--cap-add=NET_ADMIN`, reglas
# dentro de su espacio de red; el cortafuegos del host no se toca).
#
# La imagen solo aporta lo que el arranque necesita (`iptables`, CA y `wget`); el motor
# (CLI de OpenCode) se monta en solo lectura desde el host para no duplicar 200 MB ni
# congelar una version distinta de la fijada.
#
# Construir:  scripts/research_worker_image.sh
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends iptables ca-certificates wget util-linux \
 && rm -rf /var/lib/apt/lists/*

# El worker corre como root **solo** para aplicar las reglas de salida; el CLI se ejecuta
# despues con `setpriv` y el usuario del host, asi el estado del job no queda con dueño
# root. No hay usuario propio en la imagen porque el UID debe coincidir con el del host.
WORKDIR /worker
