"""Worker: article encoding (8.4), fallback lists (8.5), purge (8.7) and the loop around them."""

import zlib
from collections.abc import Sequence
from datetime import datetime, timedelta

import fakeredis
import numpy as np
import pytest

from swipread_reco import worker
from swipread_reco.models import Topic
from swipread_reco.params import EMBEDDING_MODEL, Params
from swipread_reco.worker import run_worker
from swipread_reco.worker.encode import E5Encoder, article_text, encode_pending
from swipread_reco.worker.fallback import write_fallback
from swipread_reco.worker.purge import run_purge

from ..fakes import NOW, FakeStore

DIM = 8


class FakeEncoder:
    """Deterministic unit vectors of dimension DIM, seeded by the text."""

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        out = np.stack([
            np.random.default_rng(zlib.crc32(t.encode())).standard_normal(DIM) for t in texts
        ]).astype(np.float32)
        return out / np.linalg.norm(out, axis=1, keepdims=True)


class FakeClock:
    """Simulated time starting at `start`; only `advance` (called by StopAfter) moves it."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _Stop(Exception):
    """Ends run_worker once StopAfter's budget of simulated time is spent."""


class StopAfter:
    """Injected `sleep`: advances `clock`, then raises _Stop once `seconds` have elapsed."""

    def __init__(self, clock: FakeClock, *, seconds: float) -> None:
        self.clock = clock
        self.left = seconds

    def __call__(self, seconds: float) -> None:
        assert seconds >= 0
        self.clock.advance(seconds)
        self.left -= seconds
        if self.left <= 0:
            raise _Stop


@pytest.fixture
def store() -> FakeStore:
    """FakeStore whose topic 1 is `tech`."""
    s = FakeStore()
    s.topic_list[0] = Topic(id=1, slug="tech", label="Tech")
    return s


@pytest.fixture
def redis() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.mark.parametrize("title, summary, expected", [
    ("Titre", "Un <b>résumé</b>  propre.", "query: Titre. Un résumé propre."),
    ("Vraiment ?", "Oui &amp; non", "query: Vraiment ? Oui & non"),
    ("Titre", "", "query: Titre."),
])
def test_article_text(title, summary, expected):
    assert article_text(title, summary) == expected


def test_article_text_keeps_escaped_markup_as_text_and_trims_title():
    assert article_text(" Fin! ", "<p>a &lt;b&gt;\n b</p>") == "query: Fin! a <b> b"


def test_encode_pending_writes_model_and_stops_when_done(store):
    store.pending = [("a1", "T1", "S1"), ("a2", "T2", "S2")]
    assert encode_pending(store, FakeEncoder(), NOW) == 2
    assert store.written_model == "multilingual-e5-small" and set(store.written) == {"a1", "a2"}
    assert encode_pending(store, FakeEncoder(), NOW) == 0


def test_encode_pending_writes_the_encoder_vectors_one_batch_at_a_time(store):
    store.pending = [(f"a{i}", f"T{i}", "S") for i in range(5)]
    assert encode_pending(store, FakeEncoder(), NOW, batch=3) == 3
    expected = FakeEncoder().encode([article_text("T0", "S")])[0]
    np.testing.assert_array_equal(store.written["a0"], expected)
    assert store.written_at == NOW and store.encode_calls == 1
    assert encode_pending(store, FakeEncoder(), NOW, batch=3) == 2


def test_encode_pending_reencodes_articles_of_another_model(store):
    store.pending = [("a1", "T1", "S1")]
    store.write_embeddings([("a1", np.ones(DIM, np.float32))], "old-model", NOW)
    assert encode_pending(store, FakeEncoder(), NOW) == 1
    assert store.written_model == EMBEDDING_MODEL


def test_e5_encoder_loads_nothing_until_first_encode():
    assert E5Encoder()._model is None


def test_fallback_lists_by_slug_with_ttl(store, redis):
    write_fallback(store, redis, NOW, Params())
    slug = store.topics()[0].slug
    assert redis.lrange(f"fallback:{slug}", 0, -1) == store.fallback_ids(store.topics()[0].id, NOW, 14, 50)
    assert 0 < redis.ttl(f"fallback:{slug}") <= 3600 and not redis.exists(f"fallback:{slug}:tmp")


def test_fallback_holds_50_newest_of_the_topic_for_every_topic(store, redis):
    write_fallback(store, redis, NOW, Params())
    for topic in store.topics():
        ids = redis.lrange(f"fallback:{topic.slug}", 0, -1)
        assert len(ids) == 50 and {store.sim.subtopic[i][0] for i in ids} == {topic.id}


def test_fallback_replaces_previous_list_and_deletes_empty_ones(store, redis):
    redis.rpush("fallback:tech", "old"); store.drop_articles(store.sim.ids(1)); write_fallback(store, redis, NOW, Params())
    assert not redis.exists("fallback:tech")


def test_fallback_overwrites_a_previous_list(store, redis):
    redis.rpush("fallback:tech", "old")
    write_fallback(store, redis, NOW, Params())
    assert "old" not in redis.lrange("fallback:tech", 0, -1)
    assert redis.llen("fallback:tech") == 50


def test_run_purge_returns_deleted_count(store):
    assert run_purge(store, NOW) == 0
    assert store.purged == [(NOW, 30)]


def test_worker_loop_survives_a_failing_step(store, redis, caplog):
    store.fail_purge = True
    clock = FakeClock(NOW)
    with pytest.raises(_Stop):
        run_worker(store, redis, FakeEncoder(), Params(), clock, sleep=StopAfter(clock, seconds=3700))
    assert "purge" in caplog.text and store.encode_calls > 200 and redis.exists("fallback:tech")


def test_worker_loop_runs_each_activity_at_its_own_period(store, redis, monkeypatch):
    calls: dict[str, list[datetime]] = {"encode": [], "fallback": [], "purge": []}
    monkeypatch.setattr(worker, "encode_pending", lambda s, e, now: calls["encode"].append(now))
    monkeypatch.setattr(worker, "write_fallback", lambda s, r, now, p: calls["fallback"].append(now))
    monkeypatch.setattr(worker, "run_purge", lambda s, now: calls["purge"].append(now))
    clock = FakeClock(NOW)
    with pytest.raises(_Stop):
        run_worker(store, redis, FakeEncoder(), Params(), clock, sleep=StopAfter(clock, seconds=3601))
    assert calls["purge"] == [NOW, NOW + timedelta(seconds=3600)]
    assert calls["fallback"] == [NOW + timedelta(seconds=600 * k) for k in range(7)]
    assert calls["encode"] == [NOW + timedelta(seconds=15 * k) for k in range(241)]
