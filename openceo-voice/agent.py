from __future__ import annotations

import json
import logging
import os
from typing import Any

from livekit.agents import Agent, AgentSession, AutoSubscribe, JobContext, JobProcess, WorkerOptions, cli
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


def _build_tts() -> openai.TTS | None:
    tts_key = (os.getenv("VOICE_TTS_API_KEY") or "").strip()
    if not tts_key:
        allow_no_tts = _env_bool("VOICE_ALLOW_NO_TTS", default=True)
        if not allow_no_tts:
            raise ValueError("VOICE_TTS_API_KEY is required when VOICE_ALLOW_NO_TTS=false")
        logger.warning("VOICE_TTS_API_KEY not configured; running without spoken replies.")
        return None

    tts_base = (os.getenv("VOICE_TTS_BASE_URL") or "https://api.openai.com/v1").strip()

    return openai.TTS(
        model=(os.getenv("VOICE_TTS_MODEL") or "gpt-4o-mini-tts").strip(),
        voice=(os.getenv("VOICE_TTS_VOICE") or "alloy").strip(),
        api_key=tts_key,
        base_url=tts_base,
        instructions=(os.getenv("VOICE_TTS_INSTRUCTIONS") or "Habla en español, claro y breve.").strip(),
    )


def _agent_instructions(model: str) -> str:
    return (
        "Eres el puente de voz de OpenCEO. "
        f"El modelo backend seleccionado por el usuario es '{model}'. "
        "Mantén el estilo breve, claro y operativo."
    )


def _ready_message(model: str) -> str:
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

    agent = Agent(
        instructions=_agent_instructions(model),
    )

    session = AgentSession(
        llm=llm,
        stt=stt,
        tts=tts,
        vad=vad,
        min_endpointing_delay=float(os.getenv("VOICE_MIN_ENDPOINTING_DELAY", "0.2")),
        max_endpointing_delay=float(os.getenv("VOICE_MAX_ENDPOINTING_DELAY", "0.8")),
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
