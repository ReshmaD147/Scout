import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

from scout.agents.evidence import EvidenceEntry, record_tool_call
from scout.agents.result_normalization import (
    evidence_entries_from_tool_messages,
    extract_product_candidates,
    extract_tool_result_candidates,
    normalize_agent_result,
)
from scout.agents.supervisor import _run_single_intent


def ai(name, content):
    return SimpleNamespace(type="ai", name=name, content=content, tool_calls=None)


def human(content):
    return SimpleNamespace(type="human", name=None, content=content)


def tool(name, content, artifact=None):
    msg = SimpleNamespace(type="tool", name=name, content=content)
    if artifact is not None:
        msg.artifact = artifact
    return msg


def evidence(tool_name="recommend_products", facts=None, success=True, error_code=None):
    return EvidenceEntry(
        sequence=0,
        sub_intent_id="sub_1",
        attempt_number=0,
        agent_name="recommend_agent",
        tool_name=tool_name,
        validated_args={},
        entity_type="product",
        entity_id="P001",
        normalized_facts=facts or {"items": [{"product_id": "P001", "name": "Dress", "price": 79.99}]},
        success=success,
        error_code=error_code,
    )


def test_direct_result_with_only_new_messages_is_not_lost_by_prefix_slicing():
    messages = [tool("recommend_products", json.dumps([{"product_id": "P001", "name": "Dress", "price": 79.99}])), ai("recommend_agent", "Dress is $79.99.")]

    normalized = normalize_agent_result(result={"messages": messages}, input_message_count=5, execution_mode="direct")

    assert normalized.messages == messages
    assert normalized.final_text == "Dress is $79.99."
    assert normalized.tool_result_candidates == [{"product_id": "P001", "name": "Dress", "price": 79.99, "source": "internal"}]


def test_direct_result_nested_under_agent_key_is_discovered():
    messages = [tool("recommend_products", json.dumps([{"product_id": "P001", "name": "Dress", "price": 79.99}])), ai("recommend_agent", "Dress is $79.99.")]

    normalized = normalize_agent_result(
        result={"recommend_agent": {"messages": messages}},
        input_message_count=1,
        execution_mode="direct",
    )

    assert normalized.messages == messages
    assert normalized.tool_result_candidates[0]["product_id"] == "P001"


def test_supervisor_full_history_prefix_is_removed_correctly():
    prefix = [human("prior"), ai("supervisor", "old")]
    new = [tool("recommend_products", json.dumps([{"product_id": "P001", "name": "Dress", "price": 79.99}])), ai("recommend_agent", "Dress is $79.99.")]

    normalized = normalize_agent_result(result={"messages": [*prefix, *new]}, input_message_count=2, execution_mode="supervisor")

    assert normalized.messages == new
    assert normalized.final_text == "Dress is $79.99."


def test_tool_message_dict_json_string_array_nested_and_artifact_shapes():
    product = {"product_id": "P001", "name": "Dress", "price": 79.99}
    nested = [{"type": "text", "text": json.dumps([{"product_id": "P002", "name": "Skirt", "price": 49.99}])}]
    messages = [
        tool("recommend_products", {"product_id": "P000", "name": "Top", "price": 19.99}),
        tool("recommend_products", json.dumps(product)),
        tool("recommend_products", json.dumps([{"product_id": "P003", "name": "Coat", "price": 99.99}])),
        tool("recommend_products", [nested]),
        tool("recommend_products", "not json", artifact={"product_id": "P004", "name": "Bag", "price": 39.99}),
    ]

    products = extract_tool_result_candidates(messages)

    assert [p["product_id"] for p in products] == ["P000", "P001", "P003", "P002", "P004"]


def test_malformed_json_and_arbitrary_objects_fail_safely():
    products = extract_tool_result_candidates([
        tool("recommend_products", "{bad json"),
        tool("recommend_products", object()),
        tool("stock", {"product_id": "P001", "name": "Dress", "price": 79.99}),
    ])

    assert products == []


def test_original_inputs_are_not_mutated():
    messages = [tool("recommend_products", json.dumps([{"product_id": "P001", "name": "Dress", "price": 79.99}]))]
    before = deepcopy(messages[0].content)

    normalize_agent_result(result={"messages": messages}, input_message_count=0, execution_mode="direct")

    assert messages[0].content == before


