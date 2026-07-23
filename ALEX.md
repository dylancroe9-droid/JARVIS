# Alex — the homework product

Alex is the schoolwork assistant Dylan is selling. It is the **same codebase** as
JARVIS, separated at runtime by the `JARVIS_PRODUCT` flag. This doc is the handoff
for a Claude session dedicated to the Alex track, so it doesn't tangle with
JARVIS-the-assistant work happening in the other session.

## The one rule that keeps this sane

**One codebase, not a fork.** JARVIS and Alex share `brain/`, `tools/`, `voice/`,
`server.py`. A fork would mean every bug fix has to be made twice forever. Product
differences live in `brain/product.py` and are chosen by the `JARVIS_PRODUCT` env
var at run/build time. "Merge them into one app later" = drop the flag.

**Because the core is shared, changes to `brain/`, `tools/`, `voice/`, `server.py`
affect BOTH products.** Coordinate those with the JARVIS session. Alex-only work
(persona, tool whitelist, packaging, branding, pricing, distribution) is safe to do
here.

## Run Alex

```bash
JARVIS_PRODUCT=alex ./start.sh      # or: JARVIS_PRODUCT=alex python server.py
```

Default (no flag) = JARVIS.

## What Alex IS

- **Name / persona:** Alex — a warm, plain-spoken study partner (NOT JARVIS's dry
  British butler). Defined in `_alex_stable_prompt()` in `brain/personality.py`.
- **Tools:** a 13-tool whitelist in `brain/product.py` (`_ALEX_TOOLS`). Schoolwork
  core + light helpers (web_search, calculate, define_word…). Music, movies,
  stocks, smart home, ad-skip are **hidden, not deferred** — a student can't trip
  over them and the build can't be pointed off-task.
- **The two features that matter:**
  - Read a problem off the screen and answer it (`take_screenshot`), answer-first.
  - Generate a practice test with **independently verified answer keys**
    (`create_practice_test` / `show_test_answers`, in `tools/practice_test.py`).

## The boundary (do not cross)

Alex helps you **study** and generates **its own** practice tests it can auto-answer.
It does **not** take real graded tests, fill in school portals, or do assigned work.
That line is a business decision as much as an ethics one: parents buy study tools,
nobody buys a cheating app, and one such feature makes everything else unsellable.

## Current state (verified)

- Product profile system: built, 404 tests passing.
- Alex persona + 13-tool whitelist: built and unit-tested.
- Practice tests: verified live end-to-end as JARVIS (real tool call, real page,
  correct questions, verified keys). Not yet re-verified under the Alex flag.
- Screen-reading homework: verified 6/6 on AP questions (as JARVIS).

## Not yet done — the Alex roadmap

1. **Verify Alex live.** Boot `JARVIS_PRODUCT=alex`, confirm the persona, the tool
   whitelist, and that practice tests + screen reading work under the flag.
2. **Package `Alex.app`.** A build that bundles the Python brain (currently
   `jarvis-app/main.js` hardcodes `~/JARVIS/.venv/bin/python` — that must become a
   bundled runtime), blue accent (`#5b8dee`), Alex branding.
3. **The distribution wall** (shared with JARVIS): no code-signing identity, needs
   a terminal, "unidentified developer" gate. This is the real work.
4. **Per-user API key / billing.** Alex currently runs on Dylan's key. To sell it,
   each user's usage must bill somewhere. Prepaid **credits** are the plan (see
   pricing) — no stored cards, no surprise bills, sellable for cash at school.

## Pricing (from MEASURED costs)

| unit | cost |
|---|---|
| homework question (screenshot→answer) | $0.0069 |
| 6-question verified practice test | $0.0817 |

Blended realistic student: **~$2.55/user/month.**
- **$10/mo** → ~74% margin. ($5 loses money on heavy/exam-week users.)
- **Credits** (recommended): "250 questions for $10" ≈ 78% margin.
- Dylan's $20 Anthropic credit supports ~7 pilot users for a month.

## Legal / practical notes

- "JARVIS" is a Marvel trademark — fine for Dylan's private assistant, NOT for a
  sold product. "Alex" is the sellable name. Keep JARVIS branding out of Alex.
- Stripe and the Chrome/App Store developer accounts require someone 18+. A parent
  is in the loop for the money side earlier than expected. Credits-for-cash at
  school is the no-parent-needed first step.

## First move for this session

Bring Alex up live (`JARVIS_PRODUCT=alex`) and confirm it behaves — persona, tool
whitelist, a real practice test, a real screen read. Then start on packaging
`Alex.app`. Do NOT touch shared core (`brain/`, `tools/`, `voice/`, `server.py`)
without checking with the JARVIS session first.
