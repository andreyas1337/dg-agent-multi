# dg-agent-second

A minimal FastAPI app that serves a web page and proxies audio between the
browser and Deepgram's [Voice Agent API](https://developers.deepgram.com/docs/voice-agent)
over a WebSocket.

The agent is configured in `main.py`:

- **Listen:** Deepgram `nova-3` (speech-to-text)
- **Think:** OpenAI `gpt-4o-mini`
- **Speak:** Deepgram `aura-2-asteria-en` (text-to-speech)

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
