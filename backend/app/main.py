import asyncio
import secrets
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

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
    aware,
)
from .network import Fetcher
from .profile import JEV_MODEL
from .relevance import article_output, prioritized_feed
from .schemas import (
    ArticleOutput,
    CategoryInput,
    CategoryOutput,
    DigestOutput,
    Preferences,
    ReaderProfile,
    SendRequest,
    SourceInput,
    SourceOutput,
)
from .seed import seed
from .service import Service
from .telegram import deliver, format_articles
from .trending import source_output


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    if settings.environment == "production" and not settings.app_api_key:
        raise RuntimeError("APP_API_KEY is required in production")
    engine, sessions = database(settings.database_url)
    with sessions() as session:
        seed(session, settings.demo_mode)
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

    app = FastAPI(title="News Feeder", version="0.1.0", lifespan=lifespan)
    app.state.settings, app.state.sessions, app.state.service = settings, sessions, service

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        if request.url.path == "/llm/config":
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
        if settings.app_api_key:
            supplied = authorization.removeprefix("Bearer ") if authorization else ""
            if not secrets.compare_digest(supplied, settings.app_api_key):
                raise HTTPException(401, "Invalid backend credential")
        elif not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(403, "Unconfigured development API accepts only loopback clients")

    protected = [Depends(authorize)]

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/status", dependencies=protected)
    def status(session=Depends(session_dependency)):
        return {
            "demo": settings.demo_mode,
            "llm_configured": effective_settings(settings, session).llm_ready,
            "telegram_configured": settings.telegram_ready,
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

    @app.post("/telegram/send", dependencies=protected)
    async def send_telegram(
        value: SendRequest,
        x_request_id: str | None = Header(default=None),
        session=Depends(session_dependency),
    ):
        if not settings.telegram_ready:
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
                settings,
                format_articles([a for a in selected if a], title, preferences(session)),
                "manual:" + (x_request_id or str(uuid4()))[:100],
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
