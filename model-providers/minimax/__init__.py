"""MiniMax M3 + Anthropic-compatible endpoint: adaptive thinking override.

WHY THIS PLUGIN EXISTS
======================

Hermes Agent's built-in minimax provider emits M3's thinking config only via
the OpenAI-compatible endpoint (https://api.minimax.io/v1). On the
Anthropic-compatible endpoint (https://api.minimax.io/anthropic — the
default and the one MiniMax documents as "Recommended"), the core
Anthropic adapter builds the ``thinking`` parameter the Claude way:

    thinking: {type: "enabled", budget_tokens: <8000..32000>}

But the MiniMax Anthropic-API reference
(https://platform.minimax.io/docs/api-reference/text-anthropic-api#thinking-control)
specifies:

  * ``thinking`` omitted → thinking OFF by default
  * ``thinking: {"type": "adaptive"}`` → thinking ON
  * ``thinking: {"type": "disabled"}`` → thinking OFF
  * M2.x ignores ``disabled`` and keeps thinking on

So with the Anthropic-compatible endpoint, every Hermes request today
sends the Claude-style manual-thinking shape to a model that only
recognises the adaptive/disable pair — silently getting "no thinking
blocks" back instead of the reasoning trace MiniMax intends.

WHAT THIS PLUGIN DOES
=====================

1. Registers a ``minimax`` provider profile that wins last-writer-wins
   over the bundled one — useful for any future config tweaks.
2. Monkey-patches ``agent.anthropic_adapter.build_anthropic_kwargs`` so
   that when the request targets ``api.minimax.io`` or
   ``api.minimaxi.com`` (the China endpoint) AND the model is an M3
   variant, the final ``kwargs["thinking"]`` becomes
   ``{"type": "adaptive"}`` and the Claude-only ``output_config`` /
   ``temperature: 1`` are dropped. Manual ``budget_tokens`` thinking is
   replaced wholesale — MiniMax's Anthropic endpoint only honours
   ``adaptive`` / ``disabled`` for M3.

WHY MONKEY-PATCH INSTEAD OF A PROVIDER HOOK
===========================================

The chat-completions transport calls ``ProviderProfile.build_api_kwargs_extras``
but the Anthropic transport (used for ``/anthropic`` URLs by Hermes's
auto-detect in ``runtime_provider._detect_api_mode_for_url``) does not —
the Anthropic path is a self-contained ``build_anthropic_kwargs`` function
in ``agent/anthropic_adapter.py`` that ignores provider profiles entirely.
There is no upstream hook to plug into, so the patch lives in this
plugin's import-time code. It is narrowly scoped (only fires for
``minimax`` model + ``minimax`` base URL) and bails through to the
original function on any other model or host.

UPDATE RESILIENCE
=================

This plugin lives under ``$HERMES_HOME/plugins/model-providers/minimax/``
(not the bundled ``plugins/model-providers/minimax/`` inside the
``hermes-agent`` repo). The bundled provider is refreshed by
``hermes update``; the user plugin overrides it via last-writer-wins
(``providers/__init__.py:register_provider``). The monkey-patch targets
``agent.anthropic_adapter.build_anthropic_kwargs`` — if Hermes renames
or moves that function, the patch logs a clear warning and the user
sees "thinking unchanged" behaviour until this repo is bumped. The
plugin then needs a version bump in ``plugin.yaml`` and a release.

CREDITS
=======

Reasoning chain and live API testing by omarchy using MiniMax-M3 via
``~/.hermes/.env`` + ``hermes`` 0.18.0. Verified against the
``platform.minimax.io`` docs MCP server on 2026-07-05.
"""

from __future__ import annotations

import logging
from typing import Any, Callable
from urllib.parse import urlparse

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)


# ── Model / endpoint detection ────────────────────────────────────────

_M3_MODEL_SUBSTRINGS = ("minimax-m3",)


def _is_minimax_m3(model: str | None) -> bool:
    if not model:
        return False
    normalized = model.strip().lower()
    return any(sub in normalized for sub in _M3_MODEL_SUBSTRINGS)


def _is_minimax_anthropic_base_url(base_url: str | None) -> bool:
    """Match MiniMax's Anthropic-compatible endpoints (international + China).

    MiniMax docs list the international URL as
    ``https://api.minimax.io/anthropic`` and the China variant as
    ``https://api.minimaxi.com/anthropic``. We accept both with or
    without trailing version suffix (``/anthropic/v1``), and require
    the path to actually contain ``/anthropic`` — bare ``/v1`` is the
    OpenAI-compatible endpoint, which the bundled provider already
    handles correctly via its ``build_api_kwargs_extras`` hook.

    Host-only check would match the OpenAI-compat endpoint too, since
    both live on the same host. That would silently rewrite the
    thinking shape on the wrong path.
    """
    if not base_url:
        return False
    try:
        host = (urlparse(base_url).hostname or "").lower()
        path = (urlparse(base_url).path or "").lower().rstrip("/")
    except Exception:
        return False
    if host not in {"api.minimax.io", "api.minimaxi.com"}:
        return False
    # Match /anthropic or /anthropic/v1 (some clients add a /v1 suffix).
    return path == "/anthropic" or path.startswith("/anthropic/")


# ── Anthropic adapter monkey-patch ─────────────────────────────────────

_PATCH_FLAG = "_hermes_minimax_thinking_patched"


