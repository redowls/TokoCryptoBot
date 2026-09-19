import pytest

from tokocrypto import config, notify


class _Resp:
    def __init__(self, status=200):
        self.status_code = status


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")


def test_send_fans_out_to_every_chat(token, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_CHAT_IDS", ["111", "222", "333"])
    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append(json["chat_id"])
        return _Resp(200)

    monkeypatch.setattr(notify.requests, "post", fake_post)
    assert notify.send("hello") is True
    assert sent == ["111", "222", "333"]


def test_one_failing_chat_does_not_block_the_others(token, monkeypatch):
    """A blocked or deleted chat must not silence the remaining recipients."""
    monkeypatch.setattr(config, "TELEGRAM_CHAT_IDS", ["111", "222", "333"])
    sent = []

    def fake_post(url, json=None, timeout=None):
        cid = json["chat_id"]
        sent.append(cid)
        return _Resp(400 if cid == "222" else 200)   # middle chat rejects

    monkeypatch.setattr(notify.requests, "post", fake_post)
    assert notify.send("hello") is True              # the other two landed
    assert sent == ["111", "222", "333"]             # 333 still attempted


def test_raising_chat_does_not_block_the_others(token, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_CHAT_IDS", ["111", "222"])
    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append(json["chat_id"])
        if json["chat_id"] == "111":
            raise notify.requests.RequestException("network down")
        return _Resp(200)

    monkeypatch.setattr(notify.requests, "post", fake_post)
    assert notify.send("hello") is True
    assert sent == ["111", "222"]


def test_send_false_when_every_chat_fails(token, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_CHAT_IDS", ["111", "222"])
    monkeypatch.setattr(notify.requests, "post",
                        lambda *a, **k: _Resp(403))
    assert notify.send("hello") is False


def test_send_false_with_no_chats_configured(token, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_CHAT_IDS", [])
    monkeypatch.setattr(notify.requests, "post",
                        lambda *a, **k: pytest.fail("should not post with no chats"))
    assert notify.send("hello") is False


def test_send_false_without_token(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", None)
    monkeypatch.setattr(config, "TELEGRAM_CHAT_IDS", ["111"])
    assert notify.send("hello") is False


def test_chat_ids_parse_comma_separated_with_whitespace():
    parse = lambda v: [c.strip() for c in (v or "").split(",") if c.strip()]
    assert parse("111,222") == ["111", "222"]
    assert parse(" 111 , 222 ") == ["111", "222"]
    assert parse("111") == ["111"]                  # single id still works
    assert parse("") == []
    assert parse("111,,222") == ["111", "222"]      # tolerate a stray comma


def test_a_live_chat_is_configured():
    """At least one recipient, or every alert this bot raises goes nowhere."""
    assert config.TELEGRAM_CHAT_IDS, "TELEGRAM_CHAT_ID is unset in .env"
    assert all(c.isdigit() for c in config.TELEGRAM_CHAT_IDS)
