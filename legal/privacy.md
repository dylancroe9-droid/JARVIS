# JARVIS — Privacy Policy

**Last updated: 2026-05-09**

> **NOTE FOR DYLAN:** This is a small-app boilerplate template based on
> common-sense practice and what JARVIS actually does today. It is *not*
> legal advice. Before publishing on a marketing site or App Store
> listing, have an attorney review (or use Iubenda / Termly /
> PrivacyPolicies.com to generate a reviewed version). Replace any
> bracketed `[…]` placeholders.

JARVIS ("we", "our", "the app") is a desktop voice assistant that runs
locally on your Mac. We've designed it to keep as much of your data on
your device as possible.

## What runs on your device, not on our servers

The following happen **only on your Mac** — we never see them:

- **Microphone audio.** Your voice is processed locally by [OpenAI Whisper](https://github.com/openai/whisper)
  for speech-to-text. Audio buffers are kept in memory only long enough
  to transcribe a single utterance, then discarded.
- **Wake-word detection.** All wake-word checking happens locally.
- **Conversation history.** JARVIS maintains a short-term memory in
  `~/JARVIS/memory.txt` on your machine. We don't read it.
- **Screen captures.** When you ask JARVIS to look at your screen, the
  capture is sent directly from your Mac to your chosen LLM provider's
  vision API. It never touches our infrastructure.

## What goes to third-party services you configure

You provide your own API keys for the following providers, and your
requests go directly to them under your account:

| Service       | What we send                              | Why                       |
|---------------|-------------------------------------------|---------------------------|
| Anthropic     | Your prompts + relevant tool results      | The "brain"               |
| Groq (optional) | Your prompts                            | Faster fallback brain     |
| ElevenLabs (optional) | The text JARVIS is about to speak | Higher-quality voice    |
| Microsoft edge-tts (free) | The text JARVIS is about to speak | Default voice (no key) |
| OpenWeather / Wikipedia | Specific queries you trigger    | "What's the weather", etc. |

Each of these providers has their own privacy policy and terms. By using
JARVIS with their keys, you agree to their terms. We strongly recommend
reading them.

We do not relay or proxy any of these requests. They go directly from
your Mac to the provider.

## API keys

API keys you enter through the setup wizard or settings panel are
written to `~/JARVIS/.env` on your local disk with `0600` (owner-only)
permissions. They are never transmitted to us. If you uninstall JARVIS,
delete that file to remove your keys.

## Telemetry and analytics

JARVIS does not currently send any telemetry, analytics, or crash
reports to us. [If/when this changes — e.g., we add an opt-in
"anonymous usage stats" toggle — we will list exactly what is collected
here and require your consent before turning it on.]

## Updates

When you check for updates or download a new version, your IP address
and operating system are visible to our update server (or to GitHub if
we host releases there). We do not store IP addresses beyond standard
web-server logs (typically retained for [30] days for security
purposes).

## Children

JARVIS is not directed at children under 13. If you are a parent or
guardian and believe your child has used JARVIS to enter personal
information, contact us and we'll help you delete it.

## Your rights

Since most of your data never leaves your device, "deletion" is
straightforward: uninstall JARVIS and remove `~/JARVIS/`. To remove
data from third-party providers (Anthropic conversation logs,
ElevenLabs voice generations, etc.), use those providers' own deletion
tools — we cannot delete data we never received.

## Changes to this policy

We'll update the "Last updated" date above and post material changes
prominently in the app or on our website. Continued use after changes
means you accept them.

## Contact

Questions, concerns, or requests:
- Email: **[support@jarvis.app — replace with real contact]**
- We aim to respond within 5 business days.
