package io.livekit.android.example.voiceassistant.viewmodel

import android.app.Application
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
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

class VoiceAssistantViewModel(application: Application, savedStateHandle: SavedStateHandle) : AndroidViewModel(application) {

    val room = LiveKit.create(application)
    val selectedModel: String

    val tokenSource: TokenSource

    init {
        val route = savedStateHandle.toRoute<VoiceAssistantRoute>()
        selectedModel = route.model

        tokenSource = if (route.sandboxId.isNotEmpty()) {
            TokenSource.fromSandboxTokenServer(sandboxId = route.sandboxId).cached()
        } else if (route.hardcodedToken.isNotEmpty()) {
            TokenSource.fromLiteral(route.hardcodedUrl, route.hardcodedToken).cached()
        } else {
            // Use OpenCEO token server
            TokenSource.fromCustom { _ -> Result.success(fetchTokenFromServer(model = route.model)) }.cached()
        }
    }

    private fun fetchTokenFromServer(model: String): io.livekit.android.token.TokenSourceResponse {
        val client = OkHttpClient()
        val body = Gson().toJson(
            mapOf(
                "user_id" to DEFAULT_USER_ID,
                "room" to DEFAULT_ROOM,
                "project" to DEFAULT_PROJECT,
                "model" to model,
            )
        )

        val request = Request.Builder()
            .url(TOKEN_SERVER_URL)
            .post(body.toRequestBody("application/json".toMediaType()))
            .build()

        val response = client.newCall(request).execute()
        val responseBody = response.body?.string() ?: throw Exception("Empty token response")

        val data = Gson().fromJson(responseBody, TokenResponse::class.java)
        return io.livekit.android.token.TokenSourceResponse(
            serverUrl = data.url ?: LIVEKIT_URL,
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
        val token: String,
        val url: String?,
    )
}
