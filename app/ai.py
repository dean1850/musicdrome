"""The AI backend: Anthropic, OpenAI-compatible, or Ollama.

One interface over three HTTP APIs, spoken directly with httpx — no vendor SDKs
to pin or upgrade. Every caller wants the same thing, a JSON document matching a
shape, so :func:`complete_json` is the only entry point that matters.

Models are asked for JSON and mostly comply, but "mostly" is not a contract:
they wrap it in a code fence, or open with "Here are your recommendations:".
:func:`extract_json` tries progressively looser strategies before giving up, so
one chatty response does not throw away a whole scan.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from . import config, net

log = logging.getLogger(__name__)


class AIError(RuntimeError):
    pass


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Pull a JSON document out of a model response."""
    if not text or not text.strip():
        raise AIError("the model returned an empty response")

    candidates = [text.strip()]

    fenced = _FENCE.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())

    # Brace and bracket spans, for prose on either side of the document. Widest
    # first: a response wrapping an object that happens to contain an empty
    # array would otherwise parse as that array and lose everything else.
    spans = []
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            spans.append(text[start : end + 1])
    candidates.extend(sorted(spans, key=len, reverse=True))

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue

    raise AIError(f"could not parse JSON from the model response: {text[:300]}")


# ─── Providers ─────────────────────────────────────────────────────────────


def provider() -> str:
    return config.AI_PROVIDER if config.AI_PROVIDER in {"anthropic", "openai", "ollama"} else "ollama"


def model() -> str:
    return {
        "anthropic": config.ANTHROPIC_MODEL,
        "openai": config.OPENAI_MODEL,
        "ollama": config.OLLAMA_MODEL,
    }[provider()]


def available() -> bool:
    """Whether the selected provider has everything it needs to be called."""
    name = provider()
    if name == "anthropic":
        return bool(config.ANTHROPIC_API_KEY)
    if name == "openai":
        return bool(config.OPENAI_API_KEY)
    return bool(config.OLLAMA_BASE_URL)  # Ollama is unauthenticated


def status() -> dict[str, Any]:
    return {"provider": provider(), "model": model(), "available": available()}


def _post(url: str, *, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    def send() -> httpx.Response:
        with httpx.Client(timeout=config.AI_REQUEST_TIMEOUT) as client:
            return client.post(url, headers=headers, json=payload)

    # connect_only, unlike the history sources: a scan is one large POST, and a
    # request that timed out *waiting for the answer* may already have been
    # received and billed. Failures that never reached the provider — a tunnel
    # still coming up, a connection dropped before any response — cost nothing
    # to repeat, and they are what turns a whole scan into "network error
    # talking to anthropic: Server disconnected without sending a response".
    try:
        response = net.with_retry(send, what=f"{provider()} request", connect_only=True)
    except httpx.HTTPError as exc:
        raise AIError(f"network error talking to {provider()}: {exc}") from exc

    if response.status_code >= 400:
        raise AIError(f"{provider()} returned HTTP {response.status_code}: {response.text[:300]}")
    try:
        return response.json()
    except ValueError as exc:
        raise AIError(f"{provider()} returned a non-JSON body") from exc


def _anthropic(system: str, prompt: str) -> str:
    if not config.ANTHROPIC_API_KEY:
        raise AIError("ANTHROPIC_API_KEY is not set")
    data = _post(
        f"{config.ANTHROPIC_BASE_URL.rstrip('/')}/v1/messages",
        headers={
            "x-api-key": config.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        payload={
            "model": config.ANTHROPIC_MODEL,
            "max_tokens": config.AI_MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    blocks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(blocks)


def _openai(system: str, prompt: str, *, json_mode: bool = True) -> str:
    if not config.OPENAI_API_KEY:
        raise AIError("OPENAI_API_KEY is not set")
    payload: dict[str, Any] = {
        "model": config.OPENAI_MODEL,
        "max_tokens": config.AI_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    try:
        data = _post(
            f"{config.OPENAI_BASE_URL.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {config.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            payload=payload,
        )
    except AIError:
        # Self-hosted OpenAI-compatible servers often reject response_format.
        # One retry without it costs a request and saves the whole scan.
        if not json_mode:
            raise
        log.info("retrying %s without response_format", config.OPENAI_BASE_URL)
        return _openai(system, prompt, json_mode=False)

    choices = data.get("choices") or [{}]
    return (choices[0].get("message") or {}).get("content", "") or ""


def _ollama(system: str, prompt: str, *, json_mode: bool = True) -> str:
    payload: dict[str, Any] = {
        "model": config.OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "options": {"num_predict": config.AI_MAX_TOKENS},
    }
    if json_mode:
        payload["format"] = "json"

    data = _post(
        f"{config.OLLAMA_BASE_URL.rstrip('/')}/api/chat",
        headers={"Content-Type": "application/json"},
        payload=payload,
    )
    return (data.get("message") or {}).get("content", "") or ""


def complete(system: str, prompt: str, *, json_mode: bool = False) -> str:
    """Raw text from the configured provider."""
    name = provider()
    if name == "anthropic":
        return _anthropic(system, prompt)
    if name == "openai":
        return _openai(system, prompt, json_mode=json_mode)
    return _ollama(system, prompt, json_mode=json_mode)


def complete_json(system: str, prompt: str, *, schema: dict | None = None) -> Any:
    """A parsed JSON document from the configured provider.

    ``schema`` is described in the system prompt rather than enforced: only some
    of the backends here support structured output natively, and describing it
    works on all three.
    """
    instruction = (
        "Respond with a single valid JSON document and nothing else — "
        "no prose before or after it, no markdown code fences."
    )
    if schema:
        instruction += f"\n\nIt must match this JSON schema:\n{json.dumps(schema, indent=2)}"

    text = complete(f"{system}\n\n{instruction}", prompt, json_mode=True)
    return extract_json(text)
