"""
Tests for the capability batch added in the 'implement everything' pass:
live_data (stocks/crypto/sports), mac_extras (documents/images/facetime/maps),
health_nudges (recurring reminders). Network is mocked so these are deterministic.
"""
import tools.live_data as ld
from tools.health_nudges import health_reminders, get_nudger
from tools.mac_extras import (
    read_document, convert_image, facetime_call, get_directions,
)


def teardown_function():
    get_nudger().stop()          # never leak nudge threads between tests


# ── health_nudges ────────────────────────────────────────────────────────────

def test_health_start_list_stop():
    assert "45 minutes" in health_reminders("start", "water", 45)
    assert "water" in health_reminders("list")
    health_reminders("stop", "water")
    assert "No health reminders" in health_reminders("list")


def test_health_alias_hydrate_maps_to_water():
    assert "water" in health_reminders("start", "hydrate", 30).lower()


def test_health_bad_kind_lists_options():
    assert "which one" in health_reminders("start", "dancing").lower()


def test_health_stop_all():
    health_reminders("start", "water", 30)
    health_reminders("start", "eyes", 20)
    health_reminders("stop")
    assert "No health reminders" in health_reminders("list")


# ── mac_extras ───────────────────────────────────────────────────────────────

def test_read_document_txt(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("hello sir, this is a test")
    assert "hello sir" in read_document(str(f))


def test_read_document_missing_file():
    assert "Can't find" in read_document("/nope/does-not-exist.txt")


def test_convert_image_missing_file():
    assert "Can't find" in convert_image("/nope.png", "jpeg")


def test_convert_image_rejects_bad_format(tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(b"\x89PNG\r\n")          # exists; format check runs before sips
    assert "Can't convert" in convert_image(str(f), "exe")


def test_facetime_empty_guard():
    assert "Who should I call" in facetime_call("")


def test_directions_empty_guard():
    assert "Where to" in get_directions("")


# ── live_data (HTTP mocked) ──────────────────────────────────────────────────

def test_stock_parse(monkeypatch):
    monkeypatch.setattr(ld, "_get", lambda u, timeout=8:
        '{"chart":{"result":[{"meta":{"regularMarketPrice":200.0,"chartPreviousClose":190.0}}]}}')
    out = ld.get_stock_price("aapl")
    assert "AAPL" in out and "$200.00" in out and "+5.3%" in out


def test_stock_not_found(monkeypatch):
    monkeypatch.setattr(ld, "_get", lambda u, timeout=8: '{"chart":{"result":[]}}')
    assert "Couldn't find" in ld.get_stock_price("zzzz")


def test_crypto_parse_with_alias(monkeypatch):
    monkeypatch.setattr(ld, "_get", lambda u, timeout=8:
        '{"bitcoin":{"usd":65000,"usd_24h_change":2.5}}')
    out = ld.get_crypto_price("btc")       # alias btc → bitcoin
    assert "$65,000" in out and "+2.5%" in out


def test_sports_parse(monkeypatch):
    fake = ('{"events":[{"competitions":[{"competitors":['
            '{"team":{"abbreviation":"LAL"},"score":"100"},'
            '{"team":{"abbreviation":"BOS"},"score":"98"}]}],'
            '"status":{"type":{"shortDetail":"Final"}}}]}')
    monkeypatch.setattr(ld, "_get", lambda u, timeout=8: fake)
    out = ld.get_sports_scores("nba")
    assert "LAL 100" in out and "BOS" in out and "Final" in out


def test_sports_empty_board(monkeypatch):
    monkeypatch.setattr(ld, "_get", lambda u, timeout=8: '{"events":[]}')
    assert "No" in ld.get_sports_scores("nhl")
