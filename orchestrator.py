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

_TRANSFER_TOOL = "transfer_to_agent"

# Appended to the transfer TOOL description (not the agent's own prompt). The model
# must call the function with NO spoken text — a Voice Agent turn is either speech
# OR a tool call, so any "let me transfer you" wording makes it talk instead of
# calling the tool. For an AUDIBLE handoff line, set Agent.announce_transfer=True:
# the ORCHESTRATOR injects the line (InjectAgentMessage) after the silent tool call
# and waits for it to finish before swapping the voice (see _start_transfer).
_TRANSFER_INSTRUCTION = (
    "\n\nWhen the customer's need matches one of these targets, call this function "
    "immediately and provide no spoken response. Do NOT tell the customer you are "
    "transferring them or to hold on — the handoff is handled for you."
)

# If an announced handoff's injected line produces no audio, don't wait forever.
_ANNOUNCE_TIMEOUT = 6.0


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
    """One persona/configuration — effectively an UpdateThink payload (provider +
    model + prompt + functions) plus an UpdateSpeak payload (voice). On transfer
    the orchestrator sends exactly those, so each agent can run its OWN model.

    model        — LLM for this agent (e.g. "gpt-4o-mini", "gpt-4o",
                   "claude-sonnet-4-5"). Omit to use the orchestrator default.
    provider     — LLM provider type (e.g. "open_ai", "anthropic"). Omit to use the
                   orchestrator default's provider. Lets a cheap/fast router hand
                   off to a stronger specialist model — even across providers.
    think_options— escape hatch merged into the think provider (temperature,
                   endpoint, BYO-LLM credentials, ...).
    voice        — TTS model. Omit (None) to INHERIT the current voice (seamless,
                   same person); set it to sound like a distinct specialist.
    greeting     — opening line, spoken only when this is the ENTRY agent. Takeover
                   behavior on transfer is governed by the agent's own `prompt`.
    transfers_to — {target name: when to use}; the transfer tool + routing are
                   derived from this.
    """
    name: str
    prompt: str
    model: Optional[str] = None
    provider: Optional[str] = None
    think_options: Optional[dict] = None
    voice: Optional[str] = None
    greeting: str = ""
    tools: list[Tool] = field(default_factory=list)
    transfers_to: dict[str, str] = field(default_factory=dict)
    announce_transfer: bool = False   # speak a handoff line before the voice swaps (vs silent)
    handoff_line: str = ""            # the line to speak when announce_transfer (else a default)


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
        # Fallback voice for an agent that declares none. A per-agent `voice`
        # always takes priority over this.
        self.default_voice = default_voice

        self.current = entry
        # Whatever voice is currently playing; agents without their own voice keep it.
        self._voice_now = self._agents[entry].voice or default_voice
        # Transfer state machine: None (idle) | "await_audio" | "think" | "speak"
        self._step: Optional[str] = None
        self._target: Optional[str] = None
        self._pending_call_id: Optional[str] = None
        self._fallback_task: Optional[asyncio.Task] = None  # announced-handoff safety timer

    @property
    def voice_now(self) -> str:
        """The TTS voice currently playing (for UI display)."""
        return self._voice_now

    @property
    def model_now(self) -> Optional[str]:
        """The LLM currently driving the conversation (for UI display)."""
        return self._provider_for(self._agents[self.current]).get("model")

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

    def _provider_for(self, agent: Agent) -> dict:
        """The think provider for an agent: orchestrator default, with per-agent
        provider/model/options layered on top."""
        p = dict(self.think_provider)
        if agent.provider:
            p["type"] = agent.provider
        if agent.model:
            p["model"] = agent.model
        if agent.think_options:
            p.update(agent.think_options)
        return p

    def _think_for(self, agent: Agent) -> dict:
        return {
            "provider": self._provider_for(agent),
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

        elif self._step == "await_audio" and t in ("AgentAudioDone", "UserStartedSpeaking"):
            # Either the handoff line finished, OR the caller barged in over it. Both
            # mean "stop waiting and swap now": on barge-in the line is cut short and
            # the incoming agent should handle what the caller just said, rather than
            # sit idle until the fallback timer. (A barge-in may suppress
            # AgentAudioDone, so we can't rely on that event alone.)
            self._cancel_fallback()
            await self._begin_swap()

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
        self._target = target
        self._pending_call_id = call_id
        self._reason = args.get("reason", "")

        outgoing = self._agents[self.current]
        if outgoing.announce_transfer:
            # Speak a handoff line in the OUTGOING agent's voice (injected, because a
            # model turn is speech OR a tool call — it can't do both). Then hold the
            # swap until that line's audio finishes so the new voice doesn't talk over
            # it (see AgentAudioDone in handle).
            line = outgoing.handoff_line or "One moment, let me hand you over."
            await self.send({"type": "InjectAgentMessage", "message": line})
            self._step = "await_audio"
            self._fallback_task = asyncio.create_task(self._announce_fallback())
        else:
            await self._begin_swap()

    async def _begin_swap(self) -> None:
        """Swap the brain (and later the voice) toward the target. We answer the tool
        call only after the swap, so the NEW agent generates the next utterance."""
        self._fallback_task = None
        self._step = "think"
        self.current = self._target
        agent = self._agents[self._target]
        await self.send({"type": "UpdateThink", "think": self._think_for(agent)})
        if self.notify:
            # Voice the caller will hear after this hop (target's own, or inherited).
            await self.notify(
                {"type": "AgentActive", "agent": self._target,
                 "voice": agent.voice or self._voice_now,
                 "model": self._provider_for(agent).get("model"),
                 "reason": getattr(self, "_reason", "")}
            )

    async def _announce_fallback(self) -> None:
        """If an announced handoff never produces spoken audio, swap anyway."""
        try:
            await asyncio.sleep(_ANNOUNCE_TIMEOUT)
            if self._step == "await_audio":
                logger.warning("announced handoff: no audio seen, swapping anyway")
                await self._begin_swap()
        except asyncio.CancelledError:
            pass

    def _cancel_fallback(self) -> None:
        if self._fallback_task and not self._fallback_task.done():
            self._fallback_task.cancel()
        self._fallback_task = None

    async def _finalize_transfer(self) -> None:
        await self._respond(self._pending_call_id, _TRANSFER_TOOL, {"status": "transferring"})
        self._step = None
        self._target = None
        self._pending_call_id = None

    async def _respond(self, call_id: str, name: str, content: dict) -> None:
        await self.send({"type": "FunctionCallResponse", "id": call_id, "name": name, "content": json.dumps(content)})
