"""Together AI is the only model provider. It speaks the OpenAI-compatible API, so we use
the `openai` SDK pointed at Together's endpoint — bring-your-own key."""

from __future__ import annotations

import httpx
from openai import OpenAI

from . import config

TOGETHER_BASE_URL = "https://api.together.xyz/v1"


def get_client() -> OpenAI:
    key = config.load_settings().get("together_key")
    if not key:
        raise RuntimeError("No Together AI key configured — add it in Settings.")
    return OpenAI(api_key=key, base_url=TOGETHER_BASE_URL)


def get_model() -> str:
    return config.load_settings().get("model") or config.DEFAULT_MODEL


def list_models() -> list[str]:
    """Chat model ids available to the configured key — powers the Settings model picker so
    the user chooses a real (serverless-capable) model instead of typing a guess. Returns []
    if no key is set yet; raises on an API error (the endpoint turns that into a message)."""
    key = config.load_settings().get("together_key")
    if not key:
        return []
    resp = httpx.get(
        f"{TOGETHER_BASE_URL}/models",
        headers={"Authorization": f"Bearer {key}"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = data if isinstance(data, list) else data.get("data") or data.get("models") or []
    ids = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        # keep chat models when the API labels type; otherwise keep everything
        mtype = m.get("type")
        if mtype and mtype not in ("chat", "language"):
            continue
        mid = m.get("id")
        if mid:
            ids.append(mid)
    return sorted(ids)
