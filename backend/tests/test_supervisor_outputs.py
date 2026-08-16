import asyncio
import json
from types import SimpleNamespace

from scout.agents.evidence import record_tool_call
from scout.agents.supervisor import (
    MAX_HISTORY_MESSAGES,
    _run_single_intent_streaming,
    _run_single_intent,
    _trim_history_for_model,
    extract_products,
    get_final_reply,
)


def message(name, content, msg_type="ai", tool_calls=None):
    return SimpleNamespace(name=name, content=content, type=msg_type, tool_calls=tool_calls)


def tool_message(name, content):
    return message(name=name, content=content, msg_type="tool")


def test_get_final_reply_removes_needs_external_check_signal():
    messages = [
        message("recommend_agent", "NEEDS_EXTERNAL_CHECK: red cocktail dress"),
        message("external_offer_agent", "I found a third-party option."),
    ]

    assert get_final_reply(messages) == "I found a third-party option."


def test_get_final_reply_does_not_return_handoff_marker():
    messages = [message("recommend_agent", "Transferring back to supervisor")]

    assert get_final_reply(messages) != "Transferring back to supervisor"


def test_get_final_reply_preserves_direct_supervisor_closing_reply():
    messages = [message("supervisor", "You're welcome. Have a great day!")]

    assert get_final_reply(messages) == "You're welcome. Have a great day!"


def test_extract_products_handles_normal_provider_tool_result_shape():
    product = {"product_id": "P001", "name": "Black Midi Dress", "price": 79.99}
    messages = [tool_message("search", [{"type": "text", "text": json.dumps([product])}])]

    assert extract_products(messages) == [{**product, "source": "internal"}]


def test_extract_products_handles_claude_nested_string_encoded_tool_results():
    product = {
        "external_product_id": "EX011",
        "name": "Enid Satin Body-Con Evening Dress",
        "price": 35.98,
        "source": "external",
    }
    claude_content = json.dumps([[{"type": "text", "text": json.dumps([product])}]])
    messages = [tool_message("search_external_offers", claude_content)]

    assert extract_products(messages) == [product]


def test_external_handoff_excludes_stale_internal_products():
    internal_product = {"product_id": "P001", "name": "Near Miss", "price": 79.99}
    external_product = {
        "external_product_id": "EX011",
        "name": "External Match",
        "price": 35.98,
        "vendor_name": "Partner Shop",
        "click_url": "/affiliate/click/EX011",
        "source": "external",
    }

    class FakeApp:
        async def ainvoke(self, payload, config):
            record_tool_call(
                tool_name="search_external_offers",
                validated_args={"query": "red cocktail dress"},
                success=True,
                result=[external_product],
                agent_name="external_offer_agent",
            )
            return {
                "messages": [
                    *payload["messages"],
                    tool_message("recommend_products", json.dumps([internal_product])),
                    message("recommend_agent", "NEEDS_EXTERNAL_CHECK: red cocktail dress"),
                    tool_message("search_external_offers", json.dumps([external_product])),
                    message("external_offer_agent", "I found a third-party option."),
                ]
            }

    history = []
    reply, products = asyncio.run(
        _run_single_intent(FakeApp(), history, "red cocktail dress", debug=False)
    )

    assert reply == (
        "We don’t have a matching item in our own catalog right now, but I found another option: "
        "External Match from Partner Shop for $35.98. "
        "It’s from another retailer, so price and availability may change. "
        "You’ll complete the purchase with that retailer, and our return policy won’t apply."
    )
    assert products == [external_product]


def test_trim_history_for_model_limits_model_input_to_max_history_messages():
    history = [{"role": "user", "content": str(index)} for index in range(MAX_HISTORY_MESSAGES + 3)]

    trimmed = _trim_history_for_model(history)

    assert len(trimmed) == MAX_HISTORY_MESSAGES
    assert trimmed == history[-MAX_HISTORY_MESSAGES:]


def test_trim_history_for_model_does_not_mutate_original_stored_history():
    history = [{"role": "user", "content": str(index)} for index in range(MAX_HISTORY_MESSAGES + 3)]
    original = list(history)

    _trim_history_for_model(history)

    assert history == original


def test_streaming_first_graph_invocation_uses_trimmed_history_without_truncating_stored_history():
    original_history = [
        {"role": "user", "content": f"message {index}"}
        for index in range(MAX_HISTORY_MESSAGES + 3)
    ]
    history = list(original_history)

    class FakeStreamingApp:
        def __init__(self):
            self.received_messages = None

        async def astream_events(self, payload, version, config):
            self.received_messages = payload["messages"]
            yield {
                "event": "on_chain_end",
                "name": "LangGraph",
                "metadata": {},
                "data": {
                    "output": {
                        "messages": [
                            *original_history,
                            {"role": "user", "content": "new request"},
                            message("policy_agent", "Done."),
                        ]
                    }
                },
            }

    app = FakeStreamingApp()

    events = asyncio.run(
        _collect_async(
            _run_single_intent_streaming(app, history, "new request", debug=False)
        )
    )

    assert len(app.received_messages) == MAX_HISTORY_MESSAGES
    assert app.received_messages == history[-MAX_HISTORY_MESSAGES - 1 : -1]
    assert history[: len(original_history)] == original_history
    assert len(history) == len(original_history) + 2
    assert events[-1] == ("result", ("Done.", []))


async def _collect_async(async_iterable):
    return [item async for item in async_iterable]
