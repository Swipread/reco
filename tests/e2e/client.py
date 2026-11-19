"""Reader actions through the backend's public API, as the web app sends them, and the waits
and database reads the end-to-end scenarios share."""

import os
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
import redis

PAGE_SIZE = 10        # swipread.feed.page-size
RECO_REASONS = {"similar", "onboarding", "explore"}
SESSION_ID = "s_e2e"


def wait_for[T](probe: Callable[[], T], what: str, timeout: float = 20.0, every: float = 0.2) -> T:
    """Call `probe` until it returns a truthy value, which is returned; AssertionError on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(every)


def event(kind: str, article_id: str, card: dict[str, Any] | None = None,
          **payload: Any) -> dict[str, Any]:
    """One event of POST /v1/events, occurring now; `card` gives requestId and position."""
    body: dict[str, Any] = {
        "eventId": f"e_{uuid.uuid4().hex}",
        "type": kind,
        "articleId": article_id,
        "occurredAt": datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
    }
    if card is not None:
        body["requestId"] = card["requestId"]
        body["position"] = card["position"]
    if payload:
        body["payload"] = payload
    time.sleep(0.002)   # distinct occurredAt, so that the replay order is the sending order
    return body


class Reader:
    def __init__(self, http: httpx.Client, user_id: str, token: str) -> None:
        self.http = http
        self.user_id = user_id
        self.headers = {"Authorization": f"Bearer {token}", "X-Session-Id": SESSION_ID}

    def choose_topics(self, ids: Iterable[int]) -> None:
        r = self.http.put("/v1/me/topics", json={"topicIds": list(ids)}, headers=self.headers)
        assert r.status_code == 200, r.text

    def feed(self) -> list[dict[str, Any]]:
        """GET /v1/feed: the next page of cards (asserts 200)."""
        r = self.http.get("/v1/feed", headers=self.headers)
        assert r.status_code == 200, r.text
        return r.json()["items"]

    def send(self, *events: dict[str, Any]) -> None:
        r = self.http.post("/v1/events", json={"sessionId": SESSION_ID, "events": list(events)},
                           headers=self.headers)
        assert r.status_code == 202, r.text
        assert r.json()["accepted"] == len(events), r.text


class Backend:
    def __init__(self, http: httpx.Client) -> None:
        self.http = http

    def register(self) -> Reader:
        email = f"e2e-{uuid.uuid4().hex[:12]}@example.org"
        r = self.http.post("/v1/auth/register", json={
            "email": email, "password": "password123", "displayName": "E2E reader",
        })
        assert r.status_code == 201, r.text
        body = r.json()
        return Reader(self.http, body["userId"], body["accessToken"])


def settle(rds: redis.Redis, user_id: str, timeout: float = 20.0) -> None:
    """Wait until no refill holds the reader's lock and the stock has a page of cards."""
    wait_for(
        lambda: not rds.exists(f"feed:{user_id}:lock") and rds.llen(f"feed:{user_id}") >= PAGE_SIZE,
        f"the stock of {user_id} to be refilled", timeout,
    )


def backend_reco_failures(user_id: str) -> list[str]:
    """Lines of the backend's log where its call to the reco failed for this reader (timeout,
    connection refused...): the refill then came from the fallback feed."""
    path = os.environ.get("E2E_API_LOG")
    if not path or not os.path.exists(path):
        return []
    with open(path) as f:
        return [line.strip() for line in f
                if "Recommendation service unavailable" in line and user_id in line]


def topic_of(card: dict[str, Any]) -> int:
    """Main topic of a feed card (topics are sorted by confidence)."""
    return card["topics"][0]["id"]


def last_request_at(admin: psycopg.Connection, user_id: str) -> datetime | None:
    return admin.execute(
        "SELECT max(generated_at) FROM reco.feed_requests WHERE user_id = %s", (user_id,)
    ).fetchone()[0]


def requests_after(admin: psycopg.Connection, user_id: str, after: datetime | None) -> list[str]:
    """Request ids of the reader's lots generated after `after` (all of them if None)."""
    rows = admin.execute(
        "SELECT request_id FROM reco.feed_requests WHERE user_id = %s"
        " AND (%s::timestamptz IS NULL OR generated_at > %s) ORDER BY generated_at",
        (user_id, after, after),
    ).fetchall()
    return [r[0] for r in rows]


def lot_articles(admin: psycopg.Connection, request_ids: list[str]) -> list[tuple[str, int]]:
    """(article id, topic id) of every item of these lots."""
    return admin.execute(
        "SELECT article_id, topic_id FROM reco.feed_items WHERE request_id = ANY(%s)",
        (request_ids,),
    ).fetchall()


def unserved_articles(admin: psycopg.Connection, rds: redis.Redis, user_id: str, topic: int,
                      n: int) -> list[str]:
    """`n` sample articles of `topic` neither served to the reader nor in their stock."""
    taken = rds.smembers(f"seen:{user_id}") | set(rds.lrange(f"feed:{user_id}", 0, -1))
    rows = admin.execute(
        "SELECT a.id FROM app.articles a JOIN app.article_topics t ON t.article_id = a.id"
        " WHERE t.topic_id = %s AND a.id LIKE 'a_dev%%' ORDER BY a.id",
        (topic,),
    ).fetchall()
    ids = [r[0] for r in rows if r[0] not in taken][:n]
    assert len(ids) == n, f"not enough unserved articles in topic {topic}"
    return ids
