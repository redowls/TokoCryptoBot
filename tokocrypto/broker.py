"""Tokocrypto signed REST client (MBX-compatible).

Differences from the Indodax TAPI client this replaces:

  * Auth is HMAC-SHA256 over the urlencoded parameter string, appended as
    `&signature=`, with the key in `X-MBX-APIKEY`. Both GET and POST carry
    everything in the query string — the docs permit it for both, and using one
    form removes a whole class of signing bug.
  * `side` is 0=BUY / 1=SELL and `type` is an int (2=MARKET), not a string.
  * There is NO bulk balance endpoint. /open/v1/account/spot/asset takes one
    asset at a time, so query USDT plus the open positions only — never the
    whole watchlist, which would be 11 calls a cycle for nothing.
  * Fills report `commission` AND `taxAmount` separately. Both are money leaving
    the account. Summing only the commission understates every trade, which is
    exactly how CryptoAutoBot came to overstate profit by 22%.
  * Market BUY is sized in USDT (`quoteOrderQty`); market SELL is sized in coin
    (`quantity`). That asymmetry is why buy and sell have separate entrypoints.
  * A spot balance carries no entry price, so the ledger — not the exchange —
    stays the source of truth for what a position cost.

Tokocrypto has no verified sandbox for this account. Every call here moves real
money once TRADING_ENABLED is true.
"""
import hashlib
import hmac
import time
import urllib.parse

import requests

from . import config, net, symbols


class BrokerError(Exception):
    pass


if config.FORCE_IPV4:
    net.force_ipv4()

# Order status enum (see the ENUM definitions in the API docs).
SYSTEM_PROCESSING, NEW, PARTIALLY_FILLED = -2, 0, 1
FILLED, CANCELED, REJECTED, EXPIRED = 2, 3, 5, 6
TERMINAL = {FILLED, CANCELED, REJECTED, EXPIRED}

BUY, SELL = 0, 1
LIMIT, MARKET = 1, 2


def _credentials():
    if not (config.TOKO_KEY and config.TOKO_SECRET):
        raise BrokerError("Tokocrypto credentials not configured "
                          "(TOKOCRYPTO_API_KEY/TOKOCRYPTO_API_SECRET)")
    return config.TOKO_KEY, config.TOKO_SECRET


def _sign(secret, payload):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _request(method, path, params=None, session=None):
    """Signed call. Returns the `data` block, raising on a non-zero code.

    The signature covers exactly the string that is sent, in the order it is
    sent — rebuilding the query after signing is the classic way to produce a
    valid-looking request the exchange rejects.
    """
    key, secret = _credentials()
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = config.RECV_WINDOW_MS
    qs = urllib.parse.urlencode(params)
    url = f"{config.OPEN_BASE_URL}{path}?{qs}&signature={_sign(secret, qs)}"
    caller = (session or requests).request
    try:
        r = caller(method, url,
                   headers={"X-MBX-APIKEY": key, "User-Agent": config.USER_AGENT},
                   timeout=20)
        r.raise_for_status()
        body = r.json()
    except BrokerError:
        raise
    except Exception as e:
        raise BrokerError(f"{method} {path}: {e}") from e
    if body.get("code") not in (0, None):
        raise BrokerError(f"{method} {path}: {body.get('msg') or body.get('message')} "
                          f"(code {body.get('code')})")
    return body.get("data") or {}


# --- account --------------------------------------------------------------

def get_asset(asset, session=None):
    """Free and locked balance for one asset."""
    return _request("GET", "/open/v1/account/spot/asset", {"asset": asset},
                    session=session)


def get_positions(price_by_symbol=None, assets=None, session=None):
    """Spot balances worth more than DUST_USDT, as position dicts.

    `assets` defaults to the keys of `price_by_symbol`, which the caller should
    keep to USDT plus whatever the ledger has open — one HTTP call each.
    """
    price_by_symbol = price_by_symbol or {}
    out = []
    for asset in (assets if assets is not None else list(price_by_symbol)):
        try:
            bal = get_asset(asset, session=session)
        except BrokerError:
            continue  # a transient read must not erase a real position
        qty = float(bal.get("free") or 0) + float(bal.get("locked") or 0)
        price = price_by_symbol.get(asset)
        if not qty or not price or qty * price < config.DUST_USDT:
            continue
        out.append({"symbol": asset, "qty": qty,
                    "free": float(bal.get("free") or 0),
                    "current_price": price,
                    "value": qty * price})
    return out


