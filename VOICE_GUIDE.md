# Guía de Flujos y Control por Voz OpenCEO (DeepSeek v4 Flash)

Esta guía detalla los flujos de interacción por voz natural con OpenCEO, la app Android y la Primera Capa Cognitiva (`FirstLayerGateway`) impulsada por **DeepSeek v4 Flash**.

---

## 1. Filosofía de Conversación y Respuestas

- **Tono natural y fluido**: Habla y responde como un colega técnico o Director de Operaciones (COO).
- **Impersonal en primera persona plural**: Usa siempre *"Tenemos..."*, *"Estamos en..."*, *"Veo que tenemos..."*.
- **Optimizado para voz (TTS)**: Respuestas breves, directas y claras, sin tablas markdown, sin UUIDs ni URLs largas.
- **Orientación activa**: Si el usuario dice que está perdido o el comando es ambiguo, OpenCEO lo guía activamente con opciones contextuales.

---

## 2. Flujo de Selección de Proyecto y Foco

| Intención / Pregunta por Voz | Ejemplo de Comando | Respuesta Natural de OpenCEO |
|---|---|---|
| **Consultar proyecto actual** | *"¿En qué proyecto estamos?"* | *"Estamos en el proyecto jardal-openceo. Tenemos 3 tickets abiertos y 5 presets listos."* |
| **Cambiar de proyecto** | *"Cámbiate al proyecto trading"* / *"Vamos a melanyryr"* | *"Cambiado al proyecto jardal-trading. Tenemos 1 ticket abierto y 3 presets configurados. ¿Por dónde empezamos?"* |
| **Listar proyectos disponibles** | *"¿Qué proyectos tenemos?"* / *"Lista los proyectos"* | *"Tenemos configurados 5 proyectos: jardal-openceo, jardal-trading, jardal-vpn, melanyryr y cebitec. ¿A cuál nos cambiamos?"* |

---

## 3. Flujo de Consulta y Foco de Tickets

OpenCEO mantiene un **foco activo de ticket** en memoria para cada usuario. Puedes consultar el tablero general o entrar en detalle en un ticket:

| Acción | Comando de Ejemplo | Respuesta de OpenCEO |
|---|---|---|
| **Conteo de tickets abiertos** | *"¿Cuántos tickets tenemos abiertos?"* / *"¿Cómo vamos de tickets?"* | *"Tenemos abiertos 3 tickets en jardal-openceo: el número 48 sobre trading en progreso, el 49 sobre la VPN esperando revisión, y el 50 pendiente de inicio. ¿Quieres que enfoquemos alguno o veamos qué pasó?"* |
| **Enfocar un ticket** | *"Vamos al ticket 48"* / *"Enfoca el 49"* | *"Enfocado el ticket #48 en jardal-trading: 'Bot grid review'. Estado actual: in_progress."* |
| **Consultar detalle o fallo** | *"¿Por qué falló?"* / *"¿Qué falta?"* | *"El ticket #48 está en espera. Detalle: revisión de permisos en archivo de configuración."* |
| **Reintentar / Ejecutar** | *"Reintenta el ticket"* / *"Ejecuta el 48"* | *"Reintentando ticket #48 en jardal-trading en segundo plano."* |

---

## 4. Presets de Prompts por Proyecto (Configuración Manual + Dictado por Voz)

Los presets son **prompts iniciales preconfigurados manualmente en `projects/<nombre>/project.yaml`** diseñados para agilizar el dictado técnico.
Al invocar un preset por voz, OpenCEO escribe el texto inicial en el prompt del agente dev (Codex, Claude, etc.) dentro de su terminal `tmux` **sin presionar Enter** (`enter=False`), dejando el cursor listo para que le dictes el resto de la orden técnica.

### Flujo de Dictado con Presets:

1. **Escribir preset sin Enter**:
   - Usuario: *"Pon el preset review"* / *"Carga el preset test_fix"* / *"Preset refactor"*
   - OpenCEO: *"Listo, preset review en pantalla. Dime qué le agregamos."*

2. **Dictar el resto de la instrucción**:
   - Usuario: *"agrega que verifique la base de datos"*
   - OpenCEO: *"Anotado. ¿Algo más o le damos enter?"*

3. **Ejecutar / Enviar con Enter**:
   - Usuario: *"dale enter"* / *"envíalo"* / *"mándalo"*
   - OpenCEO: *"Listo, enviado."*

4. **Cancelar / Limpiar la línea (C-u)**:
   - Usuario: *"cancela el preset"* / *"borra la línea"* / *"cancélalo"*
   - OpenCEO: *"Listo, borrado."*

