"""Outbound address-family control.

This VPS has both an IPv4 address and an IPv6 address, and www.tokocrypto.com
(Cloudflare) publishes both A and AAAA records. Python's getaddrinfo returns the
IPv6 result first, so by default every request leaves over IPv6 and Tokocrypto sees
the v6 source address — while the API key's whitelist holds the v4 one. The
symptom is `[-2015] Invalid API-key, IP, or permissions` on a key whose credentials are
perfectly valid.

Pinning to IPv4 makes the source address deterministic and matches what an
exchange whitelist can actually express. Set FORCE_IPV4=false to undo.
"""
import socket

_applied = False


def force_ipv4():
    """Restrict urllib3 (and therefore requests) to IPv4. Idempotent."""
    global _applied
    if _applied:
        return True
    try:
        import urllib3.util.connection as urllib3_connection
    except ImportError:
        return False
    urllib3_connection.allowed_gai_family = lambda: socket.AF_INET
    _applied = True
    return True


def source_address_for(host, port=443, timeout=10):
    """The local address actually used to reach `host` — what the remote sees.

    Diagnostic helper: this is the value to put in an API key's IP whitelist.
    """
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    if _applied:
        infos = [i for i in infos if i[0] == socket.AF_INET] or infos
    family, socktype, proto, _, sockaddr = infos[0]
    s = socket.socket(family, socktype, proto)
    try:
        s.settimeout(timeout)
        s.connect(sockaddr)
        return s.getsockname()[0]
    finally:
        s.close()