def get_account(price_by_symbol=None, session=None):
    """Equity in USDT: the cash balance plus the value of held coins."""
    cash = float(get_asset(config.QUOTE, session=session).get("free") or 0)
    held = get_positions(price_by_symbol, session=session)
    value = sum(p["value"] for p in held)
    return {"cash": cash, "positions_value": value, "equity": cash + value,
            "positions": held}


# --- orders ---------------------------------------------------------------

def _order_id(data):
    oid = data.get("orderId")
    if oid is None:
        raise BrokerError(f"order response carried no orderId: {data!r:.160}")
    return str(oid)


def market_buy_quote(sym_pair, usdt_amount, client_id=None, session=None):
    """Market BUY sized in USDT."""
    params = {"symbol": sym_pair, "side": BUY, "type": MARKET,
              "quoteOrderQty": str(usdt_amount)}
    if client_id:
        params["clientId"] = client_id
    return _order_id(_request("POST", "/open/v1/orders", params, session=session))


def market_sell_qty(sym_pair, qty, client_id=None, session=None):
    """Market SELL sized in coin."""
    params = {"symbol": sym_pair, "side": SELL, "type": MARKET, "quantity": str(qty)}
    if client_id:
        params["clientId"] = client_id
    return _order_id(_request("POST", "/open/v1/orders", params, session=session))


def get_order(order_id, session=None):
    return _request("GET", "/open/v1/orders/detail", {"orderId": order_id},
                    session=session)


def cancel_order(order_id, session=None):
    return _request("POST", "/open/v1/orders/cancel", {"orderId": order_id},
                    session=session)


def close_position(sym_pair, qty, session=None):
    """Exit is a market SELL — there is no close-position endpoint.

    Round to the symbol's step size first: fees are taken in-asset, so the held
    quantity drifts below the fill and an unrounded sell is rejected.
    """
    return market_sell_qty(sym_pair, symbols.round_qty(sym_pair, qty), session=session)


# --- fills ----------------------------------------------------------------

def fill_summary(sym_pair, order_id, limit=100, session=None):
    """Quantity, quote-weighted average price, and TOTAL cost for one order.

    `commission` is the exchange fee; `taxAmount` is Indonesian withholding
    charged on top. Both are money leaving the account, so both are summed into
    the returned `commission`. Reporting only the fee is the bug that made
    CryptoAutoBot overstate profit by 22% for months — it stays invisible until
    someone reconciles against the broker.
    """
    data = _request("GET", "/open/v1/orders/trades",
                    {"symbol": sym_pair, "limit": limit}, session=session)
    mine = [f for f in (data.get("list") or []) if str(f.get("orderId")) == str(order_id)]
    qty = sum(float(f.get("qty") or 0) for f in mine)
    quote = sum(float(f.get("quoteQty") or 0) for f in mine)
    cost = sum(float(f.get("commission") or 0) + float(f.get("taxAmount") or 0)
               for f in mine)
    return {"qty": qty,
            "price": (quote / qty) if qty else None,
            "quote": quote,
            "commission": cost,
            "fills": mine}


def avg_fill_price(sym_pair, order_id, session=None):
    return fill_summary(sym_pair, order_id, session=session)["price"]


def wait_for_fill(sym_pair, order_id, timeout_s=90, poll_s=3, sleep=time.sleep,
                  session=None):
    """Poll until the order reaches a terminal status or the timeout expires.

    Returns the last order dict seen either way; a non-terminal return means the
    caller must decide what to do about an order still working.
    """
    deadline = time.time() + timeout_s
    order = {}
    while time.time() < deadline:
        order = get_order(order_id, session=session)
        if order.get("status") in TERMINAL:
            return order
        sleep(poll_s)
    return order
