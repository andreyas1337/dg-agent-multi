"""
End-to-end self-test for the multi-agent orchestrator — no microphone needed.

Drives the live Deepgram Voice Agent through the REAL orchestrator code path by
synthesizing "user" speech with Deepgram TTS and streaming it in as mic audio.

Scenario (designed to prove three things at once):
  1. Tell the FRONT DESK a name + ask about the balance  -> expect transfer to billing
  2. Ask BILLING to recall the name + the balance         -> proves (a) transfer fired,
     (b) handshake completed, (c) conversation history SURVIVED UpdateThink.

Usage:  ../.venv/bin/python selftest.py [smoke]
  smoke -> just connect, send Settings, print the greeting, exit.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request

import websockets
from dotenv import load_dotenv

from agents import AGENTS, ENTRY, THINK_PROVIDER, LISTEN_MODEL
from orchestrator import Orchestrator

load_dotenv()
KEY = os.environ["DEEPGRAM_API_KEY"]
AGENT_URL = "wss://agent.deepgram.com/v1/agent/converse"
AUDIO = {
    "input": {"encoding": "linear16", "sample_rate": 16000},
    "output": {"encoding": "linear16", "sample_rate": 24000, "container": "none"},
}
USER_VOICE = "aura-2-arcas-en"  # distinct from any agent voice


def tts(text: str) -> bytes:
    """Synthesize linear16/16k raw PCM for `text` via Deepgram TTS (blocking)."""
    url = (
        "https://api.deepgram.com/v1/speak"
        f"?model={USER_VOICE}&encoding=linear16&sample_rate=16000&container=none"
    )
    req = urllib.request.Request(
        url,
        data=json.dumps({"text": text}).encode(),
        headers={"Authorization": f"Token {KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


async def main(smoke: bool) -> None:
    events: list[dict] = []
    transfers: list[dict] = []

    async with websockets.connect(AGENT_URL, additional_headers={"Authorization": f"Token {KEY}"}) as ws:
        lock = asyncio.Lock()

        async def send(obj):
            data = obj if isinstance(obj, (bytes, bytearray, str)) else json.dumps(obj)
            async with lock:
                await ws.send(data)

        async def notify(o):
            transfers.append(o)
            print(f"  >> AgentSwitched -> {o.get('agent')} ({o.get('reason')})")

        orch = Orchestrator(
            AGENTS, entry=ENTRY, send=send, notify=notify,
            think_provider=THINK_PROVIDER, listen_model=LISTEN_MODEL,
        )
        await send(orch.initial_settings(AUDIO))

        done = asyncio.Event()

        async def reader():
            async for msg in ws:
                if isinstance(msg, bytes):
                    continue  # agent TTS audio out — ignored in this test
                ev = json.loads(msg)
                events.append(ev)
                t = ev.get("type")
                if t == "ConversationText":
                    print(f"  [{ev['role']:9}] {ev['content']}")
                elif t == "FunctionCallRequest":
                    names = [f.get("name") for f in ev.get("functions", [])]
                    print(f"  <FunctionCallRequest> {names}")
                elif t in ("ThinkUpdated", "SpeakUpdated", "SettingsApplied", "Welcome"):
                    print(f"  <{t}>")
                elif t in ("Error", "Warning"):
                    print(f"  <{t}> code={ev.get('code')} desc={ev.get('description')}")
                if t == "AgentAudioDone":
                    done.set()
                await orch.handle(ev)

        rtask = asyncio.create_task(reader())

        # A real mic streams audio continuously; without it the server closes the
        # socket with CLIENT_MESSAGE_TIMEOUT. Pump silence whenever we're not
        # actively "speaking" so the session stays alive across handoff gaps.
        chunk = 640  # 20ms @ 16k/16-bit mono
        state = {"real": False}

        async def keepalive():
            try:
                while True:
                    if not state["real"]:
                        await send(b"\x00" * chunk)
                    await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                pass

        ktask = asyncio.create_task(keepalive())

        async def wait_quiet(timeout=25.0):
            try:
                await asyncio.wait_for(done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                print("  (timeout waiting for agent to finish)")

        async def say(text: str):
            print(f"\nUSER: {text}")
            done.clear()
            audio = await asyncio.to_thread(tts, text)
            state["real"] = True
            for i in range(0, len(audio), chunk):
                await send(audio[i:i + chunk])
                await asyncio.sleep(0.02)
            for _ in range(60):  # ~1.2s silence to trigger endpointing
                await send(b"\x00" * chunk)
                await asyncio.sleep(0.02)
            state["real"] = False
            await wait_quiet()

        async def wait_for_agent(name: str, timeout=20.0):
            """Block until the orchestrator has actually switched to `name`."""
            waited = 0.0
            while orch.current != name and waited < timeout:
                await asyncio.sleep(0.25)
                waited += 0.25
            print(f"  ... active agent is now: {orch.current}")

        # Wait for the entry agent's greeting to finish.
        await wait_quiet()

        if not smoke:
            # Turn 1: give a memorable token + request billing, then wait until the
            # transfer truly completes AND billing's greeting finishes.
            await say("My name is Sam Rivera and my lucky number is forty-two. Please transfer me to billing.")
            await wait_for_agent("billing")
            done.clear()
            await wait_quiet(timeout=12.0)  # specifically wait for billing's greeting

            # Turn 2: now definitively at billing — recall facts that were only ever
            # said to the front desk. This is the history-retention test.
            await say("What name and lucky number did I give earlier? Please repeat them back.")
            await asyncio.sleep(1.0)

        ktask.cancel()
        rtask.cancel()

    # ---- summary -------------------------------------------------------
    print("\n" + "=" * 60)
    print("SUMMARY")
    fn_calls = [f.get("name") for e in events if e.get("type") == "FunctionCallRequest" for f in e.get("functions", [])]
    assistant = " ".join(e["content"] for e in events if e.get("type") == "ConversationText" and e["role"] == "assistant").lower()
    print(f"  transfers fired      : {[t.get('agent') for t in transfers]}")
    print(f"  function calls       : {fn_calls}")
    print(f"  ThinkUpdated seen    : {any(e.get('type') == 'ThinkUpdated' for e in events)}")
    print(f"  SpeakUpdated seen    : {any(e.get('type') == 'SpeakUpdated' for e in events)}")
    print(f"  errors               : {[e.get('code') for e in events if e.get('type') == 'Error']}")
    if not smoke:
        recalled_name = "rivera" in assistant or "sam" in assistant
        recalled_num = "42" in assistant or "forty" in assistant
        print(f"  history survived?    : name recalled={recalled_name}  number recalled={recalled_num}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main(smoke=len(sys.argv) > 1 and sys.argv[1] == "smoke"))
