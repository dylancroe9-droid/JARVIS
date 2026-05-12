# Next steps — sign up for these to level up

These are the SaaS tools I recommended. Each one needs you to sign up
with your email — I can't do that for you. After you sign up, come back
and I'll wire them into the swarm.

---

## 🔥 Apollo.io — for finding business leads
**What:** 270M+ business contacts. Filter "Atlanta + no website + 1–10
employees" and get 500 verified phone numbers in 30 seconds. Has built-in
dialer and email sequencer.

**Sign up:** https://www.apollo.io/sign-up
- Free tier: **60 emails/month + 50 phone-number reveals/month**.
- Free credits reset monthly. Plenty for ~1 close/week.
- **Use your dylancroe9@gmail.com.**
- Skip the credit card for the free tier.

**After signup, come back and tell me:** *"apollo signed up"* — I'll show
you the exact filter to pull your first 500 Atlanta no-website leads.

---

## 🤖 Lindy.ai — for "AI agents that act 24/7"
**What:** Cloud-hosted AI agents with real integrations (Gmail, Calendar,
Slack, Twilio for actual phone calls, etc.). The "team of specialists"
pattern you wanted, but they actually run in the cloud and never need
your laptop on.

**Sign up:** https://app.lindy.ai/signup
- Free tier: **400 tasks/month** (each task ≈ one Lindy action).
- Pick the "Personal Use" template path.

**After signup:** I'll help you build:
- **Cold Caller Lindy** — pulls leads from Apollo, dials via Twilio,
  emails the warm ones.
- **Personal Assistant Lindy** — connects Gmail + Calendar, answers
  routine questions, surfaces important emails.

---

## 🎨 v0.dev — for building client websites in 60 seconds
**What:** Vercel's AI website builder. Type *"barber shop landing page in
Atlanta with booking link"* and get a deployed site in a minute. Way
faster than scaffolding Astro from scratch.

**Sign up:** https://v0.dev (Sign in with GitHub or Google)
- Free tier: **40 messages/day**.
- One client site usually takes 3–5 messages → you can ship 8 sites/day
  on the free tier.

**After signup:** I'll show you the exact prompt format to use per
client (you paste in the lead's notes from web-leads, get a finished
site in ~3 minutes).

---

## 💻 Cursor — for coding JARVIS faster
**Already installed at /Applications/Cursor.app.**
Cursor is an AI-native editor (forked from VS Code). Better for the
JARVIS dev work than OctoGent's wrapper because it has proper IDE
integration.

**Setup:**
1. Open Cursor (it's in your Applications folder).
2. Sign in with Google using **dylancroe9@gmail.com**.
3. Free tier is plenty: GPT-4o + Claude Sonnet + 50 fast premium
   requests/day.
4. Open the JARVIS folder: `File → Open Folder → ~/JARVIS`.
5. Cmd+K to ask Cursor to make a code change.
6. Cmd+L to chat with it about the codebase.

This replaces the `jarvis` tentacle in OctoGent for code work. You can
keep the OctoGent jarvis tentacle for tracking todos, but do the actual
edits in Cursor.

---

## 🚗 Marketplace alerts — for car flips
**What:** Auto-text or email you when a fun car drops below your budget
on Facebook Marketplace.

Two options:

### Option 1: Apify (easiest, free tier)
- https://apify.com/signup
- Use the "Facebook Marketplace Scraper" actor.
- Set search to your filters + email/webhook on new results.

### Option 2: IFTTT
- https://ifttt.com (Sign in with Google)
- Free tier: 2 applets.
- Watch a Craigslist RSS feed for keywords like "Miata" + price < $13k →
  send you SMS.

This replaces the `car-flips` tentacle in OctoGent — way more reliable
than Claude WebFetch on Facebook.

---

## Quick links

| What | Link |
|---|---|
| Apollo.io | https://www.apollo.io/sign-up |
| Lindy.ai | https://app.lindy.ai/signup |
| v0.dev | https://v0.dev |
| Cursor | (already installed) |
| Apify | https://apify.com/signup |
| IFTTT | https://ifttt.com |

---

## What stays in OctoGent

After all this, you'd ideally only use OctoGent / the chat UI for:
- **personal** — life ops via iMessage/Calendar (still useful here)
- **jarvis** — todo tracking + smaller refactors (real work goes to Cursor)

The other three tentacles (web-leads, web-build, car-flips) get
replaced by purpose-built SaaS tools. That's the upgrade path.
