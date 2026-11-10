import csv
from datetime import UTC, datetime

from swipread_reco.params import Params
from swipread_reco.sim.backend import simulate
from swipread_reco.sim.catalog import make_sim_catalog
from swipread_reco.sim.readers import READERS
from swipread_reco.sim.report import write_report

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)

S = make_sim_catalog(NOW)
run = lambda name, n=100, seed=0: simulate(next(r for r in READERS if r.name == name), S, n, NOW, Params(), seed)


def test_no_repeat_and_exact_length():
    s = run("fidele"); assert len(s) == 100 and len({c.article_id for c in s}) == 100


def test_n_plus_10_rule():
    reader = next(r for r in READERS if r.name == "allergique"); s = run("allergique")
    strong = [c.rank for c in s if c.topic_id in reader.hates_topics]      # chaque carte détestée est masquée
    n = strong[0]
    assert s[n + 9].computed_after_rank >= n                                # la carte n+10 connaît l'événement
    assert all(c.computed_after_rank < n for c in s[n:n + 9])               # les 9 d'avant, non


def test_allergic_reader_stops_seeing_hated_topic():
    s = run("allergique"); share = lambda cards: sum(c.topic_id == 3 for c in cards) / len(cards)
    assert share(s[:20]) > 0.10 and share(s[50:]) < 0.05


def test_faithful_reader_gets_more_of_loved_subtopic():
    s = run("fidele"); loved = lambda cards: sum(S.subtopic[c.article_id] == (1, 0) for c in cards) / len(cards)
    assert loved(s[50:]) > loved(s[:10])


def test_skipper_always_gets_cards_and_keeps_floors():
    assert len(run("zappeur", 200)) == 200


def test_simulation_is_deterministic():
    assert run("passionne") == run("passionne")


def test_report_files(tmp_path):
    write_report(tmp_path, n_cards=100)
    rows = list(csv.DictReader((tmp_path / "report.csv").open()))
    assert len(rows) == 40 and set(rows[0]) == {"reader", "page", "relevant_share", "top_topic_share",
                                                 "explore_share", "n_topics", "n_sources"}
    md = (tmp_path / "report.md").read_text()
    assert all(f"## {r.name}" in md for r in READERS)
