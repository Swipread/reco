from swipread_reco.__main__ import bind_address


def test_bind_address_defaults_to_loopback():
    assert bind_address({}) == ("127.0.0.1", 8081)


def test_bind_address_reads_host_and_port():
    assert bind_address({"RECO_HOST": "0.0.0.0", "RECO_PORT": "9000"}) == ("0.0.0.0", 9000)
