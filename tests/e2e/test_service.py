"""E1, E2 and E9: the reco service itself, next to the backend (brief, scenarios)."""

import os
import time
from itertools import combinations

import numpy as np
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from swipread_reco.params import EMBEDDING_MODEL

from .client import event, settle, unserved_articles, wait_for
from .seed import EXTRA_IDS, EXTRA_PAIRS

pytestmark = pytest.mark.e2e

RECO_TABLES = {"feed_requests", "feed_items", "article_embeddings", "user_profiles"}
BACKEND_TIMEOUT_S = 0.300   # swipread.reco.timeout
SPEC_TARGET_S = 0.150       # spec 8.3
LATENCY_CALLS = 30


def test_e1_service_up_as_restricted_role(reco, admin, measure):
    r = reco.get("/healthz")
    assert r.status_code == 200, r.text
    health = r.json()

    role = conninfo_to_dict(os.environ["E2E_RECO_DSN"])["user"]
    with psycopg.connect(os.environ["E2E_RECO_DSN"]) as conn:
        user, superuser, member = conn.execute(
            "SELECT current_user, rolsuper, pg_has_role(current_user, 'swipread_reco', 'MEMBER')"
            " FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
    owners = dict(admin.execute(
        "SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'reco'"
        " AND tablename = ANY(%s)", (list(RECO_TABLES),),
    ).fetchall())
    # The running reco processes are connected as that role.
    connections = admin.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE usename = %s", (role,)
    ).fetchone()[0]
    measure("E1", catalog_size=health["catalogSize"], role=user, superuser=superuser,
            member_of_swipread_reco=member, owners=owners, reco_connections=connections)
    assert health["status"] == "ok"
    assert health["catalogSize"] >= 300
    assert user == role and not superuser and member
    assert owners == {t: role for t in RECO_TABLES}
    assert connections > 0


def test_e2_worker_encodes_with_real_model(admin, rds, topic_ids, measure):
    def encoded():
        rows = admin.execute(
            "SELECT article_id, embedding, model FROM reco.article_embeddings"
            " WHERE article_id = ANY(%s)", (EXTRA_IDS,),
        ).fetchall()
        return rows if len(rows) == len(EXTRA_IDS) else None

    rows = wait_for(encoded, "the worker to encode the extra articles", timeout=240, every=2)
    vectors = {aid: v.to_numpy().astype(np.float64) for aid, v, _ in rows}
    models = {m for _, _, m in rows}
    norms = [float(np.linalg.norm(v)) for v in vectors.values()]

    pairs = [tuple(a[0] for a in pair) for pair in EXTRA_PAIRS]
    within = {p: float(vectors[p[0]] @ vectors[p[1]]) for p in pairs}
    across = max(
        float(vectors[a] @ vectors[b])
        for p, q in combinations(pairs, 2) for a in p for b in q
    )
    keys = {slug: rds.llen(f"fallback:{slug}") for slug in topic_ids}
    measure("E2", models=sorted(models), norms=[round(n, 6) for n in norms],
            cos_within_pairs={"/".join(p): round(c, 4) for p, c in within.items()},
            max_cos_across_pairs=round(across, 4), fallback_list_lengths=keys)
    assert models == {EMBEDDING_MODEL}
    assert all(abs(n - 1) < 1e-4 for n in norms)
    assert min(within.values()) > across
    assert all(length > 0 for length in keys.values()), keys


def test_e9_latency(backend, reco, rds, admin, topic_ids, measure):
    reader = backend.register()
    reader.choose_topics(topic_ids[s] for s in ("tech", "science", "health"))
    reader.feed()
    settle(rds, reader.user_id)
    cards = reader.feed()
    events = [event("view", c["articleId"], c, dwellMs=800 + 900 * k) for k, c in enumerate(cards)]
    events += [event("open", c["articleId"], c) for c in cards[:4]]
    events += [event("read", c["articleId"], c, readMs=30_000) for c in cards[:2]]
    events += [event("like", c["articleId"], c) for c in cards[2:4]]
    events += [event("like", a) for a in unserved_articles(admin, rds, reader.user_id,
                                                           topic_ids["sport"], 1)]
    events += [event("hide", cards[9]["articleId"], cards[9])]
    assert len(events) == 20
    reader.send(*events)
    settle(rds, reader.user_id)

    body = {"userId": reader.user_id, "count": 50, "exclude": [],
            "context": {"sessionId": "s_e2e", "locale": "fr-FR"}}
    timings = []
    for _ in range(LATENCY_CALLS):
        start = time.perf_counter()
        r = reco.post("/v1/recommendations", json=body)
        timings.append(time.perf_counter() - start)
        assert r.status_code == 200, r.text
        assert len(r.json()["items"]) == 50
    p50, p95 = (float(np.percentile(timings, q)) for q in (50, 95))
    measure("E9", calls=LATENCY_CALLS, events=len(events), p50_ms=round(p50 * 1000, 1),
            p95_ms=round(p95 * 1000, 1), max_ms=round(max(timings) * 1000, 1),
            spec_target_150ms_met=p95 < SPEC_TARGET_S)
    assert p95 < BACKEND_TIMEOUT_S