5. **Consultar presets configurados**:
   - Usuario: *"¿Qué presets tenemos?"* / *"Lista los presets"*
   - OpenCEO: *"Tenemos los presets: review, test_fix, refactor, sync_context, security_audit. ¿Cuál ponemos?"*

6. **Configuración manual (en `projects/<proyecto>/project.yaml`)**:
   ```yaml
   presets:
     review:
       description: "Revisar cambios y cumplimiento de reglas"
       prompt: "Revisa los cambios recientes en git diff, valida el cumplimiento estricto de las reglas de AGENTS.md y confirma que no haya errores de sintaxis ni regresiones."
     test_fix:
       description: "Ejecutar tests y autocorregir fallos"
       prompt: "Ejecuta los tests del proyecto, analiza cualquier fallo reportado y aplica los arreglos necesarios manteniendo clean code."
   ```

---

## 5. Guía Activa Cuando Estás Perdido o hay Dudas

Si estás en la calle o caminando y no recuerdas qué comandos decir, simplemente pregunta:

- **Usuario**: *"Estoy perdido"* / *"¿Qué puedo hacer?"* / *"Ayuda"* / *"¿Dónde estamos?"*
- **OpenCEO**: *"Estamos en el proyecto jardal-openceo con el ticket #48 en foco. Tenemos 3 tickets abiertos. Puedes decirme por ejemplo: '¿cuántos tickets tenemos abiertos?', 'vamos al ticket 48', 'ejecuta el preset test' para correr en tmux, 'crea una sesión dev' para programar con la IA, o 'cambia al proyecto trading'. ¿Qué te gustaría hacer?"*

Si dices una frase entrecortada o confusa por ruido ambiente:
- **OpenCEO**: *"No te entendí bien esa última frase. ¿Querías consultar los tickets, ejecutar un preset de comandos, o abrir una sesión dev? Dime y te ayudo."*

---

## 6. Sesiones de Programación Dev y Control Tmux

- **Crear sesión de código**: *"Crea una sesión dev"* / *"Abre una sesión codex en jardal-trading"*
  - Respuesta: *"Sesión de desarrollo creada y activa en jardal-trading."*
- **Instrucción de código**: Si la sesión está activa, cualquier orden técnica (*"Agrega el test unitario para el endpoint de presets"*) se envía al agente de código en segundo plano de inmediato.
- **Control de sesiones Tmux**:
  - *"¿Qué sesiones de tmux hay?"* -> *"Tenemos 4 sesiones de tmux activas: agentboard, business, tr, p-jardal-openceo-build."*
  - *"¿Qué sale en tr?"* -> Lee las últimas líneas del pane de trading.
  - *"Manda git pull a la terminal tr"* -> Inyecta teclas con Enter en el pane.

---

## 6b. Registro de gastos por voz (Finanzas → Jardal ERP)

Hablas con el **CEO** y el CEO deriva a **Finanzas** (no necesitas `@finance`;
queda como atajo). Cada gasto se registra directo en Jardal ERP como asiento
idempotente: **ningún gasto crea un ticket** y puedes apagar la app después.

| Di por voz | Qué se registra |
|---|---|
| *"registra gasto personal carne 50 bolivianos"* | 50,00 BOB en `Gastos varios - J` |
| *"pago de luz de casa 300 bs"* | 300,00 BOB en `Servicios públicos - J` |
| *"anota pasaje 15 bs"* | 15,00 BOB en transporte |

Respuesta típica: *"Listo, dejamos registrados 50,00 BOB de carne en gastos
personales. Asiento ACC-JV-2026-00012."* Si el gasto ya se había registrado
(reintento), responde que no lo duplicó. Sin monto no hay registro: te pide
el monto y la moneda (BOB por defecto; USD y USDT se distinguen).

---

## 7. Configuración de la App Android (`openceo-voice.apk`)

1. **Instalar el APK**: Archivo listo en `/home/develop/development/Jardal/jardal-openceo/openceo-voice.apk`.
2. **Directo para hablar**:
   - Activa el interruptor **"Conectar directo al abrir"**. Al abrir la app en el móvil, entra automáticamente a la llamada de voz sin presionar ningún botón.
   - Selecciona tu **Proyecto Foco** predeterminado (`jardal-openceo`, `jardal-trading`, etc.).
   - Selecciona el modo **CEO (Supervisor & Voz)** para tener todo el gateway DeepSeek v4 Flash.
