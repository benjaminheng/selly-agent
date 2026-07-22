"""The stream-json parser maps each line to the common event schema, truncating and failing soft."""

from __future__ import annotations

import json

from selly_agent.pass_stream import is_cap_hit, parse_stream_line


def test_system_init_becomes_pass_init() -> None:
    line = json.dumps(
        {"type": "system", "subtype": "init", "session_id": "s1", "tools": ["get_item"]}
    )
    assert parse_stream_line(line) == [("pass.init", {"session_id": "s1", "tools": ["get_item"]})]


def test_assistant_text_and_tool_use() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "publishing now"},
                    {"type": "tool_use", "name": "carousell_ai_publish_listing", "input": {}},
                ]
            },
        }
    )
    events = parse_stream_line(line)
    assert events[0] == ("pass.message", {"text": "publishing now"})
    assert events[1] == ("pass.tool_use", {"name": "carousell_ai_publish_listing"})


def test_user_message_becomes_tool_result() -> None:
    line = json.dumps(
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}
    )
    ((kind, payload),) = parse_stream_line(line)
    assert kind == "pass.tool_result"
    assert "tool_result" in payload["content"]


def test_result_carries_usage_and_flags() -> None:
    line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 3,
            "session_id": "s1",
            "usage": {"input_tokens": 5},
        }
    )
    ((kind, payload),) = parse_stream_line(line)
    assert kind == "pass.result"
    assert payload["is_error"] is False and payload["num_turns"] == 3
    assert payload["usage"] == {"input_tokens": 5}


def test_garbage_line_becomes_pass_raw_not_a_crash() -> None:
    assert parse_stream_line("not json at all")[0][0] == "pass.raw"
    assert parse_stream_line("[1,2,3]")[0][0] == "pass.raw"  # valid json, wrong shape
    assert parse_stream_line("   ") == []


def test_text_is_truncated_to_cap() -> None:
    big = "x" * 5000
    line = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": big}]}}
    )
    ((kind, payload),) = parse_stream_line(line)
    assert kind == "pass.message"
    assert len(payload["text"]) < 5000 and payload["text"].endswith("chars)")


def test_is_cap_hit_reads_result_subtype() -> None:
    assert is_cap_hit({"subtype": "error_max_turns"}) is True
    assert is_cap_hit({"subtype": "success"}) is False
    assert is_cap_hit(None) is False
