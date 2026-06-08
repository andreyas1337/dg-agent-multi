"""
Agent (persona) definitions for the multi-agent demo.

This is the *user-authored* config layer — the part that stays application code
even if the orchestration in `orchestrator.py` eventually moves into the SDK.
Each persona is just a prompt + tool set + voice + a greeting to speak when it
takes over. Swapping between them never tears down the WebSocket; the
orchestrator reconfigures the live session via UpdateThink / UpdateSpeak.
"""

from __future__ import annotations

# Think provider shared by all personas. Kept as OpenAI gpt-4o-mini to match the
# single-agent sample (no extra API key needed). UpdateThink could also switch
# the model per persona — that's why provider lives alongside prompt/functions.
THINK_PROVIDER = {"type": "open_ai", "model": "gpt-4o-mini"}
LISTEN_MODEL = "nova-3"

# Appended to any persona that can transfer. This choreography is what keeps a
# handoff seamless: the outgoing agent says ONE short line and then goes silent,
# so the incoming agent (not the old one) delivers the real greeting. Copied from
# the robinhood-demo reference, which learned it the hard way.
TRANSFER_PROTOCOL = """

TRANSFER PROTOCOL (follow exactly):
1. Immediately BEFORE calling transfer_to_agent, say one short sentence such as
   "One moment while I connect you to the right specialist." That must be your
   last spoken utterance.
2. Then call transfer_to_agent. After the call, produce NO more text — no
   confirmation, no filler. Your turn is over; the next agent will greet the
   customer.
"""


def transfer_function(targets: dict[str, str]) -> dict:
    """Build the `transfer_to_agent` tool from a {agent_name: when_to_use} map.

    In an SDK this would be auto-injected from the agent graph's edges; here we
    build it explicitly so the wiring is visible.
    """
    options = "\n".join(f'    - "{name}": {desc}' for name, desc in targets.items())
    return {
        "name": "transfer_to_agent",
        "description": (
            "Transfer the conversation to a specialist agent when the customer's "
            "need matches one of the targets below.\n" + options + TRANSFER_PROTOCOL
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "transfer_to": {
                    "type": "string",
                    "enum": list(targets.keys()),
                    "description": "Which agent to transfer to.",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief reason for the transfer.",
                },
            },
            "required": ["transfer_to", "reason"],
        },
    }


# --- Business (mock) tools -------------------------------------------------

GET_BALANCE = {
    "name": "get_account_balance",
    "description": "Look up the customer's current account balance.",
    "parameters": {"type": "object", "properties": {}},
}

RUN_DIAGNOSTIC = {
    "name": "run_diagnostic",
    "description": "Run a connectivity/health diagnostic on the customer's device.",
    "parameters": {"type": "object", "properties": {}},
}


def call_business_function(name: str, args: dict) -> dict:
    """Mock implementations so transferred agents have something to do.

    Returns the payload sent back in a FunctionCallResponse. Real apps would do
    actual work here; this stays application code regardless of the SDK.
    """
    if name == "get_account_balance":
        return {"balance": "$1,234.56", "currency": "USD", "status": "current"}
    if name == "run_diagnostic":
        return {"result": "All systems nominal", "latency_ms": 42}
    return {"status": "ok"}


# --- Personas --------------------------------------------------------------

ENTRY = "triage"

AGENTS: dict[str, dict] = {
    "triage": {
        "label": "Front Desk",
        "voice": "aura-2-asteria-en",
        "prompt": (
            "You are the front desk for Acme Support. Greet the customer, figure "
            "out whether they need billing or technical help, and transfer them to "
            "the right specialist. Keep replies to one or two sentences. Do not try "
            "to resolve billing or technical issues yourself — transfer instead."
        ),
        "functions": [
            transfer_function(
                {
                    "billing": "billing questions, charges, balances, refunds, invoices",
                    "tech": "technical problems, login/connectivity issues, errors, outages",
                }
            )
        ],
        # Spoken automatically on connect (via Settings.greeting).
        "start_greeting": (
            "Hi, thanks for calling Acme Support! Are you calling about billing or "
            "a technical issue?"
        ),
    },
    "billing": {
        "label": "Billing Specialist",
        "voice": "aura-2-thalia-en",
        "prompt": (
            "You are a billing specialist at Acme Support. Help the customer with "
            "charges, balances, and refunds. Use get_account_balance when they ask "
            "about their balance. If they bring up a technical problem instead, "
            "transfer them back to the front desk. Keep replies concise."
        ),
        "functions": [
            GET_BALANCE,
            transfer_function({"triage": "anything that is not billing-related"}),
        ],
        # Spoken (via InjectAgentMessage) when this agent is transferred IN.
        "transfer_greeting": (
            "Hi, I'm the billing specialist. I've been briefed on your request — "
            "how can I help with your account?"
        ),
    },
    "tech": {
        "label": "Technical Support",
        "voice": "aura-2-orion-en",
        "prompt": (
            "You are a technical support specialist at Acme Support. Help the "
            "customer troubleshoot. Use run_diagnostic when a health check would "
            "help. If they bring up a billing question instead, transfer them back "
            "to the front desk. Keep replies concise."
        ),
        "functions": [
            RUN_DIAGNOSTIC,
            transfer_function({"triage": "anything that is not a technical issue"}),
        ],
        "transfer_greeting": (
            "Hi, I'm technical support. I've got the context from your conversation "
            "— what issue are you seeing?"
        ),
    },
}
