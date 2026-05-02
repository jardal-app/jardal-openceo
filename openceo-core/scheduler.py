from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from dev_session_service import DevSessionService
from settings import Settings
from task_service import TaskService

logger = logging.getLogger(__name__)


class WaitingTaskScheduler:
    """Periodic checker for waiting tasks that reached resume_at."""

    def __init__(
        self,
        settings: Settings,
        task_service: TaskService,
        dev_session_service: DevSessionService,
    ) -> None:
        self._settings = settings
        self._task_service = task_service
        self._dev_session_service = dev_session_service
        self._job: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._job is not None and not self._job.done():
            return
        self._stop.clear()
        # Start the Notion mirror queue worker (1 req/s)
        self._task_service.start_notion_worker()
        # One-time startup reconcile for orphaned tasks
        await self._task_service.startup_reconcile_notion_mirrors()
        self._job = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._job is not None:
            self._job.cancel()
            with suppress(asyncio.CancelledError):
                await self._job
        await self._task_service.stop_notion_worker()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self._task_service.resume_waiting_tasks()
            await self._task_service.resume_queued_tickets()
            await asyncio.sleep(max(5, self._settings.WAITING_POLL_SECONDS))
