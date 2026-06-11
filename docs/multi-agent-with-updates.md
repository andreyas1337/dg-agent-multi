# Multi-Agent Voice Architecture with In-Place Updates

A multi-agent voice system handles one call with several specialized behaviors:
a router, a billing specialist, a technical specialist, instead of one overloaded
prompt. On Deepgram you can build this **without opening a new session per agent**.
Keep one [Voice Agent](https://developers.deepgram.com/docs/voice-agent) session
open for the whole call and reconfigure it in place with the `UpdateThink`,
`UpdateSpeak`, and `UpdatePrompt` commands.

A useful way to think about it: with this approach there isn't a fleet of separate
agents, there is **one Voice Agent that switches roles** mid-call. That single
detail is what keeps context and latency simple.

This page is a companion to the
[Multi-Agent Architecture](https://developers.deepgram.com/docs/multi-agent-architecture)
guide, which builds the same idea by opening a new agent session per agent and
summarizing the conversation across each handoff. Both work; this page covers the
in-place alternative and when to prefer it.

> **TL;DR:** One WebSocket for the whole call. To switch roles, send `UpdateThink`
> (new prompt + tools + model) and optionally `UpdateSpeak` (new voice). The
> conversation history is kept **server-side**, so the next role already has the
> full context: no summarizer, no reconnect, no dead air, and the swap is
> measurably faster (roughly 3 to 5 times in our tests).

---

## Why in-place updates?

The classic way to run multiple agents is to spin up a fresh Voice Agent session
for each one. That works, but every handoff pays for:

- **Re-establishing the connection:** a new WebSocket, TLS handshake, and
  `Settings` round trip before the next agent can speak.
- **Re-passing context:** the new session starts empty, so you must summarize the
  conversation (often with a separate LLM call) and inject it into the new prompt.
- **Bridging audio:** the caller's media path must be kept alive across the gap or
  they hear silence.

Deepgram's Voice Agent API can instead **reconfigure the running session**:

| Command | Replaces | Use for |
|---|---|---|
| [`UpdateThink`](https://developers.deepgram.com/docs/voice-agent-update-think) | provider + model + prompt + functions (atomic) | **switching roles** |
| [`UpdateSpeak`](https://developers.deepgram.com/docs/voice-agent-update-speak) | the TTS voice | giving a role a distinct voice |
| [`UpdatePrompt`](https://developers.deepgram.com/docs/voice-agent-update-prompt) | *appends* to the prompt (does not replace) | injecting dynamic context mid-role |

Because the session never closes, the conversation transcript persists on
Deepgram's side and is replayed to whichever model is active. Switching roles
becomes "swap the brain," not "start a new call."

---

## The Update commands

### `UpdateThink` swaps provider + model + prompt + functions, atomically

A single message replaces the entire think configuration mid-session and the
server acknowledges with `ThinkUpdated`. Because provider and model are part of
it, **each role can run its own model, even its own provider**: a cheap, fast
router can hand off to a stronger specialist model mid-call. `UpdateSpeak` swaps
the voice the same way; `UpdatePrompt` *appends* to the prompt without replacing
it (handy for injecting dynamic context within a role).

### Conversation history lives server-side, so there's no summarizer

Because there is one continuous session, you **do not pass context yourself**.
`UpdateThink` replaces the *configuration*, not the conversation. Whatever model
is active sees the prior turns automatically.

> In our testing this held even **across providers**: an Anthropic specialist
> correctly recalled a customer's name that was only ever spoken to the
> OpenAI-driven router before the handoff.

This is the biggest practical difference from reconnect-based multi-agent, which
needs a summarization step on every transfer.

---

## Architecture overview

```
           Browser / phone  (mic + speaker)
                           │
                           │  audio in/out, continuous (never interrupted)
                           ▼
   ┌──────────────────────────────────────────────┐
   │                   Your app                   │
   │       route the call, swap the config        │
   └───────────────────────┬──────────────────────┘
                           │
                           │  on transfer(to):
                           │    1. UpdateThink  (prompt + tools + model)
                           │    2. UpdateSpeak  (voice, if the role sets one)
                           ▼
   ┌──────────────────────────────────────────────┐
   │             Deepgram Voice Agent             │
   │         (one session, never closed)          │
   │                                              │
   │         Listen  ->  Think  ->  Speak         │
   │                                              │
   │    conversation history kept server-side,    │
   │     replayed to whatever model is active     │
   └──────────────────────────────────────────────┘
```

Your app holds one socket open and sends Update commands. There is no second
connection to manage, no audio to bridge between sessions, and no context to
summarize.

---

## Swapping roles: the basic approach

You don't need any framework. Define each role as plain data (a prompt, the tools
it can use, and optionally its own voice/model), give the model a function it can
call to hand off, and on that call send the Update messages for the target role.

```python
import json

# A "role" is just a think config (prompt + tools, optionally its own model) and
# a voice. Omit the voice to keep the current one (the caller hears one person).
TRANSFER = {
    "name": "transfer",
    "description": ("Hand the caller to another specialist: 'billing', 'tech', or "
                    "'router'. Call this silently; the next specialist continues."),
    "parameters": {"type": "object",
                   "properties": {"to": {"type": "string",
                                         "enum": ["billing", "tech", "router"]}},
                   "required": ["to"]},
}

ROLES = {
    "router": {
        "prompt": "You are the front desk for Acme Support. Route the caller to "
                  "billing or tech. Keep replies to one sentence.",
        "voice": "aura-2-asteria-en",
        "functions": [TRANSFER],
    },
    "billing": {
        "prompt": "You are continuing as the same assistant, now handling billing. "
                  "Do not reintroduce yourself. Use get_balance for balances.",
        "voice": None,                       # inherit the current voice -> seamless
        "functions": [GET_BALANCE, TRANSFER],
    },
    "tech": {
        "prompt": "You are a technical specialist. Introduce yourself in one short "
                  "sentence, then help troubleshoot.",
        "voice": "aura-2-orion-en",          # its own voice -> distinct specialist
        "provider": "anthropic",             # and its own model, on another provider
        "model": "claude-sonnet-4-20250514",
        "functions": [RUN_DIAGNOSTIC, TRANSFER],
    },
}
```

Switching roles is two messages and their acknowledgements:

```python
async def switch_role(ws, target, current_voice):
    role = ROLES[target]

    # 1. Swap the brain: prompt + tools + (optional) model.
    await ws.send(json.dumps({
        "type": "UpdateThink",
        "think": {
            "provider": {"type": role.get("provider", "open_ai"),
                         "model":  role.get("model", "gpt-4o-mini")},
            "prompt": role["prompt"],
            "functions": role["functions"],
        },
    }))
    await wait_for(ws, "ThinkUpdated")

    # 2. Swap the voice only if this role declares its own.
    if role["voice"] and role["voice"] != current_voice:
        await ws.send(json.dumps({
            "type": "UpdateSpeak",
            "speak": {"provider": {"type": "deepgram", "model": role["voice"]}},
        }))
        await wait_for(ws, "SpeakUpdated")
        current_voice = role["voice"]

    return current_voice
```

Then trigger it when the model calls your `transfer` function:

```python
if fn["name"] == "transfer":
    target = json.loads(fn["arguments"])["to"]
    current_voice = await switch_role(ws, target, current_voice)   # swap first
    await ws.send(json.dumps({                                     # then answer the call
        "type": "FunctionCallResponse",
        "id": fn["id"], "name": "transfer",
        "content": json.dumps({"status": "transferred"}),
    }))
```

That's the whole mechanism. Three details make it smooth:

- **Swap before you answer the function call.** Answering the `transfer` call is
  what triggers the model's next turn. Do the `UpdateThink`/`UpdateSpeak` first, so
  by the time you answer, the *new* role speaks next, not the old one. Tell the
  model (in the tool description) to hand off silently, and you avoid a repeated
  "one moment" line or a double-greeting.
- **Omit a role's voice to keep things seamless.** With no `UpdateSpeak`, the
  caller keeps hearing the same voice, so a router that gains billing tools feels
  like one assistant. Give a role its own voice to make it a distinct specialist.
  Whether it introduces itself is just a matter of what its prompt says.
- **Keep the socket fed.** The Voice Agent socket expects continuous audio; if it
  goes quiet too long the server closes it. A live mic or phone leg covers this. If
  your transport can go silent, send audio frames or `KeepAlive` messages,
  including across the handoff.

---

## Example conversation flow

```
Agent (router, asteria voice):  Hi, thanks for calling Acme Support! How can I help?
You:                            Hi, I'm Sam Rivera. What's my account balance?
                                [transfer to billing]   (UpdateThink; voice inherited)
Agent (billing, same voice):    Your balance is $1,234.56.   [get_balance]
You:                            Actually my laptop won't turn on.
                                [transfer to tech]      (UpdateThink + UpdateSpeak)
Agent (tech, orion voice):      I'm technical support, let's take a look. [run_diagnostic]
                                All systems look nominal; is it plugged in and charging?
```

`billing` answered without re-greeting (same perceived person), `tech` introduced
itself in a new voice, and neither needed the customer's name re-stated: it was
already in the shared history.

---

## Advantages over reconnect-per-agent

| | Reconnect per agent | In-place Update |
|---|---|---|
| Sessions | new WebSocket each agent | **one**, whole call |
| Conversation history | lost; must summarize + re-inject | **retained server-side** |
| Extra context plumbing | summarizer LLM call per handoff | **none** |
| Audio continuity | gap / dead air during reconnect | **uninterrupted** |
| Per-role model / provider | yes (new `Settings`) | yes (`UpdateThink`) |
| Handoff latency (measured*) | **~948 ms** US / ~215 ms EU, **plus summarizer + audio re-stream** | **~300 ms** US / ~47 ms EU |
| Context isolation between roles | yes (fresh session) | no (shared history) |

\* Measured with the sample's `latency_test.py` (8 iterations, single client
location). The reconnect figure is connection + `Settings` to `SettingsApplied`
only; it does **not** include the summarizer call or re-streaming audio, both of
which a real reconnect handoff also pays and in-place Update avoids. Seamless
handoffs (no voice change) are even faster, just the `UpdateThink` round trip.

---

## When to use which

**Prefer in-place Update** when you want the caller to experience one continuous
conversation, low handoff latency, and minimal plumbing: the common case for
routing, escalation, and specialist hand-offs.

**Reconnect-per-agent** still makes sense when you need:

- **Context isolation:** a role that must *not* see earlier turns (e.g.,
  compliance, or a clean-slate sub-task). Shared server-side history is
  all-or-nothing.
- **A different audio profile:** `UpdateThink`/`UpdateSpeak` don't change the audio
  encoding/sample rate or the STT model; a fresh `Settings` does.

Two operational notes for the Update approach:

- **History grows for the whole call.** It's automatic, but very long sessions
  accumulate tokens; consider `UpdatePrompt`-based compaction for marathon calls.
- **Function-calling reliability varies by model.** The handoff depends on the
  model emitting the `transfer` call; test each model you route to.

---

## Going further

The code above is all you need. If you find yourself adding many roles, you'll
probably want to factor the role table and the swap sequence into a small helper,
for example a config type plus a `switch_role()` that also derives the `transfer`
tool's target list from each role's allowed destinations.

The sample in this repository includes one such helper (see `orchestrator.py`),
along with a browser UI, a mic-free end-to-end test (`selftest.py`), and the
latency benchmark (`latency_test.py`). Treat it as one example of organizing the
mechanism, not a required structure.

---

## Additional resources

- [Voice Agent API](https://developers.deepgram.com/docs/voice-agent)
- [`UpdateThink`](https://developers.deepgram.com/docs/voice-agent-update-think),
  [`UpdateSpeak`](https://developers.deepgram.com/docs/voice-agent-update-speak),
  [`UpdatePrompt`](https://developers.deepgram.com/docs/voice-agent-update-prompt)
- [Multi-Agent Architecture (reconnect-based companion guide)](https://developers.deepgram.com/docs/multi-agent-architecture)
