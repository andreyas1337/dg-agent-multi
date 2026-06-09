"""
Multi-agent orchestrator — the candidate SDK surface.

Public API (what an app developer writes):

    from orchestrator import Agent, Tool, Orchestrator

    billing = Agent(
        name="billing",                 # voice omitted -> inherits the current voice
        prompt="You are the same assistant, now handling billing...",
        tools=[Tool("get_account_balance", "Look up the balance.", {...}, get_balance)],
        transfers_to={"tech": "technical issues", "triage": "anything else"},
    )
    orch = Orchestrator([triage, billing, tech], entry="triage",
                        send=send, think_provider={"type": "open_ai", "model": "gpt-4o-mini"})
    await send(orch.initial_settings(audio))   # then feed every DG event to orch.handle()

Everything happens on ONE long-lived WebSocket — no teardown, no reconnect — and
(verified against the live API) conversation history carries across the swap, so
there is no summarization step.

There is NO "visible vs seamless" mode. How a handoff is perceived emerges from
configuration the app already provides:

  * VOICE — if a target agent sets its own `voice`, switching to it changes the
    voice (the caller hears a distinct specialist). If `voice` is omitted, the
    agent INHERITS the current voice, so the caller keeps hearing one person.
    A "super-agent" is just several configs that share a voice; a separate
    specialist is a config with its own voice. Both can coexist in one graph.
  * PROMPT — whether the incoming agent introduces itself ("I'm the billing
    specialist...") or just continues ("...keep helping as the same assistant")
    is up to that agent's prompt. The app writes prompts anyway; no extra knob.

The orchestrator owns only the non-negotiable mechanics every app otherwise gets
wrong: it swaps the Think config (and the voice, when the target declares one)
BEFORE answering the transfer tool call, so the new agent's natural follow-up to
the tool result is its first utterance — no double-talk, no repeated lines — and
the outgoing agent is told to transfer SILENTLY. Plus routing, edge validation,
and a re-entrancy guard.

Transport-agnostic: needs only a `send` callable to reach the Deepgram socket and
an optional `notify` callable for the UI.
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Union

logger = logging.getLogger("orchestrator")

Send = Callable[[dict], Awaitable[None]]
Notify = Callable[[dict], Awaitable[None]]
ToolResult = Union[dict, Awaitable[dict]]

_TRANSFER_TOOL = "transfer_to_agent"

# Appended to the transfer TOOL description (not the agent's own prompt). Tells the
# model to hand off silently — the receiving agent does all the talking, so the
# transfer line is never spoken (and so never duplicated).
_TRANSFER_INSTRUCTION = (
    "\n\nWhen the customer's need matches one of these targets, call this function "
    "immediately and provide no spoken response. Do NOT tell the customer you are "
    "transferring them or to hold on — the receiving agent continues the conversation."
)


@dataclass
class Tool:
    """A business tool. The orchestrator routes calls to `handler` (sync or async)
    and returns its result to the agent."""
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], ToolResult]

    def to_spec(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


@dataclass
class Agent:
    """One persona/configuration.

    voice        — TTS model for this agent. Omit (None) to INHERIT the current
                   voice: the handoff is then audibly seamless (same person).
                   Set it to make this agent sound like a distinct specialist.
    greeting     — opening line, spoken only when this is the ENTRY agent. Takeover
                   behavior on transfer is governed by the agent's own `prompt`
                   (introduce vs continue), not by a separate field.
    transfers_to — {target name: when to use}; the transfer tool + routing are
                   derived from this. think — optional per-agent LLM override.
    """
    name: str
    prompt: str
    voice: Optional[str] = None
    greeting: str = ""
    tools: list[Tool] = field(default_factory=list)
    transfers_to: dict[str, str] = field(default_factory=dict)
    think: Optional[dict] = None


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
        default_voice: str = "aura-2-asteria-en",
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
        self.default_voice = default_voice

        self.current = entry
        # Whatever voice is currently playing; agents without their own voice keep it.
        self._voice_now = self._agents[entry].voice or default_voice
        # Transfer state machine: None (idle) | "think" | "speak"
        self._step: Optional[str] = None
        self._target: Optional[str] = None
        self._pending_call_id: Optional[str] = None

    def _validate_edges(self) -> None:
        for a in self._agents.values():
            for target in a.transfers_to:
                if target not in self._agents:
                    raise ValueError(f"agent {a.name!r} transfers to unknown agent {target!r}")

    # -- settings / think config -----------------------------------------

    def _functions_for(self, agent: Agent) -> list[dict]:
        """Business tools + an auto-derived transfer tool (if the agent has edges)."""
        specs = [t.to_spec() for t in agent.tools]
        if agent.transfers_to:
            options = "\n".join(f'    - "{n}": {why}' for n, why in agent.transfers_to.items())
            specs.append(
                {
                    "name": _TRANSFER_TOOL,
                    "description": (
                        "Transfer the conversation to another agent when the customer's "
                        "need matches one of these targets:\n" + options + _TRANSFER_INSTRUCTION
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
                "speak": {"provider": {"type": "deepgram", "model": self._voice_now}},
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
            target_voice = self._agents[self._target].voice
            if target_voice and target_voice != self._voice_now:
                self._step = "speak"
                self._voice_now = target_voice
                await self.send({"type": "UpdateSpeak", "speak": {"provider": {"type": "deepgram", "model": target_voice}}})
            else:
                await self._finalize_transfer()  # voice inherited -> seamless

        elif t == "SpeakUpdated" and self._step == "speak":
            await self._finalize_transfer()

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
        agent = self._agents[target]
        self._target = target
        self._step = "think"
        self._pending_call_id = call_id
        self.current = target

        # Swap the brain (and later the voice, if the target declares one) FIRST. We
        # answer the tool call only after the swap, so the NEW agent generates the
        # next utterance — no double talk. Whether it introduces itself is up to its
        # own prompt.
        await self.send({"type": "UpdateThink", "think": self._think_for(agent)})
        if self.notify:
            await self.notify({"type": "AgentSwitched", "agent": target, "reason": args.get("reason", "")})

    async def _finalize_transfer(self) -> None:
        await self._respond(self._pending_call_id, _TRANSFER_TOOL, {"status": "transferring"})
        self._step = None
        self._target = None
        self._pending_call_id = None

    async def _respond(self, call_id: str, name: str, content: dict) -> None:
        await self.send({"type": "FunctionCallResponse", "id": call_id, "name": name, "content": json.dumps(content)})
