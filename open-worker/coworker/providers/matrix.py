"""The curated model matrix — the only models we actively suggest, label, and vouch for.

Keyed by the FULL routed id, exactly as the ProviderRouter receives it — including reseller
"ugly names" like ``together:zai-org/GLM-5.2`` (bare ids route to the OpenAI default). Each
entry carries the UI display label and the model's capabilities, making this the single
source of truth the capability probe and the GUI's pickers read from.

Deliberately SMALL (owner call, 2026-07-04): current-generation, agent-capable (tool-calling)
models only. It is not user-editable — users can still add any custom model string, which
falls back to the conservative heuristics in ``capabilities.py`` at their own risk of
degraded results. Ids verified against vendor/reseller catalogs on 2026-07-04; refresh the
reseller rows when catalogs rotate (they rename on every model generation).

Resellers: Together + Fireworks for now. TODO: add Groq and OpenRouter entries here AND their
descriptors in ``registry.py`` once the current provider surface is tested — deliberately
deferred to bound how much needs verifying at once.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import ModelCapabilities

_AGENTIC = ModelCapabilities(
    tools=True, vision=False, parallel_tool_calls=True, streaming=True
)
# The native three (OpenAI, Anthropic, Gemini) all take PDFs directly; every
# OpenAI-compatible vendor and reseller in the matrix does not (their chat APIs have
# no inline file part — checked 2026-07-17), so those fall back via pdf_support.py.
_AGENTIC_VISION = ModelCapabilities(
    tools=True, vision=True, pdf=True, parallel_tool_calls=True, streaming=True
)


@dataclass(frozen=True)
class ModelEntry:
    label: str  # UI display name, e.g. "GLM-5.2 · via Together"
    caps: ModelCapabilities = _AGENTIC


MATRIX: dict[str, ModelEntry] = {
    # -- Together AI (the only reseller in this downsized build) ------------------
    # Together's own model namespaces, verbatim. These are the only models this build
    # actively suggests; any other custom string still routes but falls back to the
    # conservative heuristics in capabilities.py.
    "together:thinkingmachines/Inkling": ModelEntry("Inkling · via Together"),
    "together:zai-org/GLM-5.2": ModelEntry("GLM-5.2 · via Together"),
    # Kimi K3 (2026-07-16) is not on Together yet — weights land ~07-27; revisit then.
    "together:moonshotai/Kimi-K2.7-Code": ModelEntry("Kimi K2.7 Code · via Together"),
    "together:moonshotai/Kimi-K2.6": ModelEntry("Kimi K2.6 · via Together"),
    "together:deepseek-ai/DeepSeek-V4-Pro": ModelEntry(
        "DeepSeek V4 Pro · via Together"
    ),
    "together:meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8": ModelEntry(
        "Llama 4 Maverick · via Together"
    ),
}


def entry_for(model: str) -> ModelEntry | None:
    return MATRIX.get(model)


def model_labels() -> dict[str, str]:
    """Full-id → display-label map, shipped to the GUI so every picker shows human names."""
    return {mid: e.label for mid, e in MATRIX.items()}


def models_for_provider(provider: str) -> list[str]:
    """BARE model ids (prefix stripped) the matrix curates for a provider — feeds the
    Settings pane's suggestions and the composer picker so both stay in lockstep with the
    matrix. OpenAI entries are stored without a prefix (bare ids route to the OpenAI
    default), so its list is every un-prefixed id."""
    if provider == "openai":
        return [mid for mid in MATRIX if ":" not in mid]
    prefix = provider + ":"
    return [mid[len(prefix) :] for mid in MATRIX if mid.startswith(prefix)]
