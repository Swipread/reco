"""HTTP API of the api process: POST /v1/recommendations, GET /healthz and the explanation
routes (spec 8.2, 8.3, 9.2)."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

import redis
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from swipread_reco.catalog_cache import CatalogCache
from swipread_reco.explain import explain_router
from swipread_reco.params import MODEL_VERSION, Params
from swipread_reco.recommend import RecoInput, recommend
from swipread_reco.store import Store

log = logging.getLogger(__name__)

# Longest wait on Redis for seen:{userId} (infrastructure setting, not a spec 10 parameter).
DEFAULT_REDIS_TIMEOUT_S = 0.1
SCORE_DECIMALS = 4
MAX_COUNT = 100  # contract: count is 1 to 100


def utcnow() -> datetime:
    return datetime.now(UTC)


class SeenReader(Protocol):
    def seen(self, user_id: str) -> frozenset[str]: ...


class RedisSeen:
    """SMEMBERS seen:{userId}; raises redis errors when Redis is unreachable."""

    def __init__(self, client: redis.Redis) -> None:
        self._client = client

    @classmethod
    def from_url(cls, url: str, timeout_s: float = DEFAULT_REDIS_TIMEOUT_S) -> "RedisSeen":
        return cls(redis.Redis.from_url(
            url, decode_responses=True, socket_timeout=timeout_s,
            socket_connect_timeout=timeout_s,
        ))

    def seen(self, user_id: str) -> frozenset[str]:
        return frozenset(self._client.smembers(f"seen:{user_id}"))


class StoreUnavailable(Exception):
    """A Store call raised; the request is answered 503."""


class _GuardedStore:
    """Wraps every Store method so that any exception it raises becomes StoreUnavailable."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Callable[..., Any]:
        method = getattr(self._store, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return method(*args, **kwargs)
            except Exception as exc:
                raise StoreUnavailable(name) from exc

        return call


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, validate_by_name=True, strict=True)


class RequestContext(_CamelModel):
    session_id: str | None = None
    locale: str | None = None
    device: str | None = None


class RecommendationRequest(_CamelModel):
    user_id: str
    count: int = Field(ge=1, le=MAX_COUNT)
    exclude: list[str] = []
    context: RequestContext = RequestContext()


class RecommendationItem(_CamelModel):
    article_id: str
    score: float
    reason: str


class RecommendationResponse(_CamelModel):
    request_id: str
    model_version: str
    generated_at: datetime
    items: list[RecommendationItem]


class Health(_CamelModel):
    status: str
    catalog_size: int
    catalog_age_seconds: float | None


def create_app(
    store: Store, seen: SeenReader, cache: CatalogCache, params: Params,
    clock: Callable[[], datetime] = utcnow,
) -> FastAPI:
    app = FastAPI(title="swipread-reco")
    db = _GuardedStore(store)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": jsonable_encoder(exc.errors())})

    @app.exception_handler(StoreUnavailable)
    async def store_unavailable(request: Request, exc: StoreUnavailable) -> JSONResponse:
        log.error("store call %s failed: %r", exc, exc.__cause__)
        return JSONResponse(status_code=503, content={"detail": "store unavailable"})

    def seen_ids(user_id: str) -> frozenset[str]:
        try:
            return seen.seen(user_id)
        except Exception:
            log.warning("seen:%s unreadable, continuing without it", user_id, exc_info=True)
            return frozenset()

    @app.post("/v1/recommendations", response_model=RecommendationResponse)
    def recommendations(req: RecommendationRequest) -> RecommendationResponse:
        request_id = "r_" + uuid4().hex
        now = clock()
        user_locale = db.user_locale(req.user_id)
        if user_locale is None:
            raise HTTPException(status_code=404, detail="unknown user")
        catalog = cache.get()
        if catalog is None or not catalog.ids:
            return RecommendationResponse(
                request_id=request_id, model_version=MODEL_VERSION, generated_at=now, items=[]
            )
        inp = RecoInput(
            request_id=request_id,
            language=(req.context.locale or user_locale)[:2].lower(),
            count=req.count,
            excluded=frozenset(req.exclude) | seen_ids(req.user_id)
            | db.user_article_ids(req.user_id),
            events=db.events(req.user_id, params.events_max),
            all_topics=[t.id for t in db.topics()],
            chosen=db.chosen_topics(req.user_id),
            last_request_at=db.last_request_at(req.user_id),
            now=now,
        )
        lot = recommend(inp, catalog, db.embeddings, params)
        db.save_lot(
            lot, user_id=req.user_id, session_id=req.context.session_id,
            requested_count=req.count, generated_at=now, params=params,
        )
        return RecommendationResponse(
            request_id=request_id, model_version=MODEL_VERSION, generated_at=now,
            items=[
                RecommendationItem(
                    article_id=i.article_id, score=round(i.score, SCORE_DECIMALS), reason=i.reason
                )
                for i in lot.items
            ],
        )

    app.include_router(explain_router(db, params, clock))

    @app.get("/healthz", response_model=Health)
    def healthz() -> Health:
        catalog = cache.get()
        if catalog is None:
            return Health(status="ok", catalog_size=0, catalog_age_seconds=None)
        return Health(
            status="ok", catalog_size=len(catalog.ids),
            catalog_age_seconds=(clock() - catalog.loaded_at).total_seconds(),
        )

    return app
