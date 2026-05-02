# Sistema de diseño completo para OpenCEO

## 1) Dirección de marca

**OpenCEO no debe verse como una app genérica de IA.**
Debe verse como un **centro de operaciones**: preciso, premium, serio, rápido y técnico.

### Sensación visual

* negro profundo
* paneles oscuros
* acento azul controlado
* glow sutil, no exagerado
* bordes limpios
* interfaces compactas
* lectura clara de estados, tareas y agentes

### Personalidad

* ejecutivo técnico
* futurista sobrio
* operador de infraestructura
* confiable
* modular
* de alto nivel, sin ruido visual

### Lo que sí debe transmitir

* control
* automatización
* orquestación
* claridad
* velocidad
* autoridad técnica

### Lo que debe evitar

* estilo gamer
* neon excesivo
* glassmorphism
* multicolor fuerte
* gradientes arcoíris
* ilustraciones “cute”
* estética de chatbot común

---

## 2) Paleta base para OpenCEO

Voy a respetar tu base de logo y expandirla a un sistema usable.

### Colores madre de marca

```css
--oceo-brand-700: #005198;   /* azul base del logo */
--oceo-charcoal:  #231F20;   /* negro cálido del logo */
--oceo-graphite:  #434345;   /* gris marca */
--oceo-steel:     #3F444A;   /* gris técnico marca */
```

### Escala extendida recomendada

```css
:root {
  /* BRAND BLUES */
  --oceo-blue-100: #D8ECFF;
  --oceo-blue-200: #9DCCFF;
  --oceo-blue-300: #5CAAF7;
  --oceo-blue-400: #2E86E6;
  --oceo-blue-500: #0F6FD3;
  --oceo-blue-600: #005198;  /* brand core */
  --oceo-blue-700: #003E75;
  --oceo-blue-800: #002B52;

  /* DARK SYSTEM */
  --oceo-black:    #0B0F14;
  --oceo-panel:    #111821;
  --oceo-surface:  #151D27;
  --oceo-elevated: #1A2430;
  --oceo-stroke:   #273445;

  /* LOGO NEUTRALS */
  --oceo-charcoal: #231F20;
  --oceo-graphite: #434345;
  --oceo-steel:    #3F444A;

  /* TEXT */
  --oceo-text-1:   #EAF2FB;
  --oceo-text-2:   #B5C2D1;
  --oceo-text-3:   #7E8B99;

  /* STATES */
  --oceo-success:  #12B981;
  --oceo-warning:  #F5A524;
  --oceo-danger:   #F04452;
  --oceo-info:     #3BA4FF;
}
```

## 3) Regla cromática

La regla más importante:

* **Azul** = acción principal, foco, selección, estado activo
* **Grises del logo** = estructura, fondos, bordes, separación
* **Negro** = profundidad
* **Verde, amarillo, rojo** = solo estados
* no usar otro color de marca principal además del azul

### Uso correcto

* CTA principal: `--oceo-blue-500`
* hover CTA: `--oceo-blue-400`
* bordes activos: `--oceo-blue-400`
* glow: azul muy sutil con baja opacidad
* paneles: `--oceo-panel`, `--oceo-surface`, `--oceo-elevated`

---

## 4) Identidad visual del logo

## Concepto recomendado

Para OpenCEO, el símbolo debe comunicar:

* orquestación
* núcleo
* control
* sistema abierto
* flujo entre agentes

### Propuesta visual del icono

Un símbolo compuesto por:

* una **O abierta** o marco geométrico incompleto
* un **núcleo central** pequeño
* 3 o 4 líneas/rutas técnicas que salen o convergen
* sensación de “control center” o “routing engine”

### Estilo del logo

* vector 2D
* sin sombras pesadas
* sin 3D
* sin íconos cliché de cerebro, robot, escudo o candado
* geometría limpia
* versión horizontal, stacked e isotipo

### Wordmark recomendado

```text
OPEN  -> peso medium / color gris claro
CEO   -> peso bold / color azul marca
```

O también:

```text
OPENCEO
```

con “CEO” visualmente enfatizado en azul.

### Prompt de logo maestro

