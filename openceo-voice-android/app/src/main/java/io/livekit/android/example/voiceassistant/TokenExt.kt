package io.livekit.android.example.voiceassistant

data class VoiceModelOption(
    val id: String,
    val label: String,
)

// LiveKit server URL (WSS)
const val LIVEKIT_URL = "wss://voice.openceo.jardal.app"

// Token server endpoint exposed by Traefik
const val TOKEN_SERVER_URL = "https://voice.openceo.jardal.app/api/voice/token"

// Default user/project for the voice session
const val DEFAULT_USER_ID = "jorge"
const val DEFAULT_PROJECT = "jardal-openceo"
const val DEFAULT_ROOM = "openceo-voice"
const val DEFAULT_MODEL = "dev"
val AVAILABLE_MODELS = listOf(
    VoiceModelOption(id = "dev", label = "Dev"),
    VoiceModelOption(id = "ceo", label = "CEO"),
    VoiceModelOption(id = "secretary", label = "Secretary"),
    VoiceModelOption(id = "finance", label = "Finance"),
)

// Legacy fields kept for route compatibility
const val sandboxID = ""
const val hardcodedUrl = LIVEKIT_URL
const val hardcodedToken = ""
