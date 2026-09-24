package io.livekit.android.example.voiceassistant.screen

import android.content.Context
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import io.livekit.android.example.voiceassistant.AVAILABLE_MODELS
import io.livekit.android.example.voiceassistant.AVAILABLE_PROJECTS
import io.livekit.android.example.voiceassistant.DEFAULT_MODEL
import io.livekit.android.example.voiceassistant.DEFAULT_PROJECT
import io.livekit.android.example.voiceassistant.R
import io.livekit.android.example.voiceassistant.hardcodedToken
import io.livekit.android.example.voiceassistant.hardcodedUrl
import io.livekit.android.example.voiceassistant.sandboxID
import io.livekit.android.example.voiceassistant.ui.theme.Blue500
import kotlinx.serialization.Serializable

@Serializable
object ConnectRoute

@Composable
fun ConnectScreen(
    navigateToVoiceAssistant: (VoiceAssistantRoute) -> Unit
) {
    val context = LocalContext.current
    val prefs = remember { context.getSharedPreferences("openceo_voice_prefs", Context.MODE_PRIVATE) }

    var selectedProject by rememberSaveable {
        mutableStateOf(prefs.getString("selected_project", DEFAULT_PROJECT) ?: DEFAULT_PROJECT)
    }
    var selectedModel by rememberSaveable {
        mutableStateOf(prefs.getString("selected_model", DEFAULT_MODEL) ?: DEFAULT_MODEL)
    }
    var autoConnect by rememberSaveable {
        mutableStateOf(prefs.getBoolean("auto_connect", false))
    }

    var isProjectMenuOpen by rememberSaveable { mutableStateOf(false) }
    var isModelMenuOpen by rememberSaveable { mutableStateOf(false) }
    var hasError by rememberSaveable { mutableStateOf(false) }
    var isConnecting by remember { mutableStateOf(false) }

    fun launchVoiceCall() {
        isConnecting = true
        hasError = false
        prefs.edit()
            .putString("selected_project", selectedProject)
            .putString("selected_model", selectedModel)
            .putBoolean("auto_connect", autoConnect)
            .apply()

        val route = VoiceAssistantRoute(
            sandboxId = sandboxID,
            hardcodedUrl = hardcodedUrl,
            hardcodedToken = hardcodedToken,
            model = selectedModel,
            project = selectedProject,
        )
        navigateToVoiceAssistant(route)
    }

    // Auto-connect on open if option enabled
    LaunchedEffect(Unit) {
        if (autoConnect && !isConnecting) {
            launchVoiceCall()
        }
    }

    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .padding(24.dp)
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
            modifier = Modifier.fillMaxWidth()
        ) {
            Image(
                painter = painterResource(R.drawable.connect_icon),
                contentDescription = "OpenCEO Voice Icon",
                modifier = Modifier.size(80.dp)
            )

            Spacer(Modifier.size(16.dp))

            Text(
                text = "OpenCEO Voice Assistant",
                style = MaterialTheme.typography.titleLarge.copy(
                    fontWeight = FontWeight.Bold,
                    fontSize = 22.sp
                ),
                color = MaterialTheme.colorScheme.onBackground,
                textAlign = TextAlign.Center,
            )

            Spacer(Modifier.size(6.dp))

            Text(
                text = "Control por voz natural con DeepSeek v4 Flash, tickets, presets y sesiones dev.",
                textAlign = TextAlign.Center,
                fontSize = 13.sp,
                color = MaterialTheme.colorScheme.onBackground.copy(alpha = 0.7f),
                modifier = Modifier.fillMaxWidth(0.9f)
            )

            Spacer(Modifier.size(16.dp))

            AnimatedVisibility(hasError) {
                Text(
                    text = "Error de conexión. Verifica que el servidor de voz esté activo.",
                    color = Color.Red,
                    fontSize = 12.sp,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.fillMaxWidth(0.85f)
                )
            }

            Spacer(Modifier.size(12.dp))

            // Project selector
            Text(
                text = "Proyecto Foco",
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onBackground.copy(alpha = 0.8f),
            )
            Spacer(Modifier.size(4.dp))
            Box {
                OutlinedButton(
                    shape = RoundedCornerShape(12.dp),
                    onClick = { isProjectMenuOpen = true },
                    modifier = Modifier.fillMaxWidth(0.85f),
                ) {
                    Text(
                        text = AVAILABLE_PROJECTS.firstOrNull { it.id == selectedProject }?.label ?: selectedProject,
                        fontSize = 14.sp
                    )
                }

                DropdownMenu(
                    expanded = isProjectMenuOpen,
                    onDismissRequest = { isProjectMenuOpen = false },
                ) {
                    AVAILABLE_PROJECTS.forEach { option ->
                        DropdownMenuItem(
                            text = { Text(option.label) },
                            onClick = {
                                selectedProject = option.id
                                isProjectMenuOpen = false
                                prefs.edit().putString("selected_project", option.id).apply()
                            }
                        )
                    }
                }
            }

            Spacer(Modifier.size(12.dp))

            // Model selector
            Text(
                text = "Agente / Modo",
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onBackground.copy(alpha = 0.8f),
            )
            Spacer(Modifier.size(4.dp))
            Box {
                OutlinedButton(
                    shape = RoundedCornerShape(12.dp),
                    onClick = { isModelMenuOpen = true },
                    modifier = Modifier.fillMaxWidth(0.85f),
                ) {
                    Text(
                        text = AVAILABLE_MODELS.firstOrNull { it.id == selectedModel }?.label ?: selectedModel,
                        fontSize = 14.sp
                    )
                }

                DropdownMenu(
                    expanded = isModelMenuOpen,
                    onDismissRequest = { isModelMenuOpen = false },
                ) {
                    AVAILABLE_MODELS.forEach { option ->
                        DropdownMenuItem(
                            text = { Text(option.label) },
                            onClick = {
                                selectedModel = option.id
                                isModelMenuOpen = false
                                prefs.edit().putString("selected_model", option.id).apply()
                            }
                        )
                    }
                }
            }

            Spacer(Modifier.size(16.dp))

            // Auto-connect Switch
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween,
                modifier = Modifier.fillMaxWidth(0.85f)
            ) {
                Text(
                    text = "Conectar directo al abrir",
                    fontSize = 13.sp,
                    color = MaterialTheme.colorScheme.onBackground.copy(alpha = 0.9f)
                )
                Switch(
                    checked = autoConnect,
                    onCheckedChange = {
                        autoConnect = it
                        prefs.edit().putBoolean("auto_connect", it).apply()
                    },
                    colors = SwitchDefaults.colors(
                        checkedThumbColor = Color.White,
                        checkedTrackColor = Blue500,
                    )
                )
            }

            Spacer(Modifier.size(24.dp))

            val buttonColors = ButtonDefaults.buttonColors(
                containerColor = Blue500,
                contentColor = Color.White
            )
            Button(
                colors = buttonColors,
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier
                    .fillMaxWidth(0.85f)
                    .size(54.dp),
                onClick = { launchVoiceCall() }
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    AnimatedVisibility(isConnecting) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            CircularProgressIndicator(
                                modifier = Modifier.size(20.dp),
                                color = Color.White,
                                trackColor = Color.Gray,
                            )
                            Spacer(Modifier.size(8.dp))
                        }
                    }
                    Text(
                        text = if (isConnecting) "CONECTANDO..." else "HABLAR AHORA",
                        style = TextStyle(
                            fontFamily = FontFamily.Monospace,
                            fontWeight = FontWeight.Bold,
                            fontSize = 16.sp,
                            letterSpacing = 1.5.sp,
                        )
                    )
                }
            }
        }
    }
}
