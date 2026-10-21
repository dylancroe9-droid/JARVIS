"""
Alex prepaid credits — the meter that makes selling Alex safe.

Until per-user billing exists Alex runs on Dylan's API key, so the balance
reaching zero MUST stop the model being called. These tests pin that, plus the
offline redeemable-code scheme and the cost→credit conversion.
"""

import importlib
import os

import pytest


@pytest.fixture
def credits(tmp_path, monkeypatch):
    # Point storage at a throwaway file and fix the tunables so the math is exact.
    monkeypatch.setenv("ALEX_CREDITS_FILE", str(tmp_path / "credits.json"))
    monkeypatch.setenv("ALEX_CREDIT_SECRET", "test-secret")
    monkeypatch.setenv("ALEX_COST_PER_CREDIT_USD", "0.01")
    monkeypatch.setenv("ALEX_FREE_TRIAL_CREDITS", "10")
    import brain.credits as c
    importlib.reload(c)
    yield c


class TestBalance:
    def test_new_user_gets_free_trial(self, credits):
        assert credits.balance() == 10
        assert credits.has_credit() is True

    def test_grant_adds(self, credits):
        assert credits.grant(50) == 60
        assert credits.balance() == 60


class TestCharging:
    def test_charge_converts_cost_to_credits(self, credits):
        # $0.05 at $0.01/credit = 5 credits.
        charged = credits.charge_usd(0.05)
        assert charged == 5
        assert credits.balance() == 5

    def test_cheap_turn_costs_at_least_one(self, credits):
        assert credits.charge_usd(0.0001) == 1
        assert credits.balance() == 9

    def test_expensive_turn_rounds_up(self, credits):
        # $0.082 (a verified practice test) / $0.01 = 8.2 → 9 credits.
        credits.grant(100)
        assert credits.charge_usd(0.082) == 9

    def test_charge_never_goes_negative(self, credits):
        # balance 10, a $1 turn would be 100 credits — cap at what's left.
        charged = credits.charge_usd(1.0)
        assert charged == 10
        assert credits.balance() == 0
        assert credits.has_credit() is False

    def test_zero_balance_stops(self, credits):
        credits.charge_usd(1.0)  # drain
        assert credits.balance() == 0
        # further charges do nothing (no model call should happen at this point)
        assert credits.charge_usd(0.05) == 0


class TestCodes:
    def test_mint_and_redeem(self, credits):
        code = credits.generate_code(250)
        assert code.startswith("ALEX-250-")
        ok, msg, bal = credits.redeem(code)
        assert ok is True
        assert bal == 260  # 10 trial + 250

    def test_code_is_single_use(self, credits):
        code = credits.generate_code(100)
        credits.redeem(code)
        ok, msg, bal = credits.redeem(code)
        assert ok is False
        assert "already" in msg.lower()
        assert bal == 110

    def test_forged_code_rejected(self, credits):
        ok, msg, bal = credits.redeem("ALEX-9999-ABCDEF-0000000000")
        assert ok is False
        assert bal == 10  # unchanged

    def test_tampered_amount_rejected(self, credits):
        code = credits.generate_code(50)
        # bump the amount, keep the old signature → must fail
        forged = code.replace("ALEX-50-", "ALEX-5000-")
        ok, _, bal = credits.redeem(forged)
        assert ok is False
        assert bal == 10

    def test_code_from_other_secret_rejected(self, credits, monkeypatch):
        good = credits.generate_code(50)
        monkeypatch.setenv("ALEX_CREDIT_SECRET", "a-different-secret")
        importlib.reload(credits)
        ok, _, _ = credits.redeem(good)
        assert ok is False

    def test_redeem_is_whitespace_and_case_tolerant(self, credits):
        code = credits.generate_code(20)
        ok, _, bal = credits.redeem("  " + code.lower() + "  ")
        assert ok is True
        assert bal == 30


class TestPersistence:
    def test_balance_survives_reload(self, credits):
        credits.grant(42)
        importlib.reload(credits)
        assert credits.balance() == 52

    def test_corrupt_file_does_not_crash(self, credits, tmp_path):
        path = tmp_path / "credits.json"
        path.write_text("{ this is not json")
        # load must not raise, and must not silently hand out a fresh free trial
        assert credits.balance() == 0
