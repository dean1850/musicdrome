"""The AI backend: Anthropic, OpenAI-compatible, or Ollama.

One interface over three HTTP APIs, spoken directly with httpx — no vendor SDKs
to pin or upgrade. Every caller wants the same thing, a JSON document matching a
shape, so :func:`complete_json` is the only entry point that matters.

Models are asked for JSON and mostly comply, but "mostly" is not a contract:
they wrap it in a code fence, or open with "Here are your recommendations:".
:func:`extract_json` tries progressively looser strategies before giving up, so
one chatty response does not throw away a whole scan.

**On asking Ollama for a shape.** ``"format": "json"`` means "emit valid JSON"
and nothing more, which is the weakest possible request: a small local model
told only that will happily answer with an object keyed by
``"Artist — Title"``, or invent ``popularity`` and ``image_url`` fields, and
neither is what the caller asked for. Ollama also accepts a JSON *schema* in
the same field, which it compiles into a grammar the sampler cannot leave — so
the schema is sent there rather than merely described in the prompt. Older
Ollama builds reject a non-string ``format``; that is caught and retried the
plain way.

**On context windows.** Ollama does not size its context to the request. It
uses the server default — commonly 4096 tokens — and silently drops whatever
does not fit, so a scan that sends a long exclusion list and asks for forty
recommendations gets a reply that stops mid-token. That reads exactly like a
model that cannot follow instructions and is really a window too small for the
question, which is why :func:`_context_window` sizes it per request instead of
pinning a number.
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

    fenced = _FENCE.search(text)
    primary = fenced.group(1).strip() if fenced else text.strip()

    def parse(candidate: str) -> tuple[bool, Any]:
        try:
            return True, json.loads(candidate)
        except ValueError:
            return False, None

    # Where the document begins. Anything before it is preamble, and a brace or
    # bracket span that starts later than this is a *piece* of the document
    # rather than the document — which matters when the answer was cut off: a
    # truncated array of forty objects still contains a perfectly parseable
    # first object, and returning that alone would silently lose the other 39.
    openings = [index for index in (primary.find("["), primary.find("{")) if index != -1]
    start = min(openings) if openings else -1

    whole: list[str] = []
    fragments: list[str] = []
    for opener, closer in (("[", "]"), ("{", "}")):
        open_at, close_at = primary.find(opener), primary.rfind(closer)
        if open_at == -1 or close_at <= open_at:
            continue
        span = primary[open_at : close_at + 1]
        (whole if open_at == start else fragments).append(span)

    # Widest first: a response wrapping an object that happens to contain an
    # empty array would otherwise parse as that array and lose everything else.
    whole.sort(key=len, reverse=True)
    fragments.sort(key=len, reverse=True)

    for candidate in (primary, *whole):
        ok, value = parse(candidate)
        if ok:
            return value

    # Nothing parses whole. Before falling back to a fragment, try the document
    # as one that was cut off — a model that hit its token limit mid-answer
    # leaves forty good recommendations and a forty-first that stops in the
    # middle of a word. Throwing the scan away over the broken tail is the
    # wrong trade, and so is keeping only the first entry.
    tail = primary[start:] if start > 0 else ""

    def salvage(candidates: tuple[str, ...]) -> tuple[bool, Any]:
        for candidate in candidates:
            for repaired in _repairs(candidate):
                ok, value = parse(repaired)
                if not ok:
                    continue
                log.warning(
                    "the model's response was cut off after %d of %d characters — "
                    "recovering the part that was complete. Raise AI_MAX_TOKENS, or "
                    "lower the batch size in Settings, if this keeps happening.",
                    len(repaired), len(candidate),
                )
                return True, value
        return False, None

    ok, value = salvage((primary, tail))
    if ok:
        return value

    # Only now the fragments, whole ones before salvaged ones.
    for candidate in fragments:
        ok, value = parse(candidate)
        if ok:
            return value

    ok, value = salvage(tuple(fragments))
    if ok:
        return value

    raise AIError(f"could not parse JSON from the model response: {text[:300]}")


def _repairs(text: str) -> list[str]:
    """Closable prefixes of a truncated JSON document, best first.

    Walks the text once, tracking string state and open containers, and marks
    two kinds of place it would be safe to stop:

    * after the last nested value that *finished* — the end of the last
      complete recommendation, which is what a batch cut off partway through
      leaves behind;
    * after the last comma inside a container — the end of the last complete
      field, for when the cut landed inside the very first entry and there is
      no finished element to fall back to.

    Closing whatever is still open at either point yields a valid document.
    The first is preferred because everything it keeps is whole; the second
    recovers a partial entry, which :func:`app.scan._store` is free to discard
    if what survived is not enough to identify a track.

    Empty when there is nothing to salvage, or when the brackets do not match —
    which means this was never a truncated document in the first place.
    """
    stack: list[str] = []
    in_string = escape = False
    closed: str | None = None
    pair: str | None = None

    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char in "[{":
            stack.append("]" if char == "[" else "}")
        elif char in "]}":
            if not stack or stack[-1] != char:
                return []
            stack.pop()
            # Still inside something, so the containers left open can simply be
            # closed — everything up to here is a finished value.
            if stack:
                closed = text[: index + 1] + "".join(reversed(stack))
        elif char == "," and stack:
            # A comma outside a string always follows a complete value, so
            # everything before it can stand on its own.
            pair = text[:index] + "".join(reversed(stack))

    return [repair for repair in (closed, pair) if repair]


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


def _tokens(text: str) -> int:
    """A deliberate over-estimate of what ``text`` costs in tokens.

    English averages about four characters a token; three is used here because
    the consequence of guessing low is a truncated answer and the consequence
    of guessing high is some unused context.
    """
    return len(text) // 3 + 1


def _context_window(text: str, output_tokens: int) -> tuple[int, int]:
    """``(num_ctx, num_predict)`` for one Ollama request.

    Sized to the request rather than pinned, because the right window depends
    on what is being asked: a forty-track scan carrying three hundred excluded
    titles needs several times what a taste summary does, and a window that
    fits neither is how a scan ends up parsing a reply that stops mid-token.

    Rounded to whole 2048s so that scans of similar size land on the same
    number — Ollama reloads the model whenever ``num_ctx`` changes, and a value
    that drifted by a few tokens per scan would pay that cost every time.

    ``OLLAMA_NUM_CTX`` pins it outright for anyone whose GPU has the last word;
    ``OLLAMA_MAX_NUM_CTX`` is the ceiling the automatic sizing may not cross,
    since the KV cache for an 8B model costs roughly 128 KB per token.
    """
    reserved = _tokens(text) + 512  # 512 for the chat template and the answer's scaffolding

    if config.OLLAMA_NUM_CTX > 0:
        num_ctx = config.OLLAMA_NUM_CTX
    else:
        ceiling = max(2048, config.OLLAMA_MAX_NUM_CTX)
        wanted = reserved + output_tokens
        num_ctx = max(4096, min(-(-wanted // 2048) * 2048, ceiling))

    # What is left after the prompt is what the answer may use. Asking for more
    # than the window holds does not produce more, it produces a prompt Ollama
    # has quietly cut the front off.
    num_predict = min(output_tokens, num_ctx - reserved)
    if num_predict < output_tokens:
        log.warning(
            "the prompt leaves room for only %d of the %d tokens this answer needs "
            "in a %d-token context — expect a truncated reply. Raise "
            "OLLAMA_MAX_NUM_CTX (or OLLAMA_NUM_CTX), or lower the batch size.",
            max(num_predict, 0), output_tokens, num_ctx,
        )
    return num_ctx, max(num_predict, 256)


def _ollama(
    system: str,
    prompt: str,
    *,
    json_mode: bool = True,
    schema: dict | None = None,
    max_output_tokens: int | None = None,
) -> str:
    output_tokens = min(max_output_tokens or config.AI_MAX_TOKENS, config.AI_MAX_TOKENS)
    num_ctx, num_predict = _context_window(f"{system}\n{prompt}", output_tokens)

    payload: dict[str, Any] = {
        "model": config.OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "options": {"num_predict": num_predict, "num_ctx": num_ctx},
    }
    if json_mode:
        # The schema, when there is one. Ollama compiles it into a grammar the
        # sampler cannot leave, which is the difference between "some JSON" and
        # the JSON that was asked for.
        payload["format"] = schema or "json"

    try:
        data = _post(
            f"{config.OLLAMA_BASE_URL.rstrip('/')}/api/chat",
            headers={"Content-Type": "application/json"},
            payload=payload,
        )
    except AIError:
        # Ollama before 0.5 only accepts the string "json" here, and a schema
        # it cannot compile is refused outright. Neither is a reason to lose
        # the scan: ask the weaker way and let the parser do more work.
        if not schema:
            raise
        log.info("ollama rejected the response schema — retrying without it")
        return _ollama(system, prompt, json_mode=json_mode, max_output_tokens=max_output_tokens)

    if data.get("done_reason") == "length":
        log.warning(
            "ollama stopped at the %d-token limit — the reply is cut off. Raise "
            "AI_MAX_TOKENS or lower the batch size in Settings.",
            num_predict,
        )
    return (data.get("message") or {}).get("content", "") or ""


def complete(
    system: str,
    prompt: str,
    *,
    json_mode: bool = False,
    schema: dict | None = None,
    max_output_tokens: int | None = None,
) -> str:
    """Raw text from the configured provider.

    ``schema`` is only enforced by Ollama; the hosted providers are given it in
    the prompt by :func:`complete_json` and are reliable enough with that.
    ``max_output_tokens`` is what the caller expects the answer to need, which
    is what Ollama's context is sized from.
    """
    name = provider()
    if name == "anthropic":
        return _anthropic(system, prompt)
    if name == "openai":
        return _openai(system, prompt, json_mode=json_mode)
    return _ollama(
        system, prompt, json_mode=json_mode, schema=schema, max_output_tokens=max_output_tokens
    )


def complete_json(
    system: str,
    prompt: str,
    *,
    schema: dict | None = None,
    max_output_tokens: int | None = None,
) -> Any:
    """A parsed JSON document from the configured provider.

    ``schema`` is both described in the system prompt and, where the backend
    can enforce it, handed to the backend: Anthropic and OpenAI follow a
    described schema reliably, and a local 8B model does not — it needs the
    grammar.
    """
    instruction = (
        "Respond with a single valid JSON document and nothing else — "
        "no prose before or after it, no markdown code fences."
    )
    if schema:
        instruction += f"\n\nIt must match this JSON schema:\n{json.dumps(schema, indent=2)}"

    text = complete(
        f"{system}\n\n{instruction}",
        prompt,
        json_mode=True,
        schema=schema,
        max_output_tokens=max_output_tokens,
    )
    return extract_json(text)
