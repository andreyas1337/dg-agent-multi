"""
End-to-end self-test for the multi-agent orchestrator — no microphone needed.

Drives the live Deepgram Voice Agent through the REAL orchestrator code path by
synthesizing "user" speech with Deepgram TTS and streaming it in as mic audio.

Scenario:
  1. Front desk, give a name + ask about billing      -> transfer to billing
  2. Ask billing for the balance + to recall the name -> history survived UpdateThink
  3. Mention a technical problem                       -> billing -> tech (direct edge)

triage+billing share a voice (seamless, one assistant); tech has its own voice
(distinct specialist). Checks: transfers fired, NO duplicated/echoed agent lines,
history retained, and exactly one voice switch (only when entering tech).

Usage:
  ../.venv/bin/python selftest.py [smoke]
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
    assistant_lines: list[str] = []

    async with websockets.connect(AGENT_URL, additional_headers={"Authorization": f"Token {KEY}"}) as ws:
        lock = asyncio.Lock()

        async def send(obj):
            data = obj if isinstance(obj, (bytes, bytearray, str)) else json.dumps(obj)
            async with lock:
                await ws.send(data)

        async def notify(o):
            transfers.append(o)
            print(f"  >> AgentActive -> {o.get('agent')} [voice={o.get('voice')} model={o.get('model')}] ({o.get('reason')})")

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
                    if ev["role"] == "assistant":
                        assistant_lines.append(ev["content"].strip())
                elif t == "FunctionCallRequest":
                    print(f"  <FunctionCallRequest> {[f.get('name') for f in ev.get('functions', [])]}")
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

        async def wait_quiet(timeout=20.0):
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
            waited = 0.0
            while orch.current != name and waited < timeout:
                await asyncio.sleep(0.25)
                waited += 0.25
            done.clear()
            await wait_quiet(timeout=12.0)  # let the new agent's first utterance finish
            print(f"  ... active agent is now: {orch.current}")

        await wait_quiet()  # entry greeting

        tech_idx = None
        if not smoke:
            await say("Hi, my name is Sam Rivera. I'm calling about my account balance.")
            await wait_for_agent("billing")  # openai, seamless

            await say("Great. What's my current balance, and do you remember the name I gave?")
            await asyncio.sleep(1.0)

            await say("Thanks. Actually I also have a technical problem — my laptop won't turn on.")
            await wait_for_agent("tech")  # anthropic, distinct voice
            tech_idx = len(assistant_lines)

            # The decisive cross-provider test: tech (Anthropic) must recall a name
            # that was only ever said to the OpenAI-driven agents before the swap.
            await say("Before we troubleshoot — what name did I give earlier? Please say it back.")
            await asyncio.sleep(1.0)

        ktask.cancel()
        rtask.cancel()

    # ---- summary -------------------------------------------------------
    print("\n" + "=" * 60)
    print("SUMMARY")
    fn_calls = [f.get("name") for e in events if e.get("type") == "FunctionCallRequest" for f in e.get("functions", [])]
    assistant = " ".join(assistant_lines).lower()
    dups = [a for i, a in enumerate(assistant_lines) if i and a.lower() == assistant_lines[i - 1].lower()]
    voice_switches = sum(1 for e in events if e.get("type") == "SpeakUpdated")
    print(f"  transfers fired      : {[t.get('agent') for t in transfers]}")
    print(f"  function calls       : {fn_calls}")
    print(f"  errors               : {[e.get('code') for e in events if e.get('type') == 'Error']}")
    print(f"  duplicate agent lines: {len(dups)}  {dups if dups else '(none ✓)'}")
    print(f"  voice switches       : {voice_switches}  (expect 1 — only entering tech)")
    if not smoke:
        print(f"  history survived?    : name={'rivera' in assistant or 'sam' in assistant}  balance={'1,234' in assistant}")
        if tech_idx is not None:
            tech_said = " ".join(assistant_lines[tech_idx:]).lower()
            print(f"  X-PROVIDER history?  : tech (anthropic) recalled name = {'rivera' in tech_said or 'sam' in tech_said}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main(smoke=len(sys.argv) > 1 and sys.argv[1] == "smoke"))
