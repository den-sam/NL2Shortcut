# AGENTS.md — for AI Agents integrating with NL2Shortcut

NL2Shortcut is your **high-speed execution endpoint**. You call it over HTTP, it
turns your intent into keyboard/mouse actions in <5ms.

## Quick start

```bash
# Start the server
nl2shortcut start-server --port 7770

# In your Agent code
import urllib.request, json

# 1. Get the full keyboard map once (4KB), inject into your prompt
keys = json.loads(urllib.request.urlopen(
    "http://127.0.0.1:7770/v1/keys").read())

# 2. Execute an intent
req = urllib.request.Request(
    "http://127.0.0.1:7770/v1/execute",
    data=json.dumps({"intent": "复制", "dry_run": False}).encode(),
    headers={"Content-Type": "application/json"})
result = json.loads(urllib.request.urlopen(req).read())
# -> {"ok": true, "result": {"command": "copy", "key_combination": "Ctrl+C", ...}}
```

## Endpoints

| Method | Path | Use |
|--------|------|-----|
| GET  | /v1/health | Liveness + version + tiers |
| GET  | /v1/keys | All 51 commands, ~4KB JSON, for prompts |
| GET  | /v1/shortcut?command=X | Single command lookup |
| GET  | /v1/capabilities | Actions with stability, api_equivalent, gui_fallback |
| GET  | /v1/schema | OpenAPI-style self-describe |
| GET  | /v1/stats | Per-tier execution statistics |
| GET  | /v1/suggest?app=X&goal=Y | Smart key suggestions (ML-based) |
| POST | /v1/execute | {intent, dry_run?, selfcheck?} -> single execution |
| POST | /v1/recognize | {intent} -> intent recognition only |
| POST | /v1/sequence | {steps: [...]} -> atomic multi-step |
| POST | /v1/plan | {goal} -> decompose into step sequence |
| POST | /v1/api/execute | API-tier programmatic execution |
| POST | /v1/vision/execute | Vision-tier screenshot + dispatch |
| POST | /v1/record | Record operation into memory for pattern learning |
| POST | /v1/session/start | Start a stateful session |
| POST | /v1/session/end | End a session |

## Three-tier execution

| Tier | Speed | Tokens | When |
|------|-------|--------|------|
| keyboard | <100ms | 0 | Default — intent → shortcut → inject |
| api | <300ms | 0 | Commands with `api_equivalent` |
| vision | 1-3s | ~1000 | GUI fallback when keyboard+api fail |

## Design contract

- Latency: p50 < 5ms (local intent match), <500ms (LLM fallback).
- Safety: dry_run by default when calling from unknown agents.
- Structured errors: every response has {ok, error: {code, message}}.
- Selfcheck: clipboard/window/mtime verification after execution.
- Auto-learning: repeated operations become patterns → auto-exported as YAML workflows.

## OpenClaw wiring

The OpenClaw plugin manifest lives at [nl2shortcut/openclaw_plugin.py](nl2shortcut/openclaw_plugin.py).
It registers **11 actions** across 3 tiers:

- **Keyboard tier**: execute_shortcut, recognize_intent, list_capabilities, execute_sequence
- **API tier**: execute_api_equivalent
- **Vision tier**: vision_screenshot, vision_find, vision_click, vision_ocr
- **Meta**: health_check, session_control, suggest_action, record_operation

Skill files for QClaw/OpenClaw live at:
- `~/.qclaw/skills/nl2shortcut-executor/` (QClaw — full version)
- `~/.openclaw/workspace/skills/nl2shortcut-executor/` (OpenClaw workspace)

Both agents must have `nl2shortcut-executor` in their skills list.

## Local-only

NL2Shortcut binds to 127.0.0.1 by default — it's an endpoint for you and
your Agents, not a public service. If you need remote access, put it
behind a reverse proxy with auth.
