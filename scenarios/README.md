# Scenarios

Each folder here is one demo **scenario** — its config plus a `call-script.md`
describing what to say while running it. There are two kinds:

- **Single-agent** — a raw Voice Agent `Settings` JSON (`settings.json`). Selected
  with `SCENARIO_FILE` in `.env`.
- **Multi-agent** — a Python definition (`<name>.py`, e.g. `acme_support.py`) of
  several personas that hand off to each other via the orchestrator. This is the
  app's default demo.

## Layout

```
scenarios/
  <single-agent-scenario>/
    settings.json     # raw Voice Agent Settings (agent prompt, voice, keyterms, …)
    call-script.md    # what to say during the demo (the presenter's script)

  <multi-agent-scenario>/
    <name>.py         # personas: AGENTS, ENTRY, THINK_PROVIDER, LISTEN_MODEL
    call-script.md    # what to say to exercise the handoffs
```

- **`settings.json`** is sent verbatim as the first `Settings` frame. Everything
  under `agent` (prompt, greeting, `listen`/`think`/`speak`, `keyterms`) is yours to
  edit; the `audio` block is overwritten by the proxy (transport-owned).
- **`<name>.py`** declares the persona graph using the `Agent`/`Tool` primitives
  from `orchestrator.py` (the reusable engine). It's scenario *definition*, not
  reusable code.
- **`call-script.md`** is human-facing only — the app never reads it. It's the demo
  runbook / help.

## Selecting a scenario

- **Multi-agent (default):** leave `SCENARIO_FILE` unset. The proxy loads the
  multi-agent definition named by `AGENTS_SCENARIO` in `.env` (default
  `scenarios/acme-support/acme_support.py`). Point it at another to switch:

  ```bash
  AGENTS_SCENARIO=scenarios/financial-advisory/financial_advisory.py
  ```
- **Single-agent:** set `SCENARIO_FILE` in `.env` to the folder name:

  ```bash
  SCENARIO_FILE=brightmoor-homecare   # -> scenarios/brightmoor-homecare/settings.json
  ```

  A value ending in `.json` is treated as a direct path instead.

Every persona's prompt is prepended with a shared voice-style preamble
(`prompt_style.py`) so responses render cleanly through TTS — keep role prompts
short and let that handle the formatting/speaking rules.

## Adding a scenario

**Single-agent:** `mkdir scenarios/<name>`, add `settings.json` (copy an existing
one) and `call-script.md`, then set `SCENARIO_FILE=<name>`.

**Multi-agent:** `mkdir scenarios/<name>`, add `<name>.py` (copy an existing one)
and `call-script.md`, wrap each persona prompt with `persona()` from
`prompt_style.py`, then set `AGENTS_SCENARIO` to its path.

## Available scenarios

- **`acme-support`** *(multi-agent, default)* — Acme Support desk: triage → billing
  (seamless) → tech (distinct specialist, Anthropic model). Shows mid-session
  transfers with retained history across providers.
- **`financial-advisory`** *(multi-agent)* — Acme Financial Services sales funnel:
  qualifier ("Alex") → advisor → closer, with distinct aura voices per phase.
  Mirrors Deepgram's multi-agent architecture example, minus the reconnect +
  summarization (context carries mid-session).
- **`brightmoor-homecare`** *(single-agent)* — UK clinical-homecare patient-services
  agent ("Debbie"), showcasing Nova-3 keyterm prompting on specialist drug names.
