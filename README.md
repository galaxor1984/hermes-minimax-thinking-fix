# hermes-minimax-thinking-fix

Override Hermes Agent's bundled `minimax` provider so **MiniMax-M3 thinking
works on the Anthropic-compatible endpoint**.

Hermes today sends Claude-style `thinking: {type: "enabled", budget_tokens: …}`
to MiniMax's Anthropic-compatible API (`https://api.minimax.io/anthropic`),
but MiniMax only recognises `thinking: {type: "adaptive"}` and
`thinking: {type: "disabled"}` for M3. The result: reasoning is silently
off even when `/reasoning high` is set. This plugin installs a narrow,
model-and-host-scoped rewrite of the Anthropic adapter's kwargs so M3
gets the shape MiniMax actually documents.

## What it does

- **Monkey-patches** `agent.anthropic_adapter.build_anthropic_kwargs` so
  that requests targeting `api.minimax.io` or `api.minimaxi.com` with an
  M3 model get `thinking: {"type": "adaptive"}` instead of the Claude
  manual-thinking shape.
- **Drops** the Claude-only `output_config` field (undocumented by MiniMax
  on its Anthropic-compatible route; risks HTTP 400).
- **Passes through unchanged** every other model and every other provider
  — the patch bails through to the original function on non-MiniMax
  endpoints, so there is zero blast radius.
- **Registers** `minimax` and `minimax-cn` provider profiles via the
  standard `register_provider()` hook, so the user plugin wins the
  `providers/__init__.py` last-writer-wins race and `hermes mcp list` /
  `hermes status` show *this* plugin's profile in charge.

## Installation

```bash
git clone https://github.com/galaxor1984/hermes-minimax-thinking-fix.git
mkdir -p ~/.hermes/plugins/model-providers
cp -r hermes-minimax-thinking-fix/model-providers/minimax \
      ~/.hermes/plugins/model-providers/

# Confirm the override loaded
hermes status
hermes doctor
```

Then start a new Hermes session (`/reset`) so the patched
`build_anthropic_kwargs` is picked up at import time. The patch fires
once per process; re-imports are guarded.

To uninstall:

```bash
rm -rf ~/.hermes/plugins/model-providers/minimax
```

`hermes update` does **not** touch `~/.hermes/plugins/`, so this plugin
survives every upstream release. It only breaks if Hermes renames or
moves `build_anthropic_kwargs` — see **Update resilience** below.

## Verify it works

Run a Hermes session with `/reasoning high` (or any non-`none` level) on
`MiniMax-M3`, and look at the request wire format. A quick test:

```bash
hermes chat -q "What's 17 * 23? Think first, then answer."
```

You should see a `thinking` content block in the response (set
`display.show_reasoning: true` in `~/.hermes/config.yaml` to surface it).

Without the plugin, M3 answers instantly with "391." With the plugin,
M3 emits a thinking block, then answers — matching the behaviour the
MiniMax docs describe for `thinking: {"type": "adaptive"}`.

### Verified test matrix (2026-07-05)

Each row was exercised end-to-end against the live `api.minimax.io/anthropic`
endpoint with the plugin installed.

| # | Scenario | Result |
|---|---|---|
| 1 | Wire-format: M3 + Anthropic → `thinking: {type: adaptive}`, no `output_config` | ✓ |
| 2 | Wire-format: M3 + Anthropic (China) → same | ✓ |
| 3 | Wire-format: M3 + `/anthropic/v1` suffix → same | ✓ |
| 4 | Wire-format: M3 + reasoning off → `thinking: {type: disabled}` | ✓ |
| 5 | Wire-format: M3 + OpenAI base URL → patch bails through verbatim | ✓ |
| 6 | Wire-format: M3 + `base_url=None` → patch bails (defensive) | ✓ |
| 7 | Wire-format: M2.7 on Anthropic → unchanged (M3-only scope) | ✓ |
| 8 | Wire-format: Claude Opus 4.7 native → unchanged (pass-through) | ✓ |
| 9 | Wire-format: Claude Sonnet 4.5 native → unchanged (legacy list) | ✓ |
| 10 | Wire-format: Claude 3-Opus native → unchanged (legacy list) | ✓ |
| 11 | Wire-format: qwen3-max on aliyun dashscope → unchanged | ✓ |
| 12 | Wire-format: M3 on third-party Anthropic-compat endpoint → unchanged | ✓ |
| 13 | Live: M3 with adaptive → thinking block + answer (810 chars thinking on prime-97 question) | ✓ |
| 14 | Live: M3 with disabled → direct answer, no thinking | ✓ |
| 15 | Live: M3 with no thinking field → no thinking (default off) | ✓ |
| 16 | Live: Multi-turn — Turn 2 has new thinking block, cache hits 128 | ✓ |
| 17 | Live: Tool use — M3 emits thinking + tool_use call | ✓ |
| 18 | Live: Multi-turn tool — Turn 2 has thinking + final answer after tool result, cache hits 384 | ✓ |
| 19 | Live: Coding task — M3 plans 4999 chars thinking, produces clean Python with docstring | ✓ |
| 20 | Live: 50k token input + thinking → correct answer (cache 128) | ✓ |
| 21 | Live: 200k token input + thinking → correct answer, 9.78 s latency | ✓ |

