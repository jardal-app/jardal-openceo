from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from hashlib import md5
from pathlib import Path
from typing import Any

from settings import Settings


class KimiSessionManager:
    """Create and manage Kimi CLI sessions programmatically for QA review workflow.

    Kimi stores sessions under ~/.kimi/sessions/{work_dir_hash}/{session_id}/
    where work_dir_hash = md5(work_directory_path.encode()).hexdigest()

    IMPORTANT: We do NOT modify kimi.json (the user's global metadata file).
    Kimi Web scans ~/.kimi/sessions/ directly, so sessions appear in the UI
    as long as the directory structure is valid.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()
        configured = os.environ.get("KIMI_SHARE_DIR", "").strip()
        self._share_dir = Path(configured).expanduser() if configured else (Path.home() / ".kimi")

    def _work_dir_hash(self, work_dir: str) -> str:
        """Compute the same hash Kimi uses for a work directory."""
        return md5(work_dir.encode("utf-8")).hexdigest()

    def _session_dir(self, work_dir: str, session_id: str) -> Path:
        h = self._work_dir_hash(work_dir)
        return self._share_dir / "sessions" / h / session_id

    def _find_any_existing_system_prompt(self) -> dict[str, Any] | None:
        """Borrow the _system_prompt from any existing Kimi session.

        Kimi Web requires a valid _system_prompt as the first line of
        context.jsonl. Rather than hard-coding a fake one, we copy it from
        an existing session on disk.
        """
        sessions_root = self._share_dir / "sessions"
        if not sessions_root.exists():
            return None
        # Walk existing sessions and grab the first _system_prompt we find
        for work_dir_hash_dir in sessions_root.iterdir():
            if not work_dir_hash_dir.is_dir():
                continue
            for session_dir in work_dir_hash_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                context_path = session_dir / "context.jsonl"
                if not context_path.exists():
                    continue
                try:
                    with open(context_path, encoding="utf-8") as f:
                        first_line = f.readline()
                        if not first_line:
                            continue
                        record = json.loads(first_line)
                        if record.get("role") == "_system_prompt":
                            return record
                except Exception:  # noqa: BLE001, S112
                    continue
        return None

    def _build_initial_context(
        self,
        ticket_number: int | None,
        title: str,
        description: str,
        modified_files: list[str],
        dev_summary: str,
        qa_feedback: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build the context.jsonl entries that prime the Kimi session."""
        context_entries: list[dict[str, Any]] = []

        # 1. System prompt (borrowed from an existing session so Kimi Web recognises it)
        system_prompt = self._find_any_existing_system_prompt()
        if system_prompt is not None:
            context_entries.append(system_prompt)
        context_entries.append({"role": "_checkpoint", "id": 0})

        # 2. Build the context message
        files_text = "\n".join(f"- {f}" for f in modified_files) if modified_files else "No se registraron archivos modificados."
        ticket_ref = f"Ticket #{ticket_number}" if ticket_number else "Ticket"

        context_text = (
            f"# Contexto de revisión QA - {ticket_ref}: {title}\n\n"
            f"## Descripción del ticket\n{description}\n\n"
            f"## Archivos modificados\n{files_text}\n\n"
            f"## Resumen del trabajo realizado\n{dev_summary or 'No hay resumen disponible.'}\n\n"
        )
        if qa_feedback:
            context_text += f"## Feedback del QA\n{qa_feedback}\n\n"

        context_text += (
            "---\n"
            "Eres un asistente de revisión QA. El ticket ya fue resuelto automáticamente. "
            "Tu tarea es ayudar al QA a verificar la calidad de la implementación. "
            "Puedes analizar los archivos modificados, sugerir mejoras, o ayudar a aplicar correcciones."
        )

        context_entries.append({
            "role": "user",
            "content": [{"type": "text", "text": context_text}],
        })
        context_entries.append({"role": "_checkpoint", "id": 1})
        context_entries.append({"role": "_usage", "token_count": len(context_text.split())})

        return context_entries

    def create_qa_session(
        self,
        work_dir: str,
        ticket_number: int | None,
        title: str,
        description: str,
        modified_files: list[str],
        dev_summary: str,
        qa_feedback: str | None = None,
    ) -> dict[str, Any]:
        """Create a new Kimi CLI session primed with ticket context.

        Returns {"session_id": str, "session_dir": str, "url": str}
        """
        return self._create_session_impl(
            work_dir=work_dir,
            custom_title=f"QA #{ticket_number}" if ticket_number else f"QA: {title[:40]}",
            context_entries=self._build_initial_context(
                ticket_number=ticket_number,
                title=title,
                description=description,
                modified_files=modified_files,
                dev_summary=dev_summary,
                qa_feedback=qa_feedback,
            ),
        )

    def create_qa_session_with_prompt(
        self,
        work_dir: str,
        custom_title: str,
        context_prompt: str,
    ) -> dict[str, Any]:
        """Create a new Kimi session with a pre-built rich context prompt.

        Returns {"session_id": str, "session_dir": str, "url": str}
        """
        context_entries = self._build_initial_context_from_prompt(context_prompt)
        return self._create_session_impl(
            work_dir=work_dir,
            custom_title=custom_title[:40] if custom_title else "QA Review",
            context_entries=context_entries,
        )

    def _create_session_impl(
        self,
        work_dir: str,
        custom_title: str,
        context_entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        sdir = self._session_dir(work_dir, session_id)
        sdir.mkdir(parents=True, exist_ok=True)

        state = {
            "version": 1,
            "approval": {
                "yolo": False,
                "auto_approve_actions": ["run command"],
            },
            "additional_dirs": [],
            "custom_title": custom_title,
            "title_generated": True,
            "title_generate_attempts": 1,
            "plan_mode": False,
            "plan_session_id": None,
            "plan_slug": None,
            "wire_mtime": None,
            "archived": False,
            "archived_at": None,
            "auto_archive_exempt": False,
            "todos": [],
        }
        with open(sdir / "state.json", "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

        with open(sdir / "context.jsonl", "w", encoding="utf-8") as f:
            for entry in context_entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        with open(sdir / "wire.jsonl", "w", encoding="utf-8") as f:
            pass

        kimi_web_base = os.environ.get("KIMI_WEB_BASE_URL", "https://kimi.jardal.app")
        url = f"{kimi_web_base}/"

        return {
            "session_id": session_id,
            "session_dir": str(sdir),
            "url": url,
        }

    def _build_initial_context_from_prompt(
        self,
        context_prompt: str,
    ) -> list[dict[str, Any]]:
        """Build context.jsonl from a pre-built rich prompt text."""
        context_entries: list[dict[str, Any]] = []

        system_prompt = self._find_any_existing_system_prompt()
        if system_prompt is not None:
            context_entries.append(system_prompt)
        context_entries.append({"role": "_checkpoint", "id": 0})

        context_entries.append({
            "role": "user",
            "content": [{"type": "text", "text": context_prompt}],
        })
        context_entries.append({"role": "_checkpoint", "id": 1})
        context_entries.append({"role": "_usage", "token_count": len(context_prompt.split())})

        return context_entries

    def touch_existing_session(self, work_dir: str, session_id: str) -> dict[str, Any]:
        """Bump the mtime of an existing session so it floats to the top in the UI.

        Used when the ticket was already developed with Kimi — no need to
        recreate context, just refresh the session timestamp.
        """
        sdir = self._session_dir(work_dir, session_id)
        now = datetime.now(timezone.utc).timestamp()
        for fname in ("state.json", "context.jsonl", "wire.jsonl"):
            fpath = sdir / fname
            if fpath.exists():
                os.utime(fpath, (now, now))

        # Also update the title in state.json to reflect the QA focus
        state_path = sdir / "state.json"
        if state_path.exists():
            try:
                with open(state_path, encoding="utf-8") as f:
                    state = json.load(f)
                if not state.get("custom_title", "").startswith("QA "):
                    state["custom_title"] = f"QA {state.get('custom_title', 'Session')}"
                    with open(state_path, "w", encoding="utf-8") as f:
                        json.dump(state, f, indent=2, ensure_ascii=False)
            except Exception:  # noqa: BLE001, S110
                pass

        kimi_web_base = os.environ.get("KIMI_WEB_BASE_URL", "https://kimi.jardal.app")
        url = f"{kimi_web_base}/"

        return {
            "session_id": session_id,
            "session_dir": str(sdir),
            "url": url,
        }

    def set_session_title(self, work_dir: str, session_id: str, title: str) -> bool:
        """Persist a human-readable title for an existing Kimi session."""
        normalized = title.strip()
        if not normalized:
            return False

        state_path = self._session_dir(work_dir, session_id) / "state.json"
        if not state_path.exists():
            return False

        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)

        truncated = normalized[:200]
        state["custom_title"] = truncated
        state["title"] = truncated
        state["title_generated"] = False
        state["title_generate_attempts"] = max(int(state.get("title_generate_attempts") or 0), 1)

        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        return True

    def session_exists(self, work_dir: str, session_id: str) -> bool:
        sdir = self._session_dir(work_dir, session_id)
        return (sdir / "state.json").exists() and (sdir / "context.jsonl").exists()
