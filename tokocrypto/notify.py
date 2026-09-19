"""Telegram notifications (best-effort — a notify failure never blocks trading).

Alerts fan out to every id in TELEGRAM_CHAT_IDS. One chat failing (blocked bot,
deleted chat, network blip) must never stop the others from being told, so each
send is isolated and the result is True if *any* delivery succeeded.

Run `python -m tokocrypto.notify resolve` after pressing Start on the bot to
discover chat ids, or `... test` to send a delivery check to all of them.
"""
import sys

import requests

from . import config


def send_to(chat_id, text):
    """Deliver to one chat. Never raises."""
    if not (config.TELEGRAM_TOKEN and chat_id):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        return r.status_code == 200
    except requests.RequestException:
        return False


def send(text):
    """Fan out to every configured chat. True if at least one delivery landed."""
    delivered = False
    for chat_id in config.TELEGRAM_CHAT_IDS:
        # `or` short-circuits, so keep the call first — every chat must be tried
        delivered = send_to(chat_id, text) or delivered
    return delivered


def resolve_chat_id():
    """Chat ids that have messaged this bot. Empty until someone presses Start."""
    if not config.TELEGRAM_TOKEN:
        return []
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/getUpdates", timeout=15)
        updates = r.json().get("result", [])
    except (requests.RequestException, ValueError):
        return []
    seen = {}
    for u in updates:
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
        if chat.get("id"):
            seen[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    return sorted(seen.items())


def _main(argv):
    cmd = argv[1] if len(argv) > 1 else "resolve"
    if cmd == "resolve":
        found = resolve_chat_id()
        if not found:
            print("No chats yet — open Telegram, find the bot, and press Start.")
            return 1
        for cid, who in found:
            mark = "configured" if str(cid) in config.TELEGRAM_CHAT_IDS else "NOT configured"
            print(f"TELEGRAM_CHAT_ID={cid}  ({who}) — {mark}")
        return 0
    if cmd == "test":
        if not config.TELEGRAM_CHAT_IDS:
            print("no chat ids configured")
            return 1
        rc = 0
        for cid in config.TELEGRAM_CHAT_IDS:
            ok = send_to(cid, "TokoCryptoBot: delivery test ✅")
            print(f"  {cid}: {'sent' if ok else 'FAILED'}")
            rc |= 0 if ok else 1
        return rc
    print("usage: python -m tokocrypto.notify [resolve|test]")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