def _patch_anthropic_kwargs() -> bool:
    """Install a wrapper around ``build_anthropic_kwargs`` that rewrites M3 thinking.

    Returns True if the patch was installed, False if it was already
    in place or the target function could not be found (in which case
    the plugin becomes a no-op until the user updates this repo).
    """
    try:
        from agent import anthropic_adapter
    except ImportError:
        logger.warning(
            "minimax-thinking-fix: agent.anthropic_adapter not importable; "
            "thinking fix inactive (this is harmless in non-CLI contexts)."
        )
        return False

    if getattr(anthropic_adapter, _PATCH_FLAG, False):
        return True  # already patched (re-import safety)

    original: Callable[..., dict[str, Any]] | None = getattr(
        anthropic_adapter, "build_anthropic_kwargs", None
    )
    if original is None:
        logger.warning(
            "minimax-thinking-fix: agent.anthropic_adapter.build_anthropic_kwargs "
            "not found; Hermes may have renamed the function. Update this plugin."
        )
        return False

    def build_anthropic_kwargs_wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original(*args, **kwargs)
        try:
            model = kwargs.get("model")
            if model is None and args:
                model = args[0]
            base_url = kwargs.get("base_url")
            reasoning_cfg = kwargs.get("reasoning_config")
        except Exception:
            return result

        if not (_is_minimax_m3(model) and _is_minimax_anthropic_base_url(base_url)):
            return result

        # Rewrite the Claude-style manual thinking block to MiniMax's
        # adaptive-only shape. Drop the Claude-only ``output_config`` —
        # MiniMax's docs do not document that field and forwarding it
        # risks an HTTP 400 on a field MiniMax does not parse.
        reasoning_cfg = kwargs.get("reasoning_config")
        enabled = (
            isinstance(reasoning_cfg, dict) and reasoning_cfg.get("enabled") is not False
        )

        if enabled:
            result["thinking"] = {"type": "adaptive"}
            # ``output_config.effort`` is Claude-4.6+ only — remove it.
            result.pop("output_config", None)
            # ``temperature: 1`` is set by the legacy manual-thinking
            # branch for Claude; MiniMax accepts the documented 0..2
            # range, so we leave the user's temperature untouched if it
            # was set elsewhere. If temperature was forced to 1 by the
            # legacy branch, restore it to the model default of 1.0
            # which is MiniMax's documented default for M3 (top_p 0.95).
            if result.get("temperature") == 1 and "temperature" not in (reasoning_cfg or {}):
                result["temperature"] = 1.0  # explicit, MiniMax-safe
        else:
            # Reasoning explicitly disabled → MiniMax wants ``disabled``.
            result["thinking"] = {"type": "disabled"}

        return result

    anthropic_adapter.build_anthropic_kwargs = build_anthropic_kwargs_wrapper
    setattr(anthropic_adapter, _PATCH_FLAG, True)
    # Expose the original for tests and for future code that needs to
    # bypass the wrapper. Production code should never import this.
    globals()["_ORIGINAL_BUILD_KWARGS"] = original
    logger.info(
        "minimax-thinking-fix: patched build_anthropic_kwargs for MiniMax-M3 adaptive thinking."
    )
    return True


# ── Provider profile override ─────────────────────────────────────────

class MiniMaxThinkingFixProfile(ProviderProfile):
    """Override of the bundled minimax provider — same identity, no-op today.

    Kept as a stable registration handle so user config files referencing
    ``provider: minimax`` resolve to *this* plugin's profile (and benefit
    from any future per-request extras added here). The thinking fix
    itself happens via the monkey-patch above because the Anthropic
    transport does not invoke provider hooks.
    """

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        # The Anthropic-compatible base URL path is handled by the
        # monkey-patch on build_anthropic_kwargs (which runs *before*
        # the transport calls any provider hook). The OpenAI-compatible
        # base URL (api.minimax.io/v1) goes through the chat-completions
        # transport, where the bundled provider already emits
        # ``thinking: {"type": "adaptive"}`` for M3 correctly — nothing
        # to do here. Return empty so we don't accidentally double-set.
        return {}, {}


minimax = MiniMaxThinkingFixProfile(
    name="minimax",
    aliases=("mini-max",),
    api_mode="anthropic_messages",
    env_vars=("MINIMAX_API_KEY",),
    base_url="https://api.minimax.io/anthropic",
    auth_type="api_key",
    default_aux_model="MiniMax-M3",
)

minimax_cn = MiniMaxThinkingFixProfile(
    name="minimax-cn",
    aliases=("minimax-china", "minimax_cn"),
    api_mode="anthropic_messages",
    env_vars=("MINIMAX_CN_API_KEY",),
    base_url="https://api.minimaxi.com/anthropic",
    auth_type="api_key",
    default_aux_model="MiniMax-M3",
)


# ── Plugin entrypoint ─────────────────────────────────────────────────

# Register the override profiles (last-writer-wins over bundled).
register_provider(minimax)
register_provider(minimax_cn)

# Install the Anthropic-adapter patch. This is the actual fix; the
# register_provider() calls above are kept so the user can see *this*
# plugin in ``hermes mcp list`` / ``hermes status`` output and so
# future per-request extras land in the right hook.
if not _patch_anthropic_kwargs():
    # Patch failure is loud-but-non-fatal: the plugin still overrides the
    # profile registration, but the thinking fix itself is inert until
    # the user updates this repo to match upstream's rename.
    logger.warning(
        "minimax-thinking-fix: provider profiles registered, but "
        "anthropic-adapter patch failed — M3 thinking is unchanged."
    )