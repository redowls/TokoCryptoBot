import socket

from tokocrypto import config, net


def test_force_ipv4_restricts_urllib3_address_family(monkeypatch):
    """Regression: this host has IPv4 + IPv6 and api.indodax.com publishes both
    A and AAAA records, so requests leave over IPv6 by default and Indodax sees
    the v6 source — while the API key whitelists the v4 one. The symptom is
    [-2015] Unauthorized IP address on perfectly valid credentials."""
    import urllib3.util.connection as conn
    original = conn.allowed_gai_family
    monkeypatch.setattr(net, "_applied", False)
    try:
        assert net.force_ipv4() is True
        assert conn.allowed_gai_family() == socket.AF_INET
    finally:
        conn.allowed_gai_family = original
        net._applied = False


def test_force_ipv4_is_idempotent(monkeypatch):
    import urllib3.util.connection as conn
    original = conn.allowed_gai_family
    monkeypatch.setattr(net, "_applied", False)
    try:
        assert net.force_ipv4() is True
        assert net.force_ipv4() is True      # second call is a no-op, still truthy
    finally:
        conn.allowed_gai_family = original
        net._applied = False


def test_force_ipv4_enabled_by_default():
    assert config.FORCE_IPV4 is True
