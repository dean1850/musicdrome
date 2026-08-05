"""AI provider abstraction.

One interface, three backends:

* **anthropic** — Claude via the official SDK. Note that Claude models reject
  ``temperature``/``top_p``/``top_k`` (400), so sampling parameters are never
  sent on this path; depth is controlled with ``output_config.effort`` instead.
* **ollama** — a local model over the Ollama HTTP API.
* **openai** — any OpenAI-compatible endpoint (OpenAI, LM Studio, vLLM, ...).

Every backend exposes :meth:`AIProvider.complete_json`, which asks the model for
a document matching a JSON schema. Providers that support native structured
output use it; the rest fall back to prompting plus tolerant extraction.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Any

import httpx

from ...config import settings

log = logging.getLogger(__name__)


class AIError(RuntimeError):
    """Raised when a provider cannot produce a usable response."""


# ─── JSON salvage ──────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Pull a JSON document out of a model response.

    Models occasionally wrap JSON in prose or a code fence even when asked not
    to, so try progressively looser strategies before giving up.
    """
    if not text or not text.strip():
        raise AIError("model returned an empty response")

    candidates: list[str] = [text.strip()]

    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())

    # Widest brace/bracket span in the response
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start:end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            continue

    raise AIError(f"could not parse JSON from model response: {text[:300]}")


# ─── Base ──────────────────────────────────────────────────────────────────


