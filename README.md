# dg-agent-second

A minimal FastAPI app that serves a web page and proxies audio between the
browser and Deepgram's [Voice Agent API](https://developers.deepgram.com/docs/voice-agent)
over a WebSocket.

> **`multi` branch:** this branch turns the single agent into a **multi-agent**
> system (front desk → billing / technical support) using in-place transfers.
> See [Multi-agent transfers](#multi-agent-transfers) below.

The agents are configured in `agents.py`:

- **Listen:** Deepgram `nova-3` (speech-to-text)
- **Think:** OpenAI `gpt-4o-mini`
- **Speak:** Deepgram Aura-2, a different voice per persona

## Prerequisites

- A Deepgram API key, set in `.env`:

  ```
  DEEPGRAM_API_KEY=your_api_key_here
  ```

  (`.env.example` shows the expected format.)

- Dependencies from `requirements.txt` (`fastapi`, `uvicorn[standard]`,
  `websockets>=12`, `python-dotenv`). These are already installed in the
  shared virtualenv one level up at `samples/py/.venv`.

## Running

> **Note:** the virtualenv is **not** in this folder — it lives one directory
> up at `samples/py/.venv`.

From this project directory:

```bash
../.venv/bin/uvicorn main:app --reload --port 8000
```

Or activate the venv first:

```bash
source ../.venv/bin/activate
uvicorn main:app --reload --port 8000
```

Then open **http://127.0.0.1:8000** in your browser.

### Fresh setup (no venv yet)

If the shared venv is missing, create one and install the deps:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

## How it works

- `GET /` serves `static/index.html`.
- `WS /ws` accepts the browser's WebSocket, opens a connection to
  `wss://agent.deepgram.com/v1/agent/converse` (authenticated with
  `DEEPGRAM_API_KEY`), sends the agent `Settings`, then pipes audio bytes
  in both directions.
- Audio I/O: 16 kHz linear16 input from the browser, 24 kHz linear16 output
  from the agent.

## Multi-agent transfers

Personas (`agents.py`) share one WebSocket session. The orchestrator derives the
`transfer_to_agent` tool and routing from each agent's `transfers_to` edges:

```python
from orchestrator import Agent, Tool, Orchestrator

triage = Agent(
    name="triage", voice="aura-2-asteria-en", prompt="You are the assistant...",
    greeting="Hi, thanks for calling Acme Support!",
    transfers_to={"billing": "charges, balances, refunds",
                  "tech": "login/connectivity issues, errors"},
)
billing = Agent(                 # voice omitted -> inherits triage's voice
    name="billing", prompt="You are continuing as the same assistant, now billing...",
    tools=[Tool("get_account_balance", "Look up the balance.",
                {"type": "object", "properties": {}}, get_account_balance)],
    transfers_to={"tech": "technical issues", "triage": "anything else"},
)

orch = Orchestrator([triage, billing, tech], entry="triage",
                    send=send, think_provider={"type": "open_ai", "model": "gpt-4o-mini"})
await send(orch.initial_settings(audio))   # then feed every DG event to orch.handle()
```

When an agent calls `transfer_to_agent`, the session is **reconfigured in place**
— the WebSocket is never torn down and the conversation history carries across
automatically, so there's no reconnect and no summarization step. (Verified in
`selftest.py`: a name given to triage is recalled by billing after the swap.)

### Each agent can run its own model — even across providers

An `Agent` is effectively an `UpdateThink` payload (provider + model + prompt +
functions) plus an `UpdateSpeak` payload (voice). So each agent can declare its
own `model`/`provider`: a cheap, fast router can hand off to a stronger specialist
model mid-call. In this sample `triage`/`billing` run `gpt-4o-mini` while `tech`
runs Anthropic `claude-sonnet-4`:

```python
tech = Agent(
    name="tech", voice="aura-2-orion-en",
    provider="anthropic", model="claude-sonnet-4-20250514",   # its own brain
    prompt="You are a technical support specialist...",
    tools=[Tool("run_diagnostic", ...)],
    transfers_to={"billing": "...", "triage": "..."},
)
```

