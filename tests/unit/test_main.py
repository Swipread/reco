import os
import signal
import tomllib
from pathlib import Path

import pytest

from swipread_reco.__main__ import bind_address, exit_on_sigterm, package_version


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


def test_package_version_is_the_one_of_pyproject() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    assert package_version() == tomllib.loads(pyproject.read_text())["project"]["version"]
