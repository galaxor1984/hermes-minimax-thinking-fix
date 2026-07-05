"""Smoke test: prove the plugin's monkey-patch produces the right thinking shape.

Run from anywhere with the agent SDK importable:
    python tests/test_wire_format.py

Asserts:
  1. Plugin loads without raising.
  2. Plugin marks anthropic_adapter as patched.
  3. ``build_anthropic_kwargs`` for M3 + Anthropic base URL emits
     ``thinking: {"type": "adaptive"}`` and drops ``output_config``.
  4. ``build_anthropic_kwargs`` for Claude 4.7 is unchanged (passes
     through to the original adapter).
  5. ``build_anthropic_kwargs`` for M3 on the OpenAI base URL is
     unchanged (not our scope).
"""

from __future__ import annotations

import importlib.util
import sys


def _import_plugin():
    """Load the plugin via the same mechanism Hermes uses for user plugins.

    Mirrors ``providers/__init__.py::_import_plugin_dir`` so we exercise
    the exact import path that production uses.
    """
    from pathlib import Path

    sys.modules.pop("_hermes_user_provider_minimax", None)
    plugin_dir = Path("/home/omarchy/.hermes/plugins/model-providers/minimax")
    init_file = plugin_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "_hermes_user_provider_minimax",
        init_file,
        submodule_search_locations=[str(plugin_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["_hermes_user_provider_minimax"] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    plugin = _import_plugin()
    assert plugin is not None, "plugin module did not import"

    import agent.anthropic_adapter as adapter

    assert getattr(adapter, plugin._PATCH_FLAG, False) is True, (
        "patch flag was not set on agent.anthropic_adapter — the monkey-patch "
        "did not install. Check the plugin logs."
    )
    print("[OK] plugin loaded and patch installed")

    # ── Test 3: M3 on Anthropic-compatible base URL → adaptive ────
    kwargs = adapter.build_anthropic_kwargs(
        model="MiniMax-M3",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=16384,
        reasoning_config={"enabled": True, "effort": "high"},
        base_url="https://api.minimax.io/anthropic",
    )
    assert kwargs.get("thinking") == {"type": "adaptive"}, (
        f"M3/Anthropic thinking should be {{type: adaptive}}, got {kwargs.get('thinking')}"
    )
    assert "output_config" not in kwargs, (
        f"M3/Anthropic should NOT have output_config (Claude-only), got {kwargs.get('output_config')}"
    )
    print("[OK] M3 + api.minimax.io/anthropic → thinking={type: adaptive}, no output_config")

    # ── Test 4: Claude 4.7 unchanged (pass-through) ───────────────
    kwargs = adapter.build_anthropic_kwargs(
        model="claude-opus-4-7",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=16384,
        reasoning_config={"enabled": True, "effort": "high"},
        base_url=None,
    )
    assert kwargs.get("thinking", {}).get("type") == "adaptive", (
        f"Claude 4.7 should still get adaptive thinking, got {kwargs.get('thinking')}"
    )
    assert kwargs.get("output_config", {}).get("effort") == "high", (
        f"Claude 4.7 should keep output_config.effort=high, got {kwargs.get('output_config')}"
    )
    print("[OK] Claude 4.7 (native) → unchanged, adaptive + output_config.effort=high")

    # ── Test 5: M3 on a host that is NOT MiniMax → patch bails ──
    # Real-world: the OpenAI base URL is never routed through the
    # Anthropic transport (api_mode auto-detects to ``chat_completions``),
    # but we still verify that the wrapper respects the host check by
    # calling the underlying function with a hypothetical OpenAI-compat
    # base URL on the Anthropic adapter path. The wrapper must return
    # the original adapter's output verbatim.
    import agent.anthropic_adapter as _adapter_mod
    plugin_mod = sys.modules["_hermes_user_provider_minimax"]
    _original = plugin_mod._ORIGINAL_BUILD_KWARGS
    kwargs_unwrapped = _original(
        model="MiniMax-M3",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=16384,
        reasoning_config={"enabled": True, "effort": "high"},
        base_url="https://api.minimax.io/v1",
    )
    kwargs_wrapped = adapter.build_anthropic_kwargs(
        model="MiniMax-M3",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=16384,
        reasoning_config={"enabled": True, "effort": "high"},
        base_url="https://api.minimax.io/v1",
    )
    assert kwargs_wrapped == kwargs_unwrapped, (
        f"OpenAI base URL should NOT trigger the patch, but wrapped\n  {kwargs_wrapped.get('thinking')}\n"
        f"differs from original\n  {kwargs_unwrapped.get('thinking')}"
    )
    print("[OK] M3 + api.minimax.io/v1 → patch bails through, output unchanged")

    # ── Test 6: M2.7 on Anthropic base URL → unchanged ────────────
    kwargs = adapter.build_anthropic_kwargs(
        model="MiniMax-M2.7",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=16384,
        reasoning_config={"enabled": True, "effort": "high"},
        base_url="https://api.minimax.io/anthropic",
    )
    # M2.x is out of our scope — the patch only targets M3. M2.x's
    # manual-thinking shape happens to work on MiniMax's Anthropic
    # endpoint (docs note M2.x always thinks regardless of input), so
    # we don't touch it. Assert the wrapper did NOT rewrite to
    # {"type": "adaptive"} — the plugin leaves M2.x alone.
    assert kwargs.get("thinking") != {"type": "adaptive"}, (
        "M2.7 should not be patched by this plugin (M3-only scope)"
    )
    print("[OK] M2.7 → not patched (M3-only scope)")

    # ── Test 7: M3 with reasoning disabled → disabled shape ───────
    kwargs = adapter.build_anthropic_kwargs(
        model="MiniMax-M3",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=16384,
        reasoning_config={"enabled": False, "effort": "none"},
        base_url="https://api.minimax.io/anthropic",
    )
    assert kwargs.get("thinking") == {"type": "disabled"}, (
        f"M3 disabled should give {{type: disabled}}, got {kwargs.get('thinking')}"
    )
    print("[OK] M3 + reasoning disabled → thinking={type: disabled}")

    # ── Test 8: M3 with no base_url → patch bails ─────────────────
    # Defensive: if some future code path forgets to pass base_url
    # but the model is M3, we MUST NOT rewrite the thinking shape —
    # the request may be heading to a non-MiniMax endpoint that
    # legitimately wants the Claude-style shape.
    kwargs = adapter.build_anthropic_kwargs(
        model="MiniMax-M3",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=16384,
        reasoning_config={"enabled": True, "effort": "high"},
        base_url=None,
    )
    assert kwargs.get("thinking") != {"type": "adaptive"}, (
        "M3 with no base_url must NOT trigger the patch "
        "(could be headed to a non-MiniMax endpoint)"
    )
    print("[OK] M3 + no base_url → patch bails (defensive)")

    print("\nAll wire-format assertions passed.")


if __name__ == "__main__":
    main()