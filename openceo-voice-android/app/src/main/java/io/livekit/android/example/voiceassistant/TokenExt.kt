package io.livekit.android.example.voiceassistant

data class VoiceModelOption(
    val id: String,
    val label: String,
)

data class ProjectOption(
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
const val DEFAULT_MODEL = "ceo"

val AVAILABLE_MODELS = listOf(
    VoiceModelOption(id = "ceo", label = "CEO (Supervisor & Voz)"),
    VoiceModelOption(id = "dev", label = "Dev (Programador)"),
    VoiceModelOption(id = "secretary", label = "Secretaría (Email & Doc)"),
    VoiceModelOption(id = "finance", label = "Finanzas"),
)

val AVAILABLE_PROJECTS = listOf(
    ProjectOption(id = "jardal-openceo", label = "jardal-openceo"),
    ProjectOption(id = "jardal-trading", label = "jardal-trading"),
    ProjectOption(id = "jardal-vpn", label = "jardal-vpn"),
    ProjectOption(id = "melanyryr", label = "melanyryr"),
    ProjectOption(id = "cebitec", label = "cebitec"),
)

// Legacy fields kept for route compatibility
const val sandboxID = ""
const val hardcodedUrl = LIVEKIT_URL
const val hardcodedToken = ""
