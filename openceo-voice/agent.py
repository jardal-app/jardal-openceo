from __future__ import annotations

import json
import logging
import os
from typing import Any
from collections.abc import AsyncGenerator, AsyncIterable

from livekit import rtc
from livekit.agents import (
    APIConnectOptions,
    Agent,
    AgentSession,
    AutoSubscribe,
    DEFAULT_API_CONNECT_OPTIONS,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
    tts,
)
from livekit.agents.tts import AudioEmitter, ChunkedStream, TTSCapabilities
from livekit.agents.voice import ModelSettings
from livekit.plugins import openai, silero
from livekit.plugins import groq as groq_plugin
from project_env import load_project_env


load_project_env()

logger = logging.getLogger("openceo.voice")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "si", "sí"}


def _parse_json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def prewarm(proc: JobProcess) -> None:
    try:
        import edge_tts  # noqa: F401
    except Exception:
        pass
    proc.userdata["vad"] = silero.VAD.load(
        min_speech_duration=float(os.getenv("VOICE_VAD_MIN_SPEECH_DURATION", "0.05")),
        min_silence_duration=float(os.getenv("VOICE_VAD_MIN_SILENCE_DURATION", "0.35")),
        prefix_padding_duration=float(os.getenv("VOICE_VAD_PREFIX_PADDING_DURATION", "0.2")),
    )


def _participant_context(ctx: JobContext, participant: Any) -> tuple[str, str | None, str]:
    participant_meta = _parse_json_dict(getattr(participant, "metadata", ""))
    room_meta = _parse_json_dict(getattr(ctx.room, "metadata", ""))
    attributes = getattr(participant, "attributes", None)

    merged: dict[str, Any] = {}
    merged.update(room_meta)
    merged.update(participant_meta)
    if isinstance(attributes, dict):
        merged.update(attributes)

    llm_model = os.getenv("VOICE_LLM_MODEL", "dev").strip()

    user_id = _first_non_empty(
        merged.get("user_id"),
        getattr(participant, "identity", None),
        getattr(participant, "name", None),
        getattr(ctx.room, "name", None),
    ) or "livekit-user"
    project = _first_non_empty(merged.get("project"))
    model = _first_non_empty(merged.get("model"), llm_model) or llm_model

    return user_id, project, model


def _build_llm(user_id: str, project: str | None, model: str) -> openai.LLM:
    openceo_host = os.getenv("OPENCEO_HOST", "172.20.0.1").strip() or "172.20.0.1"
    openceo_port = os.getenv("OPENCEO_PORT", "18765").strip() or "18765"
    default_base = f"http://{openceo_host}:{openceo_port}/v1"
    base_url = (os.getenv("OPENCEO_VOICE_BASE_URL") or "").strip() or default_base

    api_key = (
        os.getenv("OPENCEO_VOICE_API_KEY")
        or os.getenv("OPENCEO_API_KEY")
        or "openceo-local"
    ).strip()

    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
        "user": user_id,
        "timeout": float(os.getenv("VOICE_LLM_TIMEOUT", "45.0")),
    }
    if project:
        kwargs["extra_body"] = {"project": project}
    return openai.LLM(**kwargs)


def _build_stt() -> groq_plugin.STT:
    return groq_plugin.STT(
        model=os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo").strip(),
        language=os.getenv("GROQ_STT_LANGUAGE", "es").strip(),
        api_key=os.getenv("GROQ_API_KEY", "").strip() or None,
    )


class EdgeTTS(tts.TTS):
    def __init__(self, voice: str = "es-ES-AlvaroNeural", sample_rate: int = 24000):
        super().__init__(
            capabilities=TTSCapabilities(streaming=False),
            sample_rate=sample_rate,
            num_channels=1,
        )
        self._voice = voice

    @property
    def model(self) -> str:
        return "edge-tts"

    @property
    def provider(self) -> str:
        return "microsoft"

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> ChunkedStream:
        return _EdgeChunkedStream(
            tts=self,
            input_text=text,
            conn_options=conn_options,
            voice=self._voice,
            sample_rate=self.sample_rate,
            num_channels=self.num_channels,
        )


