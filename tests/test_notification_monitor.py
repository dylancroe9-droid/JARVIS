"""
Tests for notification_monitor's pure parsing helpers — the friendly-name
mapping and the log-line content extraction that turns a raw `log stream`
event into the "New <App> <body>" line JARVIS speaks.
"""

from tools.notification_monitor import (
    APP_NAMES, _friendly_name, _extract_log_content,
)


# ── _friendly_name ──────────────────────────────────────────────────────────

def test_known_bundle_maps_to_app_name():
    assert _friendly_name("com.apple.MobileSMS") == "Messages"
    assert _friendly_name("com.toyopagroup.picaboo") == "Snapchat"


def test_unknown_bundle_takes_last_segment_titlecased():
    assert _friendly_name("com.foo.bar") == "Bar"


def test_unknown_hyphenated_bundle_is_humanized():
    assert _friendly_name("com.acme.cool-app") == "Cool App"


def test_all_app_names_are_nonempty_strings():
    for bundle, name in APP_NAMES.items():
        assert isinstance(name, str) and name, bundle


# ── _extract_log_content ────────────────────────────────────────────────────

def test_title_and_body_combined():
    msg = 'req: title = "Mom" body = "call me back"'
    assert _extract_log_content(msg) == "from Mom: call me back"


def test_title_only():
    assert _extract_log_content('title = "Discord"') == "from Discord"


def test_body_only():
    assert _extract_log_content('body = "you have a match"') == "you have a match"


def test_subtitle_used_as_body_when_no_body():
    msg = 'title = "Slack" subtitle = "new message in #general"'
    assert _extract_log_content(msg) == "from Slack: new message in #general"


def test_private_redactions_dropped():
    # A fully-redacted line yields nothing rather than "from <private>: <private>"
    assert _extract_log_content('title = "<private>" body = "<private>"') == ""


def test_no_match_returns_empty():
    assert _extract_log_content("some unrelated log line with no fields") == ""


def test_output_truncated_to_200_chars():
    long_body = "x" * 500
    out = _extract_log_content(f'body = "{long_body}"')
    assert len(out) <= 200
