# Multi-Agent Voice Agents with Mid-Session Updates

A multi-agent voice experience routes a single call through several specialized
behaviors — for example a router, a billing specialist, and a technical
specialist — instead of one large prompt that tries to do everything. Splitting
the work keeps each behavior focused, easier to test, and cheaper to run, because
each role can use the prompt, tools, and even the model best suited to it.

There are two common ways to build this on the
[Voice Agent API](https://developers.deepgram.com/docs/voice-agent):

- **A new session per agent** — open a fresh Voice Agent session for each
  behavior and carry context forward across handoffs. This is covered in
  [Multi-Agent Architecture](https://developers.deepgram.com/docs/multi-agent-architecture).
- **Mid-session updates** — keep one session open for the whole call and
  reconfigure it in place as the conversation moves between behaviors. That is
  what this page describes.

## How it works

A Voice Agent session is configured once, when it opens, with a `Settings`
message. Mid-session updates use additional messages to change that configuration
while the session stays connected:

- Send [`UpdateThink`](https://developers.deepgram.com/docs/voice-agent-update-think)
  to switch the active behavior — a new prompt, tools, and model in one atomic
  change.
- Send [`UpdateSpeak`](https://developers.deepgram.com/docs/voice-agent-update-speak)
  to change the voice, if a role should sound different.
- Send [`UpdatePrompt`](https://developers.deepgram.com/docs/voice-agent-update-prompt)
  to append instructions to the current prompt without replacing it.
- Send [`UpdateListen`](https://developers.deepgram.com/docs/voice-agent-update-listen)
  to tune speech-to-text settings (such as keyterms) without a new session.

Because the session never closes, the conversation history is maintained
server-side and is available to whichever configuration is active. Switching
roles changes the *configuration*, not the conversation: the newly active role
already has the full context of what was said before.

| Command | What it changes | Typical use |
|---|---|---|
| `UpdateThink` | provider, model, prompt, and functions (atomic) | switch to a different role |
| `UpdateSpeak` | the TTS voice | give a role a distinct voice |
| `UpdatePrompt` | appends to the current prompt | inject dynamic context within a role |
| `UpdateListen` | tunable STT settings — keyterms, language hints, end-of-turn thresholds (not the model or version) | adapt recognition to the active role |

Each Update is acknowledged (`ThinkUpdated`, `SpeakUpdated`, `PromptUpdated`,
`ListenUpdated`). Note that `UpdateListen` can only tune the settings above — the
STT **model and version are fixed for the session**; changing them (or the audio
encoding or sample rate) requires a new session.

## Architecture

```
        Caller (mic + speaker, or phone leg)
                       │
                       │  audio in / out, continuous
                       ▼
   ┌──────────────────────────────────────────────┐
   │                 Your application              │
   │        routes the call, sends Updates         │
   └───────────────────────┬──────────────────────┘
                           │  on handoff:
                           │    UpdateThink  (prompt + tools + model)
                           │    UpdateSpeak  (voice, if the role changes it)
                           ▼
   ┌──────────────────────────────────────────────┐
   │            Deepgram Voice Agent               │
   │            (one session per call)             │
   │                                               │
   │          Listen  ->  Think  ->  Speak         │
   │                                               │
   │   conversation history kept server-side,      │
   │    available to whichever role is active      │
   └──────────────────────────────────────────────┘
```

Your application keeps one socket open and sends Update messages as the call
progresses. There is no second connection to manage and no context to carry
between sessions.

## Switching roles

Define each role as data — a prompt, the functions it can use, and optionally its
own voice and model — and give the model a function it can call to hand off. When
the model calls that function, send the Update messages for the target role.

```python
import json

# The model calls this to hand off. Its enum lists the reachable roles.
TRANSFER = {
    "name": "transfer",
    "description": ("Hand the caller to another specialist: 'billing', 'tech', or "
                    "'router'. Call this without saying anything; the next "
                    "specialist continues the conversation."),
    "parameters": {"type": "object",
                   "properties": {"to": {"type": "string",
                                         "enum": ["billing", "tech", "router"]}},
                   "required": ["to"]},
}

# A role is a think config (prompt + tools, optionally its own model) and a voice.
# Omit the voice to keep the current one, so the caller keeps hearing one person.
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
        "voice": None,                       # inherit the current voice
        "functions": [GET_BALANCE, TRANSFER],
    },
    "tech": {
        "prompt": "You are a technical specialist. Introduce yourself in one short "
                  "sentence, then help troubleshoot.",
        "voice": "aura-2-orion-en",          # its own voice
        "provider": "anthropic",             # and its own model / provider
        "model": "claude-sonnet-4-5",
        "functions": [RUN_DIAGNOSTIC, TRANSFER],
    },
}


async def switch_role(ws, target, current_voice):
    role = ROLES[target]

    # 1. Swap the behavior: prompt + tools + (optional) model, atomically.
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

Trigger the switch when the model calls the `transfer` function, then answer the
function call so the newly active role produces the next turn:

```python
if fn["name"] == "transfer":
    target = json.loads(fn["arguments"])["to"]
    current_voice = await switch_role(ws, target, current_voice)   # swap first
    await ws.send(json.dumps({                                     # then answer
        "type": "FunctionCallResponse",
        "id": fn["id"], "name": "transfer",
        "content": json.dumps({"status": "transferred"}),
    }))
```

## Sequencing a handoff

A handoff is a short, ordered exchange on the same socket. The order matters:
answering the transfer function call is what makes the model take its next turn,
so you want the new configuration in place *before* that happens.

1. The active model calls `transfer`, and you receive a `FunctionCallRequest`. Do
   not answer it yet.
2. Send `UpdateThink` with the target role's prompt, tools, and model, and wait for
   the `ThinkUpdated` acknowledgement.
3. If the target role uses a different voice, send `UpdateSpeak` and wait for
   `SpeakUpdated`. Skip this to keep the current voice.
4. Answer the original call with `FunctionCallResponse`. The now-current role
   produces the next turn.

```
transfer() called ─► FunctionCallRequest
                         │   (hold the response)
                         ▼
                     UpdateThink ─► ThinkUpdated
                         │
                         ▼   (only if the voice changes)
                     UpdateSpeak ─► SpeakUpdated
                         │
                         ▼
                     FunctionCallResponse ─► new role speaks
```

When and how to queue the update:

- **Send on one socket, in order, and wait for each acknowledgement**
  (`ThinkUpdated`, then `SpeakUpdated`) before the next step. Don't send the
  Update messages concurrently or assume they applied without the ack.
- **An Update applies to the model's next turn, not to audio already in flight.**
  Sending `UpdateThink` in the middle of a spoken turn won't rewrite that turn; it
  takes effect on the following generation.
- **Answer the transfer call last.** Answering it before the swap lands lets the
  outgoing role produce another turn in its old configuration.
- **Keep audio flowing during the handshake** so the session doesn't time out
  while you wait for acknowledgements.
- **For a spoken handoff line** ("let me hand you over"), play it after step 1 —
  for example by injecting an agent message — and wait for its audio to finish
  before step 3, so the new voice doesn't talk over it.

## Structuring role prompts

Each role's prompt is self-contained and scoped to that one role. A workable
shape:

- **Identity and scope.** Who the role is and what it handles: "You are a billing
  specialist for Acme Support; handle charges, balances, and refunds."
- **Continuation vs. introduction.** Because history is shared, decide whether the
  role continues as the same assistant ("do not reintroduce yourself, just keep
  helping") or presents itself as a distinct specialist ("introduce yourself in one
  short sentence"). Pair this with the voice: same voice + continue reads as one
  assistant; new voice + introduce reads as a separate specialist.
- **Tool use.** Name the tools the role should use, tell it to rely on them for
  facts instead of guessing, and have it ask for any value a tool needs (a rating,
  a date, an amount) before making the call.
- **Handoff conditions.** Describe when to hand off and to which role, and instruct
  the model to do so by calling the transfer function. Do **not** tell it to
  announce the transfer in words — a turn is either speech or a function call, so
  narrating the handoff makes it speak instead of transferring (see Best
  practices).
- **Voice-first formatting.** Plain conversational text, numbers spelled as words,
  one or two sentences per turn.

Keep role prompts short. Routing lives in the transfer tool's target list, so a
prompt only needs the handoff *conditions*, not the whole routing table — and
because history is shared, a role does not need the earlier conversation restated
in its prompt.

## Example flow

```
Agent (router, asteria voice):  Hi, thanks for calling Acme Support! How can I help?
You:                            Hi, I'm Sam Rivera. What's my account balance?
                                [transfer to billing]   (UpdateThink; voice inherited)
Agent (billing, same voice):    Your balance is $1,234.56.   [get_balance]
You:                            Actually my laptop won't turn on.
                                [transfer to tech]      (UpdateThink + UpdateSpeak)
Agent (tech, orion voice):      I'm technical support, let's take a look. [run_diagnostic]
                                Everything looks nominal; is it plugged in and charging?
```

`billing` answered without re-greeting, `tech` introduced itself in a new voice,
and neither needed the caller's name repeated — it was already in the shared
history.

## Best practices

- **Sequence the handoff correctly.** Swap the configuration before answering the
  transfer call, and wait for each acknowledgement — see
  [Sequencing a handoff](#sequencing-a-handoff).

- **Decide how the handoff sounds.** A model turn is either spoken text *or* a
  function call, not both — so a role cannot both say "let me transfer you" and
  call the transfer function in the same turn, and a prompt that asks it to
  announce the handoff tends to make it speak and skip the call. Keep the transfer
  function silent (state that in its description) and shape the experience instead:
  - *Seamless:* don't change the voice; the same voice continues, so a router that
    gains billing tools feels like one assistant.
  - *Distinct specialist:* give the target its own voice and let its prompt
    introduce it.
  - *Announced:* to have the outgoing role say a line like "let me hand you over,"
    have your application speak it (for example with an injected agent message)
    after the silent transfer call and before the voice changes, so nothing is
    talked over.

- **Keep the socket fed.** The Voice Agent socket expects continuous audio; if it
  goes quiet for too long the server closes it. A live mic or phone leg covers
  this. If your transport can go silent, send audio frames or `KeepAlive`
  messages, including across a handoff.

- **Write prompts for speech.** Responses are read aloud verbatim, so instruct
  each role to produce plain conversational text — no markdown, lists, or
  bracketed stage directions — and to spell numbers as words.

- **Gather tool inputs before calling tools.** When a function needs a detail from
  the caller (a rating, a date, an amount), have the role ask for it and wait for
  the answer before making the call, rather than assuming a value.

## Considerations

- **Shared history has no isolation.** Every role sees the earlier turns. This is
  what removes the summarization step, but it means you cannot hide prior context
  from a specific role. If a role must start from a clean slate, use a separate
  session for it.

- **The STT model and audio format are fixed for the session.** `UpdateListen` can
  tune recognition mid-session (keyterms, language hints, end-of-turn thresholds),
  but the speech-to-text model and version can't change — attempting to swap them
  is rejected with a `Warning` (`UPDATE_LISTEN_UNSUPPORTED_FIELDS_CHANGED`) and the
  current settings are kept. The input/output audio encoding and sample rate are
  fixed too. Changing any of these requires a new session with a fresh `Settings`.

- **History grows for the whole call.** Because context accumulates automatically,
  very long calls build up tokens. For extended sessions, consider compacting
  earlier context (for example with `UpdatePrompt`).

- **Function-calling reliability varies by model.** A handoff depends on the active
  model reliably emitting the `transfer` call. Test the behavior with each model
  you route to, especially smaller or faster ones.

## Choosing an approach

Both approaches are valid; they trade off along a few axes.

| | New session per agent | Mid-session updates |
|---|---|---|
| Sessions per call | one per agent | one for the whole call |
| Conversation history | starts empty; summarize and re-inject | retained server-side, shared |
| Context isolation between roles | yes (each session is fresh) | no (roles share history) |
| Audio continuity across handoff | media path must be bridged | uninterrupted |
| Per-role model, provider, voice | yes (new `Settings`) | yes (`UpdateThink` / `UpdateSpeak`) |
| Audio format / STT model per role | yes (new `Settings`) | fixed for the session |
| Handoff steps | reconnect, `Settings`, and context re-injection | one or two Update round-trips |

Reach for a **new session per agent** when a role needs context isolation or a
different audio profile. Reach for **mid-session updates** when you want one
continuous conversation, no summarization step, and no audio gap at the handoff —
the common case for routing, escalation, and specialist hand-offs.

The mechanism is small enough to use directly. If you add many roles, it helps to
factor the role table and the swap sequence into a helper — for example a role
type plus a `switch_role()` that also derives the `transfer` tool's target list
from each role's allowed destinations.

## Resources

- [Voice Agent API](https://developers.deepgram.com/docs/voice-agent)
- [`UpdateThink`](https://developers.deepgram.com/docs/voice-agent-update-think),
  [`UpdateSpeak`](https://developers.deepgram.com/docs/voice-agent-update-speak),
  [`UpdatePrompt`](https://developers.deepgram.com/docs/voice-agent-update-prompt)
- [Multi-Agent Architecture (new-session-per-agent approach)](https://developers.deepgram.com/docs/multi-agent-architecture)