```text
A premium ultra-minimalist vector logo for a multi-agent enterprise orchestration platform called "OpenCEO".

ICON:
- abstract control-core symbol
- an open geometric ring forming an "O"
- a centered core node
- 3 routed lines or segmented paths suggesting orchestration, workflow routing, and intelligent control
- balanced symmetry
- sharp but refined geometry
- recognizable at favicon size

STYLE:
- flat 2D vector
- dark futuristic enterprise aesthetic
- not crypto, not gaming, not playful
- should feel like mission control software for operators
- subtle cyber-industrial feel
- no shields, no robots, no brains, no chat bubbles, no generic AI stars

COLORS:
- primary brand blue #005198
- supporting blues #2E86E6 and #5CAAF7
- dark neutrals #231F20, #434345, #3F444A
- background near-black #0B0F14

WORDMARK:
- geometric sans
- "OPEN" in muted light gray
- "CEO" in electric blue
- tight professional spacing

DELIVER:
1. horizontal logo
2. stacked logo
3. icon-only version
4. monochrome version
```

---

## 5) Tipografía

Para mantener el ADN futurista de FugoVPN, pero más corporativo:

### Fuentes

```css
--font-display: 'Orbitron', 'Rajdhani', 'Chakra Petch', sans-serif;
--font-body: 'Inter', 'IBM Plex Sans', 'Segoe UI', sans-serif;
--font-mono: 'JetBrains Mono', 'Fira Code', monospace;
```

### Regla

* títulos y branding: `Orbitron` o `Rajdhani`
* interfaz y textos: `Inter`
* logs, ids, workflows, terminal: `JetBrains Mono`

### Jerarquía

```css
--text-display: clamp(2.5rem, 5vw, 4rem);
--text-h1: clamp(2rem, 4vw, 3rem);
--text-h2: clamp(1.5rem, 3vw, 2rem);
--text-h3: 1.25rem;
--text-body: 1rem;
--text-small: 0.875rem;
--text-code: 0.875rem;
```

---

## 6) Estilo de componentes

## Botón primario

```css
.btn-primary {
  background: var(--oceo-blue-500);
  color: #F8FBFF;
  border: 1px solid var(--oceo-blue-400);
  border-radius: 12px;
  padding: 14px 20px;
  font-family: var(--font-body);
  font-weight: 600;
  transition: all .18s ease;
}

.btn-primary:hover {
  background: var(--oceo-blue-400);
  box-shadow: 0 0 0 1px rgba(92,170,247,.35),
              0 0 28px rgba(46,134,230,.16);
}
```

## Botón secundario

```css
.btn-secondary {
  background: transparent;
  color: var(--oceo-text-2);
  border: 1px solid var(--oceo-stroke);
  border-radius: 12px;
}

.btn-secondary:hover {
  background: var(--oceo-surface);
  border-color: var(--oceo-blue-600);
  color: var(--oceo-text-1);
}
```

## Card

```css
.card {
  background: linear-gradient(180deg, #151D27 0%, #111821 100%);
  border: 1px solid var(--oceo-stroke);
  border-radius: 16px;
  padding: 24px;
}

.card:hover {
  border-color: rgba(46,134,230,.5);
  box-shadow: 0 0 0 1px rgba(46,134,230,.12);
}
```

## Input

```css
.input {
  background: #0F151D;
  border: 1px solid var(--oceo-stroke);
  color: var(--oceo-text-1);
  border-radius: 12px;
  padding: 14px 16px;
}

.input:focus {
  outline: none;
  border-color: var(--oceo-blue-400);
  box-shadow: 0 0 0 3px rgba(46,134,230,.14);
}
```

## Badge

```css
.badge-active {
  background: rgba(46,134,230,.12);
  color: var(--oceo-blue-300);
  border: 1px solid rgba(46,134,230,.35);
}

.badge-success {
  background: rgba(18,185,129,.12);
  color: var(--oceo-success);
  border: 1px solid rgba(18,185,129,.35);
}
```

---

## 7) Layout general

## Fondo principal

* body oscuro absoluto
* textura muy sutil de grid o noise
* nada de fondos decorativos pesados

```css
body {
  background: radial-gradient(circle at top, rgba(0,81,152,.12), transparent 30%),
              #0B0F14;
  color: var(--oceo-text-1);
}
```

## Estructura de app

* sidebar izquierda
* top bar compacta
* panel principal central
* panel derecho opcional para detalles
* cards modulares
* terminal/log view abajo o lateral

### Grid principal

```css
.app-shell {
  display: grid;
  grid-template-columns: 280px 1fr 360px;
  min-height: 100vh;
  background: var(--oceo-black);
}
```

---

## 8) Secciones clave de OpenCEO

## A. Hero / landing

Debe vender “orquestación real”, no “chat bonito”.

### Estructura

* izquierda: claim fuerte
* derecha: mockup de dashboard
* abajo: métricas rápidas

### Copy ideal

