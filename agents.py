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

TRIAGE = Agent(
    name="triage",
    voice="aura-2-asteria-en",
    prompt=(
        "You are the front desk for Acme Support. Greet the customer, figure out "
        "whether they need billing or technical help, and transfer them to the "
        "right specialist. Keep replies to one or two sentences. Do not try to "
        "resolve billing or technical issues yourself — transfer instead."
    ),
    greeting="Hi, thanks for calling Acme Support! Are you calling about billing or a technical issue?",
    transfers_to={
        "billing": "billing questions, charges, balances, refunds, invoices",
        "tech": "technical problems, login/connectivity issues, errors, outages",
    },
)

BILLING = Agent(
    name="billing",
    voice="aura-2-thalia-en",
    prompt=(
        "You are a billing specialist at Acme Support. Help the customer with "
        "charges, balances, and refunds. Use get_account_balance when they ask "
        "about their balance. If they bring up a technical problem instead, "
        "transfer them back to the front desk. Answer the customer's questions "
        "directly, including about details they mentioned earlier. Keep replies concise."
    ),
    greeting="Hi, I'm the billing specialist. I've been briefed on your request — how can I help with your account?",
    tools=[
        Tool(
            "get_account_balance",
            "Look up the customer's current account balance.",
            {"type": "object", "properties": {}},
            get_account_balance,
        )
    ],
    transfers_to={"triage": "anything that is not billing-related"},
)

TECH = Agent(
    name="tech",
    voice="aura-2-orion-en",
    prompt=(
        "You are a technical support specialist at Acme Support. Help the customer "
        "troubleshoot. Use run_diagnostic when a health check would help. If they "
        "bring up a billing question instead, transfer them back to the front desk. "
        "Keep replies concise."
    ),
    greeting="Hi, I'm technical support. I've got the context from your conversation — what issue are you seeing?",
    tools=[
        Tool(
            "run_diagnostic",
            "Run a connectivity/health diagnostic on the customer's device.",
            {"type": "object", "properties": {}},
            run_diagnostic,
        )
    ],
    transfers_to={"triage": "anything that is not a technical issue"},
)

AGENTS = [TRIAGE, BILLING, TECH]