class _EdgeChunkedStream(ChunkedStream):
    def __init__(
        self,
        *,
        tts: tts.TTS,
        input_text: str,
        conn_options: APIConnectOptions,
        voice: str,
        sample_rate: int,
        num_channels: int,
    ):
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._voice = voice
        self._sample_rate = sample_rate
        self._num_channels = num_channels

    async def _run(self, output_emitter: AudioEmitter) -> None:
        import edge_tts

        output_emitter.initialize(
            request_id="edge-tts",
            sample_rate=self._sample_rate,
            num_channels=self._num_channels,
            mime_type="audio/mp3",
        )
        comm = edge_tts.Communicate(self.input_text, self._voice)
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                output_emitter.push(chunk["data"])


class VoiceAgent(Agent):
    """Custom Agent ensuring tts_node safely handles None TTS without throwing AssertionError."""

    async def tts_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncGenerator[rtc.AudioFrame, None]:
        activity = self._get_activity_or_raise()
        if activity.tts is not None:
            async for frame in Agent.default.tts_node(self, text, model_settings):
                yield frame
        else:
            async for _ in text:
                pass


def _build_tts() -> tts.TTS | None:
    tts_key = (os.getenv("VOICE_TTS_API_KEY") or "").strip()
    if tts_key:
        tts_base = (os.getenv("VOICE_TTS_BASE_URL") or "https://api.openai.com/v1").strip()
        logger.info("Using OpenAI TTS with key")
        return openai.TTS(
            model=(os.getenv("VOICE_TTS_MODEL") or "gpt-4o-mini-tts").strip(),
            voice=(os.getenv("VOICE_TTS_VOICE") or "alloy").strip(),
            api_key=tts_key,
            base_url=tts_base,
            instructions=(os.getenv("VOICE_TTS_INSTRUCTIONS") or "Habla en español, claro y breve.").strip(),
        )

    # Free high-quality neural Spanish voice using edge-tts
    try:
        import edge_tts  # noqa: F401
        voice = (os.getenv("VOICE_TTS_VOICE") or "es-ES-AlvaroNeural").strip()
        if voice in {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}:
            voice = "es-ES-AlvaroNeural"
        logger.info("VOICE_TTS_API_KEY not configured; using free EdgeTTS with voice '%s'", voice)
        return EdgeTTS(voice=voice)
    except Exception as e:
        logger.warning("EdgeTTS failed to initialize: %s", e)

    allow_no_tts = _env_bool("VOICE_ALLOW_NO_TTS", default=True)
    if not allow_no_tts:
        raise ValueError("VOICE_TTS_API_KEY is required when VOICE_ALLOW_NO_TTS=false")
    logger.warning("No TTS engine available; running in text-only response mode.")
    return None


def _agent_instructions(model: str) -> str:
    return (
        "Eres el puente de voz de OpenCEO. "
        f"El modelo backend seleccionado por el usuario es '{model}'. "
        "Mantén el estilo breve, claro y operativo."
    )


def _ready_message(model: str) -> str:
    if model == "ceo":
        return "OpenCEO listo para hablar. Dime en qué proyecto, ticket o preset trabajamos hoy."
    if model == "dev":
        return (
            "Puente de voz listo. Modelo activo: dev. "
            "Puedes pedirme que cree una sesión dev o continuar una que ya esté abierta."
        )
    return f"Puente de voz listo. Modelo activo: {model}. Puedes hablarme directamente."


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    participant = await ctx.wait_for_participant()
    user_id, project, model = _participant_context(ctx, participant)

    logger.info(
        "Voice bridge: participant=%s room=%s user_id=%s project=%s model=%s",
        getattr(participant, "identity", "unknown"),
        getattr(ctx.room, "name", "unknown"),
        user_id,
        project or "-",
        model,
    )

    llm = _build_llm(user_id=user_id, project=project, model=model)
    stt = _build_stt()
    tts = _build_tts()
    vad = ctx.proc.userdata["vad"]

    agent = VoiceAgent(
        instructions=_agent_instructions(model),
    )

    session = AgentSession(
        llm=llm,
        stt=stt,
        tts=tts,
        vad=vad,
        min_endpointing_delay=float(os.getenv("VOICE_MIN_ENDPOINTING_DELAY", "0.6")),
        max_endpointing_delay=float(os.getenv("VOICE_MAX_ENDPOINTING_DELAY", "1.5")),
    )
    await session.start(agent=agent, room=ctx.room)

    if tts is not None:
        session.say(
            _ready_message(model),
            add_to_chat_ctx=False,
        )


if __name__ == "__main__":
    # Validate required env vars
    missing = [
        name for name in ("LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "GROQ_API_KEY")
        if not (os.getenv(name) or "").strip()
    ]
    if missing:
        raise ValueError(f"Missing required env vars: {', '.join(missing)}")

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        )
    )
