"""Cliente HTTP determinista hacia ERPNext. Cero logica LLM.

Vive en `tools/` y no en `agents/finance/` (donde lo ubicaba el contrato de Finance,
seccion 2) porque el ticket es transversal: lo crean PM, CEO, Horizon, las sesiones dev
y la voz, no solo Finance. `docs/ARCHITECTURE_REFERENCE_2026.md` reserva `tools/` para
las herramientas compartidas por varios agentes, que es este caso.

Decisiones deliberadas:

- **El cliente es async.** El unico camino de escritura sincrono es la creacion de un
  ticket, que ocurre dentro del turno de voz. Un `httpx.Client` sincrono ahi bloquearia
  el event loop entero del proceso, no solo esa peticion.
- **Timeouts cortos (connect 2 s, total 5 s por defecto).** El cliente de Notion usa 20 s;
  copiar ese valor colgaria el turno de voz 20 segundos con ERPNext caido.
- **Las excepciones distinguen tres casos**, porque el llamador tiene que reaccionar
  distinto a cada uno: `ERPNextUnavailable` (timeout, red, 5xx) se marca como operacion
  pendiente; `ERPNextValidationError` (4xx de datos) es un fallo duro; `ERPNextDuplicateError`
  (417) significa que la clave de idempotencia ya existia y hay que releer.
- **Solo se puede escribir en DocTypes de la allowlist.** El contrato pide que la superficie
  REST quede acotada; la allowlist lo garantiza en la capa mas baja, no solo en los tools.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

from settings import Settings

logger = logging.getLogger(__name__)

# DocTypes que este cliente puede tocar. Ampliarla es una decision explicita:
# el contrato de Finance pide que cada DocType nuevo se justifique por un flujo real.
# `Task Type` entra solo para lectura: el adapter valida contra el los tipos que existen
# antes de mandarlos, porque un Link invalido tumbaria la creacion del ticket.
# `GL Entry` NO entra a la allowlist a proposito: es el libro mayor que
# genera el submit del `Journal Entry`, y abrirlo ahi permitiria crearlo a
# mano. La lectura Fase A (TASK-2026-00044, `finance.get_scope_summary`)
# usa el lector dedicado `list_gl_entries` (solo GET), sin abrir escritura.
#
# `File` entra por TASK-2026-00079, y solo para renombrar o cambiar la privacidad de un
# adjunto ya existente: subir y borrar adjuntos van por los metodos oficiales de Frappe
# (`upload_file`, `frappe.desk.form.utils.remove_attach`), que no pasan por la allowlist.
# `Comment` NO entra: no se toca por `/api/resource`, solo por los metodos de la linea de
# tiempo, que comprueban permiso sobre el ticket.
ALLOWED_DOCTYPES: frozenset[str] = frozenset(
    {"Task", "Project", "Task Type", "Journal Entry", "Account", "Cost Center", "Company", "File"}
)

_DEFAULT_TIMEOUT_SECONDS = 5.0
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 2.0
_MAX_ATTEMPTS = 3
_INITIAL_RETRY_DELAY_SECONDS = 0.5


class ERPNextError(RuntimeError):
    """Error base del adapter ERPNext."""

    def __init__(self, message: str, *, status_code: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class ERPNextUnavailable(ERPNextError):
    """ERPNext no respondio o respondio con un fallo transitorio.

    Es el caso "operacion pendiente": el llamador debe registrar el ticket como
    pendiente de enlace, no como fallo de negocio.
    """


class ERPNextValidationError(ERPNextError):
    """ERPNext rechazo los datos. Fallo duro: reintentar no cambia el resultado."""


class ERPNextPermissionError(ERPNextValidationError):
    """ERPNext nego el acceso (401/403): falta un rol, o el permiso es del autor.

    Subclase de `ERPNextValidationError` para no cambiar el contrato de los llamadores
    existentes (los dos son fallos duros, no se reintentan); se separa porque el
    llamador necesita distinguir "los datos estan mal" de "no tienes permiso" al
    clasificar el error. Caso real verificado el 2026-10-01: el principal de la API
    (`Projects User` + `Accounts User`) puede leer y crear comentarios por los metodos
    de la linea de tiempo, pero NO editar el comentario de otra persona ni borrar
    ninguno: Frappe solo da ese permiso a `System Manager` / `Website Manager`.
    """


class ERPNextDuplicateError(ERPNextError):
    """La clave de idempotencia ya existia (HTTP 417).

    No es un fallo: significa que un intento anterior si llego a crear el documento.
    El llamador debe releer por la clave y tratarlo como exito idempotente.
    """


class ERPNextClient:
    """Cliente REST minimo sobre los DocTypes de la allowlist."""

    def __init__(self, settings: Settings) -> None:
        self._base_url = (settings.ERPNEXT_URL or "").strip().rstrip("/")
        self._user = (settings.ERPNEXT_USER or "").strip()
        self._api_key = (settings.ERPNEXT_API_KEY or "").strip()
        self._api_secret = (settings.ERPNEXT_API_SECRET or "").strip()
        self._timeout = httpx.Timeout(
            float(settings.ERPNEXT_TIMEOUT_SECONDS or _DEFAULT_TIMEOUT_SECONDS),
            connect=float(settings.ERPNEXT_CONNECT_TIMEOUT_SECONDS or _DEFAULT_CONNECT_TIMEOUT_SECONDS),
        )
        self._http: httpx.AsyncClient | None = None

    # --- configuracion ---------------------------------------------------

    @property
    def is_configured(self) -> bool:
        return bool(self._base_url and self._api_key and self._api_secret)

    def config_error(self) -> str | None:
        if self.is_configured:
            return None
        missing = [
            name
            for name, value in (
                ("ERPNEXT_URL", self._base_url),
                ("ERPNEXT_API_KEY", self._api_key),
                ("ERPNEXT_API_SECRET", self._api_secret),
            )
            if not value
        ]
        return f"Falta configuracion de ERPNext: {', '.join(missing)}."

    # --- transporte ------------------------------------------------------

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            # Sin `Content-Type` fijo: httpx lo pone cuando se manda `json=`, y un
            # encabezado fijo aqui romperia el multipart de `upload_file` (que necesita
            # el boundary que genera httpx).
            self._http = httpx.AsyncClient(
                timeout=self._timeout,
                headers={
                    "Authorization": f"token {self._api_key}:{self._api_secret}",
                    "Accept": "application/json",
                },
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
        self._http = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        attempts: int = _MAX_ATTEMPTS,
    ) -> dict[str, Any]:
        """Una peticion. `attempts=1` en las escrituras que **crean**: ver abajo."""
        config_error = self.config_error()
        if config_error:
            raise ERPNextValidationError(config_error)

        url = f"{self._base_url}{path}"
        client = self._get_http()
        delay = _INITIAL_RETRY_DELAY_SECONDS
        last_error: Exception | None = None
        max_attempts = max(1, int(attempts))

        for attempt in range(max_attempts):
            try:
                response = await client.request(
                    method, url, params=params, json=json, files=files, data=data
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt < max_attempts - 1:
                    await asyncio.sleep(delay + random.uniform(0, 0.2))
                    delay *= 2
                    continue
                raise ERPNextUnavailable(f"ERPNext no respondio: {exc}") from exc

            status = response.status_code

            # 429 y 5xx son transitorios: reintentar. 4xx (salvo 417) es culpa de los datos.
            if status == 429 or status >= 500:
                if attempt < max_attempts - 1:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        wait = float(retry_after) if retry_after else delay
                    except (TypeError, ValueError):
                        wait = delay
                    await asyncio.sleep(max(wait, delay) + random.uniform(0, 0.2))
                    delay *= 2
                    continue
                raise ERPNextUnavailable(
                    f"ERPNext devolvio HTTP {status} tras {max_attempts} intentos. {self._format_error(response)}"
                )

            if status == 417:
                raise ERPNextDuplicateError(
                    self._format_error(response), status_code=status, payload=self._safe_json(response)
                )

            if status in (401, 403):
                raise ERPNextPermissionError(
                    self._format_error(response), status_code=status, payload=self._safe_json(response)
                )

            if status >= 400:
                raise ERPNextValidationError(
                    self._format_error(response), status_code=status, payload=self._safe_json(response)
                )

            return self._safe_json(response) or {}

        raise ERPNextUnavailable(f"ERPNext no acepto la solicitud: {last_error}")

    # --- operaciones -----------------------------------------------------

    def _check_doctype(self, doctype: str) -> None:
        if doctype not in ALLOWED_DOCTYPES:
            raise ERPNextValidationError(
                f"DocType '{doctype}' fuera de la allowlist del adapter ({sorted(ALLOWED_DOCTYPES)})."
            )

    async def get_doc(self, doctype: str, name: str) -> dict[str, Any]:
        self._check_doctype(doctype)
        payload = await self.request("GET", f"/api/resource/{doctype}/{name}")
        return payload.get("data") or {}

    async def list_docs(
        self,
        doctype: str,
        *,
        filters: list[list[Any]] | None = None,
        fields: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self._check_doctype(doctype)
        import json as _json

        params: dict[str, Any] = {"limit_page_length": limit}
        if filters:
            params["filters"] = _json.dumps(filters)
        if fields:
            params["fields"] = _json.dumps(fields)
        payload = await self.request("GET", f"/api/resource/{doctype}", params=params)
        return payload.get("data") or []

    async def create_doc(self, doctype: str, body: dict[str, Any]) -> dict[str, Any]:
        self._check_doctype(doctype)
        payload = await self.request("POST", f"/api/resource/{doctype}", json=body)
        return payload.get("data") or {}

    async def update_doc(self, doctype: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
        self._check_doctype(doctype)
        payload = await self.request("PUT", f"/api/resource/{doctype}/{name}", json=body)
        return payload.get("data") or {}

    # --- metodos oficiales de Frappe (linea de tiempo) -------------------
    #
    # Comentarios y adjuntos NO se escriben por `/api/resource/Comment|File`: eso exigiria
    # abrir `Comment` en la allowlist y dar al principal permiso de doctype, que Frappe
    # reserva a `System Manager`/`Website Manager`. Se usan los metodos que el propio desk
    # usa, que comprueban permiso sobre el **ticket**:
    #   - `frappe.desk.form.utils.add_comment`      (POST, exige leer el doc)
    #   - `frappe.desk.form.utils.update_comment`   (POST, solo el autor)
    #   - `frappe.desk.form.utils.remove_attach`    (POST, borra el File)
    #   - `frappe.desk.form.load.get_docinfo`       (GET, lee comentarios y adjuntos)
    #   - `upload_file`                             (multipart, exige escribir el doc)

    async def call_method(
        self,
        method: str,
        *,
        http_method: str = "POST",
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        attempts: int = _MAX_ATTEMPTS,
    ) -> dict[str, Any]:
        """Llama a un metodo whitelisted. Devuelve el cuerpo crudo de Frappe.

        Los metodos devuelven `{"message": ...}` o escriben claves propias en
        `frappe.response` (`get_docinfo` devuelve `{"docinfo": ...}`), asi que quien
        llama decide donde mirar.
        """
        return await self.request(
            http_method,
            f"/api/method/{method}",
            params=params,
            json=json,
            attempts=attempts,
        )

    async def upload_file(
        self,
        *,
        filename: str,
        content: bytes,
        doctype: str,
        docname: str,
        is_private: bool,
        folder: str = "Home/Attachments",
    ) -> dict[str, Any]:
        """`upload_file` oficial. Un solo intento a proposito.

        El POST **crea** un `File` nuevo en cada llamada (`content_hash` reutiliza el
        archivo en disco, no el documento), asi que un reintento automatico tras un
        timeout ambiguo duplicaria el adjunto. La deduplicacion vive en el adapter, que
        comprueba el `content_hash` antes de subir: asi un reintento del llamador no
        duplica.
        """
        self._check_doctype(doctype)
        body = await self.request(
            "POST",
            "/api/method/upload_file",
            data={
                "doctype": doctype,
                "docname": docname,
                "is_private": "1" if is_private else "0",
                "folder": folder,
            },
            files={"file": (filename, content, "application/octet-stream")},
            attempts=1,
        )
        message = body.get("message")
        return message if isinstance(message, dict) else body

    async def download_bytes(self, path: str, *, max_bytes: int) -> bytes:
        """Descarga el contenido de un `file_url` del sitio (`/private/files/...`).

        `path` es una **ruta relativa** del sitio, nunca una URL: por construccion este
        metodo no puede alcanzar otro host, asi que no hay SSRF que validar. Un adjunto
        remoto (`is_remote_file`, guardado con `file_url = http://...`) se rechaza aqui
        por la misma razon: no se sigue.
        """
        config_error = self.config_error()
        if config_error:
            raise ERPNextValidationError(config_error)
        raw = str(path or "").strip()
        if not raw.startswith("/") or raw.startswith("//") or "://" in raw:
            raise ERPNextValidationError(f"Ruta de archivo no descargable: {raw or '(vacia)'}")

        client = self._get_http()
        try:
            async with client.stream("GET", f"{self._base_url}{raw}") as response:
                status = response.status_code
                if status in (401, 403):
                    raise ERPNextPermissionError(
                        f"ERPNext nego el acceso al archivo (HTTP {status}).", status_code=status
                    )
                if status >= 400:
                    raise ERPNextValidationError(
                        f"ERPNext devolvio HTTP {status} al leer el archivo.", status_code=status
                    )
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > max_bytes:
                        raise ERPNextValidationError(
                            f"El archivo supera {max_bytes} bytes.", status_code=413
                        )
                return bytes(chunks)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ERPNextUnavailable(f"ERPNext no respondio: {exc}") from exc

    async def list_gl_entries(
        self,
        *,
        filters: list[list[Any]] | None = None,
        fields: list[str] | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Lector dedicado del libro mayor (TASK-2026-00044). Solo GET por
        construccion: no pasa por la allowlist de escritura porque `GL Entry`
        nunca se crea a mano (lo genera el submit del `Journal Entry`)."""
        import json as _json

        params: dict[str, Any] = {"limit_page_length": limit}
        if filters:
            params["filters"] = _json.dumps(filters)
        if fields:
            params["fields"] = _json.dumps(fields)
        payload = await self.request("GET", "/api/resource/GL Entry", params=params)
        return payload.get("data") or []

    # --- errores ---------------------------------------------------------

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return None

    def _format_error(self, response: httpx.Response) -> str:
        payload = self._safe_json(response)
        if not isinstance(payload, dict):
            return f"ERPNext devolvio HTTP {response.status_code}."

        # Frappe anida el mensaje util en varias formas segun el camino del error.
        parts: list[str] = []
        for key in ("exception", "exc_type", "_server_messages", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
            elif isinstance(value, list):
                parts.extend(str(item).strip() for item in value if str(item).strip())
        if not parts:
            parts.append(f"HTTP {response.status_code}")
        joined = " - ".join(parts)
        return f"Error de ERPNext ({response.status_code}): {joined[:600]}"
