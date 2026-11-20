from datetime import UTC, datetime, timedelta

from swipread_reco.models import ReasonOutcome
from swipread_reco.reasons import markdown, report

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def test_rows_in_fixed_order_with_shares_of_the_cards():
    outcomes = [
        ReasonOutcome("explore", 20, 3, 5, 2, 1),
        ReasonOutcome("fallback", 10, 2, 1, 0, 0),
        ReasonOutcome("similar", 40, 3, 20, 8, 0),
    ]
    lines = markdown(outcomes, NOW - timedelta(days=30), NOW).splitlines()
    assert lines[0] == "Cartes affichées du 21/10/2026 12:00 au 20/11/2026 12:00 (UTC), par raison."
    assert lines[4:] == [
        "| similar | 40 | 3 | 50 % | 20 % | 0 % |",
        "| explore | 20 | 3 | 25 % | 10 % | 5 % |",
        "| fallback | 10 | 2 | 10 % | 0 % | 0 % |",
    ]


def test_unknown_reason_last_and_empty_period():
    text = markdown([ReasonOutcome("trending", 1, 1, 1, 1, 0)] + [ReasonOutcome("explore", 1, 1, 0, 0, 0)], NOW, NOW)
    assert text.splitlines()[-2:] == ["| explore | 1 | 1 | 0 % | 0 % | 0 % |", "| trending | 1 | 1 | 100 % | 100 % | 0 % |"]
    assert markdown([], NOW, NOW).splitlines()[-1] == "| (aucune carte) | 0 | 0 | – | – | – |"


def test_report_reads_the_period_back_from_now():
    class Store:
        def reason_outcomes(self, since):
            self.since = since
            return []

    store = Store()
    report(store, NOW, days=7)
    assert store.since == NOW - timedelta(days=7)