class AIProvider(ABC):
    name: str = "base"

    @property
    @abstractmethod
    def model(self) -> str: ...

    @property
    @abstractmethod
    def available(self) -> bool:
        """Whether this provider is configured well enough to be called."""

    @abstractmethod
    def complete(self, system: str, prompt: str, *, max_tokens: int | None = None) -> str:
        """Return the model's plain-text response."""

    def complete_json(
        self,
        system: str,
        prompt: str,
        *,
        schema: dict | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        """Return a parsed JSON document. Default: prompt and salvage."""
        instruction = (
            "Respond with a single valid JSON document and nothing else — "
            "no prose, no markdown fences."
        )
        if schema:
            instruction += f"\n\nIt must match this JSON schema:\n{json.dumps(schema, indent=2)}"
        return extract_json(
            self.complete(f"{system}\n\n{instruction}", prompt, max_tokens=max_tokens)
        )


# ─── Anthropic ─────────────────────────────────────────────────────────────


class AnthropicProvider(AIProvider):
    name = "anthropic"

    def __init__(self) -> None:
        self._client = None

    @property
    def model(self) -> str:
        return settings.anthropic_model

    @property
    def available(self) -> bool:
        return bool(settings.anthropic_api_key)

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - dependency is pinned
                raise AIError("the 'anthropic' package is not installed") from exc
            if not settings.anthropic_api_key:
                raise AIError("ANTHROPIC_API_KEY is not set")
            kwargs: dict[str, Any] = {
                "api_key": settings.anthropic_api_key,
                "timeout": float(settings.ai_request_timeout),
            }
            if settings.anthropic_base_url:
                kwargs["base_url"] = settings.anthropic_base_url
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def _output_config(self, schema: dict | None) -> dict[str, Any]:
        config: dict[str, Any] = {}
        effort = (settings.anthropic_effort or "").strip().lower()
        if effort in {"low", "medium", "high", "xhigh", "max"}:
            config["effort"] = effort
        if schema:
            config["format"] = {"type": "json_schema", "schema": schema}
        return config

    def _create(self, system: str, prompt: str, max_tokens: int, schema: dict | None):
        """Call the Messages API.

        Deliberately no ``temperature``/``top_p``/``top_k`` — current Claude
        models reject them outright. ``output_config`` is passed through
        ``extra_body`` when the installed SDK is older than the field.
        """
        client = self._get_client()
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        output_config = self._output_config(schema)

        try:
            if output_config:
                return client.messages.create(output_config=output_config, **payload)
            return client.messages.create(**payload)
        except TypeError:
            # SDK predates output_config as a named parameter
            if output_config:
                return client.messages.create(
                    extra_body={"output_config": output_config}, **payload
                )
            raise

    @staticmethod
    def _text_of(response) -> str:
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise AIError(
                "Claude declined this request"
                + (f" (category: {category})" if category else "")
            )
        parts = [
            block.text
            for block in getattr(response, "content", [])
            if getattr(block, "type", None) == "text"
        ]
        return "".join(parts).strip()

    def complete(self, system: str, prompt: str, *, max_tokens: int | None = None) -> str:
        try:
            response = self._create(
                system, prompt, max_tokens or settings.ai_max_tokens, None
            )
        except AIError:
            raise
        except Exception as exc:
            raise AIError(f"Anthropic request failed: {exc}") from exc
        return self._text_of(response)

    def complete_json(
        self,
        system: str,
        prompt: str,
        *,
        schema: dict | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        if not schema:
            return super().complete_json(system, prompt, max_tokens=max_tokens)
        try:
            response = self._create(
                system, prompt, max_tokens or settings.ai_max_tokens, schema
            )
        except AIError:
            raise
        except Exception as exc:
            log.warning(
                "structured output failed (%s); retrying with prompted JSON", exc
            )
            return super().complete_json(
                system, prompt, schema=schema, max_tokens=max_tokens
            )
        return extract_json(self._text_of(response))


# ─── Ollama ────────────────────────────────────────────────────────────────


class OllamaProvider(AIProvider):
    name = "ollama"

    @property
    def model(self) -> str:
        return settings.ollama_model

    @property
    def available(self) -> bool:
        return bool(settings.ollama_base_url)

    def _chat(self, system: str, prompt: str, max_tokens: int, schema: dict | None) -> str:
        url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "options": {
                "temperature": settings.ai_temperature,
                "num_predict": max_tokens,
            },
        }
        if schema:
            payload["format"] = schema  # Ollama accepts a JSON schema here

        try:
            with httpx.Client(timeout=float(settings.ai_request_timeout)) as client:
                response = client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AIError(f"Ollama request failed: {exc}") from exc

        return (data.get("message", {}) or {}).get("content", "").strip()

    def complete(self, system: str, prompt: str, *, max_tokens: int | None = None) -> str:
        return self._chat(system, prompt, max_tokens or settings.ai_max_tokens, None)

    def complete_json(
        self,
        system: str,
        prompt: str,
        *,
        schema: dict | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        text = self._chat(
            system, prompt, max_tokens or settings.ai_max_tokens, schema
        )
        return extract_json(text)


# ─── OpenAI-compatible ─────────────────────────────────────────────────────


class OpenAIProvider(AIProvider):
    name = "openai"

    @property
    def model(self) -> str:
        return settings.openai_model

    @property
    def available(self) -> bool:
        return bool(settings.openai_api_key and settings.openai_base_url)

    def _chat(self, system: str, prompt: str, max_tokens: int, schema: dict | None) -> str:
        url = f"{settings.openai_base_url.rstrip('/')}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": settings.ai_temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        if schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "musicdrome", "schema": schema, "strict": False},
            }

        headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
        try:
            with httpx.Client(timeout=float(settings.ai_request_timeout)) as client:
                response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AIError(f"OpenAI-compatible request failed: {exc}") from exc

        choices = data.get("choices") or []
        if not choices:
            raise AIError("OpenAI-compatible endpoint returned no choices")
        return (choices[0].get("message", {}) or {}).get("content", "").strip()

    def complete(self, system: str, prompt: str, *, max_tokens: int | None = None) -> str:
        return self._chat(system, prompt, max_tokens or settings.ai_max_tokens, None)

    def complete_json(
        self,
        system: str,
        prompt: str,
        *,
        schema: dict | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        return extract_json(
            self._chat(system, prompt, max_tokens or settings.ai_max_tokens, schema)
        )


# ─── Selection ─────────────────────────────────────────────────────────────

_PROVIDERS: dict[str, type[AIProvider]] = {
    "anthropic": AnthropicProvider,
    "ollama": OllamaProvider,
    "openai": OpenAIProvider,
}


@lru_cache(maxsize=4)
def _build(name: str) -> AIProvider:
    return _PROVIDERS[name]()


def get_provider(name: str | None = None) -> AIProvider:
    """Return the configured provider. Raises :class:`AIError` if unusable."""
    if not settings.ai_enabled:
        raise AIError("AI features are disabled (AI_ENABLED=false)")

    key = (name or settings.ai_provider or "anthropic").lower()
    if key not in _PROVIDERS:
        raise AIError(f"unknown AI provider: {key}")

    provider = _build(key)
    if not provider.available:
        raise AIError(
            f"AI provider '{key}' is not configured — check the relevant "
            f"credentials in .env"
        )
    return provider


def provider_status() -> dict[str, Any]:
    """Diagnostics for the settings screen."""
    return {
        "enabled": settings.ai_enabled,
        "provider": settings.ai_provider,
        "providers": {
            name: {"available": _build(name).available, "model": _build(name).model}
            for name in _PROVIDERS
        },
    }
