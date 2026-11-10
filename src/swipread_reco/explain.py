"""Read-only explanation routes: GET /v1/explain/users/{userId} and /v1/explain/requests/{requestId}
(spec 9.2).

The user profile is replayed exactly as POST /v1/recommendations does it: same events, same
last_request_at, same clock, and the liked and rejected articles the algorithm can use.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi.exceptions import HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from swipread_reco.allocation import cap_shares
from swipread_reco.models import ProfileItem
from swipread_reco.params import Params
from swipread_reco.profile import build_profile
from swipread_reco.store import Store

# Definition fixed by spec 9.2 (not a tunable of section 10): weight changes shown.
HISTORY_MAX = 50


class _Out(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, validate_by_name=True)


class TopicWeight(_Out):
    id: int
    slug: str
    label: str
    chosen: bool
    weight: float
    share: float


class ProfileArticle(_Out):
    article_id: str
    title: str | None
    strength: float


class HistoryEntry(_Out):
    event_id: str
    type: str
    article_id: str
    topic_slug: str
    delta: float
    weight_after: float
    occurred_at: datetime


class UserExplanation(_Out):
    user_id: str
    topics: list[TopicWeight]
    liked: list[ProfileArticle]
    rejected: list[ProfileArticle]
    history: list[HistoryEntry]


class ExplainedItem(_Out):
    position: int
    article_id: str
    title: str | None
    topic_slug: str | None
    reason: str
    score: float
    explanation: dict[str, Any]


class RequestExplanation(_Out):
    request_id: str
    user_id: str
    session_id: str | None
    model_version: str
    requested_count: int
    seed: int
    generated_at: datetime
    profile: dict[str, Any]
    params: dict[str, Any]
    items: list[ExplainedItem]


def explain_router(store: Store, params: Params, clock: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(prefix="/v1/explain")

    @router.get("/users/{user_id}", response_model=UserExplanation)
    def explain_user(user_id: str) -> UserExplanation:
        if store.user_locale(user_id) is None:
            raise HTTPException(status_code=404, detail="unknown user")
        topics = store.topics()
        chosen = store.chosen_topics(user_id)
        profile = build_profile(
            store.events(user_id, params.events_max), [t.id for t in topics], chosen,
            clock(), store.last_request_at(user_id), params,
        )
        shares = cap_shares(profile.weights, params.p_max)

        # recommend() drops the liked and rejected articles that have no embedding.
        wanted = [i.article_id for i in profile.liked + profile.rejected]
        usable = store.embeddings(wanted) if wanted else {}
        liked = [i for i in profile.liked if i.article_id in usable]
        rejected = [i for i in profile.rejected if i.article_id in usable]
        shown = [i.article_id for i in liked + rejected]
        titles = store.article_titles(shown) if shown else {}

        def articles(items: list[ProfileItem]) -> list[ProfileArticle]:
            return [
                ProfileArticle(
                    article_id=i.article_id, title=titles.get(i.article_id), strength=i.strength
                )
                for i in items
            ]

        slug = {t.id: t.slug for t in topics}
        return UserExplanation(
            user_id=user_id,
            topics=sorted(
                (
                    TopicWeight(
                        id=t.id, slug=t.slug, label=t.label, chosen=t.id in chosen,
                        weight=profile.weights[t.id], share=shares[t.id],
                    )
                    for t in topics
                ),
                key=lambda t: (-t.weight, t.id),
            ),
            liked=articles(liked),
            rejected=articles(rejected),
            history=[
                HistoryEntry(
                    event_id=c.event_id, type=c.type, article_id=c.article_id,
                    topic_slug=slug[c.topic_id], delta=c.delta, weight_after=c.weight_after,
                    occurred_at=c.occurred_at,
                )
                for c in reversed(profile.history[-HISTORY_MAX:])
            ],
        )

    @router.get("/requests/{request_id}", response_model=RequestExplanation)
    def explain_request(request_id: str) -> RequestExplanation:
        row = store.request(request_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown request")
        slug = {t.id: t.slug for t in store.topics()}
        return RequestExplanation(
            request_id=row["request_id"], user_id=row["user_id"], session_id=row["session_id"],
            model_version=row["model_version"], requested_count=row["requested_count"],
            seed=row["seed"], generated_at=row["generated_at"], profile=row["profile"],
            params=row["params"],
            items=[
                ExplainedItem(
                    position=i["position"], article_id=i["article_id"], title=i["title"],
                    topic_slug=slug.get(i["topic_id"]), reason=i["reason"], score=i["score"],
                    explanation=i["explanation"],
                )
                for i in row["items"]
            ],
        )

    return router
