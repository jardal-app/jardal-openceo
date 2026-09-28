"""Shared media transcription tool: audio extraction (yt-dlp) + Groq Whisper STT.

Source-agnostic: accepts any URL yt-dlp supports (TikTok, YouTube, Instagram,
X/Twitter, Facebook, Vimeo, direct media files, ...). Nothing here is tied to a
specific platform.

Reusable by any agent. Two entry points:

    transcribe_urls(urls, ...)          -> in-process, returns a flat summary dict
    transcribe_links_file(path, ...)    -> batch from a one-link-per-line file

CLI (also usable as a standalone script):

    python3 tools/media_transcribe.py --links tmp/links.txt --out tmp/out.txt

Audio is cached per video id while a batch runs, so an interrupted batch never
re-downloads. Files are deleted after each successful transcript unless
--keep-audio is passed.

Credentials resolve from the process env, then openceo-core/.env. There is
deliberately NO fallback to openceo-voice/.env so this tool's Groq usage stays on
its own account, separate from the voice pipeline's billing.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = CORE_DIR / ".env"

# Groq accepts these containers without re-encoding; m4a/webm is the ffmpeg-less fallback.
AUDIO_EXTS = (".mp3", ".m4a", ".webm", ".opus", ".ogg", ".wav", ".mp4", ".aac", ".flac")

DEFAULT_MODEL = "whisper-large-v3-turbo"
DEFAULT_SLEEP_SECONDS = 3.0  # free tier allows ~20 requests/min
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # Groq free-tier per-file limit
MARKER_PREFIX = "-----"


def _read_env_file(path: Path) -> dict[str, str]:
    """Minimal .env reader. Values are returned verbatim (no quotes, no expansion)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def resolve_secret(name: str) -> str:
    """Resolve a credential from the process env, then from openceo-core/.env only."""
    from_env = os.environ.get(name, "").strip()
    if from_env:
        return from_env
    return _read_env_file(ENV_FILE).get(name, "").strip()


def load_groq_api_key() -> str:
    """GROQ_API_KEY for Whisper STT. Sole source: process env or openceo-core/.env.

    No fallback to openceo-voice/.env: this tool must bill its own Groq account.
    """
    return resolve_secret("GROQ_API_KEY")


def ffmpeg_available() -> bool:
    from shutil import which

    return which("ffmpeg") is not None


def impersonation_available() -> bool:
    """TikTok requires TLS impersonation; yt-dlp needs curl_cffi to provide a target."""
    try:
        import curl_cffi  # noqa: F401

        return True
    except ImportError:
        return False


def build_client(api_key: str | None = None):
    """Return an authenticated Groq client, or raise with a clear message."""
    key = (api_key or load_groq_api_key()).strip()
    if not key:
        raise RuntimeError(
            "Falta GROQ_API_KEY. Agrega 'GROQ_API_KEY=tu_clave' en openceo-core/.env "
            "(o exportala en el entorno)."
        )
    try:
        from groq import Groq
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("Falta el paquete 'groq'. Instala con: pip install groq") from exc
    return Groq(api_key=key)


def _ytdlp():
    try:
        import yt_dlp

        return yt_dlp
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("Falta el paquete 'yt-dlp'. Instala con: pip install yt-dlp") from exc


def _base_options() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
    }


