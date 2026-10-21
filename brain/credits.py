"""
Alex — prepaid credits.

Alex is sold to students who pay up front, in cash, at school. There is no
Stripe, no stored card, no server: a student buys a code, redeems it in the app,
and that tops up a local balance. Every turn spends credits against that balance;
when it hits zero Alex stops calling the model — which is the whole point, because
until per-user billing exists Alex runs on Dylan's API key and an uncapped app is
an uncapped bill.

The unit the student sees is a CREDIT, and one credit is roughly one homework
question. Internally we meter on the turn's ACTUAL measured API cost and convert
to credits, so an expensive turn (a verified practice test is a dozen model calls)
costs proportionally more than a one-line answer — no per-tool bookkeeping, and
the margin can't be gamed by asking for huge tests.

Codes are HMAC-signed so the app can validate them OFFLINE. The signing secret is
shared between Dylan's generator (see generate_code / the CLI in
tools/alex_credits_cli.py) and the app. This is deliberately lightweight: the
threat model is a high-schooler, not a payments processor, and the worst case is
a forged code granting credits, not stolen money. Set ALEX_CREDIT_SECRET to your
own value before building for real distribution.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

# ── Tunables ─────────────────────────────────────────────────────────────────
# One credit is worth this much API spend. A homework question measured at
# ~$0.0069, so 1 credit ≈ 1 question. Selling 250 credits for $10 is ~$0.04/credit
# of revenue against $0.0069 of cost → ~82% margin (matches ALEX.md pricing).
COST_PER_CREDIT_USD = float(os.getenv("ALEX_COST_PER_CREDIT_USD", "0.0069"))

# New users get a small free trial so the very first launch works without a code.
FREE_TRIAL_CREDITS = int(os.getenv("ALEX_FREE_TRIAL_CREDITS", "15"))

# Shared secret for code signing. OVERRIDE THIS before real distribution.
_SECRET = os.getenv("ALEX_CREDIT_SECRET", "alex-dev-secret-change-me").encode()

_CODE_PREFIX = "ALEX"
_SIG_LEN = 10  # hex chars of the HMAC kept in the code


def _store_path() -> Path:
    """Where the balance lives. Per-user, outside the app bundle so it survives
    updates. Override with ALEX_CREDITS_FILE (tests point this at a tmp file)."""
    override = os.getenv("ALEX_CREDITS_FILE")
    if override:
        return Path(override)
    base = Path.home() / "Library" / "Application Support" / "Alex"
    return base / "credits.json"


_lock = threading.RLock()


# ── Storage ──────────────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {"balance": FREE_TRIAL_CREDITS, "spent_usd": 0.0,
            "redeemed": [], "history": [], "seeded": True}


def _load() -> dict:
    path = _store_path()
    try:
        with open(path, "r") as f:
            state = json.load(f)
        # Repair any missing keys so an older/partial file never crashes a turn.
        for k, v in _default_state().items():
            state.setdefault(k, v if k != "seeded" else state.get("seeded", True))
        return state
    except FileNotFoundError:
        return _default_state()
    except (json.JSONDecodeError, OSError):
        # A corrupt file must never brick the app or silently wipe paid credits;
        # keep it aside for inspection and start fresh (no free trial re-grant).
        try:
            os.replace(path, str(path) + ".corrupt")
        except OSError:
            pass
        s = _default_state()
        s["balance"] = 0
        s["seeded"] = True
        return s


def _save(state: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + f".tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic


# ── Public: balance & spending ───────────────────────────────────────────────

def balance() -> int:
    with _lock:
        return int(_load().get("balance", 0))


def has_credit() -> bool:
    return balance() > 0


def _append_history(state: dict, delta: int, reason: str) -> None:
    state["history"].append({"ts": int(time.time()), "delta": delta, "reason": reason})
    # Keep the log bounded — it's for the user's own "where did my credits go".
    if len(state["history"]) > 500:
        state["history"] = state["history"][-500:]


def charge_usd(turn_cost_usd: float, reason: str = "turn") -> int:
    """
    Convert a turn's measured API cost to whole credits and deduct them.
    Returns the number of credits charged (minimum 1 for any real turn).
    Never drives the balance below zero.
    """
    if turn_cost_usd <= 0:
        credits = 1
    else:
        import math
        credits = max(1, math.ceil(turn_cost_usd / COST_PER_CREDIT_USD))
    with _lock:
        state = _load()
        credits = min(credits, int(state.get("balance", 0)))  # can't go negative
        if credits <= 0:
            return 0
        state["balance"] = int(state["balance"]) - credits
        state["spent_usd"] = round(float(state.get("spent_usd", 0.0)) + turn_cost_usd, 6)
        _append_history(state, -credits, reason)
        _save(state)
        return credits


def grant(credits: int, reason: str = "grant") -> int:
    """Add credits directly (pilot users, refunds, manual top-ups). Returns new balance."""
    credits = int(credits)
    with _lock:
        state = _load()
        state["balance"] = int(state.get("balance", 0)) + credits
        _append_history(state, credits, reason)
        _save(state)
        return state["balance"]


# ── Public: redeemable codes ─────────────────────────────────────────────────

def _sign(amount: int, nonce: str) -> str:
    msg = f"{amount}:{nonce}".encode()
    return hmac.new(_SECRET, msg, hashlib.sha256).hexdigest()[:_SIG_LEN]


def generate_code(amount: int, nonce: str | None = None) -> str:
    """Make a redeemable code for `amount` credits. Dylan runs this to mint codes
    he sells. Same secret in the app validates it offline."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    nonce = nonce or secrets.token_hex(3).upper()  # 6 hex chars
    sig = _sign(amount, nonce).upper()
    return f"{_CODE_PREFIX}-{amount}-{nonce}-{sig}"


def _parse_and_verify(code: str) -> tuple[bool, int, str]:
    """→ (valid, amount, nonce). Whitespace/case tolerant."""
    parts = (code or "").strip().upper().split("-")
    if len(parts) != 4 or parts[0] != _CODE_PREFIX:
        return False, 0, ""
    _, amount_s, nonce, sig = parts
    if not amount_s.isdigit():
        return False, 0, ""
    amount = int(amount_s)
    expected = _sign(amount, nonce).upper()
    if not hmac.compare_digest(expected, sig):
        return False, 0, ""
    return True, amount, nonce


def redeem(code: str) -> tuple[bool, str, int]:
    """
    Redeem a code. → (ok, message, new_balance).
    A code can only be redeemed once (nonce is recorded).
    """
    ok, amount, nonce = _parse_and_verify(code)
    if not ok:
        return False, "That code isn't valid — check for typos.", balance()
    with _lock:
        state = _load()
        if nonce in state.get("redeemed", []):
            return False, "That code has already been used.", int(state.get("balance", 0))
        state["balance"] = int(state.get("balance", 0)) + amount
        state["redeemed"].append(nonce)
        _append_history(state, amount, f"redeem:{nonce}")
        _save(state)
        return True, f"Added {amount} credits. You now have {state['balance']}.", state["balance"]


# ── Public: the message Alex shows when the tank is empty ─────────────────────

def out_of_credits_message() -> str:
    return ("You're out of credits, so I can't pull up new answers right now. "
            "Grab a top-up code and say \"redeem CODE\" to keep going.")
