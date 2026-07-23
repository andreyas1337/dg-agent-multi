# Acme Financial Services — Multi-Agent Call Script

A multi-agent **sales funnel**: one call flows through three specialists, each a
distinct phase. Definition: [`financial_advisory.py`](financial_advisory.py).

It mirrors Deepgram's [multi-agent architecture example](https://developers.deepgram.com/docs/multi-agent-architecture)
(Qualifier → Advisor → Closer) — but on this repo's **mid-session** orchestrator.
That example reopens a session per phase and needs an external LLM (Groq) to
summarize and re-inject context on each handoff; here the session **stays open**
and `UpdateThink`/`UpdateSpeak` swap the config in place, so the conversation
carries across automatically — **no reconnect, no summarization**.

## The funnel

| Phase | Agent | Role | Voice | Tools |
|---|---|---|---|---|
| 1 | **qualifier** ("Alex") | Qualify the lead — name, location, need | `aura-2-mars-en` | `end_conversation` |
| 2 | **advisor** | High-level guidance, recommend a consultation | `aura-2-thalia-en` | — |
| 3 | **closer** | Schedule follow-up + capture feedback | `aura-2-helena-en` | `schedule_followup`, `record_satisfaction`, `end_conversation` |

Linear flow: `qualifier → advisor → closer`. Each phase has its **own voice**
(aura switches mid-session correctly, so you actually hear the handoff) and opens
by acknowledging what you already said.

## What to say

You're the **lead** who asked to be called back. Alex opens the call.

1. **Confirm it's a good time:**
   > "Yeah, now's fine."

2. **Give your details (qualifier gathers, then hands to the advisor):**
   > "I'm Robert, I'm in Chicago, and I'm trying to figure out retirement planning."

3. **Engage the advisor (they should acknowledge your name + goal):**
   > "I'm forty-five and I'd like to retire by sixty — am I on track?"
   > "What kind of accounts should I be thinking about?"

4. **Ask to book (advisor hands to the closer):**
   > "Okay, let's set up a proper consultation."

5. **Schedule + rate (closer books it and asks for feedback):**
   > "Sometime next week works." · "Wednesday afternoon, maybe."
   > "Sure — I'd rate it a five."

## Watch-fors

- **Announced handoff** — Alex says a short "let me hand you over to an advisor"
  line first, *then* the voice switches to Jordan, who introduces himself. The
  swap waits for Alex's line to finish, so nothing talks over it. (This is the
  qualifier's `announce_transfer=True`; the advisor→closer hop stays seamless since
  they share a voice.)
- **The active-agent badge** updates on each handoff (Alex → advisor → closer),
  showing the phase, its model, and its voice.
- **Context carries with no summarization step** — the advisor greets you by name
  and knows your goal; the closer knows you discussed retirement. That's the
  mid-session orchestrator, not a re-injected summary.

> `end_conversation` is a mock: the orchestrator has no session-end primitive, so
> the agent says its closing line but the socket stays open — just click **Stop**.
