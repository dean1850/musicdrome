"""Models are asked for JSON. What comes back is not always only JSON."""

import pytest

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
