import os
import signal

import pytest

from swipread_reco.__main__ import bind_address, exit_on_sigterm


def test_bind_address_defaults_to_loopback():
    assert bind_address({}) == ("127.0.0.1", 8081)


def test_bind_address_reads_host_and_port():
    assert bind_address({"RECO_HOST": "0.0.0.0", "RECO_PORT": "9000"}) == ("0.0.0.0", 9000)


def test_sigterm_ends_the_worker_with_a_clean_exit() -> None:
    previous = signal.getsignal(signal.SIGTERM)
    try:
        exit_on_sigterm()
        with pytest.raises(SystemExit) as exc:
            os.kill(os.getpid(), signal.SIGTERM)
        assert exc.value.code == 0
    finally:
        signal.signal(signal.SIGTERM, previous)
