"""
Tests for the streaming concierge (tools/streaming.py).

JustWatch is mocked so these are fast and deterministic — no network. We verify
the decision logic: prioritise Dylan's own services, be honest when a title isn't
on them, never auto-open something he'd have to pay for, and never dead-end.
"""
import types
import pytest

import tools.streaming as s


def _result(title, year, offers):
    """Build a fake JustWatch result. offers = [(package_name, monetization, url), ...]."""
    def offer(pkg, mon, url):
        return types.SimpleNamespace(
            package=types.SimpleNamespace(name=pkg), monetization_type=mon, url=url
        )
    return types.SimpleNamespace(
        title=title, release_year=year, offers=[offer(*o) for o in offers]
    )


@pytest.fixture(autouse=True)
def opened(monkeypatch):
    """Stub the browser opener; record what WOULD have opened."""
    urls: list = []
    monkeypatch.setattr(s, "_open_in_chrome", lambda url: (urls.append(url), True)[1])
    return urls


def _mock_search(monkeypatch, results):
    import simplejustwatchapi.justwatch as jw
    monkeypatch.setattr(jw, "search", lambda *a, **k: results)


def test_on_a_service_you_have_opens_deep_link(monkeypatch, opened):
    _mock_search(monkeypatch, [_result("Wicked", 2024,
                 [("Peacock Premium", "FLATRATE", "https://peacocktv.com/watch/x")])])
    line = s.watch("Wicked")
    assert "Peacock" in line and "Pulling up" in line
    assert opened and "peacocktv.com/watch/x" in opened[0]


def test_and_play_changes_phrasing(monkeypatch, opened):
    _mock_search(monkeypatch, [_result("Wicked", 2024,
                 [("Peacock Premium", "FLATRATE", "https://x")])])
    assert s.watch("Wicked", and_play=True).startswith("Playing")


def test_not_on_your_services_does_not_barge_in(monkeypatch, opened):
    _mock_search(monkeypatch, [_result("The Last of Us", 2023,
                 [("Max", "FLATRATE", "https://max/x")])])
    line = s.watch("The Last of Us")
    assert "not one of your subscriptions" in line
    assert opened == []          # must ASK first, not auto-open a service he lacks


def test_rent_only_opens_page_and_says_rent(monkeypatch, opened):
    _mock_search(monkeypatch, [_result("Oppenheimer", 2023,
                 [("Apple TV", "RENT", "https://tv.apple.com/x")])])
    line = s.watch("Oppenheimer")
    assert "rent" in line.lower()
    assert opened          # opening the page is fine — it does not purchase


def test_not_found_falls_back_to_search(monkeypatch, opened):
    _mock_search(monkeypatch, [])
    line = s.watch("zzxqwerty")
    assert "search" in line.lower()
    assert opened and "google.com/search" in opened[0]


def test_empty_title_asks():
    assert "watching" in s.watch("").lower()


def test_classification_splits_correctly(monkeypatch, opened):
    _mock_search(monkeypatch, [_result("X", 2020, [
        ("Netflix", "FLATRATE", "n"),
        ("Max", "FLATRATE", "m"),
        ("Apple TV", "RENT", "a"),
    ])])
    info = s.where_to_stream("X")
    assert [e["service"] for e in info["yours"]] == ["Netflix"]      # he has it
    assert [e["service"] for e in info["other_sub"]] == ["Max"]      # he doesn't
    assert info["rent_buy"][0]["service"] == "Apple TV+"
    assert info["match"] == "X (2020)"


def test_user_services_default():
    assert set(s.user_services()) == {"Netflix", "Disney+", "Peacock", "Prime Video"}
