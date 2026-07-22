"""send_message — the queue-and-catchup shape without a channel yet.

Publishes a message.out event and acks {queued: true}. The Telegram workstream replaces the
sink (a bound channel, or the needs-me queue when unbound), not this tool.
"""

from __future__ import annotations

from .registry import TIER_ATTENDED, TIER_PASS_PUBLISH, ToolContext, ToolSpec, register


def _send_message(ctx: ToolContext, params: dict) -> dict:
    payload = {"text": params["text"]}
    if "ref" in params:
        payload["ref"] = params["ref"]
    ctx.bus.publish("message.out", payload, pass_id=ctx.session.pass_id)
    return {"queued": True}


register(
    ToolSpec(
        name="send_message",
        description="Queue a message to the seller's bound channel (a no-op sink until a "
        "channel is connected).",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "ref": {"type": "string"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=_send_message,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_PUBLISH}),
    )
)
