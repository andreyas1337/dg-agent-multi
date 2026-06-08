"""
Multi-agent orchestrator — the candidate SDK surface.

Public API (what an app developer writes):

    from orchestrator import Agent, Tool, Orchestrator

    billing = Agent(
        name="billing",
        voice="aura-2-thalia-en",
        prompt="You are a billing specialist...",
        greeting="Hi, I'm the billing specialist...",
        tools=[Tool("get_account_balance", "Look up the balance.", {...}, get_balance)],
        transfers_to={"triage": "anything not billing-related"},
    )
    orch = Orchestrator([triage, billing, tech], entry="triage",
                        send=send, think_provider={"type": "open_ai", "model": "gpt-4o-mini"})
    await send(orch.initial_settings(audio))   # then feed every DG event to orch.handle()

The orchestrator owns the fiddly, timing-sensitive handoff that every multi-agent
voice app otherwise re-implements (and gets subtly wrong). It all happens on ONE
long-lived WebSocket — no teardown, no reconnect — and (verified against the live
API) conversation history carries across the swap, so there's no summarization:

    transfer_to_agent(target)
      -> FunctionCallResponse(transferring)
      -> UpdateThink(target prompt + tools)  -> await ThinkUpdated
      -> UpdateSpeak(target voice)           -> await SpeakUpdated
      -> wait for the outgoing line to finish (AgentAudioDone, bounded by timeout)
      -> InjectAgentMessage(greeting)        -> retry on WARNING (still speaking)

Gating the greeting on AgentAudioDone (with a fallback timeout) is a deliberate
improvement over a blind fixed delay; this is exactly the kind of constant an SDK
should own and tune centrally.

The class is transport-agnostic: it needs only a `send` callable to reach the
Deepgram socket and an optional `notify` callable for the UI. It knows nothing
about whether audio comes from a browser, Twilio, or SIP.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Union

logger = logging.getLogger("orchestrator")

Send = Callable[[dict], Awaitable[None]]
Notify = Callable[[dict], Awaitable[None]]
ToolResult = Union[dict, Awaitable[dict]]

_INJECT_DURING_SPEECH = "INJECT_AGENT_MESSAGE_DURING_AGENT_SPEECH"
_TRANSFER_TOOL = "transfer_to_agent"

# The SDK owns the handoff choreography prompt. The outgoing agent says ONE short
# line and then goes silent, so the incoming agent delivers the real greeting —
# this is what keeps the handoff from double-greeting or talking over itself.
TRANSFER_PROTOCOL = """

TRANSFER PROTOCOL (follow exactly):
1. Immediately BEFORE calling transfer_to_agent, say one short sentence such as
   "One moment while I connect you to the right specialist." That must be your
   last spoken utterance.
2. Then call transfer_to_agent. After the call, produce NO more text. Your turn
   is over; the next agent greets the customer.
