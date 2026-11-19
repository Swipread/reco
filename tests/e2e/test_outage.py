"""E8: the reco api stops, the backend serves its fallback feed, the reco comes back."""

import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from .client import requests_after, settle, wait_for

pytestmark = pytest.mark.e2e

REPO = Path(__file__).resolve().parents[2]
MAX_PAGES = 15
RESTART_TIMEOUT_S = 90


def _reco_up(reco: httpx.Client) -> bool:
    try:
        return reco.get("/healthz").status_code == 200
    except httpx.TransportError:
        return False


def _stop_reco_api(reco: httpx.Client, pid_file: Path) -> None:
    os.kill(int(pid_file.read_text()), signal.SIGTERM)
    wait_for(lambda: not _reco_up(reco), "the reco api to stop", timeout=20)


def _start_reco_api(pid_file: Path, log_file: Path) -> None:
    """Start the api as scripts/e2e.sh does; its pid file lets the script stop it at the end."""
    env = dict(os.environ, RECO_DATABASE_URL=os.environ["E2E_RECO_DSN"],
               RECO_REDIS_URL=os.environ["E2E_REDIS_URL"],
               RECO_PORT=str(urlsplit(os.environ["E2E_RECO_URL"]).port),
               PYTHONPATH=str(REPO / "src"))
    with log_file.open("a") as log:
        proc = subprocess.Popen([sys.executable, "-m", "swipread_reco", "api"], env=env,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    pid_file.write_text(str(proc.pid))


def test_e8_reco_down_then_back(backend, reco, rds, admin, topic_ids, measure):
    run_dir = Path(os.environ["E2E_RUN_DIR"])
    pid_file, log_file = run_dir / "reco-api.pid", run_dir / "reco-api.log"
    reader = backend.register()
    reader.choose_topics(topic_ids[s] for s in ("tech", "economy", "culture"))
    reader.feed()
    settle(rds, reader.user_id)

    _stop_reco_api(reco, pid_file)
    try:
        down_pages = []
        while not any(c["reason"] == "fallback" for page in down_pages for c in page):
            assert len(down_pages) < MAX_PAGES, "no fallback card while the reco is down"
            down_pages.append(reader.feed())        # asserts 200
            assert down_pages[-1], "empty page while the reco is down"
            settle(rds, reader.user_id)
    finally:
        restarted_at = datetime.now(UTC)
        _start_reco_api(pid_file, log_file)
    wait_for(lambda: _reco_up(reco), "the reco api to restart", timeout=60, every=0.5)

    start = time.monotonic()
    fresh: set[str] = set()
    pages_after = 0
    while not fresh:
        assert time.monotonic() - start < RESTART_TIMEOUT_S and pages_after < MAX_PAGES, \
            "the feed did not serve reco cards again"
        settle(rds, reader.user_id)
        page = reader.feed()
        pages_after += 1
        new_lots = set(requests_after(admin, reader.user_id, restarted_at))
        fresh = {c["requestId"] for c in page} & new_lots
    measure("E8", pages_while_down=len(down_pages),
            fallback_cards_while_down=sum(c["reason"] == "fallback" for p in down_pages for c in p),
            pages_until_reco_cards=pages_after,
            seconds_until_reco_cards=round(time.monotonic() - start, 1))
