"""
Multi-agent orchestrator — the candidate SDK primitive.

This is the piece that's worth pushing down into the Deepgram SDK. It owns the
fiddly, timing-sensitive handoff choreography that every multi-agent app
otherwise re-implements (and gets subtly wrong):

    FunctionCallRequest(transfer_to_agent)
      -> FunctionCallResponse(transferring)
      -> UpdateThink(new prompt + tools)   -> await ThinkUpdated
      -> UpdateSpeak(new voice)            -> await SpeakUpdated
      -> wait for the outgoing line to finish (AgentAudioDone, with a timeout)
      -> InjectAgentMessage(greeting)      -> retry on WARNING (still speaking)

The whole thing happens on ONE long-lived WebSocket — no teardown, no
reconnect, no dead-air gap, and (per the Voice Agent API) the conversation
history carries across the swap automatically, so there's no summarization step.

Deliberate improvement over the robinhood-demo reference: it gates the greeting
on `AgentAudioDone` (bounded by a fallback timeout) instead of a blind
`setTimeout(3000)`. The exact timing here is precisely the kind of hard-won
constant an SDK should own and tune centrally.

The class is transport-agnostic: it only needs a `send` callable to reach the
Deepgram socket and an optional `notify` callable to inform the UI. It does not
know or care whether audio comes from a browser, Twilio, or SIP.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("orchestrator")

Send = Callable[[dict], Awaitable[None]]
Notify = Callable[[dict], Awaitable[None]]

_INJECT_DURING_SPEECH = "INJECT_AGENT_MESSAGE_DURING_AGENT_SPEECH"


class MultiAgentOrchestrator:
    def __init__(
        self,
        agents: dict[str, dict],
        entry: str,
        send: Send,
        *,
        notify: Optional[Notify] = None,
        think_provider: dict,
        listen_model: str = "nova-3",
        business_handler: Optional[Callable[[str, dict], dict]] = None,
        audio_done_timeout: float = 1.5,
        max_inject_retries: int = 3,
        inject_retry_delay: float = 0.8,
    ):
        self.agents = agents
        self.entry = entry
        self.send = send
        self.notify = notify
        self.think_provider = think_provider
        self.listen_model = listen_model
        self.business = business_handler or (lambda name, args: {"status": "ok"})
        self.audio_done_timeout = audio_done_timeout
        self.max_inject_retries = max_inject_retries
        self.inject_retry_delay = inject_retry_delay

        self.current = entry
        # Handoff state machine: None | "think" | "speak" | "audio"
        self._step: Optional[str] = None
        self._target: Optional[str] = None
        self._audio_done = asyncio.Event()
        self._inject_greeting = ""
        self._inject_retries = 0

    # -- settings ---------------------------------------------------------

    def initial_settings(self, audio: dict) -> dict:
        """Build the Settings message for the entry agent."""
        cfg = self.agents[self.entry]
        return {
            "type": "Settings",
            "audio": audio,
            "agent": {
                "listen": {"provider": {"type": "deepgram", "model": self.listen_model}},
                "think": {
                    "provider": self.think_provider,
                    "prompt": cfg["prompt"],
                    "functions": cfg.get("functions", []),
                },
                "speak": {"provider": {"type": "deepgram", "model": cfg["voice"]}},
                "greeting": cfg.get("start_greeting", ""),
            },
        }

    # -- event pump -------------------------------------------------------

    async def handle(self, ev: dict) -> None:
        """Feed every JSON message from the Deepgram socket through here."""
        t = ev.get("type")

        if t == "FunctionCallRequest":
            for fn in ev.get("functions", []):
                await self._on_function(fn)

        elif t == "ThinkUpdated" and self._step == "think":
            self._step = "speak"
            voice = self.agents[self._target]["voice"]
            await self.send({"type": "UpdateSpeak", "speak": {"provider": {"type": "deepgram", "model": voice}}})

        elif t == "SpeakUpdated" and self._step == "speak":
            # Voice is now swapped. Wait for the outgoing agent's last line to
            # finish before injecting the new greeting, so they don't overlap.
            self._step = "audio"
            self._audio_done.clear()
            asyncio.create_task(self._inject_after_audio())

        elif t == "AgentAudioDone":
            self._audio_done.set()

        elif t == "Warning" and ev.get("code") == _INJECT_DURING_SPEECH:
            if self._inject_greeting and self._inject_retries < self.max_inject_retries:
                self._inject_retries += 1
                asyncio.create_task(self._retry_inject())

    # -- function calls ---------------------------------------------------

    async def _on_function(self, fn: dict) -> None:
        name = fn.get("name", "")
        call_id = fn.get("id", "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (ValueError, TypeError):
            args = {}

        if name == "transfer_to_agent":
            await self._start_transfer(args, call_id)
        else:
            result = self.business(name, args)
            await self._respond(call_id, name, result)

    async def _start_transfer(self, args: dict, call_id: str) -> None:
        target = args.get("transfer_to")
        if target not in self.agents or target == self.current:
            logger.warning("rejecting transfer to %r (current=%s)", target, self.current)
            await self._respond(call_id, "transfer_to_agent", {"status": "invalid_target"})
            return

        logger.info("transfer %s -> %s (%s)", self.current, target, args.get("reason"))
        await self._respond(call_id, "transfer_to_agent", {"status": "transferring"})

        cfg = self.agents[target]
        # Arm the handshake state machine, then swap the Think layer.
        self._target = target
        self._step = "think"
        self._inject_greeting = cfg.get("transfer_greeting", "")
        self._inject_retries = 0
        await self.send(
            {
                "type": "UpdateThink",
                "think": {
                    "provider": self.think_provider,
                    "prompt": cfg["prompt"],
                    "functions": cfg.get("functions", []),
                },
            }
        )
        self.current = target
        if self.notify:
            await self.notify(
                {"type": "AgentSwitched", "agent": target, "label": cfg.get("label", target),
                 "reason": args.get("reason", "")}
            )

    async def _respond(self, call_id: str, name: str, content: dict) -> None:
        await self.send(
            {"type": "FunctionCallResponse", "id": call_id, "name": name, "content": json.dumps(content)}
        )

    # -- greeting injection ----------------------------------------------

    async def _inject_after_audio(self) -> None:
        try:
            await asyncio.wait_for(self._audio_done.wait(), timeout=self.audio_done_timeout)
        except asyncio.TimeoutError:
            logger.debug("AgentAudioDone not seen within %.1fs; injecting anyway", self.audio_done_timeout)
        await self._send_inject()

    async def _send_inject(self) -> None:
        if not self._inject_greeting:
            return
        self._step = None
        await self.send({"type": "InjectAgentMessage", "message": self._inject_greeting})

    async def _retry_inject(self) -> None:
        await asyncio.sleep(self.inject_retry_delay)
        logger.debug("retrying greeting inject (attempt %d)", self._inject_retries)
        await self.send({"type": "InjectAgentMessage", "message": self._inject_greeting})
