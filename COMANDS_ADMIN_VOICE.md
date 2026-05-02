# COMANDS_ADMIN

Comandos para dejar abierto el backend de voz self-hosted con Traefik + LiveKit.

## UFW

Si `80/tcp` y `443/tcp` ya existen en tu firewall, no los repitas.

```bash
sudo ufw allow 80/tcp comment 'Traefik HTTP'
sudo ufw allow 443/tcp comment 'Traefik HTTPS + WSS'
sudo ufw allow 7881/tcp comment 'LiveKit ICE TCP fallback'
sudo ufw allow 7882/udp comment 'LiveKit ICE UDP mux'
sudo ufw reload
sudo ufw status numbered
```

## Hetzner Firewall

Reglas de entrada recomendadas:

- `TCP 80` desde `0.0.0.0/0` y `::/0`
- `TCP 443` desde `0.0.0.0/0` y `::/0`
- `TCP 7881` desde `0.0.0.0/0` y `::/0`
- `UDP 7882` desde `0.0.0.0/0` y `::/0`

No hace falta abrir un puerto extra para el token backend; va por `443`.

## Verificación rápida

```bash
sudo ss -ltnup | rg ':80|:443|:7881|:7882'
curl -k https://voice.openceo.jardal.app/health
curl -k -X POST https://voice.openceo.jardal.app/api/voice/token \
  -H 'content-type: application/json' \
  -d '{"user_id":"jorge","room":"openceo-voice"}'
```
