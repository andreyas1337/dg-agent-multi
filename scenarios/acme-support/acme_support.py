"""
Acme Support — a specific multi-agent scenario (triage / billing / tech).

This is scenario *definition*, not reusable code: the personas, tools, prompts and
routing for one concrete support desk. The reusable engine lives in
`orchestrator.py`; this file just declares the graph. Each persona is a prompt +
tools + voice + greeting, and transitions are declared with `transfers_to`; the
orchestrator derives the transfer tool and routing from those edges, and swapping
personas never tears down the WebSocket. See `call-script.md` for how to drive it.
"""

from __future__ import annotations

from orchestrator import Agent, Tool
from prompt_style import persona

# Think provider shared by all personas (per-agent override via Agent.think).
# OpenAI gpt-4o-mini matches the single-agent sample — no extra API key needed.
THINK_PROVIDER = {"type": "open_ai", "model": "gpt-4o-mini"}
LISTEN_MODEL = "flux-general-en"

ENTRY = "triage"


# --- Business (mock) tool handlers -----------------------------------------

def get_account_balance(args: dict) -> dict:
    return {"balance": "$1,234.56", "currency": "USD", "status": "current"}


def run_diagnostic(args: dict) -> dict:
    return {"result": "All systems nominal", "latency_ms": 42}


# --- Personas --------------------------------------------------------------

# This graph deliberately MIXES two handoff feels, with no mode flag:
#
#   triage + billing  -> share one voice (billing omits `voice`, inheriting it)
#                        and use "continue as the same assistant" prompts, so the
#                        caller perceives ONE assistant that gains billing tools.
#   tech              -> declares its own voice and introduces itself, so it's
#                        perceived as a distinct specialist.
#
# Distinctness emerges from `voice` (set vs omitted) + prompt wording — that's it.
#
# ⚠️ FLUX VOICES DON'T SWITCH MID-SESSION (Deepgram limitation, verified). A
# transfer changes voice via `UpdateSpeak`, which the Voice Agent applies for
# aura-2-* voices but NOT for flux-* — it acks the change but keeps the voice from
# the initial Settings. So the differing flux voices below (triage=flux-priya-en,
# tech=flux-jack-en) will NOT be heard as distinct: the whole call stays on the
# entry agent's voice. For distinct per-agent voices, use aura-2-* voices; to keep
# Flux, give every agent the SAME flux voice (or omit it so all inherit). See README.

TRIAGE = Agent(
    name="triage",
    voice="flux-rufus-en",  # the shared "house" voice (a per-agent voice wins over DEFAULT_VOICE)
    prompt=persona(
        "You are the front desk for Acme Support. Answer general questions and route "
        "the caller to the right specialist: billing for charges, balances, and refunds, "
        "or technical support for devices, connectivity, and errors. Once you know where "
        "they need to go, hand off. Don't give legal or financial advice, and if you "
        "can't resolve something in a couple of turns, offer to escalate."
    ),
    greeting="Hi, thanks for calling Acme Support! How can I help you today?",
    transfers_to={
        "billing": "billing questions, charges, balances, refunds, invoices",
        "tech": "technical problems, login/connectivity issues, errors, outages",
    },
)

BILLING = Agent(
    name="billing",
    # voice omitted -> inherits triage's voice: seamless, same perceived person.
    prompt=persona(
        "You are continuing as the same Acme Support assistant, now handling billing. "
        "Don't reintroduce yourself or restart a greeting, just keep helping. Use "
        "get_account_balance for balance questions, and answer directly, including "
        "about anything the caller mentioned earlier in the call."
    ),
    tools=[
        Tool(
            "get_account_balance",
            "Look up the customer's current account balance.",
            {"type": "object", "properties": {}},
            get_account_balance,
        )
    ],
    transfers_to={
        "tech": "technical problems, login/connectivity issues, errors, outages",
        "triage": "general questions, or anything not billing or technical",
    },
)

TECH = Agent(
    name="tech",
    voice="flux-rufus-en",  # its own voice -> perceived as a distinct specialist
    # ...and its OWN model, on a different provider, to show per-agent think:
    # a fast/cheap router (gpt-4o-mini) hands off to a stronger specialist model.
    provider="anthropic",
    model="claude-sonnet-4-5",
    prompt=persona(
        "You are a technical support specialist at Acme Support. Briefly introduce "
        "yourself by your role in your first turn, then help the caller troubleshoot. "
        "Use run_diagnostic when a health check would help, and walk them through one "
        "step at a time."
    ),
    tools=[
        Tool(
            "run_diagnostic",
            "Run a connectivity/health diagnostic on the customer's device.",
            {"type": "object", "properties": {}},
            run_diagnostic,
        )
    ],
    transfers_to={
        "billing": "billing questions, charges, balances, refunds, invoices",
        "triage": "general questions, or anything not billing or technical",
    },
)

AGENTS = [TRIAGE, BILLING, TECH]
