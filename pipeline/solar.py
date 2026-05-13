"""Thin wrapper around Solar Chat Completions (OpenAI-compatible)."""
from __future__ import annotations

import os
from typing import Any

import httpx


class SolarClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.upstage.ai/v1",
        model: str = "solar-pro2",
        timeout: float = 120.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
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

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"Solar chat failed {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        return data["choices"][0]["message"]["content"]


def from_env() -> SolarClient:
    api_key = os.environ.get("UPSTAGE_API_KEY")
    if not api_key:
        raise RuntimeError("UPSTAGE_API_KEY not set")
    base_url = os.environ.get("UPSTAGE_BASE_URL", "https://api.upstage.ai/v1")
    model = os.environ.get("SOLAR_MODEL", "solar-pro2")
    return SolarClient(api_key=api_key, base_url=base_url, model=model)