"""


# --------------------------------------------------------------------------
# Public types
# --------------------------------------------------------------------------

@dataclass
class Tool:
    """A business tool. The orchestrator routes calls to `handler` and returns
    its result to the agent. `handler` may be sync or async."""
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], ToolResult]

    def to_spec(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


@dataclass
class Agent:
    """One persona. `transfers_to` maps target agent name -> when to use it; the
    orchestrator derives the transfer tool + routing from it (no hand-wiring).
    `greeting` is spoken on connect for the entry agent, and injected when this
    agent is transferred in. `think` optionally overrides the LLM provider."""
    name: str
    voice: str
    prompt: str
    greeting: str = ""
    tools: list[Tool] = field(default_factory=list)
    transfers_to: dict[str, str] = field(default_factory=dict)
    think: Optional[dict] = None


@dataclass
class Tuning:
    """Advanced handoff timing — defaults are fine for most apps."""
    audio_done_timeout: float = 1.5
    max_inject_retries: int = 3
    inject_retry_delay: float = 0.8


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

class Orchestrator:
    def __init__(
        self,
        agents: list[Agent],
        entry: str,
        *,
        send: Send,
        think_provider: dict,
        notify: Optional[Notify] = None,
        listen_model: str = "nova-3",
        tuning: Tuning = Tuning(),
    ):
        self._agents: dict[str, Agent] = {a.name: a for a in agents}
        if entry not in self._agents:
            raise ValueError(f"entry agent {entry!r} not in agents")
        self._validate_edges()

        self.entry = entry
        self.send = send
        self.notify = notify
        self.think_provider = think_provider
        self.listen_model = listen_model
        self.tuning = tuning

        self.current = entry
        # Handoff state machine: None (idle) | "think" | "speak" | "audio"
        self._step: Optional[str] = None
        self._target: Optional[str] = None
        self._audio_done = asyncio.Event()
        self._inject_greeting = ""
        self._inject_retries = 0

    def _validate_edges(self) -> None:
        for a in self._agents.values():
            for target in a.transfers_to:
                if target not in self._agents:
                    raise ValueError(f"agent {a.name!r} transfers to unknown agent {target!r}")

    # -- settings ---------------------------------------------------------

    def _functions_for(self, agent: Agent) -> list[dict]:
        """Business tools + an auto-derived transfer tool (if the agent has edges)."""
        specs = [t.to_spec() for t in agent.tools]
        if agent.transfers_to:
            options = "\n".join(f'    - "{n}": {why}' for n, why in agent.transfers_to.items())
            specs.append(
                {
                    "name": _TRANSFER_TOOL,
                    "description": (
                        "Transfer the conversation to another agent when the "
                        "customer's need matches one of these targets:\n"
                        + options + TRANSFER_PROTOCOL
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "transfer_to": {"type": "string", "enum": list(agent.transfers_to)},
                            "reason": {"type": "string", "description": "Brief reason for the transfer."},
                        },
                        "required": ["transfer_to", "reason"],
                    },
                }
            )
        return specs

    def _think_for(self, agent: Agent) -> dict:
        return {
            "provider": agent.think or self.think_provider,
            "prompt": agent.prompt,
            "functions": self._functions_for(agent),
        }

    def initial_settings(self, audio: dict) -> dict:
        """Build the Settings message for the entry agent."""
        agent = self._agents[self.entry]
        return {
            "type": "Settings",
            "audio": audio,
            "agent": {
                "listen": {"provider": {"type": "deepgram", "model": self.listen_model}},
                "think": self._think_for(agent),
                "speak": {"provider": {"type": "deepgram", "model": agent.voice}},
                "greeting": agent.greeting,
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
            await self.send(
                {"type": "UpdateSpeak", "speak": {"provider": {"type": "deepgram", "model": self._agents[self._target].voice}}}
            )

        elif t == "SpeakUpdated" and self._step == "speak":
            # Voice is swapped. Wait for the outgoing line to finish before the
            # new greeting so they don't overlap.
            self._step = "audio"
            self._audio_done.clear()
            asyncio.create_task(self._inject_after_audio())

        elif t == "AgentAudioDone":
            self._audio_done.set()

        elif t == "Warning" and ev.get("code") == _INJECT_DURING_SPEECH:
            if self._inject_greeting and self._inject_retries < self.tuning.max_inject_retries:
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

        if name == _TRANSFER_TOOL:
            await self._start_transfer(args, call_id)
        else:
            await self._respond(call_id, name, await self._run_tool(name, args))

    async def _run_tool(self, name: str, args: dict) -> dict:
        for tool in self._agents[self.current].tools:
            if tool.name == name:
                res = tool.handler(args)
                return await res if inspect.isawaitable(res) else res
        logger.warning("unknown tool %r for agent %s", name, self.current)
        return {"status": "unknown_function"}

    async def _start_transfer(self, args: dict, call_id: str) -> None:
        target = args.get("transfer_to")

        # Re-entrancy guard: ignore a transfer requested while one is mid-flight.
        if self._step is not None:
            logger.warning("transfer to %r ignored — handoff in progress", target)
            await self._respond(call_id, _TRANSFER_TOOL, {"status": "transfer_in_progress"})
            return

        # Only allow edges declared on the current agent.
        if target not in self._agents[self.current].transfers_to or target == self.current:
            logger.warning("rejecting transfer to %r (current=%s)", target, self.current)
            await self._respond(call_id, _TRANSFER_TOOL, {"status": "invalid_target"})
            return

        logger.info("transfer %s -> %s (%s)", self.current, target, args.get("reason"))
        await self._respond(call_id, _TRANSFER_TOOL, {"status": "transferring"})

        agent = self._agents[target]
        self._target = target
        self._step = "think"
        self._inject_greeting = agent.greeting
        self._inject_retries = 0
        await self.send({"type": "UpdateThink", "think": self._think_for(agent)})
        self.current = target
        if self.notify:
            await self.notify({"type": "AgentSwitched", "agent": target, "reason": args.get("reason", "")})

    async def _respond(self, call_id: str, name: str, content: dict) -> None:
        await self.send({"type": "FunctionCallResponse", "id": call_id, "name": name, "content": json.dumps(content)})

    # -- greeting injection ----------------------------------------------

    async def _inject_after_audio(self) -> None:
        try:
            await asyncio.wait_for(self._audio_done.wait(), timeout=self.tuning.audio_done_timeout)
        except asyncio.TimeoutError:
            logger.debug("AgentAudioDone not seen in %.1fs; injecting anyway", self.tuning.audio_done_timeout)
        await self._send_inject()

    async def _send_inject(self) -> None:
        self._step = None  # handoff complete; re-entrancy guard lifts here
        if self._inject_greeting:
            await self.send({"type": "InjectAgentMessage", "message": self._inject_greeting})

    async def _retry_inject(self) -> None:
        await asyncio.sleep(self.tuning.inject_retry_delay)
        logger.debug("retrying greeting inject (attempt %d)", self._inject_retries)
        await self.send({"type": "InjectAgentMessage", "message": self._inject_greeting})