def test_routing_marker_is_detected_but_not_product_data():
    normalized = normalize_agent_result(
        result={"messages": [ai("recommend_agent", "NEEDS_EXTERNAL_CHECK: red dress")]},
        input_message_count=0,
        execution_mode="direct",
    )

    assert normalized.routing_marker_present
    assert normalized.tool_result_candidates == []


def test_successful_recommend_evidence_creates_ordered_deduped_product_candidates():
    entries = [
        evidence(facts={"items": [
            {"product_id": "P001", "name": "Dress", "price": 79.99, "rating": 4.8},
            {"product_id": "P002", "name": "Skirt"},
            {"product_id": "P001", "name": "Dress", "promotion": {"name": "Sale", "discounted_price": 69.99}},
            {"name": "Missing ID", "price": 1.0},
        ]})
    ]

    products = extract_product_candidates(normalized_messages=[], evidence_entries=entries)

    assert products == [
        {"product_id": "P001", "name": "Dress", "source": "internal", "price": 79.99, "rating": 4.8, "promotion": {"name": "Sale", "discounted_price": 69.99}},
        {"product_id": "P002", "name": "Skirt", "source": "internal"},
    ]


def test_failed_denied_and_unrelated_evidence_are_ignored():
    entries = [
        evidence(success=False, error_code="tool_execution_failed"),
        evidence(success=False, error_code="repeated_tool_call"),
        evidence(tool_name="stock", facts={"product_id": "P001", "name": "Dress", "quantity": 3}),
        evidence(tool_name="retrieve_policy_chunks", facts={"statement": "Returns accepted."}),
    ]

    assert extract_product_candidates(normalized_messages=[], evidence_entries=entries) == []


def test_message_products_fill_missing_evidence_fields_without_overwriting():
    entries = [evidence(facts={"items": [{"product_id": "P001", "name": "Dress"}]})]
    messages = [tool("recommend_products", json.dumps([{"product_id": "P001", "name": "Dress", "price": 79.99, "rating": 4.5}]))]

    products = extract_product_candidates(normalized_messages=messages, evidence_entries=entries)

    assert products == [{"product_id": "P001", "name": "Dress", "source": "internal", "price": 79.99, "rating": 4.5}]


def test_tool_messages_can_create_local_normalized_evidence_for_verification():
    messages = [tool("recommend_products", json.dumps([{"product_id": "P001", "name": "Dress", "price": 79.99}]))]

    entries = evidence_entries_from_tool_messages(
        normalized_messages=messages,
        sub_intent_id="sub_local",
        attempt_number=0,
    )

    assert len(entries) == 1
    assert entries[0].tool_name == "recommend_products"
    assert entries[0].agent_name == "recommend_agent"
    assert entries[0].normalized_facts == {"items": [{"product_id": "P001", "name": "Dress", "price": 79.99, "source": "internal"}]}


def test_direct_recommendation_with_successful_tool_evidence_returns_verified_products():
    product = {"product_id": "P001", "name": "Dress", "price": 79.99}

    class App:
        async def ainvoke(self, payload, config):
            record_tool_call(
                tool_name="recommend_products",
                validated_args={"query": "dress"},
                success=True,
                result=[product],
                agent_name="recommend_agent",
            )
            return {"messages": [tool("recommend_products", json.dumps([product])), ai("recommend_agent", "Dress is $79.99.")]}

    history = []
    reply, products = asyncio.run(_run_single_intent(App(), history, "Recommend a dress under $80.", debug=False))

    assert reply == "We have the Dress for $79.99."
    assert products == [{"product_id": "P001", "name": "Dress", "source": "internal", "price": 79.99}]
    assert history[-1]["content"] == reply


def test_external_fallback_marker_does_not_reach_customer():
    normalized = normalize_agent_result(
        result={"messages": [ai("recommend_agent", "NEEDS_EXTERNAL_CHECK: red dress"), ai("external_offer_agent", "External result.")]},
        input_message_count=0,
        execution_mode="direct",
    )

    assert normalized.routing_marker_present
    assert normalized.final_text == "External result."
