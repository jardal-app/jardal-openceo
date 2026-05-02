# OpenCEO Voice

Puente LiveKit para peer programming por voz usando:

- LiveKit Agents
- Silero VAD
- Groq STT (`whisper-large-v3-turbo`)
- `dev` como backend OpenAI-compatible
- TTS OpenAI-compatible opcional

## Qué hace

Cada sala de LiveKit crea una sesión de voz que:

1. Detecta fin de turno con Silero VAD.
2. Transcribe audio con Groq STT.
3. Envía el texto a `openceo-core` usando el `model` seleccionado para la sesión.
4. Conserva estado por `user_id`, igual que OpenWebUI.
5. Si hay TTS configurado, responde hablando.

La app Android incluida ahora muestra un selector básico de modelo antes de conectarse.

## Variables usadas

Cada proyecto usa su propio `.env` local. Para `openceo-voice`, la fuente única es:

- `openceo-voice/.env`

- `LIVEKIT_URL`
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`
- `OPENCEO_VOICE_BASE_URL` default: `http://172.20.0.1:18765/v1`
- `OPENCEO_VOICE_API_KEY`
- `VOICE_LLM_MODEL` default: `dev`
- `GROQ_API_KEY`
- `GROQ_STT_BASE_URL` default: `https://api.groq.com/openai/v1`
- `GROQ_STT_MODEL` default: `whisper-large-v3-turbo`
- `GROQ_STT_LANGUAGE` default: `es`
- `GROQ_STT_DETECT_LANGUAGE` default: `false`
- `VOICE_TTS_API_KEY` opcional
- `VOICE_TTS_BASE_URL` opcional
- `VOICE_TTS_MODEL` default: `gpt-4o-mini-tts`
- `VOICE_TTS_VOICE` default: `alloy`

## Arranque

### Docker recomendado

El backend HTTP se expone por Traefik en `https://voice.openceo.jardal.app` sin publicar `ports:` en la VM.

```bash
cd openceo-voice
cp .env.example .env
docker compose up -d --build
```

Notas:

- El servicio `token` es el backend HTTP detrás de Traefik.
- El servicio `agent` queda preparado en el mismo compose bajo profile `agent`.
- Para levantar también el worker LiveKit:

```bash
docker compose --profile agent up -d --build
```

### Desarrollo local

```bash
cd openceo-voice
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python agent.py download-files
python agent.py dev
```

Para ejecución normal del worker:

```bash
python agent.py start
```

## Metadata recomendada del participante

Si el cliente móvil/web puede enviar metadata JSON al participante o a la sala, usar:

```json
{
  "user_id": "jorge",
  "project": "jardal-openceo",
  "model": "dev"
}
```

Fallbacks:

- `user_id`: `participant.identity` -> `participant.name` -> `room.name`
- `project`: proyecto seleccionado previamente en OpenCEO
- `model`: `dev`

## Nota sobre TTS

Si `VOICE_TTS_API_KEY` no está configurado, el worker sigue levantando para pruebas del lado STT/LLM, pero no emitirá respuesta hablada.
