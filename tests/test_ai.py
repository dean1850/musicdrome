"""Models are asked for JSON. What comes back is not always only JSON."""

import json

import pytest

from app import ai, config
from app.ai import AIError, extract_json


def test_plain_json_object():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_json_array():
    assert extract_json('[{"artist": "A"}]') == [{"artist": "A"}]


def test_fenced_json_is_unwrapped():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json("```\n[1, 2]\n```") == [1, 2]


def test_prose_around_the_document_is_discarded():
    text = 'Here are your recommendations:\n{"recommendations": []}\nHope that helps!'
    assert extract_json(text) == {"recommendations": []}


def test_an_empty_response_is_an_error():
    with pytest.raises(AIError):
        extract_json("   ")


def test_unparseable_output_is_an_error():
    with pytest.raises(AIError):
        extract_json("I am afraid I cannot do that.")


# ─── Truncated answers ─────────────────────────────────────────────────────


def test_a_truncated_array_keeps_the_complete_entries():
    """A model that runs out of tokens mid-answer leaves everything before the
    cut intact. Losing forty recommendations over the forty-first is the wrong
    trade, so the complete prefix is recovered."""
    text = '[{"artist": "A", "title": "One"}, {"artist": "B", "title": "Tw'
    assert extract_json(text) == [{"artist": "A", "title": "One"}]


def test_a_truncated_wrapped_array_keeps_the_complete_entries():
    text = (
        '{"recommendations": [{"artist": "A", "title": "One"}, '
        '{"artist": "B", "popularity": "0.00000000000000000'
    )
    assert extract_json(text) == {"recommendations": [{"artist": "A", "title": "One"}]}


def test_a_cut_inside_the_first_entry_keeps_the_fields_that_completed():
    """The failure from the field report: the model rambled into a made-up
    "popularity" field until it ran out of tokens, so no entry ever closed.
    The fields it did finish are still a usable recommendation."""
    text = (
        '{"Gareth Emery — Laserface 01 (Aperture)": {"artist": "Gareth Emery", '
        '"name": "Laserface 01 (Aperture)", "popularity": "0.00000000000000'
    )
    assert extract_json(text) == {
        "Gareth Emery — Laserface 01 (Aperture)": {
            "artist": "Gareth Emery",
            "name": "Laserface 01 (Aperture)",
        }
    }


def test_an_answer_with_nothing_complete_is_still_an_error():
    """Cut before the first field ever finished — there is nothing in there."""
    with pytest.raises(AIError):
        extract_json('{"recommendations": [{"artist": "Gareth Eme')


def test_a_cut_inside_a_string_containing_a_brace_is_not_mistaken_for_structure():
    text = '[{"reason": "you play {loud} music", "title": "One"}, {"reason": "beca'
    assert extract_json(text) == [{"reason": "you play {loud} music", "title": "One"}]


def test_an_escaped_quote_does_not_end_the_string():
    text = '[{"title": "She said \\"go\\"", "artist": "A"}, {"title": "Tw'
    assert extract_json(text) == [{"title": 'She said "go"', "artist": "A"}]


# ─── Ollama ────────────────────────────────────────────────────────────────


@pytest.fixture
def ollama(monkeypatch):
    """Capture the payload Ollama would have been sent."""
    sent = {}

    def fake_post(url, *, headers, payload):
        sent.update(url=url, payload=payload)
        return {"message": {"content": '{"ok": true}'}, "done_reason": "stop"}

    monkeypatch.setattr(ai, "_post", fake_post)
    return sent


def test_the_schema_is_sent_as_the_response_format(ollama):
    """"format": "json" only asks for valid JSON. The schema is what makes the
    model answer with the shape the caller actually needs."""
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    ai._ollama("system", "prompt", schema=schema)
    assert ollama["payload"]["format"] == schema


def test_without_a_schema_the_plain_json_mode_is_used(ollama):
    ai._ollama("system", "prompt")
    assert ollama["payload"]["format"] == "json"


def test_json_mode_off_sends_no_format_at_all(ollama):
    ai._ollama("system", "prompt", json_mode=False, schema={"type": "object"})
    assert "format" not in ollama["payload"]


def test_an_ollama_that_rejects_the_schema_is_retried_without_it(monkeypatch):
    """Ollama before 0.5 only accepts the string "json" here. Losing structured
    output is a fair price; losing the scan is not."""
    attempts = []

    def fake_post(url, *, headers, payload):
        attempts.append(payload.get("format"))
        if payload.get("format") != "json":
            raise AIError("ollama returned HTTP 400: invalid format")
        return {"message": {"content": "[]"}}

    monkeypatch.setattr(ai, "_post", fake_post)
    assert ai._ollama("system", "prompt", schema={"type": "object"}) == "[]"
    assert attempts == [{"type": "object"}, "json"]


def test_a_failure_without_a_schema_is_not_retried(monkeypatch):
    def fake_post(url, *, headers, payload):
        raise AIError("network error talking to ollama")

    monkeypatch.setattr(ai, "_post", fake_post)
    with pytest.raises(AIError):
        ai._ollama("system", "prompt")


def test_the_context_window_is_sized_to_the_request(ollama, monkeypatch):
    """Ollama's own default is commonly 4096 tokens and it drops the overflow
    silently, which is what produces a reply that stops mid-token."""
    monkeypatch.setattr(config, "OLLAMA_NUM_CTX", 0)
    monkeypatch.setattr(config, "OLLAMA_MAX_NUM_CTX", 16384)

    ai._ollama("system", "x" * 30_000, max_output_tokens=4800)
    options = ollama["payload"]["options"]

    # ~10k tokens of prompt plus 4800 of answer, rounded up to whole 2048s.
    assert options["num_ctx"] == 16384
    assert options["num_predict"] == 4800


def test_a_small_request_does_not_reserve_a_large_window(ollama, monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_NUM_CTX", 0)
    ai._ollama("system", "a short prompt", max_output_tokens=400)
    assert ollama["payload"]["options"]["num_ctx"] == 4096


def test_the_window_can_be_pinned(ollama, monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_NUM_CTX", 8192)
    ai._ollama("system", "a short prompt", max_output_tokens=400)
    assert ollama["payload"]["options"]["num_ctx"] == 8192


def test_the_answer_is_never_asked_to_exceed_the_window(ollama, monkeypatch):
    """num_predict above what the window has left does not produce a longer
    answer — it produces a prompt Ollama has quietly cut the front off."""
    monkeypatch.setattr(config, "OLLAMA_NUM_CTX", 4096)
    ai._ollama("system", "x" * 9_000, max_output_tokens=8192)
    options = ollama["payload"]["options"]
    assert options["num_predict"] < options["num_ctx"]


def test_the_ceiling_is_respected(ollama, monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_NUM_CTX", 0)
    monkeypatch.setattr(config, "OLLAMA_MAX_NUM_CTX", 8192)
    ai._ollama("system", "x" * 60_000, max_output_tokens=8192)
    assert ollama["payload"]["options"]["num_ctx"] == 8192


def test_complete_json_hands_the_schema_to_the_backend_and_the_prompt(ollama):
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    assert ai.complete_json("system", "prompt", schema=schema) == {"ok": True}
    assert ollama["payload"]["format"] == schema
    system = ollama["payload"]["messages"][0]["content"]
    assert json.dumps(schema, indent=2) in system
