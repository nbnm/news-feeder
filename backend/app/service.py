import asyncio
import logging

import httpx

from .digests import build_digest, cutoff_at, preferences
from .gateway import effective_settings
from .models import Article, now
from .pipeline import refresh
from .relevance import process_relevance
from .telegram import deliver, format_articles, poll_commands

logger = logging.getLogger(__name__)


class Service:
    def __init__(self, settings, sessions):
        self.settings, self.sessions = settings, sessions
        self.lock = asyncio.Lock()
        self.collecting = False
        self.last_result = None
        self.worker_error = None
        self.refresh_task = None
        self.relevance_task = None
        self.ranking = False
        self.relevance_requested = False

    async def refresh(self, force=False):
        async with self.lock:
            self.collecting = True
            try:
                with self.sessions() as session:
                    current = effective_settings(self.settings, session)
                    self.last_result = await refresh(session, current, force)
                    self.ranking = True
                    async with httpx.AsyncClient() as client:
                        self.last_result["relevance_ready"] = await process_relevance(
                            session, current, client
                        )
            except Exception as error:
                self.worker_error = type(error).__name__
                logger.error("Collection failed: %s", type(error).__name__)
            finally:
                self.collecting = False
                self.ranking = False

    def start_refresh(self):
        if self.refresh_task and not self.refresh_task.done():
            return
        self.refresh_task = asyncio.create_task(self.refresh(force=True))

    def start_relevance(self):
        self.relevance_requested = True
        if self.relevance_task and not self.relevance_task.done():
            return
        self.relevance_task = asyncio.create_task(self.rescore())

    async def rescore(self):
        async with self.lock:
            self.ranking = True
            try:
                while self.relevance_requested:
                    self.relevance_requested = False
                    with self.sessions() as session:
                        async with httpx.AsyncClient() as client:
                            await process_relevance(
                                session,
                                effective_settings(self.settings, session),
                                client,
                                limit=500,
                                force=True,
                            )
            except Exception as error:
                self.worker_error = type(error).__name__
                logger.error("Relevance refresh failed: %s", type(error).__name__)
            finally:
                self.ranking = False

    async def tick(self):
        await self.refresh()
        async with self.lock:
            with self.sessions() as session:
                prefs = preferences(session)
                cutoff = cutoff_at(now(), prefs)
                digest = build_digest(session, cutoff)
                if prefs.telegram_enabled and self.settings.telegram_ready:
                    articles = [session.get(Article, article_id) for article_id in digest.article_ids]
                    await deliver(
                        session,
                        self.settings,
                        format_articles([a for a in articles if a], "News Feeder · Daily digest", prefs),
                        f"daily:{digest.id}",
                    )
                await poll_commands(session, self.settings)

    async def run(self):
        while True:
            try:
                await self.tick()
                self.worker_error = None
            except Exception as error:
                self.worker_error = type(error).__name__
                logger.error("Worker tick failed: %s", type(error).__name__)
            await asyncio.sleep(30)
