# dg-agent-second

A minimal FastAPI app that serves a web page and proxies audio between the
browser and Deepgram's [Voice Agent API](https://developers.deepgram.com/docs/voice-agent)
over a WebSocket.

> **`multi` branch:** this branch turns the single agent into a **multi-agent**
> system (front desk → billing / technical support) using mid-session transfers.
> See [Multi-agent transfers](#multi-agent-transfers) below.

The agents are configured in `scenarios/acme-support/acme_support.py`:

- **Listen:** Deepgram `nova-3` (speech-to-text)
- **Think:** per agent — `gpt-4o-mini` by default; `tech` runs Anthropic
  `claude-sonnet-4-5` (the Anthropic model is served via Deepgram, so the
  `DEEPGRAM_API_KEY` is the only credential needed)
- **Speak:** Deepgram Aura-2 — a distinct voice for `tech`; `billing` inherits
  `triage`'s voice (seamless)

## Prerequisites

- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** (recommended) —
  it manages Python and dependencies for you, identically on macOS, Linux, and
  Windows. A plain `pip` path is in [Without uv](#without-uv) if you prefer.
- A **Deepgram API key**.

Dependencies (`fastapi`, `uvicorn[standard]`, `websockets>=12`, `python-dotenv`) are
declared in `pyproject.toml`; uv installs them automatically on first run.

## Get started

1. **Add your API key.** Copy the example env file, then edit `.env` and set
   `DEEPGRAM_API_KEY`:

   ```bash
   cp .env.example .env          # Windows PowerShell: Copy-Item .env.example .env
   ```

2. **Run it** (uv creates a local `.venv` and installs deps on the first run — the
   command is the same on Windows, macOS, and Linux):

   ```bash
   uv run uvicorn main:app --reload --port 8000
   ```

3. Open **http://127.0.0.1:8000** in your browser.

Configuration lives in `.env` (scenario, voice, interim transcripts, access
password) — see the sections below and `.env.example`.

### Without uv

<details>
<summary>pip + virtualenv</summary>

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

**Windows (PowerShell)**

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

`requirements.txt` mirrors `pyproject.toml` and is what the Docker image uses.
</details>

## How it works

- `GET /` serves `static/index.html`.
- `WS /ws` accepts the browser's WebSocket, opens a connection to
  `wss://agent.deepgram.com/v1/agent/converse` (authenticated with
  `DEEPGRAM_API_KEY`), sends the agent `Settings`, then pipes audio bytes
  in both directions.
- Audio I/O: 16 kHz linear16 input from the browser, 24 kHz linear16 output
  from the agent.

## Scenario mode (single agent from a JSON file)

For a one-off demo you usually want **one** agent whose persona you can tweak
without touching Python. Scenarios live in [`scenarios/`](scenarios/) — one folder
each, pairing a raw Voice Agent `Settings` JSON with its "call script" (what to say
during the demo). See [`scenarios/README.md`](scenarios/README.md) for the layout
and how to add one.

Set `SCENARIO_FILE` in `.env` to a scenario name and the proxy runs it verbatim:

```bash
SCENARIO_FILE=brightmoor-homecare   # -> scenarios/brightmoor-homecare/settings.json
```

Editing that JSON (prompt, greeting, voice, `keyterms`, …) is the whole workflow.
Unset `SCENARIO_FILE` to fall back to the multi-agent orchestrator demo below.

Notes:

- **Audio is transport-owned.** `load_scenario()` stamps the proxy's `AUDIO`
  block (raw `linear16` PCM, `container: none`) over whatever the file declares —
  a `wav` container would corrupt the browser's AudioWorklet playback.
- **Built-in LLM limits.** `agent.think.context_length` is only valid with a
  BYO LLM endpoint; with a built-in model (e.g. `open_ai`/`gpt-4o-mini`) the API
  rejects it. Omit it.
- **Keyterm spelling.** The Voice Agent listen provider uses `keyterms` (plural).
  The separate STT socket (below) passes them through as the v1 `keyterm` param.

## Multi-agent transfers

> 📄 For a shareable, docs-style explainer (architecture, advantages, latency,
> when to use which), see
> [`docs/multi-agent-with-updates.md`](docs/multi-agent-with-updates.md).

Personas (`scenarios/acme-support/acme_support.py`) share one WebSocket session. The orchestrator derives the
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

When an agent calls `transfer_to_agent`, the session is **reconfigured mid-session**
— the WebSocket is never torn down and the conversation history carries across
automatically, so there's no reconnect and no summarization step. (Verified in
`selftest.py`: a name given to triage is recalled by billing after the swap.)
It's also faster: `latency_test.py` measures the mid-session swap at ~3–5× quicker
than opening a fresh agent session (and that's before counting the audio
re-stream and context re-pass a reconnect would also need).

The active agent — with its current voice and model — is shown as a badge in the
UI header, and each transfer is marked in the transcript.

### Each agent can run its own model — even across providers

An `Agent` is effectively an `UpdateThink` payload (provider + model + prompt +
functions) plus an `UpdateSpeak` payload (voice). So each agent can declare its
own `model`/`provider`: a cheap, fast router can hand off to a stronger specialist
model mid-call. In this sample `triage`/`billing` run `gpt-4o-mini` while `tech`
runs Anthropic `claude-sonnet-4-5`:

```python
tech = Agent(
    name="tech", voice="aura-2-orion-en",
    provider="anthropic", model="claude-sonnet-4-5",   # its own brain
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

**Announced handoffs.** Silent transfer is ideal for a seamless, shared-voice
handoff, but when the voice changes a silent swap feels abrupt. Set
`announce_transfer=True` (and a `handoff_line`) on an agent to have it say
"let me hand you over to an advisor" *before* the voice switches.

The line can't come from the model's own turn: a Voice Agent turn is **either**
speech **or** a tool call, so any "I'll transfer you" wording in the prompt makes
the model talk *instead of* calling the transfer tool (the transfer then never
fires). So the tool call stays **silent**, and the **orchestrator injects** the
handoff line (`InjectAgentMessage`) in the outgoing agent's voice, waits for its
`AgentAudioDone`, and only then swaps the voice — announced, and no talk-over. See
the `financial-advisory` qualifier.

### Files

- **`scenarios/acme-support/acme_support.py`** — the personas, declared with `Agent` /
  `Tool`. Scenario definition (see also `scenarios/README.md`).
- **`orchestrator.py`** — `Agent`, `Tool`, `Orchestrator`: the typed surface plus
  the transfer-handshake engine. This is the reusable piece — a candidate to live
  in the Deepgram SDK.
- **`main.py`** — the proxy: pipes audio and feeds Deepgram events to the
  orchestrator.
- **`selftest.py`** — mic-free end-to-end test (TTS-synthesized user speech):
  transfers fire, no duplicated lines, history retained (incl. cross-provider).
  Run: `uv run python selftest.py` (or `uv run python selftest.py smoke`).
- **`latency_test.py`** — handoff-latency benchmark: mid-session Update vs
  reconnect. `uv run python latency_test.py [iterations]`
  (`DG_AGENT_HOST=api.eu.deepgram.com` to test the EU region).

Try it: ask "what's my balance?" — the assistant silently gains billing tools and
answers in the *same* voice (seamless); then say "my laptop won't turn on" and you
hand off to `tech`, who introduces itself in a *different* voice (distinct specialist).

## Live (interim) transcripts

Controlled by the **Interim** toggle in the UI header (per session; locked while
a conversation is running). `ENABLE_INTERIM` in `.env` seeds the toggle's
*initial* position; the user's choice is then remembered in `localStorage`. When
off, user turns are shown from the agent's own final `ConversationText`.

The Voice Agent socket only emits **final** user turns (`ConversationText`),
**regardless of its listen model** — it has no interim/partial event. So showing
text *as you speak* always needs a **second STT socket** fed the same mic audio.
`INTERIM_PROVIDER` picks which model backs it:

| `INTERIM_PROVIDER` | Endpoint | What you get |
|---|---|---|
| `auto` (default) | — | Follows the agent's listen model family: `flux-*` → flux, otherwise → nova. Keeps the live transcript's turn boundaries consistent with what the agent hears. |
| `nova` | `/v1/listen` | Native `interim_results` + `smart_format`, and it **reuses the scenario's `keyterms`** (medication names get boosted in the live transcript too). |
| `flux` | `/v2/listen` | Semantic turn events (`StartOfTurn`/`EndOfTurn`). Separate model; no smart formatting. |

> **Why `auto`?** flux's `EndOfTurn` (semantic turn detection) and nova's
> `speech_final` (silence-based endpointing) mark turn ends differently. If the
> agent runs flux but the interim socket runs nova (or vice-versa), the "final"
> line in the panel can land at a different moment than the agent's real turn end.
> `auto` keeps them on the same engine so the boundaries line up. The proxy also
> guards against invalid combos (never sends a flux model to `/v1/listen`).

Wiring:

- `GET /config` returns `{ "interimDefault": <bool>, "interimProvider": "nova" }`.
- On **Start**, the client connects to `ws://…/ws?interim=1` (or `0`); the proxy
  opens the STT socket only when `interim=1`.
- Both providers are **normalized** server-side into one `Interim` event
  `{ text, final }`. nova accumulates `is_final` segments and ends a turn on
  `speech_final`; flux maps `Update`→partial, `EndOfTurn`→final.
- The proxy sends `ProxyConfig` with the **actual** state, so if the socket
  couldn't connect the client still shows the agent's user transcript instead of
  suppressing it.
- The frontend renders an italic/dimmed **interim** bubble that updates live, then
  solidifies into a final user message. The agent's redundant `ConversationText`
  for `role: user` is suppressed (assistant turns still come from the agent).
- The connection is **best-effort** (its own try/except), so if the key can't
  reach it the agent still works — you just lose live text.

## Text-to-speech voices

Just name a voice — there's **no aura/flux flag**. Any Deepgram voice works:
classic `aura-2-*` or the next-gen `flux-*` (e.g. `flux-jack-en`). Both are served
natively by the Voice Agent under provider type `deepgram` (see [Flux TTS + Voice
Agent](https://developers.deepgram.com/docs/flux-tts/voice-agent)), so the **model
name alone** selects the engine — no separate endpoint, no extra socket, no double
billing.

`DEFAULT_VOICE` in `.env` is the fallback; **an explicit voice in config wins:**

- **Multi-agent mode:** `DEFAULT_VOICE` is the orchestrator's `default_voice`. Each
  `Agent.voice` in the scenario file overrides it; an agent that omits `voice` inherits
  the current one (seamless). So you can mix — e.g. `triage` = `aura-2-asteria-en`,
  `tech` = `aura-2-orion-en`, `billing` omits `voice` to stay on triage's.
- **Scenario mode:** `apply_default_voice()` only fills in `DEFAULT_VOICE` when the
  scenario JSON doesn't already name a speak voice — the scenario's own voice wins.

The active-agent badge shows whichever voice is active.

> ⚠️ **Flux voices can't change mid-session yet.** The Voice Agent applies a
> mid-session `UpdateSpeak` only for **aura** voices; for **flux** voices it acks
> `SpeakUpdated` but keeps the voice set in the initial `Settings` (verified against
> the live API by pitch analysis). Practical consequence for the multi-agent demo,
> where transfers switch voice via `UpdateSpeak`:
> - **Want distinct per-agent voices?** Use `aura-2-*` — those switch correctly on
>   transfer.
> - **Want Flux quality?** Use a **single** flux voice for the whole session (set it
>   on the entry agent / `DEFAULT_VOICE`; don't give agents differing flux voices —
>   the change won't be heard).
>
> This is a Deepgram-side limitation; revisit when mid-session flux switching ships.
