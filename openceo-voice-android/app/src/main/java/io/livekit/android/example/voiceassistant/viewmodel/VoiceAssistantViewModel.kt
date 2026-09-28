package io.livekit.android.example.voiceassistant.viewmodel

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.SavedStateHandle
import androidx.navigation.toRoute
import com.google.gson.Gson
import io.livekit.android.LiveKit
import io.livekit.android.example.voiceassistant.DEFAULT_PROJECT
import io.livekit.android.example.voiceassistant.DEFAULT_ROOM
import io.livekit.android.example.voiceassistant.DEFAULT_USER_ID
import io.livekit.android.example.voiceassistant.LIVEKIT_URL
import io.livekit.android.example.voiceassistant.TOKEN_SERVER_URL
import io.livekit.android.example.voiceassistant.screen.VoiceAssistantRoute
import io.livekit.android.token.TokenSource
import io.livekit.android.token.cached
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

class VoiceAssistantViewModel(application: Application, savedStateHandle: SavedStateHandle) : AndroidViewModel(application) {

    private val tag = "VoiceAssistantVM"
    val room = LiveKit.create(application)
    val selectedModel: String

    val tokenSource: TokenSource

    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .writeTimeout(15, TimeUnit.SECONDS)
        .build()

    init {
        val route = savedStateHandle.toRoute<VoiceAssistantRoute>()
        selectedModel = route.model

        tokenSource = if (route.sandboxId.isNotEmpty()) {
            TokenSource.fromSandboxTokenServer(sandboxId = route.sandboxId).cached()
        } else if (route.hardcodedToken.isNotEmpty()) {
            TokenSource.fromLiteral(route.hardcodedUrl, route.hardcodedToken).cached()
        } else {
            // Use OpenCEO token server
            TokenSource.fromCustom { _ ->
                withContext(Dispatchers.IO) {
                    runCatching {
                        fetchTokenFromServer(model = route.model, project = route.project)
                    }
                }
            }.cached()
        }
    }

    private fun fetchTokenFromServer(model: String, project: String): io.livekit.android.token.TokenSourceResponse {
        Log.d(tag, "Fetching voice token from $TOKEN_SERVER_URL for user=$DEFAULT_USER_ID, project=$project, model=$model")
        val body = Gson().toJson(
            mapOf(
                "user_id" to DEFAULT_USER_ID,
                "room" to DEFAULT_ROOM,
                "project" to project,
                "model" to model,
            )
        )

        val request = Request.Builder()
            .url(TOKEN_SERVER_URL)
            .post(body.toRequestBody("application/json".toMediaType()))
            .build()

        val response = httpClient.newCall(request).execute()
        val responseBody = response.body?.string() ?: throw Exception("Respuesta vacía del servidor de tokens")

        if (!response.isSuccessful) {
            throw Exception("Error del servidor de tokens (${response.code}): $responseBody")
        }

        val data = Gson().fromJson(responseBody, TokenResponse::class.java)
            ?: throw Exception("No se pudo decodificar el token recibido")

        if (data.token.isNullOrEmpty()) {
            throw Exception("El token recibido está vacío")
        }

        val serverUrl = data.url ?: LIVEKIT_URL
        Log.d(tag, "Token recibido con éxito. Conectando a $serverUrl")

        return io.livekit.android.token.TokenSourceResponse(
            serverUrl = serverUrl,
            participantToken = data.token,
            roomName = DEFAULT_ROOM,
            participantName = DEFAULT_USER_ID,
        )
    }

    override fun onCleared() {
        super.onCleared()
        room.disconnect()
        room.release()
    }

    private data class TokenResponse(
        val token: String?,
        val url: String?,
    )
}
