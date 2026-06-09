"""
Handoff latency: reconnect-a-new-agent vs UpdateThink/UpdateSpeak (in place).

Measures, against the live Deepgram Voice Agent API, the wall-clock cost of the
two ways to switch agents:

  RECONNECT — open a fresh WebSocket + send Settings + await SettingsApplied
              (the minimum to have a new agent session ready; a real reconnect
              handoff also re-streams audio and must re-pass context — extra cost
              this benchmark does NOT include).
  UPDATE    — on one warm socket, send UpdateThink -> await ThinkUpdated, then
              UpdateSpeak -> await SpeakUpdated (what the orchestrator does).

Usage: ../.venv/bin/python latency_test.py [iterations]
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time

import websockets
from dotenv import load_dotenv

load_dotenv()
KEY = os.environ["DEEPGRAM_API_KEY"]
URL = "wss://agent.deepgram.com/v1/agent/converse"
AUTH = {"Authorization": f"Token {KEY}"}
AUDIO = {
    "input": {"encoding": "linear16", "sample_rate": 16000},
    "output": {"encoding": "linear16", "sample_rate": 24000, "container": "none"},
}
PROVIDER = {"type": "open_ai", "model": "gpt-4o-mini"}  # same provider both paths, for fairness
PROMPTS = ["You are agent A. Be brief.", "You are agent B. Be brief."]
VOICES = ["aura-2-asteria-en", "aura-2-thalia-en"]


def settings(prompt=PROMPTS[0], voice=VOICES[0]) -> dict:
    return {
        "type": "Settings",
        "audio": AUDIO,
        "agent": {
            "listen": {"provider": {"type": "deepgram", "model": "nova-3"}},
            "think": {"provider": PROVIDER, "prompt": prompt, "functions": []},
            "speak": {"provider": {"type": "deepgram", "model": voice}},
        },
    }


async def recv_until(ws, type_, timeout=20.0):
    while True:
        msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
        if isinstance(msg, (bytes, bytearray)):
            continue
        if json.loads(msg).get("type") == type_:
            return


async def measure_reconnect(n: int) -> list[float]:
    out = []
    for i in range(n):
        t0 = time.perf_counter()
        async with websockets.connect(URL, additional_headers=AUTH) as ws:
            await ws.send(json.dumps(settings(PROMPTS[i % 2], VOICES[i % 2])))
            await recv_until(ws, "SettingsApplied")
        out.append((time.perf_counter() - t0) * 1000)
    return out


async def measure_update(n: int) -> tuple[list[float], list[float]]:
    think_ms, speak_ms = [], []
    async with websockets.connect(URL, additional_headers=AUTH) as ws:
        await ws.send(json.dumps(settings()))
        await recv_until(ws, "SettingsApplied")
        for i in range(n):
            think = {"type": "UpdateThink", "think": {"provider": PROVIDER, "prompt": PROMPTS[i % 2], "functions": []}}
            t0 = time.perf_counter()
            await ws.send(json.dumps(think))
            await recv_until(ws, "ThinkUpdated")
            t1 = time.perf_counter()
            await ws.send(json.dumps({"type": "UpdateSpeak", "speak": {"provider": {"type": "deepgram", "model": VOICES[i % 2]}}}))
            await recv_until(ws, "SpeakUpdated")
            t2 = time.perf_counter()
            think_ms.append((t1 - t0) * 1000)
            speak_ms.append((t2 - t1) * 1000)
    return think_ms, speak_ms


def stats(label, xs):
    print(f"  {label:34} n={len(xs)}  min={min(xs):7.1f}  median={statistics.median(xs):7.1f}  "
          f"mean={statistics.mean(xs):7.1f}  max={max(xs):7.1f}  (ms)")


async def main(n: int):
    print(f"Measuring handoff latency, {n} iterations each...\n")
    rc = await measure_reconnect(n)
    th, sp = await measure_update(n)
    total_update = [a + b for a, b in zip(th, sp)]

    print("RECONNECT (new WS + Settings -> SettingsApplied):")
    stats("reconnect total", rc)
    print("\nUPDATE (in place, one warm socket):")
    stats("UpdateThink -> ThinkUpdated", th)
    stats("UpdateSpeak -> SpeakUpdated", sp)
    stats("update total (think + speak)", total_update)

    print("\n" + "-" * 60)
    print(f"  median reconnect : {statistics.median(rc):7.1f} ms")
    print(f"  median update    : {statistics.median(total_update):7.1f} ms")
    print(f"  speedup (median) : {statistics.median(rc) / statistics.median(total_update):6.1f}x faster in place")
    print("  NB: reconnect figure EXCLUDES re-streaming audio and re-passing")
    print("      context (summarizer LLM call), which Update avoids entirely.")
    print("-" * 60)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8))
