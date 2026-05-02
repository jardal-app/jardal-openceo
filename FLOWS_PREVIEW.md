### 1. Pagos Automáticos de Servicios Básicos

**Agente responsable:** Finance Agent

* **Objetivo:** Pagar facturas de luz, agua, internet y servicios básicos (empresa y hogar) en portales sin API.
* **Flujo Operativo:**
1. Se activa un cron job mensual.
2. El agente lanza una instancia headless de navegador web.
3. Navega a las URLs de las empresas de servicios básicos.
4. Inyecta credenciales en el DOM, resuelve CAPTCHAs simples si existen, y extrae el valor de la deuda actual.
5. Si hay deuda, procede a la pasarela de pago inyectando los datos de la tarjeta corporativa o conectándose a la API de Binance Pay.
6. Toma una captura de pantalla del recibo y la guarda en el almacenamiento.


* **Requerimientos Técnicos:**
* **Automatización Web:** `Playwright` o `Puppeteer` utilizando *Browser Contexts* para mantener sesiones aisladas.
* **Gestión de selectores:** Uso de esperas explícitas (`wait_for_selector`) para portales lentos o inestables.
* **Ejecución:** Contenedor Docker aislado con dependencias de navegador preinstaladas, sin ocupar el Worker PC principal.



### 2. Marketing en Redes Sociales

**Agente responsable:** Marketing Agent

* **Objetivo:** Gestión de publicaciones y lectura de analíticas de forma aislada para proyectos SaaS y personales.
* **Flujo Operativo:**
1. El agente lee el archivo YAML del proyecto específico.
2. Extrae las directivas de publicación, IDs de páginas, tokens de acceso y determina si el proyecto requiere marketing.
3. Ejecuta la publicación en las plataformas vinculadas.
4. Para las analíticas web, realiza peticiones de lectura a Cloudflare.


* **Requerimientos Técnicos:**
* **Configuración (YAML):** Uso de `PyYAML` para parsear la configuración. Estructura requerida: `project_name`, `meta_page_id`, `cloudflare_zone_id`.
* **Publicación:** Peticiones HTTP POST a la API Graph de Meta (`graph.facebook.com/v19.0/{page_id}/feed`).
* **Analíticas (Cloudflare):** Peticiones a la API de Cloudflare con un token restringido **únicamente** a permisos `Zone:Analytics:Read`. No tiene acceso a modificación de DNS.



### 3. Configuración Técnica de Correos Multicuenta

**Agente responsable:** Secretary Agent

* **Objetivo:** Selección dinámica de cuenta y envío seguro de correos según el departamento (ventas, soporte, admin, legal).
* **Flujo Operativo:**
1. El LLM programador recibe la instrucción de enviar un correo junto con el contexto de la tarea.
2. La agente analiza la solicitud y determina qué alias o cuenta departamental debe utilizar.
3. El script accede al entorno de Docker y recupera el set de credenciales específico para ese departamento.
4. Se establece la conexión TLS/SSL con el servidor SMTP autenticándose con la cuenta seleccionada.
5. Se despacha el correo, asegurando que el remitente (`From:`) coincida exactamente con la cuenta utilizada para evitar rechazos por políticas DMARC/SPF.


* **Requerimientos Técnicos:**
* **Librerías:** `smtplib` y `email.mime` (Python) o `Nodemailer` (Node.js).
* **Gestión de Credenciales:** Estructura de datos (JSON o variables de entorno prefijadas como `SALES_SMTP_USER`, `LEGAL_SMTP_USER`) leída directamente desde el host de Proxmox/Docker o un gestor de secretos.
* **Parámetros SMTP:** `SMTP_HOST`, `SMTP_PORT` (465 o 587), `USER`, `PASS` instanciados dinámicamente.



### 4. Creación y Revisión de Documentos (Human in the Loop)

**Agentes responsables:** Sales Agent / Secretary Agent

* **Objetivo:** Generar documentos en formatos reales, almacenamiento privado y acceso temporal seguro para revisión humana.
* **Flujo Operativo:**
1. El agente recibe la solicitud de un documento (cotización, certificado, etc.) e inyecta los datos variables en una plantilla `.docx` preexistente.
2. El archivo generado se sube a tu servidor MinIO / Filestash (bucket personal/privado).
3. El agente genera una **Presigned URL** (URL prefirmada) de acceso temporal exclusivo **solo para ese archivo**.
4. Se envía la URL única al empleado humano a través de Telegram o WhatsApp.
5. El humano descarga el documento, lo edita localmente, le da el toque final y lo sube nuevamente o confirma la revisión.
6. Una vez aprobado, el agente revoca la URL temporal y traslada el archivo final a la carpeta compartida de la empresa, evitando exposición de datos sensibles.


* **Requerimientos Técnicos:**
* **Generación:** `python-docx-template` (docxtpl) para inyectar diccionarios de datos en plantillas Word.
* **Almacenamiento:** Librería `boto3` (Python S3 client) para interactuar con la API de MinIO.
* **Seguridad:** Método `generate_presigned_url` con el parámetro `ExpiresIn` configurado (ej. 24 horas) para restringir el acceso estricto a un solo objeto.
* **Notificaciones:** Integración con la API de Telegram (`python-telegram-bot`) o WhatsApp para notificaciones directas al número del empleado.



### 5. Flujos Operativos Estándar

**Agentes responsables:** Varios

* **Atención al Cliente:** Soporte automatizado estandarizado interactuando con bases de conocimiento.
* **Pago a Empleados (Nómina):** Gestión de pagos internos y cálculo de salarios para el equipo.
* **Monitoreo de SaaS:** Supervisión del estado, uptime y funcionamiento de tus servicios y contenedores.