"""
Acme Financial Services — a multi-agent "sales funnel" scenario.

Mirrors Deepgram's multi-agent architecture example
(https://developers.deepgram.com/docs/multi-agent-architecture): a linear
Qualifier -> Advisor -> Closer funnel for an outbound advisory call.

The difference is the mechanism. That example tears down and reopens a Voice
Agent session per phase, so it needs an external LLM (Groq) to summarize and
re-inject context on every handoff. This repo's orchestrator instead swaps the
think/voice config MID-SESSION (`UpdateThink`/`UpdateSpeak`) over one socket, so
the full conversation carries across each handoff automatically — no reconnect,
no summarization step. Each agent still runs its own prompt, tools and voice.

You play the lead; "Alex" (the qualifier) opens the call. See call-script.md.
"""

from __future__ import annotations

from orchestrator import Agent, Tool
from prompt_style import persona

# All phases share one cheap/fast model; per-agent overrides are possible (see
# the acme-support scenario for a cross-provider example).
THINK_PROVIDER = {"type": "open_ai", "model": "gpt-4o-mini"}
LISTEN_MODEL = "flux-general-en"

ENTRY = "qualifier"


# --- Business (mock) tool handlers -----------------------------------------

def schedule_followup(args: dict) -> dict:
    return {"status": "scheduled",
            "when": args.get("preferred_timeframe", "next week"),
            "confirmation": "AFS-4821"}


def record_satisfaction(args: dict) -> dict:
    return {"status": "recorded", "rating": args.get("rating")}


def end_conversation(args: dict) -> dict:
    # Mock: the orchestrator has no session-end primitive, so this just
    # acknowledges. The agent says its closing line; the caller hangs up.
    return {"status": "ended"}


END_CALL = Tool(
    "end_conversation",
    "End the call politely when the customer is done, or if it's not a good time.",
    {"type": "object", "properties": {"reason": {"type": "string"}}},
    end_conversation,
)


# --- Personas (linear funnel: qualifier -> advisor -> closer) ---------------
#
# Distinct aura voices per phase (aura switches mid-session correctly), so the
# caller hears a genuine handoff between specialists. Each downstream agent
# continues the SAME call — its prompt opens by acknowledging what the caller
# already said, since the orchestrator carries the history across.

QUALIFIER = Agent(
    name="qualifier",
    voice="aura-2-mars-en",
    prompt=persona(
        "You are Alex from Acme Financial Services, calling a warm lead who asked to "
        "hear from us. Acknowledge their interest, check that now is a good time, then "
        "find out their first name, roughly where they're based, and what they're "
        "hoping to get help with. As soon as you know their need, transfer to the "
        "advisor by calling the transfer function — don't announce it yourself, the "
        "handoff is spoken for you. If it's a bad time, offer to call back and close warmly."
    ),
    greeting=(
        "Hi, this is Alex from Acme Financial Services, you'd asked to hear from us, "
        "is now an okay time to chat for a minute?"
    ),
    tools=[END_CALL],
    transfers_to={"advisor": "the lead is engaged and has shared what they need help with"},
    # Speak a handoff line (in Alex's voice) before the voice switches to the advisor.
    announce_transfer=True,
    handoff_line="Sure, let me hand you over to one of our advisors.",
)

ADVISOR = Agent(
    name="advisor",
    voice="aura-2-thalia-en",
    provider="anthropic",
    model="claude-sonnet-4-5",
    prompt=persona(
        "You are Jordan, a financial advisor at Acme Financial Services, who has just "
        "been brought onto the call by Alex. Your FIRST turn must be a brief, warm "
        "handover: introduce yourself by name and as the advisor, and reference what "
        "Alex passed along, the caller's name and what they're after, so it's clear a "
        "new person has taken over. Then ask a couple of clarifying questions, give "
        "high-level general guidance, and recommend a formal consultation for the "
        "specifics. Keep it general, no personalized investment advice. When they're "
        "ready to book, hand off to scheduling."
    ),
    transfers_to={"closer": "the caller wants to book a follow-up consultation"},
)

CLOSER = Agent(
    name="closer",
    voice="aura-2-thalia-en",
    prompt=persona(
        "You are the scheduling specialist at Acme Financial Services, continuing the "
        "same call. Thank the caller, confirm a time for the follow-up consultation "
        "(offer a couple of slots next week if they're unsure) and book it with "
        "schedule_followup once they pick one. Then ask how they'd rate the call from "
        "one to five, WAIT for them to actually say a number, and only then log THAT "
        "number with record_satisfaction — never guess or assume a rating. After they "
        "give it, close warmly and call end_conversation."
    ),
    tools=[
        Tool(
            "schedule_followup",
            "Book the follow-up consultation for the customer.",
            {"type": "object",
             "properties": {"preferred_timeframe": {"type": "string"},
                            "notes": {"type": "string"}},
             "required": ["preferred_timeframe"]},
            schedule_followup,
        ),
        Tool(
            "record_satisfaction",
            "Record the customer's satisfaction rating from one to five.",
            {"type": "object",
             "properties": {"rating": {"type": "integer"},
                            "feedback": {"type": "string"}},
             "required": ["rating"]},
            record_satisfaction,
        ),
        END_CALL,
    ],
    transfers_to={},  # terminal phase
)

AGENTS = [QUALIFIER, ADVISOR, CLOSER]
