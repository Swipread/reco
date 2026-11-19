"""E10 and E11: the `events` stream read by the worker (contract v1.5, spec 8.6)."""

import psycopg
import pytest
import redis

from .client import event, settle, wait_for

pytestmark = pytest.mark.e2e


def _profile_count(admin: psycopg.Connection, user_id: str) -> int | None:
    row = admin.execute(
        "SELECT events_count FROM reco.user_profiles WHERE user_id = %s", (user_id,)
    ).fetchone()
    return row[0] if row else None


def _group(rds: redis.Redis) -> dict:
    return next(g for g in rds.xinfo_groups("events") if g["name"] == "reco")


def test_e10_worker_keeps_the_profile_up_to_date(backend, rds, admin, topic_ids, measure):
    reader = backend.register()
    reader.choose_topics(topic_ids[s] for s in ("tech", "economy", "culture"))
    cards = reader.feed()
    settle(rds, reader.user_id)
    before = _profile_count(admin, reader.user_id)      # stored by the api's first lot

    # Weak events: no refill, so only the worker can bring the profile up to date.
    reader.send(*(event("view", c["articleId"], c, dwellMs=7000) for c in cards[:3]))
    wait_for(lambda: _profile_count(admin, reader.user_id) == 3,
             "the worker to store the profile", timeout=30)
    count = _profile_count(admin, reader.user_id)
    group = _group(rds)
    measure("E10", profile_before=before, profile_after=count, lag=group.get("lag"),
            pending=group["pending"])
    assert count == 3
    assert group["pending"] == 0


def test_e11_deleted_account_leaves_nothing_in_reco(backend, rds, admin, topic_ids, measure):
    reader = backend.register()
    reader.choose_topics(topic_ids[s] for s in ("tech", "economy", "culture"))
    cards = reader.feed()
    settle(rds, reader.user_id)
    reader.send(event("like", cards[0]["articleId"], cards[0]))
    settle(rds, reader.user_id)
    assert _profile_count(admin, reader.user_id) is not None

    r = reader.http.delete("/v1/me", headers=reader.headers)
    assert r.status_code == 204, r.text

    def gone():
        (profiles,) = admin.execute(
            "SELECT count(*) FROM reco.user_profiles WHERE user_id = %s", (reader.user_id,)
        ).fetchone()
        (lots,) = admin.execute(
            "SELECT count(*) FROM reco.feed_requests WHERE user_id = %s", (reader.user_id,)
        ).fetchone()
        return profiles == 0 and lots == 0

    wait_for(gone, "the reco to delete the reader's profile and lots", timeout=30)
    measure("E11", deleted=True)