Conversation history survives the swap **even across providers** — verified in
`selftest.py`: `tech` (Anthropic) recalls a name that was only ever said to the
OpenAI-driven agents before the handoff.

### How a handoff *feels* is emergent — no mode flag

There is no "visible vs seamless" switch. Distinctness comes from config the app
already writes:

- **Voice** — set an agent's `voice` and switching to it changes the voice (a
  distinct specialist); **omit** it and the agent **inherits the current voice**,
  so the caller keeps hearing one person.
- **Prompt** — the incoming agent's prompt decides whether it introduces itself
  ("I'm technical support…") or just continues ("…keep helping as the same
  assistant").

So you can **mix within one graph**: in this sample, `triage`+`billing` share a
voice and use "continue" prompts (one perceived assistant that gains billing
tools), while `tech` has its own voice and introduces itself (a distinct
specialist).

The smoothness trick (in `orchestrator.py`): the orchestrator swaps `UpdateThink`
(and `UpdateSpeak`, only when the target declares a voice) **before** answering
the transfer tool call, so the *new* agent's follow-up is the first thing spoken
— no repeated "one moment" line — and the outgoing agent is told to transfer
silently.

### Files

- **`agents.py`** — the personas, declared with `Agent` / `Tool`. User config.
- **`orchestrator.py`** — `Agent`, `Tool`, `Orchestrator`: the typed surface plus
  the transfer-handshake engine. This is the reusable piece — a candidate to live
  in the Deepgram SDK.
- **`main.py`** — the proxy: pipes audio and feeds Deepgram events to the
  orchestrator.
- **`selftest.py`** — mic-free end-to-end test (TTS-synthesized user speech).
  Run: `../.venv/bin/python selftest.py` (or `selftest.py smoke`).

Try it: ask "what's my balance?" — the assistant silently gains billing tools and
answers in the *same* voice (seamless); then say "my laptop won't turn on" and you
hand off to `tech`, who introduces itself in a *different* voice (distinct specialist).

## Live (interim) transcripts via Flux

Controlled by the **Interim** toggle in the UI header (per session; locked while
a conversation is running). `ENABLE_FLUX_INTERIM` in `.env` only seeds the
toggle's *initial* position — the user's choice is then remembered in
`localStorage`. When off, user turns are shown from the agent's own final
`ConversationText`.

The Voice Agent socket only emits **final** user turns (`ConversationText`) —
it has no interim/partial transcript event. To show text *as you speak*, the
proxy opens a second, parallel STT connection to Deepgram **Flux**
(`wss://api.deepgram.com/v2/listen?model=flux-general-en`) and feeds it the same
mic audio.

Wiring:

- `GET /config` returns `{ "fluxInterimDefault": <bool> }` — the toggle's default.
- On **Start**, the client connects to `ws://…/ws?interim=1` (or `0`). The proxy
  opens Flux only when `interim=1`.
- The proxy then sends a `ProxyConfig` message reporting the **actual** state
  (`fluxInterim`), so if Flux couldn't connect the client still shows the agent's
  user transcript instead of suppressing it.
- Frontend renders user speech from Flux: an italic/dimmed interim bubble that
  updates on each `Update`, then solidifies on `EndOfTurn`.

- Flux `TurnInfo` events are relayed to the browser re-tagged as `FluxTurnInfo`
  (so they never collide with agent event types).
- The frontend renders user speech from Flux: an italic/dimmed **interim** bubble
  that updates on each `Update`, then solidifies into a final message on
  `EndOfTurn`. The agent's redundant `ConversationText` for `role: user` is
  suppressed to avoid duplicate bubbles; assistant turns still come from the
  agent.
- The Flux connection is **best-effort** — it's wrapped in its own try/except,
  so if the key can't reach Flux the agent still works (you just lose live text).
- Flux's `encoding`/`sample_rate` must match the agent's `audio.input` settings
  in `main.py` (currently `linear16` @ 16 kHz).
