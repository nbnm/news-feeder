import asyncio
import logging

import httpx
from sqlalchemy import select

from .digests import build_digest, cutoff_at, daily_delivery_key, daily_due, preferences, prepare_daily_digest
from .gateway import effective_settings
from .models import Article, Delivery, aware, now
from .pipeline import refresh
from .relevance import process_relevance
from .telegram import deliver, format_articles, poll_commands
from .telegram_config import effective_settings as telegram_settings

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
                if daily_due(now(), prefs):
                    prepare_daily_digest(session)
                    await self._publish_daily(session)
                else:
                    build_digest(session, cutoff_at(now(), prefs))
                await poll_commands(session, telegram_settings(self.settings, session))

    def _delivery_due(self, session, force=False):
        prefs = preferences(session)
        current = telegram_settings(self.settings, session)
        if not current.telegram_ready:
            return None, "unconfigured"
        if not force and (not prefs.telegram_enabled or not daily_due(now(), prefs)):
            return None, "not_due"
        key = daily_delivery_key(now(), prefs, current.telegram_chat_id)
        delivery = session.scalar(select(Delivery).where(Delivery.dedupe_key == key))
        if delivery and delivery.state in {"sent", "sending", "uncertain"}:
            return delivery, "already_claimed"
        if delivery and delivery.telegram_record and delivery.telegram_record.retry_at:
            if aware(delivery.telegram_record.retry_at) > now():
                return delivery, "retry_scheduled"
        return delivery, None

    async def _publish_daily(self, session, force=False):
        existing, reason = self._delivery_due(session, force)
        if reason:
            return self.delivery_result(existing, reason)
        prefs = preferences(session)
        current = telegram_settings(self.settings, session)
        timestamp = now()
        digest = prepare_daily_digest(session, timestamp)
        articles = [session.get(Article, article_id) for article_id in digest.article_ids]
        from zoneinfo import ZoneInfo

        date = timestamp.astimezone(ZoneInfo(prefs.timezone)).date().isoformat()
        title = f"News Feeder · Daily digest · {date}"
        if self.worker_error or (self.last_result and self.last_result.get("errors")):
            title += "\nSome sources could not refresh; this edition uses available stories."
        messages = format_articles(
            [a for a in articles if a],
            title,
            prefs,
            channel=current.telegram_target_type == "channel",
        )
        result = await deliver(
            session, current, messages, daily_delivery_key(timestamp, prefs, current.telegram_chat_id)
        )
        return self.delivery_result(result)

    @staticmethod
    def delivery_result(delivery, reason=None):
        return {
            "state": delivery.state if delivery else "skipped",
            "sent_count": delivery.sent_count if delivery else 0,
            "total_count": delivery.total_count if delivery else 0,
            "error": delivery.error if delivery else None,
            "reason": reason,
        }

    async def publish_daily(self, force=False):
        with self.sessions() as session:
            existing, reason = self._delivery_due(session, force)
            if reason:
                return self.delivery_result(existing, reason)
        # Headless runs collect before freezing membership so fresh discoveries
        # are included even when collection finishes after the scheduled hour.
        try:
            await asyncio.wait_for(self.refresh(), timeout=600)
        except TimeoutError:
            self.worker_error = "Collection timed out; using available stories"
        async with self.lock:
            with self.sessions() as session:
                return await self._publish_daily(session, force)

    async def run(self):
        while True:
            try:
                await self.tick()
                self.worker_error = None
            except Exception as error:
                self.worker_error = type(error).__name__
                logger.error("Worker tick failed: %s", type(error).__name__)
            await asyncio.sleep(30)