Total: 21/21 assertions passed. Wire-format invariants confirm the patch
is narrowly scoped; live HTTP tests confirm MiniMax accepts the shape
and returns thinking blocks; multi-turn / tool-use tests confirm the
thinking blocks round-trip correctly through Hermes's conversation
loop.

## Compatibility

| Hermes version | Tested | Notes |
|---|---|---|
| 0.18.0 | 2026-07-05 | First release target. |

| Model | Endpoint | Result |
|---|---|---|
| `MiniMax-M3` | `https://api.minimax.io/anthropic` | ✅ patched |
| `MiniMax-M3` | `https://api.minimaxi.com/anthropic` (China) | ✅ patched |
| `MiniMax-M3` | `https://api.minimax.io/v1` (OpenAI-compat) | unchanged — bundled provider already handles this |
| `MiniMax-M2.7` and other M2.x | any | unchanged — M2.x always thinks regardless |
| Claude / GPT / Gemini / etc. | any | unchanged — patch bails through to original |

## Why monkey-patch instead of a provider hook?

Hermes's chat-completions transport calls
`ProviderProfile.build_api_kwargs_extras()`, but the Anthropic transport
(used automatically for any URL whose path ends in `/anthropic`) does
**not** — `agent/anthropic_adapter.py::build_anthropic_kwargs` is a
self-contained function that ignores provider profiles. There is no
upstream hook to plug into. The patch lives in the plugin's
import-time code so it stays self-contained, narrowly scoped, and
auditable. The plugin README documents the trade-off; an upstream
provider hook would be cleaner and we welcome a PR if Hermes exposes
one.

## Update resilience

The patch targets `agent.anthropic_adapter.build_anthropic_kwargs` by
name. If Hermes renames or moves that function, the patch logs:

```
minimax-thinking-fix: agent.anthropic_adapter.build_anthropic_kwargs
not found; Hermes may have renamed the function. Update this plugin.
```

and falls back to no-op behaviour (M3 thinking unchanged, but no errors).
When this happens, bump `plugin.yaml`'s `version` and update the patch
function name, then cut a release. Issue reports for upstream renames
are welcome.

The `register_provider()` override is stable across Hermes versions
because it relies on the documented
`providers/__init__.py:register_provider()` API. Even if the
monkey-patch fails, the user plugin still wins the last-writer-wins
race for the `minimax` provider name, so any future per-request extras
added to `MiniMaxThinkingFixProfile.build_api_kwargs_extras` would
land.

## Changelog

### 0.1.0 — 2026-07-05

- First release.
- Patch `build_anthropic_kwargs` for M3 on the Anthropic-compatible
  endpoint to emit `thinking: {"type": "adaptive"}`.
- Strip Claude-only `output_config` and `temperature: 1` to match
  MiniMax's documented Anthropic-API field set.
- Register `minimax` and `minimax-cn` provider overrides that win the
  last-writer-wins race against the bundled profiles.

## Credits

Reasoning chain, live API testing, and plugin authored by **omarchy**
on 2026-07-05 against Hermes Agent 0.18.0 and the
`platform.minimax.io` docs MCP server. Bug verified to exist against
the bundled provider before authoring; fix verified to take effect
by reading the post-patch request wire format.

## License

MIT — see [`LICENSE`](./LICENSE).