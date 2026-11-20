"""`python -m swipread_reco reasons`: success of each `reason` on real readers (spec 11.4).

The simulated readers of the report (11.3) say what the algorithm should do; this table says
what real readers did with the cards they were shown, exploration (`explore`) next to the
other reasons and to the backend's fallback feed.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from swipread_reco.models import ReasonOutcome

# Batches are purged after 30 days (spec 8.7, RETENTION_DAYS): further back, cards lose their
# reason.
DEFAULT_DAYS = 30
ORDER = ["similar", "onboarding", "explore", "fallback"]


def _percent(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)} %" if whole else "–"


def markdown(outcomes: Sequence[ReasonOutcome], since: datetime, now: datetime) -> str:
    """Markdown table, one row per reason, in a fixed order, then any other reason."""
    rank = {r: k for k, r in enumerate(ORDER)}
    rows = sorted(outcomes, key=lambda o: (rank.get(o.reason, len(ORDER)), o.reason))
    lines = [
        f"Cartes affichées du {since:%d/%m/%Y %H:%M} au {now:%d/%m/%Y %H:%M} (UTC), par raison.",
        "",
        "| Raison | Cartes | Lecteurs | Ouvertes | Aimées, sauvegardées ou partagées | Masquées |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for o in rows:
        lines.append(
            f"| {o.reason} | {o.cards} | {o.readers} | {_percent(o.opened, o.cards)} "
            f"| {_percent(o.positive, o.cards)} | {_percent(o.hidden, o.cards)} |"
        )
    if not rows:
        lines.append("| (aucune carte) | 0 | 0 | – | – | – |")
    return "\n".join(lines) + "\n"


class _Source(Protocol):
    def reason_outcomes(self, since: datetime) -> list[ReasonOutcome]: ...


def report(store: _Source, now: datetime, days: int = DEFAULT_DAYS) -> str:
    since = now - timedelta(days=days)
    return markdown(store.reason_outcomes(since), since, now)
