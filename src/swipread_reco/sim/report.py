"""Jury report: simulated readers served through the stock rules, measured page by page (spec 11.3).

Run with `python -m swipread_reco.sim.report --out report/`.
"""

import argparse
import csv
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from swipread_reco.params import Params
from swipread_reco.sim.backend import PAGE_SIZE, REFILL_BELOW, REFILL_COUNT, Served, simulate
from swipread_reco.sim.catalog import SimCatalog, make_sim_catalog
from swipread_reco.sim.readers import LONG_DWELL_MS, READERS, SHORT_DWELL_MS, Reader

START = datetime(2026, 11, 10, 12, tzinfo=UTC)   # fixed clock: the report is reproducible
COLUMNS = ["reader", "page", "relevant_share", "top_topic_share", "explore_share",
           "n_topics", "n_sources"]
TOP_TOPIC = {"fidele": 1, "passionne": 4, "allergique": 1, "zappeur": 1}
PROFILE = {
    "fidele": "fidèle à ses thèmes choisis",
    "passionne": "passionné d'un thème non choisi",
    "allergique": "allergique à un thème choisi",
    "zappeur": "qui zappe tout",
}
MD_HEADER = ("| Page | Pertinentes | Thème préféré | Exploration | Thèmes distincts "
             "| Sources distinctes |\n| --- | --- | --- | --- | --- | --- |")


def page_rows(served: Sequence[Served], reader: Reader, sim: SimCatalog) -> list[dict]:
    """One row per page of 10 cards, with the measures of spec 11.3."""
    rows = []
    for start in range(0, len(served), PAGE_SIZE):
        cards = served[start:start + PAGE_SIZE]
        n = len(cards)
        rows.append({
            "reader": reader.name,
            "page": start // PAGE_SIZE + 1,
            "relevant_share": sum(reader.relevant(c.article_id, sim) for c in cards) / n,
            "top_topic_share": sum(c.topic_id == TOP_TOPIC[reader.name] for c in cards) / n,
            "explore_share": sum(c.reason == "explore" for c in cards) / n,
            "n_topics": len({c.topic_id for c in cards}),
            "n_sources": len({int(sim.catalog.source_ids[sim.catalog.index[c.article_id]])
                              for c in cards}),
        })
    return rows


def _numbers(values: Sequence[int]) -> str:
    return ", ".join(str(v) for v in sorted(values))


def _rule(reader: Reader) -> str:
    """French sentence describing the reader's fixed rule."""
    long_s, short_ms = LONG_DWELL_MS // 1000, SHORT_DWELL_MS
    parts = [f"Lecteur {PROFILE[reader.name]}. Thèmes choisis : {_numbers(reader.chosen)}."]
    if reader.loves:
        loved = ", ".join(f"{t}.{s}" for t, s in sorted(reader.loves))
        which = "des sous-thèmes" if len(reader.loves) > 1 else "du sous-thème"
        parts.append(f"Regarde {long_s} s puis aime chaque article {which} {loved}.")
    if reader.hates_topics:
        parts.append(f"Masque chaque article du thème {_numbers(reader.hates_topics)}.")
    if reader.likes_topics:
        parts.append(f"Regarde {long_s} s, sans aimer, les autres articles des thèmes "
                     f"{_numbers(reader.likes_topics)}.")
    if reader.loves or reader.hates_topics or reader.likes_topics:
        parts.append(f"Zappe tout le reste (vue de {short_ms} ms).")
    else:
        parts.append(f"Zappe tout (vue de {short_ms} ms).")
    parts.append(f"Thème préféré mesuré : {TOP_TOPIC[reader.name]}.")
    return " ".join(parts)


def _percent(share: float) -> str:
    return f"{round(share * 100)} %"


def _markdown(rows_by_reader: dict[str, list[dict]], n_cards: int, seed: int) -> str:
    lines = [
        "# Lecteurs simulés : adaptation du fil",
        "",
        (f"Chaque lecteur voit {n_cards} cartes, une toutes les 10 secondes, servies par pages "
         f"de {PAGE_SIZE} à partir d'un stock qui imite le backend : remplissage de "
         f"{REFILL_COUNT} articles quand le stock passe sous {REFILL_BELOW}, et après chaque "
         "événement fort (like, masquage) sur la carte de position p de la page, seuls les p "
         "premiers articles du stock sont gardés avant un nouveau remplissage. Chaque lot vient "
         "de l'algorithme réel (`recommend`), sur le catalogue synthétique (10 thèmes, "
         f"3 sous-thèmes, 8 sources), graine {seed}."),
        "",
        (f"Colonnes, par page de {PAGE_SIZE} cartes : part de cartes pertinentes (dans les goûts "
         "du lecteur), part du thème préféré, part d'exploration (`reason = explore`), nombre de "
         "thèmes et de sources distincts."),
    ]
    for reader in READERS:
        lines += ["", f"## {reader.name}", "", _rule(reader), "", MD_HEADER]
        for row in rows_by_reader[reader.name]:
            lines.append(
                f"| {row['page']} | {_percent(row['relevant_share'])} "
                f"| {_percent(row['top_topic_share'])} | {_percent(row['explore_share'])} "
                f"| {row['n_topics']} | {row['n_sources']} |"
            )
    return "\n".join(lines) + "\n"


def write_report(out_dir: Path, n_cards: int = 100, seed: int = 0) -> None:
    """Simulate every reader and write `report.md` and `report.csv` into `out_dir`."""
    sim = make_sim_catalog(START)
    params = Params()
    rows_by_reader = {
        reader.name: page_rows(simulate(reader, sim, n_cards, START, params, seed), reader, sim)
        for reader in READERS
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "report.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for reader in READERS:
            writer.writerows(rows_by_reader[reader.name])
    (out_dir / "report.md").write_text(_markdown(rows_by_reader, n_cards, seed))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Simulated readers report (spec 11.3).")
    parser.add_argument("--out", type=Path, default=Path("report"), help="output directory")
    parser.add_argument("--cards", type=int, default=100, help="cards per reader")
    parser.add_argument("--seed", type=int, default=0, help="simulation seed")
    args = parser.parse_args(argv)
    write_report(args.out, n_cards=args.cards, seed=args.seed)


if __name__ == "__main__":
    main()
