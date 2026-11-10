"""Imitation of the backend's article stock around `recommend` (contract, Redis rules 1 to 4).

Rules imitated: the stock starts empty; a page of PAGE_SIZE cards is popped from it when the
reader has seen every card of the previous page; it is refilled with REFILL_COUNT articles,
excluding its current content and every served article, when it falls under REFILL_BELOW;
a strong event on the card at position p of the page (from 0) keeps the p first articles of
the stock and refills it, so an event on card n shows from card n + 10 on.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from swipread_reco.allocation import seed_from_request_id
from swipread_reco.models import Event, LotItem
from swipread_reco.params import Params
from swipread_reco.recommend import RecoInput, recommend
from swipread_reco.sim.catalog import SimCatalog
from swipread_reco.sim.readers import Reader

PAGE_SIZE = 10
REFILL_COUNT = 50
REFILL_BELOW = 20
STRONG_TYPES = frozenset({"like", "save", "share", "hide"})
CARD_INTERVAL = timedelta(seconds=10)       # one card every 10 s of simulated clock
PAGE_FETCH_LEAD = timedelta(seconds=5)      # a page is fetched 5 s before its first card
STRONG_REFILL_DELAY = timedelta(seconds=1)  # refill after a strong event, strictly after it
LANGUAGE = "fr"
WORD_COUNT = 230
SEED_MODULUS = 2**63                        # seeds stay in a bigint, like seed_from_request_id


@dataclass(frozen=True)
class Served:
    rank: int                 # 1-based, over the whole session
    article_id: str
    topic_id: int
    reason: str
    lot_index: int            # index of the reco lot the card comes from
    computed_after_rank: int  # rank of the last card seen when that lot was computed


@dataclass(frozen=True)
class _Stocked:
    item: LotItem
    lot_index: int
    computed_after_rank: int


def simulate(
    reader: Reader, sim: SimCatalog, n_cards: int, start: datetime, params: Params,
    seed: int = 0,
) -> list[Served]:
    """Serve `n_cards` cards to `reader` through the stock rules, calling the real `recommend`.

    Card k (1-based) is seen at `start + k x 10 s`; its events get that time as `occurred_at`
    and `received_at`. Lot i uses `request_id = r_{reader.name}_{i}` and the seed derived from
    it, shifted by `seed`.
    """
    all_topics = sorted({int(t) for t in sim.catalog.topic_ids})
    stock: list[_Stocked] = []
    seen: set[str] = set()
    events: list[Event] = []
    served: list[Served] = []
    lot_index = 0
    last_request_at: datetime | None = None

    def refill(at: datetime) -> None:
        nonlocal lot_index, last_request_at
        request_id = f"r_{reader.name}_{lot_index}"
        kept = tuple(s.item.article_id for s in stock)
        inp = RecoInput(
            request_id=request_id,
            language=LANGUAGE,
            count=REFILL_COUNT,
            excluded=frozenset(kept) | frozenset(seen),
            events=list(events),
            all_topics=all_topics,
            chosen=reader.chosen,
            last_request_at=last_request_at,
            now=at,
            stock=kept,
        )
        lot_seed = (seed_from_request_id(request_id) + seed) % SEED_MODULUS
        lot = recommend(inp, sim.catalog, sim.embeddings, params, seed=lot_seed)
        stock.extend(_Stocked(item, lot_index, len(served)) for item in lot.items)
        lot_index += 1
        last_request_at = at

    while len(served) < n_cards:
        fetch_at = start + (len(served) + 1) * CARD_INTERVAL - PAGE_FETCH_LEAD
        if len(stock) < PAGE_SIZE:
            refill(fetch_at)
        page = stock[:PAGE_SIZE]
        del stock[:PAGE_SIZE]
        if not page:
            raise RuntimeError(f"no card left to serve to {reader.name} after {len(served)}")
        seen.update(s.item.article_id for s in page)
        if len(stock) < REFILL_BELOW:
            refill(fetch_at)

        for position, card in enumerate(page):
            if len(served) == n_cards:
                break
            rank = len(served) + 1
            at = start + rank * CARD_INTERVAL
            served.append(Served(
                rank=rank,
                article_id=card.item.article_id,
                topic_id=card.item.topic_id,
                reason=card.item.reason,
                lot_index=card.lot_index,
                computed_after_rank=card.computed_after_rank,
            ))
            reactions = reader.react(card.item.article_id, sim)
            events.extend(
                Event(
                    event_id=f"e_{reader.name}_{rank}_{k}",
                    type=event_type,
                    article_id=card.item.article_id,
                    occurred_at=at,
                    received_at=at,
                    payload=payload,
                    topic_id=card.item.topic_id,
                    word_count=WORD_COUNT,
                )
                for k, (event_type, payload) in enumerate(reactions)
            )
            if any(event_type in STRONG_TYPES for event_type, _ in reactions):
                del stock[position:]
                refill(at + STRONG_REFILL_DELAY)
    return served
