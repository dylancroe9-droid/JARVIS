# Asks for Dylan

Questions accumulated by autopilot agents that genuinely need Dylan's input. Each entry: agent, date, question, why it matters, and any default I'm using until he answers.

---

## car-flips · 2026-05-09 · soft-top "unknown vs bad" handling

**Question:** Region.md HARD RULE says skip any soft-top with reported issues. But how should I handle convertibles where the listing **doesn't mention the top at all** (silence ≠ confirmation it works)?

**Why it matters:** Tonight I skipped a 2005 BMW Z4 ($6k, 107k mi) because the listing said nothing about top condition. Z4 is on the target list as "only if soft top works perfectly, or hardtop coupe." If silence-defaults-to-skip, I'm filtering out viable cars where the top is fine. If silence-defaults-to-ask-the-seller, I need permission to send a one-line "does the top work?" message before filtering.

**Three options:**
1. **Strict (current behavior):** silence on top status = skip. Auditable, safe, but loses real candidates.
2. **Ask first:** allow me to send a single pre-screen message ("does the convertible top operate normally?") to soft-top listings before deciding. This breaks the "never contact sellers without explicit go" rule for a narrow purpose.
3. **Surface anyway, flag yellow:** include in daily list with a 🟡 marker reading "soft-top status unknown — confirm before any visit." You decide whether to call.

**My default until you decide:** option 1 (skip). Logged the Z4 in `~/Code/car-flips/skipped.md`.

**Recommendation:** option 3. Keeps the no-contact rule intact and puts the decision on you with full context, but doesn't lose the candidate.

**Update 2026-05-09 late evening (urgency: medium → high):** The night-2 scan added **3 more skipped cabrios** to the same pile — including a **2010 B8 S5 Cabriolet at $10,494 / 91k mi in Lawrenceville** that, if its top works, projects $2.5k–$5k margin (the biggest cabrio margin on today's board). Every day this stays at "default skip," real candidates leak past. Bumping ask to please-decide-this-week.

**Update 2026-05-09 even later (urgency: high, 4 candidates filtered):** Night-3 scan added a **4th skipped car** to the same pile — **2007 NC Miata Grand Touring at $9,990 / 93,760 mi in Marietta (16 mi from Buckhead)**. VIN-decoded as soft-top (not PRHT). This was the closest-distance, best-price NC Miata candidate of the week, and Miata GT is a sweet-spot target per `comps.md`. Loss rate now ~1 real candidate per scan. Pattern: silence-on-top-status is overwhelmingly the norm in dealer listings, not a red flag. Recommendation stands: option 3 (surface with 🟡 marker, no contact, you decide).

**Update 2026-05-09 deeper night (urgency: CRITICAL, 5 candidates filtered):** Night-5 scan added a **5th skipped car** — **2006 BMW Z4 3.0i Roadster at $8,949 / 79k mi in Marietta (12 mi from Buckhead)**. E85 soft-top, listing description not visible from search row. Loss rate is now ~2 candidates per scan and the cars filtered are getting closer to Buckhead and lower in mileage. **If you pick option 3 today, at minimum 2 of the 5 filtered cars (Lawrenceville S5 Cabrio at $10.5k / 91k mi, and Marietta Z4 at $8.9k / 79k mi) plausibly re-enter the leaderboard immediately.** Both are in top half of leaderboard candidate space.
