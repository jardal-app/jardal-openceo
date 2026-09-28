#!/usr/bin/env python3
import sys
import os
import json
import urllib.request
import urllib.error

API_URL = os.environ.get("OPENCEO_API_URL", "http://127.0.0.1:18765/v1/chat/completions")
API_KEY = os.environ.get("OPENCEO_API_KEY", "sk-openceo-7c7e2dcb15e530f5a6a2e9c3f9e71310a25179971a1ddc47")
USER_ID = os.environ.get("OPENCEO_USER", "jorge")
MODEL = os.environ.get("OPENCEO_MODEL", "ceo")

def main():
    if len(sys.argv) > 1:
        # Single command execution mode: ./scripts/chat.sh "crea una sesion dev"
        user_input = " ".join(sys.argv[1:]).strip()
        send_message(user_input)
        return

    print("\033[1;34m╔══════════════════════════════════════════════════════╗\033[0m")
    print("\033[1;34m║            OpenCEO Terminal Chat Test                ║\033[0m")
    print("\033[1;34m╚══════════════════════════════════════════════════════╝\033[0m")
    print(f"• Modelo: \033[1;32m{MODEL}\033[0m  • Usuario: \033[1;32m{USER_ID}\033[0m  • Backend: \033[1;32m{API_URL}\033[0m")
    print("Escribe tus órdenes tal como las hablarías (o 'salir' / Ctrl+C para terminar).\n")

    while True:
        try:
            user_input = input("\033[1;33mTú > \033[0m").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\033[1;30mHasta luego.\033[0m")
            break

        if not user_input:
            continue
        if user_input.lower() in ("salir", "exit", "quit", "q"):
            print("\033[1;30mHasta luego.\033[0m")
            break

        send_message(user_input)

def send_message(user_input: str):
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": user_input}],
        "user": USER_ID,
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            print(f"\033[1;36mOpenCEO > \033[0m{content}\n")
    except urllib.error.HTTPError as e:
        print(f"\033[1;31mError HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')}\033[0m\n")
    except Exception as e:
        print(f"\033[1;31mError: {e}\033[0m\n")

if __name__ == "__main__":
    main()
