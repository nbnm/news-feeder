import asyncio
import hashlib
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from . import telegram_config
from .collectors import collect
from .config import Settings
from .database import database
from .digests import build_digest, digest_output, preferences
from .gateway import (
    GatewayError,
    GatewayUpdate,
    available_models,
    effective_settings,
    public_configuration,
    update_configuration,
)
from .models import (
    Article,
    Category,
    Delivery,
    Digest,
    Preference,
    Source,
    SourceCategory,
    SourceCollectionOptions,
    WorkerState,
    aware,
    now,
)
from .network import Fetcher
from .profile import JEV_MODEL
from .relevance import article_output, prioritized_feed
from .schemas import (
    ArticleOutput,
    CategoryInput,
    CategoryOutput,
    DailyPublishRequest,
    DigestOutput,
    Preferences,
    ReaderProfile,
    SendRequest,
    SourceInput,
    SourceOutput,
    TelegramRetryRequest,
)
from .seed import seed
from .service import Service
from .telegram import deliver, format_articles
from .telegram_config import TelegramBotUpdate, TelegramSetupError, TelegramUpdate
from .trending import source_output


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    if settings.environment == "production" and not settings.app_api_key:
        raise RuntimeError("APP_API_KEY is required in production")
    engine, sessions = database(settings.database_url)
    with sessions() as session:
        seed(session, settings.demo_mode)
        for key in ["service:instance_id", "service:runner_key"]:
            if session.get(WorkerState, key) is None:
                session.add(WorkerState(key=key, value=secrets.token_urlsafe(32)))
        session.commit()
        instance_id = session.get(WorkerState, "service:instance_id").value
        runner_key = session.get(WorkerState, "service:runner_key").value
    service = Service(settings, sessions)

    @asynccontextmanager
    async def lifespan(app):
        worker = asyncio.create_task(service.run()) if settings.worker_enabled else None
        yield
        for task in [worker, service.refresh_task, service.relevance_task]:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        engine.dispose()

    app = FastAPI(title="News Feeder", version="0.2.1", lifespan=lifespan)
    app.state.settings, app.state.sessions, app.state.service = settings, sessions, service

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        if request.url.path == "/llm/config" or request.url.path.startswith("/telegram/"):
            # FastAPI normally echoes invalid inputs. A virtual key must never be
            # returned even when validation rejects its length or whitespace.
            details = [
                {key: value for key, value in item.items() if key != "input"} for item in error.errors()
            ]
            return JSONResponse(status_code=422, content={"detail": jsonable_encoder(details)})
        return await request_validation_exception_handler(request, error)

    def session_dependency():
        with sessions() as session:
            yield session

    def authorize(request: Request, authorization: str | None = Header(default=None)):
        request.state.runner_authenticated = False
        if (
            (
                (request.url.path == "/telegram/daily" and request.method == "POST")
                or (request.url.path == "/status" and request.method == "GET")
            )
            and request.client
            and request.client.host in {"127.0.0.1", "::1", "testclient"}
        ):
            supplied_runner = request.headers.get("X-NewsFeeder-Runner-Token", "")
            if supplied_runner and secrets.compare_digest(supplied_runner.encode(), runner_key.encode()):
                request.state.runner_authenticated = True
                return
        if settings.app_api_key:
            supplied = authorization.removeprefix("Bearer ") if authorization else ""
            if not secrets.compare_digest(supplied.encode(), settings.app_api_key.encode()):
                raise HTTPException(401, "Invalid backend credential")
        elif not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(403, "Unconfigured development API accepts only loopback clients")

    protected = [Depends(authorize)]

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/status", dependencies=protected)
    def status(session=Depends(session_dependency)):
        telegram = telegram_config.effective_settings(settings, session)
        telegram_saved = telegram_config.saved_configuration(session)
        return {
            "demo": settings.demo_mode,
            "llm_configured": effective_settings(settings, session).llm_ready,
            "telegram_configured": telegram.telegram_ready,
            "telegram_background_configured": telegram.telegram_ready
            and bool(telegram_saved.get("verified_at")),
            "telegram_target_type": telegram.telegram_target_type,
            "instance_id": instance_id,
            "collecting": service.collecting,
            "last_refresh": service.last_result,
            "worker_error": service.worker_error,
            "ranking": service.ranking,
            "jev_configured": bool(effective_settings(settings, session).litellm_api_key),
            "jev_model": JEV_MODEL,
        }

    @app.get("/llm/config", dependencies=protected)
    def gateway_configuration(session=Depends(session_dependency)):
        return public_configuration(effective_settings(settings, session))

    @app.get("/llm/models", dependencies=protected)
    async def gateway_models(session=Depends(session_dependency)):
        try:
            async with httpx.AsyncClient() as client:
                current = effective_settings(settings, session)
                models = await available_models(current, client)
            return {"models": models, "selected_model": current.litellm_model}
        except GatewayError as error:
            raise HTTPException(error.status, str(error)) from error

    @app.put("/llm/config", dependencies=protected)
    async def save_gateway_configuration(value: GatewayUpdate, session=Depends(session_dependency)):
        try:
            async with httpx.AsyncClient() as client:
                result = await update_configuration(settings, session, value, client)
                service.start_relevance()
                return result
        except GatewayError as error:
            raise HTTPException(error.status, str(error)) from error

    @app.get("/categories", response_model=list[CategoryOutput], dependencies=protected)
    def list_categories(session=Depends(session_dependency)):
        return list(session.scalars(select(Category).order_by(Category.name)))

    @app.post("/categories", response_model=CategoryOutput, status_code=201, dependencies=protected)
    def add_category(value: CategoryInput, session=Depends(session_dependency)):
        category = Category(**value.model_dump())
        session.add(category)
        session.commit()
        return category

    @app.put("/categories/{category_id}", response_model=CategoryOutput, dependencies=protected)
    def update_category(category_id: str, value: CategoryInput, session=Depends(session_dependency)):
        category = session.get(Category, category_id)
        if category is None:
            raise HTTPException(404, "Category not found")
        category.name = value.name
        session.commit()
        return category

    @app.delete("/categories/{category_id}", dependencies=protected)
    def remove_category(category_id: str, session=Depends(session_dependency)):
        category = session.get(Category, category_id)
        if category is None:
            raise HTTPException(404, "Category not found")
        for membership in session.scalars(
            select(SourceCategory).where(SourceCategory.category_id == category_id)
        ):
            session.delete(membership)
        session.delete(category)
        session.commit()
        return {"ok": True}

    def configure_source(session, source, value):
        if "category_id" in value.model_fields_set:
            if value.category_id and session.get(Category, value.category_id) is None:
                raise HTTPException(422, "Category not found")
            record = session.get(SourceCategory, source.id)
            if value.category_id:
                if record:
                    record.category_id = value.category_id
                else:
                    session.add(SourceCategory(source_id=source.id, category_id=value.category_id))
            elif record:
                session.delete(record)
        if "release_tag_pattern" in value.model_fields_set:
            record = session.get(SourceCollectionOptions, source.id)
            if record:
                record.release_tag_pattern = value.release_tag_pattern
            else:
                session.add(
                    SourceCollectionOptions(
                        source_id=source.id, release_tag_pattern=value.release_tag_pattern
                    )
                )

    @app.get("/sources", response_model=list[SourceOutput], dependencies=protected)
    def list_sources(session=Depends(session_dependency)):
        return [
            source_output(session, source)
            for source in session.scalars(
                select(Source).where(Source.deleted.is_(False)).order_by(Source.name)
            )
        ]

    @app.post("/sources", response_model=SourceOutput, status_code=201, dependencies=protected)
    def add_source(value: SourceInput, session=Depends(session_dependency)):
        source = Source(**value.model_dump(exclude={"category_id", "release_tag_pattern"}))
        session.add(source)
        session.flush()
        configure_source(session, source, value)
        session.commit()
        session.refresh(source)
        return source_output(session, source)

    @app.put("/sources/{source_id}", response_model=SourceOutput, dependencies=protected)
    def update_source(source_id: str, value: SourceInput, session=Depends(session_dependency)):
        source = session.get(Source, source_id)
        if source is None or source.deleted:
            raise HTTPException(404, "Source not found")
        for key, item in value.model_dump(exclude={"category_id", "release_tag_pattern"}).items():
            setattr(source, key, item)
        configure_source(session, source, value)
        source.last_refreshed_at = None
        session.commit()
        session.refresh(source)
        return source_output(session, source)

    @app.delete("/sources/{source_id}", dependencies=protected)
    def remove_source(source_id: str, session=Depends(session_dependency)):
        source = session.get(Source, source_id)
        if source is None:
            raise HTTPException(404, "Source not found")
        source.enabled, source.deleted = False, True
        session.commit()
        return {"ok": True}

    @app.post("/sources/preview", dependencies=protected)
    async def preview_source(value: SourceInput):
        if settings.demo_mode:
            return {"articles": [], "note": "Live source requests are disabled in demo mode"}
        source = Source(**value.model_dump(exclude={"category_id", "release_tag_pattern"}))
        source.collection_options = SourceCollectionOptions(release_tag_pattern=value.release_tag_pattern)
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                items = await collect(source, Fetcher(client))
            return {"articles": [{"title": item.title, "url": item.url} for item in items[:5]], "note": None}
        except Exception as error:
            detail = str(error)[:300] if isinstance(error, ValueError) else type(error).__name__
            raise HTTPException(422, detail) from error

    @app.get("/articles", response_model=list[ArticleOutput], dependencies=protected)
    def articles(session=Depends(session_dependency)):
        # Full snapshot for the personal v0 archive. Pagination/incremental sync is a later milestone.
        prefs = preferences(session)
        return [
            article_output(article, prefs)
            for article in session.scalars(
                select(Article).order_by(func.coalesce(Article.published_at, Article.discovered_at).desc())
            )
        ]

    @app.post("/refresh", status_code=202, dependencies=protected)
    async def refresh_sources():
        service.start_refresh()
        return {"accepted": True}

    @app.get("/preferences", response_model=Preferences, dependencies=protected)
    def get_preferences(session=Depends(session_dependency)):
        return preferences(session)

    @app.put("/preferences", response_model=Preferences, dependencies=protected)
    def save_preferences(value: Preferences, session=Depends(session_dependency)):
        if (
            value.telegram_enabled
            and not telegram_config.effective_settings(settings, session).telegram_ready
        ):
            raise HTTPException(
                409, "Connect and verify a Telegram destination before enabling daily delivery"
            )
        # Digest preferences never overwrite the separately edited reader profile.
        owner = session.get(Preference, "owner")
        owner.value = {
            **owner.value,
            **value.model_dump(exclude={"profile", "relevance_enabled", "profile_hash"}),
        }
        session.commit()
        return preferences(session)

    @app.put("/profile", response_model=Preferences, dependencies=protected)
    async def save_profile(value: ReaderProfile, session=Depends(session_dependency)):
        owner = session.get(Preference, "owner")
        owner.value = {**owner.value, **value.model_dump(exclude={"profile_hash"})}
        session.commit()
        service.start_relevance()
        return preferences(session)

    @app.post("/relevance/refresh", status_code=202, dependencies=protected)
    async def refresh_relevance():
        service.start_relevance()
        return {"accepted": True}

    @app.get("/digests", response_model=list[DigestOutput], dependencies=protected)
    def digests(session=Depends(session_dependency)):
        return [
            digest_output(session, digest)
            for digest in session.scalars(select(Digest).order_by(Digest.end_at.desc()))
        ]

    @app.post("/digests", response_model=DigestOutput, dependencies=protected)
    async def make_digest(session=Depends(session_dependency)):
        async with service.lock:
            return digest_output(session, build_digest(session))

    @app.get("/telegram/config", dependencies=protected)
    def get_telegram_configuration(session=Depends(session_dependency)):
        return telegram_config.public_configuration(settings, session)

    @app.put("/telegram/bot", dependencies=protected)
    async def save_telegram_bot(value: TelegramBotUpdate, session=Depends(session_dependency)):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                return await telegram_config.configure_bot(settings, session, value, client)
        except TelegramSetupError as error:
            raise HTTPException(422, str(error)) from None

    @app.put("/telegram/config", dependencies=protected)
    async def save_telegram_configuration(value: TelegramUpdate, session=Depends(session_dependency)):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                return await telegram_config.configure(settings, session, value, client)
        except TelegramSetupError as error:
            raise HTTPException(422, str(error)) from None

    @app.get("/telegram/targets", dependencies=protected)
    async def find_telegram_targets(session=Depends(session_dependency)):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                return await telegram_config.recent_targets(settings, session, client)
        except TelegramSetupError as error:
            raise HTTPException(422, str(error)) from None

    @app.post("/telegram/daily", dependencies=protected)
    async def publish_daily(value: DailyPublishRequest, request: Request):
        if request.state.runner_authenticated and not value.scheduled:
            raise HTTPException(403, "The background runner can only request scheduled delivery")
        return await service.publish_daily(force=not value.scheduled)

    @app.post("/telegram/deliveries/{delivery_id}/retry", dependencies=protected)
    async def retry_telegram(
        delivery_id: str, value: TelegramRetryRequest, session=Depends(session_dependency)
    ):
        if not value.confirmed_unsent:
            raise HTTPException(
                422, "Check that the pending message is missing from Telegram before retrying"
            )
        async with service.lock:
            delivery = session.get(Delivery, delivery_id)
            current = telegram_config.effective_settings(settings, session)
            if not current.telegram_ready:
                raise HTTPException(409, "Connect Telegram before retrying delivery")
            if (
                not delivery
                or not delivery.telegram_record
                or delivery.telegram_record.target != current.telegram_chat_id
            ):
                raise HTTPException(404, "Delivery not found for the current destination")
            if delivery.state == "sent":
                return service.delivery_result(delivery, "already_sent")
            record = delivery.telegram_record
            if (
                delivery.state == "sending"
                and record.attempted_at
                and aware(record.attempted_at) > now() - timedelta(minutes=2)
            ):
                return service.delivery_result(delivery, "in_progress")
            if record.retry_at and aware(record.retry_at) > now():
                return service.delivery_result(delivery, "retry_scheduled")
            delivery.state, delivery.error = "failed", None
            session.commit()
            result = await deliver(session, current, delivery.messages, delivery.dedupe_key)
            return service.delivery_result(result)

    @app.post("/telegram/send", dependencies=protected)
    async def send_telegram(
        value: SendRequest,
        x_request_id: str | None = Header(default=None),
        session=Depends(session_dependency),
    ):
        current = telegram_config.effective_settings(settings, session)
        if not current.telegram_ready:
            raise HTTPException(
                409, "Configure bot token and chat ID on the backend; demo delivery is disabled"
            )
        async with service.lock:
            if value.article_id:
                article = session.get(Article, value.article_id)
                if not article:
                    raise HTTPException(404, "Article not found")
                selected, title = [article], "News Feeder · Saved story"
            elif value.kind == "digest":
                digest = session.scalar(select(Digest).order_by(Digest.end_at.desc()).limit(1))
                digest = digest or build_digest(session)
                selected = [session.get(Article, article_id) for article_id in digest.article_ids]
                title = "News Feeder · Daily digest"
            else:
                selected = prioritized_feed(session, preferences(session))
                title = "News Feeder · Prioritized stories"
            result = await deliver(
                session,
                current,
                format_articles(
                    [a for a in selected if a],
                    title,
                    preferences(session),
                    channel=current.telegram_target_type == "channel",
                ),
                "manual:"
                + hashlib.sha256(current.telegram_chat_id.encode()).hexdigest()[:16]
                + ":"
                + (x_request_id or str(uuid4()))[:80],
            )
            return {
                "id": result.id,
                "state": result.state,
                "sent_count": result.sent_count,
                "total_count": result.total_count,
                "error": result.error,
            }

    @app.get("/telegram/deliveries", dependencies=protected)
    def deliveries(session=Depends(session_dependency)):
        return [
            {
                "id": item.id,
                "created_at": aware(item.created_at),
                "state": item.state,
                "sent_count": item.sent_count,
                "total_count": item.total_count,
                "error": item.error,
            }
            for item in session.scalars(select(Delivery).order_by(Delivery.created_at.desc()).limit(20))
        ]

    return app


app = create_app()
