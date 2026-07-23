# Acme Support — Multi-Agent Call Script

A **multi-agent** voice scenario: one WebSocket session, three personas that hand
off to each other mid-call with no reconnect. Definition: [`acme_support.py`](acme_support.py).
This is the default demo (runs when `SCENARIO_FILE` is unset in `.env`).

## The desk

| Agent | Role | Model | Voice |
|---|---|---|---|
| **triage** | Front desk — routes the caller | `gpt-4o-mini` | `flux-priya-en` (house voice) |
| **billing** | Charges, balances, refunds | `gpt-4o-mini` | *inherits triage's* (seamless) |
| **tech** | Technical support specialist | `claude-sonnet-4-5` (Anthropic) | `flux-jack-en` (own voice) |

Two handoff *feels*, no mode flag:

- **triage → billing** shares one voice and a "keep helping as the same assistant"
  prompt, so the caller perceives **one** assistant that quietly gains billing tools.
- **triage/billing → tech** introduces itself and (on aura voices) sounds like a
  **distinct** specialist. It also runs a *different LLM provider* (Anthropic),
  showing per-agent think — history carries across the swap anyway.

## What to say

Work top to bottom for a natural ~2-minute call.

1. **Open + give a name (tests memory across handoffs):**
   > "Hi, my name is Sam Rivera. I have a question about my account balance."
   *(triage silently transfers to billing)*

2. **Ask billing to use a tool + recall earlier context:**
   > "What's my current balance — and do you remember the name I gave?"
   *(billing calls `get_account_balance`, answers, and recalls "Sam Rivera")*

3. **Switch topic to trigger the tech handoff:**
   > "Thanks. Actually my laptop won't turn on — it's a technical problem."
   *(billing → tech; tech introduces itself and may run `run_diagnostic`)*

4. **Prove cross-provider memory:**
   > "Before we troubleshoot — what name did I give earlier?"
   *(tech, running Anthropic, still recalls "Sam Rivera" from the OpenAI-driven turns)*

5. **Go back:**
   > "That's sorted, I have another billing question."
   *(tech → billing or triage)*

## Watch-fors

- **The active-agent badge** in the header shows the current persona, its model, and
  its voice — it updates on each transfer.
- **No double-talk:** the outgoing agent transfers silently; the incoming agent
  speaks first.
- **History survives** every handoff (name + balance), including across providers.

> ⚠️ **Voice note.** Mid-session voice switching currently works for `aura-2-*`
> voices but **not** for `flux-*` (Deepgram-side limitation — see the repo README).
> With the flux voices above, transfers won't be *heard* as a different voice: the
> whole call stays on triage's voice. For an audible distinct specialist, set the
> agents to `aura-2-*` voices in `acme_support.py`.
