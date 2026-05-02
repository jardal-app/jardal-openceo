package io.livekit.android.example.voiceassistant.screen

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import io.livekit.android.example.voiceassistant.AVAILABLE_MODELS
import io.livekit.android.example.voiceassistant.DEFAULT_MODEL
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
    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Image(painter = painterResource(R.drawable.connect_icon), contentDescription = "Connect icon")

            Spacer(Modifier.size(16.dp))
            Text(
                text = buildAnnotatedString {
                    append("Inicia una llamada para hablar por voz con OpenCEO.")
                },
                textAlign = TextAlign.Center,
                modifier = Modifier.fillMaxWidth(0.8f)
            )

            var hasError by rememberSaveable { mutableStateOf(false) }
            var isConnecting by remember { mutableStateOf(false) }
            var isModelMenuOpen by rememberSaveable { mutableStateOf(false) }
            var selectedModel by rememberSaveable { mutableStateOf(DEFAULT_MODEL) }

            Spacer(Modifier.size(8.dp))

            AnimatedVisibility(hasError) {
                Text(
                    text = "Error de conexion. Verifica que el agente este corriendo e intenta de nuevo.",
                    color = Color.Red,
                    fontSize = 12.sp,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.fillMaxWidth(0.8f)
                )
            }

            Spacer(Modifier.size(16.dp))

            Text(
                text = "Modelo",
                style = MaterialTheme.typography.labelLarge,
                color = MaterialTheme.colorScheme.onBackground,
            )

            Spacer(Modifier.size(8.dp))

            Box {
                OutlinedButton(
                    shape = RoundedCornerShape(16.dp),
                    onClick = { isModelMenuOpen = true },
                    modifier = Modifier.fillMaxWidth(0.72f),
                ) {
                    Text(
                        text = AVAILABLE_MODELS.firstOrNull { it.id == selectedModel }?.label ?: selectedModel,
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
                            }
                        )
                    }
                }
            }

            Spacer(Modifier.size(24.dp))

            val buttonColors = ButtonDefaults.buttonColors(
                containerColor = Blue500,
                contentColor = Color.White
            )
            Button(
                colors = buttonColors,
                shape = RoundedCornerShape(20),
                onClick = {
                    isConnecting = true
                    hasError = false
                    val route = VoiceAssistantRoute(
                        sandboxId = sandboxID,
                        hardcodedUrl = hardcodedUrl,
                        hardcodedToken = hardcodedToken,
                        model = selectedModel,
                    )
                    navigateToVoiceAssistant(route)
                }
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
                        text = if (isConnecting) "CONECTANDO" else "INICIAR LLAMADA",
                        style = TextStyle(
                            fontFamily = FontFamily.Monospace,
                            letterSpacing = 2.sp,
                        )
                    )
                }
            }
        }
    }
}
