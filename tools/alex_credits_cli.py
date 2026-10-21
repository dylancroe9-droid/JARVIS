#!/usr/bin/env python3
"""
Alex credit-code tool — for Dylan, not the student.

Mint the codes you sell for cash, check what a balance file looks like, or grant
credits to a pilot user. Codes are HMAC-signed and validated OFFLINE by the app,
so no server is involved.

IMPORTANT: the code you mint here is only valid in an app built with the SAME
ALEX_CREDIT_SECRET. Set that env var to your own secret and keep it private —
anyone with it can mint free credits.

Usage:
  # mint one code for 250 credits
  python -m tools.alex_credits_cli mint 250

  # mint 30 codes of 250 credits each (a batch to sell), to a CSV
  python -m tools.alex_credits_cli mint 250 --count 30 --csv codes.csv

  # what's the current balance on THIS machine?
  python -m tools.alex_credits_cli balance

  # grant credits straight into this machine's balance (pilot user on your Mac)
  python -m tools.alex_credits_cli grant 100
"""

from __future__ import annotations

import argparse
import sys

from brain import credits


def _mint(args) -> None:
    rows = [credits.generate_code(args.amount) for _ in range(args.count)]
    if args.csv:
        with open(args.csv, "w") as f:
            f.write("code,credits\n")
            for c in rows:
                f.write(f"{c},{args.amount}\n")
        print(f"Wrote {len(rows)} code(s) for {args.amount} credits each → {args.csv}")
    else:
        for c in rows:
            print(c)


def _balance(_args) -> None:
    print(f"{credits.balance()} credits")


def _grant(args) -> None:
    new = credits.grant(args.amount, reason="cli-grant")
    print(f"Granted {args.amount}. Balance now {new}.")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="alex_credits_cli", description="Alex credit codes")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mint", help="mint redeemable code(s)")
    m.add_argument("amount", type=int, help="credits per code")
    m.add_argument("--count", type=int, default=1, help="how many codes")
    m.add_argument("--csv", help="write codes to this CSV instead of stdout")
    m.set_defaults(func=_mint)

    b = sub.add_parser("balance", help="show this machine's balance")
    b.set_defaults(func=_balance)

    g = sub.add_parser("grant", help="add credits to this machine directly")
    g.add_argument("amount", type=int)
    g.set_defaults(func=_grant)

    args = p.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
