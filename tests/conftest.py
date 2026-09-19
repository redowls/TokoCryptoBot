import pytest

from tokocrypto import symbols

PERMISSIVE = {
    "type": 1, "symbol": "ANY_USDT", "baseAsset": "ANY", "quoteAsset": "USDT",
    "filters": [
        {"filterType": "LOT_SIZE", "minQty": "0.00000001",
         "maxQty": "9000000.0", "stepSize": "0.00000001"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
    ],
}


class _AnySymbol(dict):
    """Metadata for any pair, so the suite does not break when the watchlist
    changes. Real per-symbol constraints are exercised in test_symbols.py."""

    def get(self, key, default=None):
        return PERMISSIVE


@pytest.fixture(autouse=True)
def _no_network_symbol_metadata(monkeypatch, request):
    """Keep symbol metadata off the network for every test.

    round_qty and meets_minimums sit on the sizing path, so without this the
    suite would hit tokocrypto.com. test_symbols.py drives the cache itself and
    opts out.
    """
    if request.node.fspath.basename == "test_symbols.py":
        return
    monkeypatch.setattr(symbols, "load", lambda *a, **k: _AnySymbol())