* **Orquesta agentes, tareas y workflows desde un solo núcleo**
* **Menos fricción. Más ejecución.**
* **Mission control para operaciones multiagente**

### CTAs

* Iniciar despliegue
* Ver arquitectura
* Abrir dashboard

---

## B. Dashboard principal

Debe parecer un centro de mando.

### Widgets clave

* estado del CEO
* agentes activos
* tasks en progreso
* workflows bloqueados
* approvals pendientes
* activity stream
* panel de logs
* health del sistema

### Visual

* cards compactas
* chips de estado
* gráficas lineales simples
* bordes finos
* un único azul como foco

---

## C. Vista de agentes

Cada agente debe tener:

* avatar geométrico simple
* estado
* tarea actual
* canal activo
* última acción
* uso de recursos

### Color por agente

No usar colores fuertes por agente.
Todos viven en neutros y el azul solo marca selección/actividad.
El color fuerte por agente rompe la autoridad visual del sistema.

---

## D. Workflow view

Aquí sí puedes meter más lenguaje futurista.

### Elementos

* nodos conectados
* líneas finas azules
* estado por paso
* bloqueos humanos
* ejecución completada
* loops y branches

### Estilo

* blueprint técnico
* nada cartoon
* nada rounded exagerado

---

## 9) Iconografía

### Estilo

* outline
* 2px stroke
* limpio
* técnico
* consistente

### Librerías recomendadas

* Lucide
* Heroicons
* Phosphor en versión outline

### Iconos principales

* activity
* workflow
* nodes
* terminal
* tasks
* approvals
* finance
* pm
* dev
* secretary
* notifications
* logs
* automation

---

## 10) Animación

La animación debe sentirse “sistema vivo”, no “marketing flashy”.

### Animaciones correctas

* pulse suave en estado activo
* glow leve en botón principal
* líneas de carga
* counters
* transición suave entre paneles

### Animaciones incorrectas

* rebotes
* zoom agresivo
* parallax fuerte
* partículas innecesarias

```css
@keyframes oceoPulse {
  0%,100% { opacity: 1; }
  50% { opacity: .65; }
}
```

---

## 11) Estilo visual final

### Resumen visual definitivo

* **base:** negro profundo
* **superficies:** gris azulado oscuro
* **bordes:** gris técnico
* **texto:** blanco frío / gris claro
* **acento:** azul marca
* **glow:** muy sutil
* **formas:** rectángulos elegantes con radio 12–16px
* **tipografía:** técnica y premium
* **mood:** enterprise futurista

---

## 12) Prompt maestro para UI de OpenCEO

```text
Design a premium dark futuristic enterprise dashboard for "OpenCEO", a multi-agent orchestration platform.

STYLE:
- black and deep graphite interface
- minimal, clean, technical, operator-style
- subtle blue accent only
- no colorful gradients
- no glassmorphism
- no white cards
- no playful illustrations
- should feel like mission control for an advanced AI operations system

COLOR PALETTE:
- primary blue #005198
- active blue #2E86E6
- soft blue #5CAAF7
- deep black #0B0F14
- charcoal #231F20
- graphite #434345
- steel #3F444A
- text #EAF2FB
- muted text #B5C2D1

LAYOUT:
- left sidebar navigation
- top command/status bar
- central dashboard grid
- right detail inspector
- cards for agents, workflows, blocked tasks, approvals, logs, metrics

VISUAL LANGUAGE:
- subtle rounded corners
- thin borders
- elegant data cards
- blueprint-like workflow lines
- code/terminal areas with monospace font
- strong hierarchy and spacing
- premium SaaS / enterprise OS aesthetic

KEY PANELS:
- CEO supervisor status
- active agents
- workflow execution map
- blocked approvals
- logs / console
- recent tasks
- health and observability

TYPOGRAPHY:
- geometric futuristic headings
- clean sans-serif body
- monospace for technical data
```

---

## 13) Mi recomendación final

Para que OpenCEO quede realmente fuerte, la fórmula correcta es esta:

**FugoVPN visual language**

* **OpenCEO operator discipline**
* **tu azul #005198 como ADN de marca**
  = **una identidad mucho más premium y propia**

La decisión más importante aquí es esta:
**no copiar el neón lime de FugoVPN, sino traducir su autoridad visual a un azul ejecutivo-tecnológico**.

Si sigues esta línea, OpenCEO puede verse como:

* una plataforma seria
* una herramienta de mando real
* y una marca mucho más escalable

Puedo convertir esto enseguida en una de estas tres salidas: **landing page completa**, **dashboard completo**, o **design system en formato CSS/Tailwind listo para desarrollo**.
