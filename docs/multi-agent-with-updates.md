# Multi-Agent Voice Architecture with In-Place Updates

A multi-agent voice system lets a single call be handled by several specialized
agents (a router, a billing specialist, a technical specialist) instead of one
overloaded prompt. This page describes how to build that on Deepgram's
[Voice Agent API](https://developers.deepgram.com/docs/voice-agent) **without ever
tearing down the WebSocket**, by reconfiguring the live session with the
`UpdateThink`, `UpdateSpeak`, and `UpdatePrompt` commands.

It is a companion to the
[Multi-Agent Architecture](https://developers.deepgram.com/docs/multi-agent-architecture)
guide, which builds the same idea by **opening a new agent session per agent** and
summarizing the conversation across each handoff. Both work; this page explains
the in-place alternative and when to prefer it.

> **TL;DR:** One WebSocket for the whole call. On handoff, send `UpdateThink`
> (new prompt + tools + model) and optionally `UpdateSpeak` (new voice). The
> conversation history is kept **server-side**, so the next agent already has the
> full context, with **no summarizer, no reconnect, no dead air**, and the swap is
> measurably faster (roughly 3 to 5 times in our tests).

---

## Why in-place updates?

The classic way to run multiple agents is to spin up a fresh Voice Agent session
for each one. That works, but every handoff pays for:

- **Re-establishing the connection:** a new WebSocket, TLS handshake, and
  `Settings` round trip before the next agent can speak.
- **Re-passing context:** the new session starts empty, so you must summarize the
  conversation (often with a separate LLM call) and inject it into the new prompt.
- **Bridging audio:** the caller's media path must be kept alive across the gap or
  they hear silence.

Deepgram's Voice Agent API can instead **reconfigure the running session**:

| Command | Replaces | Use for |
|---|---|---|
| [`UpdateThink`](https://developers.deepgram.com/docs/voice-agent-update-think) | provider + model + prompt + functions (atomic) | **switching agents** |
| [`UpdateSpeak`](https://developers.deepgram.com/docs/voice-agent-update-speak) | the TTS voice | giving an agent a distinct voice |
| [`UpdatePrompt`](https://developers.deepgram.com/docs/voice-agent-update-prompt) | *appends* to the prompt (does not replace) | injecting dynamic context mid-agent |

Because the session never closes, the conversation transcript persists on
Deepgram's side and is replayed to whichever model is active. Switching agents
becomes "swap the brain," not "start a new call."

---

## Architecture overview

```
           Browser / phone  (mic + speaker)
                           │
                           │  audio in/out, continuous (never interrupted)
                           ▼
   ┌──────────────────────────────────────────────┐
   │               Your app / proxy               │
   │       Orchestrator: routing + handoff        │
   └───────────────────────┬──────────────────────┘
                           │
                           │  on transfer_to_agent(target):
                           │    1. UpdateThink  (prompt + tools + model)
                           │    2. UpdateSpeak  (voice, if target sets one)
                           ▼
   ┌──────────────────────────────────────────────┐
   │             Deepgram Voice Agent             │
   │         (one session, never closed)          │
   │                                              │
   │         Listen  ->  Think  ->  Speak         │
   │                                              │
   │    conversation history kept server-side,    │
   │     replayed to whatever model is active     │
   └──────────────────────────────────────────────┘
```

The orchestrator never manages multiple connections, audio bridging across
sessions, or context summarization. It owns one socket and sends Update commands.

---

## The Update commands

### `UpdateThink` swaps provider + model + prompt + functions, atomically

A single message replaces the entire think configuration mid-session and the
server acknowledges with `ThinkUpdated`. Because provider and model are part of
it, **each agent can run its own model, even its own provider**: a cheap, fast
router can hand off to a stronger specialist model mid-call. `UpdateSpeak` swaps
the voice the same way; `UpdatePrompt` *appends* to the prompt without replacing
it (handy for injecting dynamic context within an agent).

### Conversation history lives server-side, so there's no summarizer

Because there is one continuous session, you **do not pass context yourself**.
`UpdateThink` replaces the *configuration*, not the conversation. Whatever model
is active sees the prior turns automatically.

> In our testing this held even **across providers**: an Anthropic specialist
> correctly recalled a customer's name that was only ever spoken to the
> OpenAI-driven router before the handoff.

This is the biggest practical difference from reconnect-based multi-agent, which
needs a summarization step on every transfer.

---

## One way to implement it

Here is how you could build multi-agent handoff on top of those commands. The
`Agent` and `Orchestrator` below are a small amount of example code (in this
sample, not the SDK); an agent is essentially a bundle of an `UpdateThink` payload
and an `UpdateSpeak` payload.

```python
@dataclass
class Agent:
    name: str
    prompt: str
    model: str | None = None        # e.g. "gpt-4o-mini", "claude-sonnet-4-..."
    provider: str | None = None     # e.g. "open_ai", "anthropic"
    voice: str | None = None        # omit to inherit the current voice
    tools: list[Tool] = ...
    transfers_to: dict[str, str] = ...   # {target: when to use}  derives the transfer tool
```

### A derived `transfer_to_agent` tool

The orchestrator generates one `transfer_to_agent` function (with the reachable
targets as an enum) from each agent's `transfers_to` edges, and intercepts the
call to drive the handoff, so you don't hand-write the tool schema.

### Swap before you respond (the smooth-handoff trick)

When the model calls `transfer_to_agent`, answer it *last*, not first:

1. Send `UpdateThink` for the target, then wait for `ThinkUpdated`.
2. If the target declares its own voice, send `UpdateSpeak` and wait for `SpeakUpdated`.
3. **Then** answer the tool call (`FunctionCallResponse`).

Answering the tool call is what triggers the model's next turn, and by then the
*new* agent is active, so its follow-up is the first thing spoken. The outgoing
agent is told (in the transfer tool's description) to hand off silently. The
result: no repeated "one moment" line, no double-greeting.

### Distinct vs. seamless, from config

How a handoff *feels* falls out of the agent config, with no extra setting:

- **Voice:** set an agent's `voice` and the caller hears a distinct specialist;
  omit it and the agent inherits the current voice, so the caller keeps hearing
  one person.
- **Prompt:** the incoming agent's prompt decides whether it introduces itself
  ("I'm technical support...") or just continues ("...keep helping as the same
  assistant").

This lets you **mix within one graph**: a "super-agent" of several configs that
share a voice and continue seamlessly, alongside a clearly separate specialist
with its own voice.

---

## The agents (example)

| Agent | Model | Voice | Role |
|-------|-------|-------|------|
| `triage` (entry) | `gpt-4o-mini` | `aura-2-asteria-en` | Routes the caller |
| `billing` | `gpt-4o-mini` | *(inherits triage's)* | Balances, charges, refunds |
| `tech` | `claude-sonnet-4` | `aura-2-orion-en` | Troubleshooting |

`triage` + `billing` share a voice and use "continue" prompts, so the caller
perceives **one assistant that gains billing tools**. `tech` has its own voice and
model, so it's perceived as a **distinct specialist**, all from config.

```python
from orchestrator import Agent, Tool, Orchestrator

triage = Agent(
    name="triage", voice="aura-2-asteria-en",
    prompt="You are the assistant for Acme Support. Route billing vs technical...",
    greeting="Hi, thanks for calling Acme Support! How can I help?",
    transfers_to={"billing": "charges, balances, refunds",
                  "tech": "technical problems, errors, outages"},
)

billing = Agent(                       # voice omitted, so it stays seamless
    name="billing",
    prompt="You are continuing as the same assistant, now handling billing...",
    tools=[Tool("get_account_balance", "Look up the balance.",
                {"type": "object", "properties": {}}, get_account_balance)],
    transfers_to={"tech": "technical issues", "triage": "anything else"},
)

tech = Agent(
    name="tech", voice="aura-2-orion-en",
    provider="anthropic", model="claude-sonnet-4-20250514",   # its own brain
    prompt="You are a technical specialist. Introduce yourself briefly, then help...",
    tools=[Tool("run_diagnostic", "Run a device health check.",
                {"type": "object", "properties": {}}, run_diagnostic)],
    transfers_to={"billing": "billing questions", "triage": "anything else"},
)

orch = Orchestrator([triage, billing, tech], entry="triage",
                    send=send, think_provider={"type": "open_ai", "model": "gpt-4o-mini"})
```

The `transfer_to_agent` tool and routing are **derived automatically** from each
agent's `transfers_to` edges; you don't hand-write the tool schema.

---

## Implementation details

### The Update messages

```jsonc
// Swap the brain: provider + model + prompt + functions (full replace)
{ "type": "UpdateThink",
  "think": { "provider": { "type": "anthropic", "model": "claude-sonnet-4-20250514" },
             "prompt": "You are a technical specialist...",
             "functions": [ /* this agent's tools */ ] } }

// Swap the voice (only when the target declares one)
{ "type": "UpdateSpeak",
  "speak": { "provider": { "type": "deepgram", "model": "aura-2-orion-en" } } }
```

The server acknowledges with `ThinkUpdated` and `SpeakUpdated` respectively.

### The handoff sequence

```
FunctionCallRequest(transfer_to_agent)
  → UpdateThink(target prompt + tools + model)        → await ThinkUpdated
  → UpdateSpeak(target voice)  [only if target sets one]  → await SpeakUpdated
  → FunctionCallResponse(transferring)   (released last, so the NEW agent speaks next)
```

### Keep the session fed

The Voice Agent socket expects a continuous audio stream; if it goes quiet for
too long the server closes it (`CLIENT_MESSAGE_TIMEOUT`). A live mic or phone leg
satisfies this naturally. If your transport can go silent (hold music, a pause),
keep sending audio frames or `KeepAlive` messages, including across the handoff.

---

## Quick start

**Prerequisites**

- A Deepgram API key in `.env` (`DEEPGRAM_API_KEY=...`).
- Python 3.9+ and the dependencies in `requirements.txt`.
- A modern browser (the sample uses the browser mic; no telephony required).

**Run**

```bash
../.venv/bin/uvicorn main:app --reload --port 8000
# open http://127.0.0.1:8000
```

Say *"what's my balance?"* and the assistant silently gains billing tools and
answers in the **same** voice (seamless). Then say *"my laptop won't turn on"* and
you're handed to `tech`, who introduces itself in a **different** voice (distinct
specialist). The active agent, its voice, and its model are shown as a badge in
the header.

**Verify it without a microphone**

```bash
../.venv/bin/python selftest.py     # drives a full scenario via synthesized speech
```

---

## Example conversation flow

```
Agent (triage, asteria voice):  Hi, thanks for calling Acme Support! How can I help?
You:                            Hi, I'm Sam Rivera. What's my account balance?
                                [transfer_to_agent → billing]   (UpdateThink; voice inherited)
Agent (billing, same voice):    Your balance is $1,234.56.      [get_account_balance]
You:                            Actually my laptop won't turn on.
                                [transfer_to_agent → tech]      (UpdateThink + UpdateSpeak)
Agent (tech, orion voice):      I'm technical support, let's take a look. [run_diagnostic]
                                All systems look nominal; is it plugged in and charging?
```

Note that `billing` answered without re-greeting (same perceived person), `tech`
introduced itself in a new voice, and neither agent needed the customer's name
re-stated; it was already in the shared history.

---

## Advantages over reconnect-per-agent

| | Reconnect per agent | In-place Update |
|---|---|---|
| Sessions | new WebSocket each agent | **one**, whole call |
| Conversation history | lost; must summarize + re-inject | **retained server-side** |
| Extra context plumbing | summarizer LLM call per handoff | **none** |
| Audio continuity | gap / dead air during reconnect | **uninterrupted** |
| Per-agent model / provider | yes (new `Settings`) | yes (`UpdateThink`) |
| Handoff latency (measured*) | **~948 ms** US / ~215 ms EU, **plus summarizer + audio re-stream** | **~300 ms** US / ~47 ms EU |
| Context isolation between agents | yes (fresh session) | no (shared history) |

\* Measured with `latency_test.py` (8 iterations, single client location). The
reconnect figure is connection + `Settings` to `SettingsApplied` only; it does
**not** include the summarizer call or re-streaming audio, both of which a real
reconnect handoff also pays and in-place Update avoids. Seamless handoffs (no
voice change) are even faster, just the `UpdateThink` round trip.

---

## When to use which

**Prefer in-place Update** (this page) when you want the caller to experience one
continuous conversation, low handoff latency, and minimal plumbing, the common
case for routing, escalation, and specialist hand-offs.

**Reconnect-per-agent** (the original guide) still makes sense when you need:

- **Context isolation:** a specialist that must *not* see earlier turns (e.g.,
  compliance, or a clean-slate sub-task). Shared server-side history is
  all-or-nothing.
- **A different audio profile:** `UpdateThink`/`UpdateSpeak` don't change the
  audio encoding/sample rate or the STT model; a fresh `Settings` does.

A couple of operational notes for the Update approach:

- **History grows for the whole call.** It's automatic, but very long sessions
  accumulate tokens; consider `UpdatePrompt`-based compaction for marathon calls.
- **Function-calling reliability varies by model.** The handoff depends on the
  model emitting the `transfer_to_agent` call; test each model you route to.

---

## Project structure

```
agents.py         Personas, declared with Agent / Tool (your config)
orchestrator.py   Agent, Tool, Orchestrator: derives the transfer tool, owns the
                  in-place handoff (the reusable core)
main.py           FastAPI proxy: browser to Deepgram, feeds events to the orchestrator
static/index.html Browser UI (mic capture, playback, active-agent badge)
selftest.py       Mic-free end-to-end test (synthesized speech)
latency_test.py   In-place Update vs reconnect latency benchmark
```

---

## Additional resources

- [Voice Agent API](https://developers.deepgram.com/docs/voice-agent)
- [`UpdateThink`](https://developers.deepgram.com/docs/voice-agent-update-think),
  [`UpdateSpeak`](https://developers.deepgram.com/docs/voice-agent-update-speak),
  [`UpdatePrompt`](https://developers.deepgram.com/docs/voice-agent-update-prompt)
- [Multi-Agent Architecture (reconnect-based companion guide)](https://developers.deepgram.com/docs/multi-agent-architecture)
