from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any

import httpx

from settings import Settings


@dataclass(frozen=True)
class ZoneMatch:
    zone_id: str
    zone_name: str


class CloudflareDNSManager:
    """Small shared DNS helper for coding agents and internal runtime use."""

    _BASE_URL = "https://api.cloudflare.com/client/v4"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token = settings.CLOUDFLARE_API_TOKEN.strip()
        self._account_id = settings.CLOUDFLARE_ACCOUNT_ID.strip()
        self._timeout = httpx.Timeout(20.0, connect=10.0)
        self._zones_cache: list[dict[str, Any]] | None = None

    def check_or_create(
        self,
        hostname: str,
    ) -> dict[str, Any]:
        fqdn = _normalize_hostname(hostname)
        token_error = self._token_error()
        if token_error is not None:
            return token_error

        zone = self._resolve_zone(fqdn)
        if zone is None:
            return {
                "ok": False,
                "status": "unknown_zone",
                "hostname": fqdn,
                "message": "El dominio base no está registrado en Cloudflare para este token.",
            }

        desired = self._build_desired_record(
            hostname=fqdn,
            zone_name=zone.zone_name,
        )
        existing_records = self._list_dns_records(zone.zone_id, fqdn)

        for record in existing_records:
            if self._record_matches(record, desired):
                return {
                    "ok": True,
                    "status": "exists",
                    "hostname": fqdn,
                    "zone": zone.zone_name,
                    "record_id": record["id"],
                    "record_type": record["type"],
                    "target": record["content"],
                    "proxied": record.get("proxied"),
                    "message": "El DNS ya existía. Puedes continuar con el deploy.",
                }

        if existing_records:
            return {
                "ok": False,
                "status": "conflict",
                "hostname": fqdn,
                "zone": zone.zone_name,
                "desired": desired,
                "existing_records": [
                    {
                        "id": record["id"],
                        "type": record["type"],
                        "target": record["content"],
                        "proxied": record.get("proxied"),
                    }
                    for record in existing_records
                ],
                "message": "El hostname ya existe con un record distinto. No continúes el deploy hasta corregirlo.",
            }

        payload = {
            "type": desired["type"],
            "name": fqdn,
            "content": desired["content"],
            "ttl": desired["ttl"],
        }
        if desired["type"] in {"A", "AAAA", "CNAME"}:
            payload["proxied"] = desired["proxied"]

        created = self._request(
            "POST",
            f"/zones/{zone.zone_id}/dns_records",
            json=payload,
        )
        record = created["result"]
        return {
            "ok": True,
            "status": "created",
            "hostname": fqdn,
            "zone": zone.zone_name,
            "record_id": record["id"],
            "record_type": record["type"],
            "target": record["content"],
            "proxied": record.get("proxied"),
            "message": "DNS creado. Puedes continuar con el deploy.",
        }

    def delete(
        self,
        hostname: str,
    ) -> dict[str, Any]:
        fqdn = _normalize_hostname(hostname)
        token_error = self._token_error()
        if token_error is not None:
            return token_error

        zone = self._resolve_zone(fqdn)
        if zone is None:
            return {
                "ok": False,
                "status": "unknown_zone",
                "hostname": fqdn,
                "message": "El dominio base no está registrado en Cloudflare para este token.",
            }

        existing_records = self._list_dns_records(zone.zone_id, fqdn)
        to_delete = [
            record for record in existing_records
            if self._record_matches(record, self._build_desired_record(hostname=fqdn, zone_name=zone.zone_name))
        ]
        deleted_ids: list[str] = []
        for record in to_delete:
            self._request("DELETE", f"/zones/{zone.zone_id}/dns_records/{record['id']}")
            deleted_ids.append(record["id"])

        return {
            "ok": True,
            "status": "deleted",
            "hostname": fqdn,
            "zone": zone.zone_name if zone else None,
            "deleted_count": len(deleted_ids),
            "deleted_ids": deleted_ids,
            "message": "Registros eliminados." if deleted_ids else "No había registros para eliminar.",
        }

    def _build_desired_record(
        self,
        hostname: str,
        zone_name: str,
    ) -> dict[str, Any]:
        _ = hostname
        _ = zone_name
        return {
            "type": "A",
            "content": self._default_target_ip(),
            "proxied": self._settings.CLOUDFLARE_DNS_DEFAULT_PROXIED,
            "ttl": self._settings.CLOUDFLARE_DNS_DEFAULT_TTL,
        }

    def _resolve_zone(self, hostname: str) -> ZoneMatch | None:
        zones = self._list_zones()
        matches: list[tuple[int, dict[str, Any]]] = []
        for zone in zones:
            zone_name = str(zone["name"]).strip().lower()
            if hostname == zone_name or hostname.endswith(f".{zone_name}"):
                matches.append((len(zone_name), zone))

        if not matches:
            return None

        _, best = max(matches, key=lambda item: item[0])
        return ZoneMatch(zone_id=str(best["id"]), zone_name=str(best["name"]).strip().lower())

    def _list_zones(self) -> list[dict[str, Any]]:
        if self._zones_cache is not None:
            return self._zones_cache

        params: dict[str, Any] = {"per_page": 100}
        if self._account_id:
            params["account.id"] = self._account_id
        payload = self._request("GET", "/zones", params=params)
        result = payload.get("result", [])
        if not isinstance(result, list):
            raise ValueError("Cloudflare no devolvió una lista de zonas.")
        self._zones_cache = result
        return result

    def _list_dns_records(self, zone_id: str, hostname: str) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            f"/zones/{zone_id}/dns_records",
            params={"name": hostname, "per_page": 100},
        )
        result = payload.get("result", [])
        if not isinstance(result, list):
            raise ValueError("Cloudflare no devolvió una lista de records DNS.")
        return result

    def _record_matches(self, record: dict[str, Any], desired: dict[str, Any]) -> bool:
        return (
            str(record.get("type", "")).upper() == desired["type"]
            and _normalize_content(str(record.get("content", ""))) == _normalize_content(desired["content"])
        )

    def _default_target_ip(self) -> str:
        value = self._settings.CLOUDFLARE_DNS_DEFAULT_TARGET.strip()
        address = ipaddress.ip_address(value)
        if address.version != 4:
            raise ValueError("CLOUDFLARE_DNS_DEFAULT_TARGET debe ser una IPv4 válida.")
        return value

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with httpx.Client(
            base_url=self._BASE_URL,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            timeout=self._timeout,
        ) as client:
            response = client.request(method, path, params=params, json=json)
            response.raise_for_status()
            payload = response.json()

        if not payload.get("success"):
            errors = payload.get("errors") or []
            message = "; ".join(str(item.get("message") or item) for item in errors) or "Cloudflare API error"
            raise ValueError(message)

        return payload

    def _token_error(self) -> dict[str, Any] | None:
        if self._token:
            return None
        return {
            "ok": False,
            "status": "missing_token",
            "message": "CLOUDFLARE_API_TOKEN no está configurado.",
        }


def _normalize_hostname(value: str) -> str:
    hostname = value.strip().lower().rstrip(".")
    if not hostname or "." not in hostname:
        raise ValueError("Debes enviar un hostname FQDN válido.")
    return hostname


def _normalize_content(value: str) -> str:
    return value.strip().lower().rstrip(".")
