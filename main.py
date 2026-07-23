import asyncio
import importlib.util
import json
import os
from contextlib import suppress
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import websockets
from dotenv import load_dotenv

from orchestrator import Orchestrator

load_dotenv()

# The active multi-agent scenario definition (loaded by path — scenario folders
# have hyphens, so a normal import won't work). Override with AGENTS_SCENARIO in
# .env, e.g. scenarios/financial-advisory/financial_advisory.py.
_DEFAULT_AGENTS_SCENARIO = os.path.join("scenarios", "acme-support", "acme_support.py")
AGENTS_SCENARIO = os.path.join(
    os.path.dirname(__file__), os.environ.get("AGENTS_SCENARIO") or _DEFAULT_AGENTS_SCENARIO
)


def load_agents(path: str):
    spec = importlib.util.spec_from_file_location("agents_scenario", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.AGENTS, mod.ENTRY, mod.THINK_PROVIDER, mod.LISTEN_MODEL


AGENTS, ENTRY, THINK_PROVIDER, LISTEN_MODEL = load_agents(AGENTS_SCENARIO)

DEEPGRAM_API_KEY = os.environ["DEEPGRAM_API_KEY"]
DG_AGENT_URL = "wss://agent.deepgram.com/v1/agent/converse"
LISTEN_V1_URL = "wss://api.deepgram.com/v1/listen"   # nova-3 (interim + smart_format)
LISTEN_V2_URL = "wss://api.deepgram.com/v2/listen"   # flux (semantic turns)

_TRUTHY = ("1", "true", "yes", "on")

# SCENARIO MODE: when SCENARIO_FILE is set, the proxy runs that ONE agent from a
# raw Voice Agent `Settings` JSON — the "edit one file per demo" workflow.
# Unset -> fall back to the multi-agent orchestrator (the other demo in this repo).
# The value is either a scenario NAME under scenarios/ (resolved to
# scenarios/<name>/settings.json) or a direct path to a .json file.
SCENARIOS_DIR = "scenarios"
SCENARIO_FILE = os.environ.get("SCENARIO_FILE") or None


def resolve_scenario(value: str) -> str:
    """Map a SCENARIO_FILE value to a Settings JSON path. A bare name points at a
    scenario folder (scenarios/<name>/settings.json); anything ending in .json is
    used as-is."""
    if value.endswith(".json"):
        return value
    return os.path.join(SCENARIOS_DIR, value, "settings.json")

# Live interim transcripts ALWAYS need a separate STT socket — the Voice Agent
# socket only emits final ConversationText, whatever its listen model is. This
# picks which model that socket uses:
#   auto -> follow the agent's listen model family, so turn detection matches
#           (flux EndOfTurn vs nova speech_final). This is the default.
#   nova -> /v1/listen with interim_results + smart_format, reusing the scenario's
#           listen model + keyterms.
#   flux -> /v2/listen, semantic turn events (separate model, no smart formatting).
INTERIM_PROVIDER = os.environ.get("INTERIM_PROVIDER", "auto").lower()


def resolve_interim_provider(settings: dict) -> str:
    """Turn INTERIM_PROVIDER into a concrete 'nova' or 'flux'. 'auto' follows the
    agent's listen model family so the live transcript's turn boundaries match what
    the agent hears; an explicit 'nova'/'flux' overrides."""
    if INTERIM_PROVIDER in ("nova", "flux"):
        return INTERIM_PROVIDER
    listen_model = str(
        settings.get("agent", {}).get("listen", {}).get("provider", {}).get("model") or ""
    )
    return "flux" if listen_model.startswith("flux") else "nova"

# Initial position of the UI's interim toggle (the per-connection ?interim= query
# param is what actually opens the socket).
ENABLE_INTERIM_DEFAULT = os.environ.get("ENABLE_INTERIM", "false").lower() in _TRUTHY

# DEFAULT TTS voice — used when an agent (or scenario) doesn't name its own. Any
# Deepgram voice works: classic aura-2-* or the next-gen flux-* (e.g. flux-jack-en).
# Both are served natively under provider type "deepgram"; the model name alone
# selects the engine, so there's no separate flag or endpoint. A per-agent voice
# always wins over this default.
DEFAULT_VOICE = os.environ.get("DEFAULT_VOICE", "aura-2-asteria-en")

# Audio I/O is a property of the transport (this browser proxy), not the agent, so
# it's owned here and matches what the frontend records/plays (raw linear16 PCM —
# NOT a WAV container, which would corrupt the AudioWorklet playback). Interim STT
# encoding/sample_rate must match audio.input below.
AUDIO = {
    "input": {"encoding": "linear16", "sample_rate": 16000},
    "output": {"encoding": "linear16", "sample_rate": 24000, "container": "none"},
}


def load_scenario(path: str) -> dict:
    """Load a raw `Settings` JSON scenario. Audio is transport-owned, so we stamp
    our AUDIO over whatever the file declares (e.g. a `wav` container that the
    browser can't play) — everything under `agent` is taken verbatim."""
    with open(path) as f:
        settings = json.load(f)
    settings["audio"] = AUDIO
    return settings


def interim_url(settings: dict) -> str:
    """Build the separate STT URL for live interim transcripts. We mirror the
    agent's listen model when it's valid for the chosen endpoint, but never send a
    flux model to /v1/listen (or a nova model to /v2/listen) — that 400s."""
    agent = settings.get("agent", {})
    listen = agent.get("listen", {}).get("provider", {})
    agent_model = str(listen.get("model") or "")

    if resolve_interim_provider(settings) == "flux":
        # /v2/listen. Reuse the agent's flux model if it listens with flux.
        model = agent_model if agent_model.startswith("flux") else "flux-general-en"
        return LISTEN_V2_URL + "?" + urlencode(
            {"model": model, "encoding": "linear16", "sample_rate": 16000}
        )

    # /v1/listen (nova): reuse the agent's model only if it's a nova model; a flux
    # model isn't valid here, so fall back to nova-3.
    model = agent_model if agent_model.startswith("nova") else "nova-3"
    params = [
        ("model", model),
        ("language", agent.get("language", "en")),
        ("interim_results", "true"),
        ("smart_format", "true"),
        ("encoding", "linear16"),
        ("sample_rate", "16000"),
    ]
    # Voice Agent spells it `keyterms` (plural); the v1 /listen query param is the
    # singular `keyterm`. Accept either on the scenario side.
    keyterms = listen.get("keyterms") or listen.get("keyterm") or []
    params += [("keyterm", kt) for kt in keyterms]
    return LISTEN_V1_URL + "?" + urlencode(params)

def apply_default_voice(settings: dict) -> dict:
    """Fill in DEFAULT_VOICE only when the scenario doesn't already name a speak
    voice. An explicit voice in the scenario JSON always wins."""
    provider = settings.setdefault("agent", {}).setdefault("speak", {}).setdefault("provider", {})
    if not provider.get("model"):
        provider.update({"type": "deepgram", "model": DEFAULT_VOICE})
    return settings


app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/config")
async def config():
    return {
        "interimDefault": ENABLE_INTERIM_DEFAULT,
        "interimProvider": INTERIM_PROVIDER,
        "defaultVoice": DEFAULT_VOICE,
    }


def active_scenario_dir() -> str:
    """Folder of the currently-active scenario (single-agent or multi-agent)."""
    if SCENARIO_FILE:
        return os.path.dirname(resolve_scenario(SCENARIO_FILE))
    return os.path.dirname(AGENTS_SCENARIO)


@app.get("/call-script")
async def call_script():
    """The active scenario's call-script.md (the demo helper panel loads this)."""
    path = os.path.join(active_scenario_dir(), "call-script.md")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="no call script for the active scenario")
    return FileResponse(path, media_type="text/markdown")


