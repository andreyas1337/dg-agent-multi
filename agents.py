"""
Agent (persona) definitions for the multi-agent demo.

This is the *user-authored* config layer — what stays application code even after
the orchestration moves into the SDK. Compare to the declarative style of
frameworks like Google ADK / AWS Strands: each persona is a prompt + tools +
voice + greeting, and transitions are declared with `transfers_to`. The
orchestrator derives the transfer tool and routing from those edges; swapping
personas never tears down the WebSocket.
"""

from __future__ import annotations

from orchestrator import Agent, Tool

# Think provider shared by all personas (per-agent override via Agent.think).
# OpenAI gpt-4o-mini matches the single-agent sample — no extra API key needed.
THINK_PROVIDER = {"type": "open_ai", "model": "gpt-4o-mini"}
LISTEN_MODEL = "nova-3"

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

TRIAGE = Agent(
    name="triage",
    voice="aura-2-asteria-en",  # the shared "house" voice
    prompt=(
        "You are the assistant for Acme Support. Help with general questions and "
        "route the customer to the right capability. For billing matters (charges, "
        "balances, refunds) transfer to billing. For technical problems (devices, "
        "connectivity, errors) transfer to technical support. Keep replies to one "
        "or two sentences."
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
    prompt=(
        "You are continuing as the same Acme Support assistant, now handling "
        "billing. Do NOT reintroduce yourself or restart a greeting — just keep "
        "helping. Use get_account_balance for balance questions. Answer directly, "
        "including about details the customer mentioned earlier. Keep replies concise."
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
    voice="aura-2-orion-en",  # its own voice -> perceived as a distinct specialist
    # ...and its OWN model, on a different provider, to show per-agent think:
    # a fast/cheap router (gpt-4o-mini) hands off to a stronger specialist model.
    provider="anthropic",
    model="claude-sonnet-4-20250514",
    prompt=(
        "You are a technical support specialist at Acme Support. In one short "
        "sentence, introduce yourself by your role, then help the customer "
        "troubleshoot. Use run_diagnostic when a health check would help. Keep "
        "replies concise."
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