def probe_media(url: str) -> dict:
    """Fetch metadata only (no download). Flat output: id, title, uploader, duration."""
    yt_dlp = _ytdlp()
    with yt_dlp.YoutubeDL({**_base_options(), "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist":
        info = (info.get("entries") or [{}])[0]
    return {
        "id": str(info.get("id") or ""),
        "title": str(info.get("title") or ""),
        "uploader": str(info.get("uploader") or info.get("channel") or ""),
        "duration": int(info.get("duration") or 0),
    }


def _remove_audio(audio_file: Path) -> None:
    """Delete one downloaded audio file, ignoring filesystem races."""
    try:
        audio_file.unlink(missing_ok=True)
    except OSError:
        pass


def _find_cached_audio(audio_dir: Path, video_id: str) -> Path | None:
    for ext in AUDIO_EXTS:
        candidate = audio_dir / f"{video_id}{ext}"
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    for match in glob.glob(str(audio_dir / f"{video_id}.*")):
        candidate = Path(match)
        if candidate.suffix.lower() in AUDIO_EXTS and candidate.stat().st_size > 0:
            return candidate
    return None


def download_audio(url: str, video_id: str, audio_dir: Path) -> Path:
    """Download the audio track to audio_dir, reusing an existing file when present.

    Re-encodes to 64 kbps MP3 when ffmpeg is available; otherwise keeps the native
    container (m4a/webm), which Groq also accepts.
    """
    audio_dir.mkdir(parents=True, exist_ok=True)
    cached = _find_cached_audio(audio_dir, video_id)
    if cached:
        return cached

    yt_dlp = _ytdlp()
    use_ffmpeg = ffmpeg_available()
    options = {
        **_base_options(),
        "format": "bestaudio/best" if use_ffmpeg else "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(audio_dir / "%(id)s.%(ext)s"),
    }
    if use_ffmpeg:
        options["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "64"}
        ]

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

    downloaded = (info.get("requested_downloads") or [{}])[0].get("filepath")
    if downloaded and Path(downloaded).is_file():
        resolved = Path(downloaded)
        if resolved.suffix.lower() in AUDIO_EXTS:
            return resolved

    cached = _find_cached_audio(audio_dir, video_id)
    if cached:
        return cached
    raise RuntimeError(f"No se pudo resolver el archivo de audio para {video_id}")


def transcribe_audio(
    audio_path: Path,
    *,
    client=None,
    model: str = DEFAULT_MODEL,
    language: str | None = None,
    prompt: str | None = None,
    max_attempts: int = 4,
) -> str:
    """Send one audio file to Groq Whisper and return the transcript text."""
    size = audio_path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise RuntimeError(
            f"El audio pesa {size / 1048576:.1f} MB y supera el limite de "
            f"{MAX_UPLOAD_BYTES // 1048576} MB del tier de Groq."
        )

    client = client or build_client()
    payload = {"model": model, "response_format": "text"}
    if language:
        payload["language"] = language
    if prompt:
        payload["prompt"] = prompt

    delay = 5.0
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with audio_path.open("rb") as handle:
                result = client.audio.transcriptions.create(file=(audio_path.name, handle.read()), **payload)
            return result if isinstance(result, str) else str(getattr(result, "text", result))
        except Exception as exc:  # rate limits, transient 5xx, network blips
            last_error = exc
            status = getattr(exc, "status_code", None)
            retriable = status in (408, 429, 500, 502, 503, 504) or "rate limit" in str(exc).lower()
            if not retriable or attempt == max_attempts:
                break
            print(f"   ... reintento {attempt}/{max_attempts - 1} en {delay:.0f}s ({exc})")
            time.sleep(delay)
            delay = min(delay * 2, 60.0)

    raise RuntimeError(f"Falló la transcripción de {audio_path.name}: {last_error}")


def _load_done_urls(out_file: Path) -> set[str]:
    """URLs already transcribed successfully in a previous run (resume support)."""
    if not out_file.is_file():
        return set()
    done: set[str] = set()
    for match in re.finditer(rf"^{MARKER_PREFIX} (?!ERROR )(.+?) {MARKER_PREFIX}$", out_file.read_text(encoding="utf-8", errors="replace"), re.M):
        done.add(match.group(1).strip())
    return done


def _read_links(links_file: Path) -> list[str]:
    if not links_file.is_file():
        raise FileNotFoundError(f"No se encontró el archivo de links: {links_file}")
    lines = links_file.read_text(encoding="utf-8", errors="replace").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def transcribe_urls(
    urls: list[str],
    *,
    out_file: str | Path,
    audio_dir: str | Path | None = None,
    model: str = DEFAULT_MODEL,
    language: str | None = None,
    prompt: str | None = None,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    resume: bool = True,
    delete_audio: bool = True,
) -> dict:
    """Download and transcribe each URL, appending results to a single output file.

    Audio for a URL that transcribed successfully is deleted right away; audio
    from a failed URL is kept so a re-run can retry without re-downloading.

    Returns a flat summary: total, ok, failed, skipped, output.
    """
    out_path = Path(out_file).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path_dir = Path(audio_dir).expanduser().resolve() if audio_dir else out_path.parent / "audios"
    audio_path_dir.mkdir(parents=True, exist_ok=True)

    client = build_client()
    done = _load_done_urls(out_path) if resume else set()
    if done:
        print(f"Reanudando: {len(done)} enlace(s) ya transcritos en {out_path.name}")

    totals = {"total": len(urls), "ok": 0, "failed": 0, "skipped": 0, "output": str(out_path)}
    if not urls:
        print(f"No hay enlaces que procesar. Se esperaba uno por línea en el archivo de links.")
        return totals

    if not ffmpeg_available():
        print("Aviso: ffmpeg no está en el PATH; se descargará el audio en su contenedor original.")
    if not impersonation_available():
        print(
            "Aviso: falta 'curl_cffi'. Sitios con detección de bots (TikTok, Instagram) exigen\n"
            "        impersonación TLS y fallarán. Instálalo con: pip install curl_cffi"
        )

    print(f"Se procesarán {len(urls)} enlace(s). Audios en: {audio_path_dir}\n")
    mode = "a" if (resume and out_path.exists()) else "w"
    with out_path.open(mode, encoding="utf-8") as out:
        for index, url in enumerate(urls, 1):
            if url in done:
                totals["skipped"] += 1
                print(f"[{index}/{len(urls)}] ⏩ Ya transcrito: {url}")
                continue

            print(f"[{index}/{len(urls)}] Procesando: {url}")
            try:
                meta = probe_media(url)
                if not meta["id"]:
                    raise RuntimeError("yt-dlp no devolvió un id de video")
                reused = _find_cached_audio(audio_path_dir, meta["id"]) is not None
                audio_file = download_audio(url, meta["id"], audio_path_dir)
                print(f"   audio: {audio_file.name} ({audio_file.stat().st_size / 1048576:.1f} MB){' [cache]' if reused else ''}")
                text = transcribe_audio(
                    audio_file, client=client, model=model, language=language, prompt=prompt
                ).strip()
                out.write(f"{MARKER_PREFIX} {url} {MARKER_PREFIX}\n{text}\n\n")
                out.flush()
                totals["ok"] += 1
                print("   ✅ Transcripción guardada.")
                if delete_audio:
                    _remove_audio(audio_file)
                if sleep_seconds and index < len(urls):
                    time.sleep(sleep_seconds)
            except Exception as exc:
                totals["failed"] += 1
                out.write(f"{MARKER_PREFIX} ERROR {url} {MARKER_PREFIX}\n{exc}\n\n")
                out.flush()
                print(f"   ❌ Error: {exc}")

    if delete_audio:
        try:
            audio_path_dir.rmdir()  # only succeeds when nothing was left behind
        except OSError:
            print(f"Quedan audios sin borrar (fallos) en: {audio_path_dir}")

    print(f"\n🎉 Listo. {totals['ok']} ok, {totals['failed']} con error, {totals['skipped']} omitidos.")
    print(f"Transcripciones en: {out_path}")
    return totals


def transcribe_links_file(
    links_file: str | Path,
    *,
    out_file: str | Path,
    audio_dir: str | Path | None = None,
    **kwargs,
) -> dict:
    """Batch entry point: read one URL per line and transcribe them all into out_file."""
    urls = _read_links(Path(links_file).expanduser().resolve())
    if not urls:
        print(f"El archivo {links_file} no contiene enlaces.")
    return transcribe_urls(urls, out_file=out_file, audio_dir=audio_dir, **kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="media_transcribe",
        description="Descarga el audio de uno o varios enlaces y los transcribe con Groq Whisper.",
        epilog="Ejemplo: python3 tools/media_transcribe.py --links tmp/links.txt --out tmp/out.txt",
    )
    parser.add_argument("urls", nargs="*", help="Enlaces sueltos (alternativa a --links)")
    parser.add_argument("--links", help="Archivo con un enlace por línea (TikTok, YouTube, Instagram, cualquiera soportado por yt-dlp)")
    parser.add_argument("--out", help="Archivo único de salida (default: stdout)")
    parser.add_argument("--audio-dir", help="Carpeta de audios descargados (default: <out>/audios)")
    parser.add_argument("--model", default=os.environ.get("GROQ_STT_MODEL", DEFAULT_MODEL))
    parser.add_argument("--language", default=os.environ.get("GROQ_STT_LANGUAGE") or None,
                        help="Forzar idioma, ej. 'es'. Por defecto se autodetecta.")
    parser.add_argument("--prompt", help="Contexto opcional para sesgar la transcripción")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_SECONDS,
                        help="Pausa entre transcripciones (limite del tier gratuito: ~20/min)")
    parser.add_argument("--no-resume", action="store_true", help="Reprocesar todo, ignorando lo ya transcrito")
    parser.add_argument("--keep-audio", action="store_true",
                        help="Conservar los audios descargados (por defecto se borran tras transcribir)")
    parser.add_argument("--probe-only", action="store_true", help="Solo leer metadatos, sin descargar ni transcribir")
    args = parser.parse_args(argv)

    if not args.out:
        print("Error: --out es obligatorio.", file=sys.stderr)
        return 2

    common = {
        "out_file": args.out,
        "audio_dir": args.audio_dir,
        "model": args.model,
        "language": args.language,
        "prompt": args.prompt,
        "sleep_seconds": args.sleep,
        "resume": not args.no_resume,
        "delete_audio": not args.keep_audio,
    }

    try:
        if args.probe_only:
            urls = args.urls or (_read_links(Path(args.links).expanduser().resolve()) if args.links else [])
            for url in urls:
                print(json.dumps({"url": url, **probe_media(url)}, ensure_ascii=False))
            return 0
        if args.links:
            summary = transcribe_links_file(args.links, **common)
        elif args.urls:
            summary = transcribe_urls(args.urls, **common)
        else:
            print("Error: pasa uno o más enlaces, o usa --links archivo.txt", file=sys.stderr)
            return 2
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    return 0 if summary.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