@app.websocket("/ws")
async def agent_proxy(websocket: WebSocket):
    await websocket.accept()
    headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
    # The client's UI toggle decides whether to open the interim socket (?interim=1).
    want_interim = websocket.query_params.get("interim", "").lower() in _TRUTHY
    interim_ws = None
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

            # Mode A: single agent from a scenario file. Mode B: multi-agent
            # orchestrator. Both send one Settings message and (in B) route Deepgram
            # events through orch.handle(). In flux mode the speak provider points at
            # the Flux voice, which the agent renders natively (one voice, no proxy).
            orch = None
            if SCENARIO_FILE:
                settings = apply_default_voice(load_scenario(resolve_scenario(SCENARIO_FILE)))
                agent = settings.get("agent", {})
                name = (settings.get("tags") or ["agent"])[0]
                await dg_send(settings)
                await notify_browser({
                    "type": "AgentActive", "agent": name,
                    "voice": agent.get("speak", {}).get("provider", {}).get("model"),
                    "model": agent.get("think", {}).get("provider", {}).get("model"),
                })
            else:
                orch = Orchestrator(
                    AGENTS, entry=ENTRY, send=dg_send, notify=notify_browser,
                    think_provider=THINK_PROVIDER, listen_model=LISTEN_MODEL,
                    default_voice=DEFAULT_VOICE,  # a per-agent `voice` wins over this
                )
                settings = orch.initial_settings(AUDIO)
                await dg_send(settings)
                await notify_browser({"type": "AgentActive", "agent": orch.current,
                                      "voice": orch.voice_now, "model": orch.model_now})

            # Best-effort: open the interim STT socket when the client asked for it.
            # If it fails (e.g. the key lacks access), the agent still works — we
            # just skip live text.
            if want_interim:
                try:
                    interim_ws = await websockets.connect(
                        interim_url(settings), additional_headers=headers
                    )
                except Exception as e:
                    print(f"Interim connect failed (live transcripts disabled): {e}")
                    interim_ws = None

            # Report the *actual* interim state so the client knows whether to
            # expect Interim events (and whether to suppress the agent's own user
            # transcript to avoid duplicates). Report the resolved provider (not
            # "auto") so the client sees the concrete engine in use.
            await notify_browser({"type": "ProxyConfig", "interim": interim_ws is not None,
                                  "provider": resolve_interim_provider(settings)})

            async def from_browser():
                # A live mic streams continuously; the same audio feeds the agent and
                # (when open) the interim socket, so neither times out for lack of it.
                try:
                    while True:
                        msg = await websocket.receive()
                        if "bytes" in msg and msg["bytes"]:
                            await dg_send(msg["bytes"])
                            if interim_ws is not None:
                                with suppress(Exception):
                                    await interim_ws.send(msg["bytes"])
                except (WebSocketDisconnect, Exception):
                    pass

            async def from_deepgram():
                try:
                    async for msg in dg_ws:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            # Drive the orchestrator (transfers) in multi-agent mode,
                            # then relay the raw event for display.
                            if orch is not None:
                                with suppress(Exception):
                                    await orch.handle(json.loads(msg))
                            await websocket.send_text(msg)
                except Exception:
                    pass

            async def from_interim():
                # Normalize both providers into one Interim event {text, final}:
                #   flux  -> TurnInfo: Update = partial, EndOfTurn = final.
                #   nova  -> Results: accumulate is_final segments; speech_final
                #            ends the turn. Non-final results are live partials.
                committed = ""
                try:
                    async for msg in interim_ws:
                        if isinstance(msg, bytes):
                            continue
                        try:
                            ev = json.loads(msg)
                        except Exception:
                            continue
                        t = ev.get("type")
                        if t == "TurnInfo":  # flux (v2)
                            text = (ev.get("transcript") or "").strip()
                            if ev.get("event") == "EndOfTurn":
                                await notify_browser({"type": "Interim", "text": text, "final": True})
                            elif text:
                                await notify_browser({"type": "Interim", "text": text, "final": False})
                        elif t == "Results":  # nova (v1)
                            alt = (ev.get("channel", {}).get("alternatives") or [{}])[0]
                            seg = (alt.get("transcript") or "").strip()
                            if ev.get("speech_final"):
                                full = (committed + " " + seg).strip()
                                committed = ""
                                if full:
                                    await notify_browser({"type": "Interim", "text": full, "final": True})
                            elif ev.get("is_final"):
                                committed = (committed + " " + seg).strip()
                                if committed:
                                    await notify_browser({"type": "Interim", "text": committed, "final": False})
                            elif seg:
                                live = (committed + " " + seg).strip()
                                await notify_browser({"type": "Interim", "text": live, "final": False})
                except Exception:
                    pass

            # Session lifetime is tied ONLY to the browser and the agent socket.
            # The interim socket is auxiliary: run it in the background so if it ever
            # closes (e.g. an STT idle timeout) it can't tear down the conversation.
            interim_task = (
                asyncio.create_task(from_interim()) if interim_ws is not None else None
            )
            critical = [
                asyncio.create_task(from_browser()),
                asyncio.create_task(from_deepgram()),
            ]
            await asyncio.wait(critical, return_when=asyncio.FIRST_COMPLETED)
            for t in critical:
                t.cancel()
            if interim_task is not None:
                interim_task.cancel()
            # Diagnostics for unexpected drops: which side closed, and why. A non-None
            # agent close_code means the agent socket ended the call; None usually
            # means the browser disconnected first.
            print(f"[session] ended — agent close_code={dg_ws.close_code} "
                  f"reason={dg_ws.close_reason!r}; "
                  f"interim close_code={getattr(interim_ws, 'close_code', None)}",
                  flush=True)
    except Exception as e:
        print(f"Error: {e}", flush=True)
    finally:
        if interim_ws is not None:
            with suppress(Exception):
                await interim_ws.close()
