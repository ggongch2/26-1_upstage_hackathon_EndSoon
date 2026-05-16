"""Thin wrapper around Solar Chat Completions (OpenAI-compatible).

Supports a pool of API keys (comma-separated in UPSTAGE_API_KEY) that
are rotated round-robin per chat() call. This spreads the per-key rate
limit across multiple keys and is the simplest way to survive demo
spikes without queueing on the API side.
"""
from __future__ import annotations

import os
import threading
from typing import Any

import httpx


def parse_keys(raw: str) -> list[str]:
    """Split UPSTAGE_API_KEY into a list. Accepts comma or whitespace
    separation so users can paste keys on multiple lines if they prefer."""
    keys: list[str] = []
    for piece in raw.replace("\n", ",").split(","):
        k = piece.strip()
        if k:
            keys.append(k)
    return keys


class SolarClient:
    def __init__(
        self,
        api_key: str | list[str],
        base_url: str = "https://api.upstage.ai/v1",
        model: str = "solar-pro2",
        timeout: float = 300.0,
    ) -> None:
        if isinstance(api_key, str):
            self._keys = parse_keys(api_key)
        else:
            self._keys = [k.strip() for k in api_key if k and k.strip()]
        if not self._keys:
            raise RuntimeError("at least one API key required")
        self._key_idx = 0
        self._key_lock = threading.Lock()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    @property
    def api_key(self) -> str:
        """Backwards-compat — returns the first configured key."""
        return self._keys[0]

    @property
    def key_count(self) -> int:
        return len(self._keys)

    def _next_key(self) -> str:
        with self._key_lock:
            key = self._keys[self._key_idx]
            self._key_idx = (self._key_idx + 1) % len(self._keys)
            return key

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._next_key()}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format
        if max_tokens:
            payload["max_tokens"] = max_tokens

        with httpx.Client(timeout=timeout or self.timeout) as client:
            resp = client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"Solar chat failed {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        return data["choices"][0]["message"]["content"]


def from_env() -> SolarClient:
    raw = os.environ.get("UPSTAGE_API_KEY", "")
    keys = parse_keys(raw)
    if not keys:
        raise RuntimeError("UPSTAGE_API_KEY not set")
    base_url = os.environ.get("UPSTAGE_BASE_URL", "https://api.upstage.ai/v1")
    model = os.environ.get("SOLAR_MODEL", "solar-pro2")
    try:
        timeout = float(os.environ.get("SOLAR_TIMEOUT", "300"))
    except ValueError:
        timeout = 300.0
    return SolarClient(api_key=keys, base_url=base_url, model=model, timeout=timeout)
