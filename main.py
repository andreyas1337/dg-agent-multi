import asyncio
import json
import os
from contextlib import suppress

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import websockets
from dotenv import load_dotenv

from agents import AGENTS, ENTRY, THINK_PROVIDER, LISTEN_MODEL
from orchestrator import Orchestrator

load_dotenv()

DEEPGRAM_API_KEY = os.environ["DEEPGRAM_API_KEY"]
DG_AGENT_URL = "wss://agent.deepgram.com/v1/agent/converse"

_TRUTHY = ("1", "true", "yes", "on")

# Default state of the UI's "interim results" toggle. The toggle (per connection,
# via the ?interim= query param) is what actually drives Flux; this just seeds the
# switch's initial position. Off by default — set ENABLE_FLUX_INTERIM=true in .env
# to have the toggle start on.
ENABLE_FLUX_INTERIM = os.environ.get("ENABLE_FLUX_INTERIM", "false").lower() in _TRUTHY

# Parallel STT connection used purely to surface live, interim user transcripts.
# The Voice Agent socket only emits *final* turns (ConversationText), so we feed
# the same mic audio to Flux and relay its TurnInfo events to the browser. Flux's
# encoding/sample_rate must match the agent's audio.input settings below.
FLUX_URL = (
    "wss://api.deepgram.com/v2/listen"
    "?model=flux-general-multi&encoding=linear16&sample_rate=16000"
)

# Audio I/O is a property of the transport (this browser proxy), not the agent —
# so it lives here and the orchestrator stamps the per-agent prompt/voice/tools on
# top of it. Flux's encoding/sample_rate must match audio.input below.
AUDIO = {
    "input": {"encoding": "linear16", "sample_rate": 16000},
    "output": {"encoding": "linear16", "sample_rate": 24000, "container": "none"},
}

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/config")
async def config():
    # Initial position of the UI's interim-results toggle.
    return {"fluxInterimDefault": ENABLE_FLUX_INTERIM}


@app.websocket("/ws")
async def agent_proxy(websocket: WebSocket):
    await websocket.accept()
    headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
    # The client's UI toggle decides whether to open Flux, sent as ?interim=1.
    want_flux = websocket.query_params.get("interim", "").lower() in _TRUTHY
    flux_ws = None
    try:
        async with websockets.connect(DG_AGENT_URL, additional_headers=headers) as dg_ws:
            # Serialize all writes to the Deepgram socket: the browser audio pump,
            # the orchestrator's handshake messages, and its retry/inject tasks all
            # send concurrently, and websockets doesn't allow concurrent send().
            dg_send_lock = asyncio.Lock()

            async def dg_send(obj):
                data = obj if isinstance(obj, (bytes, bytearray, str)) else json.dumps(obj)
                async with dg_send_lock:
                    await dg_ws.send(data)

            async def notify_browser(obj):
                with suppress(Exception):
                    await websocket.send_text(json.dumps(obj))

            # The orchestrator owns multi-agent transfers over this one socket.
            orch = Orchestrator(
                AGENTS,
                entry=ENTRY,
                send=dg_send,
                notify=notify_browser,
                think_provider=THINK_PROVIDER,
                listen_model=LISTEN_MODEL,
            )
            await dg_send(orch.initial_settings(AUDIO))
            # Tell the UI which agent/voice/model is active to start (transfers update it).
            await notify_browser({"type": "AgentActive", "agent": orch.current,
                                  "voice": orch.voice_now, "model": orch.model_now})

            # Best-effort: open Flux for interim transcripts when the client asked
            # for them. If it fails (e.g. the key lacks access), the agent still
            # works — we just skip live text.
            if want_flux:
                try:
                    flux_ws = await websockets.connect(FLUX_URL, additional_headers=headers)
                except Exception as e:
                    print(f"Flux connect failed (interim transcripts disabled): {e}")
                    flux_ws = None

            # Report the *actual* interim state so the client knows whether to
            # expect FluxTurnInfo events (and whether to suppress the agent's own
            # user transcript to avoid duplicates).
            await websocket.send_text(json.dumps(
                {"type": "ProxyConfig", "fluxInterim": flux_ws is not None}
            ))

            async def from_browser():
                try:
                    while True:
                        msg = await websocket.receive()
                        if "bytes" in msg and msg["bytes"]:
                            await dg_send(msg["bytes"])
                            if flux_ws is not None:
                                with suppress(Exception):
                                    await flux_ws.send(msg["bytes"])
                except (WebSocketDisconnect, Exception):
                    pass

            async def from_deepgram():
                try:
                    async for msg in dg_ws:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            # Drive the orchestrator (handles transfers), then
                            # relay the raw event to the browser for display.
                            with suppress(Exception):
                                await orch.handle(json.loads(msg))
                            await websocket.send_text(msg)
                except Exception:
                    pass

            async def from_flux():
                try:
                    async for msg in flux_ws:
                        if isinstance(msg, bytes):
                            continue
                        try:
                            ev = json.loads(msg)
                        except Exception:
                            continue
                        # Forward only TurnInfo, re-tagged so it never collides
                        # with agent event types on the client.
                        if ev.get("type") == "TurnInfo":
                            await websocket.send_text(json.dumps({
                                "type": "FluxTurnInfo",
                                "event": ev.get("event"),
                                "transcript": ev.get("transcript", ""),
                            }))
                except Exception:
                    pass

            # Note: only pump Flux when it's actually connected. A task that
            # returns immediately would satisfy FIRST_COMPLETED and tear down the
            # whole session before the agent even applies its Settings.
            tasks = [
                asyncio.create_task(from_browser()),
                asyncio.create_task(from_deepgram()),
            ]
            if flux_ws is not None:
                tasks.append(asyncio.create_task(from_flux()))
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in tasks:
                t.cancel()
    except Exception as e:
        print(f"Error: {e}")
    finally:
        if flux_ws is not None:
            with suppress(Exception):
                await flux_ws.close()
