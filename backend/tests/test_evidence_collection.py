import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from langchain_core.tools import StructuredTool

from scout.agents import tool_guard
from scout.agents.evidence import (
    ERROR_ARGUMENT_VALIDATION_FAILED,
    ERROR_REPEATED_TOOL_CALL,
    ERROR_TOOL_CALL_LIMIT_EXCEEDED,
    ERROR_TOOL_EXECUTION_FAILED,
    clear_evidence_context,
    get_evidence_entries,
    get_tool_call_records,
    record_tool_call,
    start_evidence_context,
    normalize_tool_result,
    unwrap_tool_result_payload,
)
from scout.agents.tools_loader import MCPToolManager
from scout.agents.supervisor import _run_single_intent, _run_single_intent_streaming
from scout.agents.tool_guard import (
    SAFE_TOOL_ARGUMENT_FAILURE,
    clear_inventory_argument_constraints,
    normalize_tool_arguments,
    reset_guard,
    set_inventory_argument_constraints,
    wrap_tool_with_guard,
)
from scout.config import settings


def message(name, content, msg_type="ai", tool_calls=None):
    return SimpleNamespace(name=name, content=content, type=msg_type, tool_calls=tool_calls)


def tool_message(name, content):
    return message(name=name, content=content, msg_type="tool")


def make_tool(func, name="search"):
    return StructuredTool.from_function(func=func, name=name, description=f"{name} tool")


async def call_tool(tool, **kwargs):
    return await tool.coroutine(**kwargs)


def test_collector_contexts_do_not_share_records():
    clear_evidence_context()
    start_evidence_context("sub_a")
    record_tool_call(
        tool_name="search",
        validated_args={"query": "dress"},
        success=True,
        result=[{"product_id": "P001", "name": "Dress", "price": 79.99}],
        agent_name="recommend_agent",
    )
    first_records = get_tool_call_records()

    start_evidence_context("sub_b")
    record_tool_call(
        tool_name="orders",
        validated_args={"order_id": "O1001"},
        success=True,
        result={"order_id": "O1001", "status": "shipped"},
        agent_name="order_agent",
    )
    second_records = get_tool_call_records()
    clear_evidence_context()

    assert [record.sub_intent_id for record in first_records] == ["sub_a"]
    assert [record.sub_intent_id for record in second_records] == ["sub_b"]


def test_collector_returns_defensive_copies_and_sequences_increase():
    clear_evidence_context()
    start_evidence_context("sub_seq")
    record_tool_call(
        tool_name="search",
        validated_args={"query": "dress"},
        success=True,
        result=[{"product_id": "P001", "price": 79.99}],
        agent_name="recommend_agent",
    )
    record_tool_call(
        tool_name="stock",
        validated_args={"product_id": "P001"},
        success=True,
        result={"product_id": "P001", "quantity": 2},
        agent_name="inventory_agent",
    )

    records = get_tool_call_records()
    records[0].validated_args["query"] = "mutated"
    fresh_records = get_tool_call_records()
    clear_evidence_context()

    assert [record.sequence for record in fresh_records] == [0, 1]
    assert fresh_records[0].validated_args == {"query": "dress"}


def test_clear_evidence_context_removes_state():
    start_evidence_context("sub_clear")
    record_tool_call(
        tool_name="search",
        validated_args={},
        success=False,
        error_code=ERROR_TOOL_EXECUTION_FAILED,
        agent_name="recommend_agent",
    )
    clear_evidence_context()

    assert get_tool_call_records() == []
    assert get_evidence_entries() == []


def test_stock_evidence_retains_product_size_quantity_and_availability():
    clear_evidence_context()
    start_evidence_context("sub_stock")
    record_tool_call(
        tool_name="stock",
        validated_args={"product_id": "P001", "size": "M"},
        success=True,
        result={
            "product_id": "P001",
            "in_stock": False,
            "total_quantity": 0,
            "variants": [{"size": "M", "color": "black", "quantity": 0}],
        },
        agent_name="inventory_agent",
    )

    evidence = get_evidence_entries()
    clear_evidence_context()

    assert evidence[0].normalized_facts["product_id"] == "P001"
    assert evidence[0].normalized_facts["items"] == [
        {"size": "M", "color": "black", "quantity": 0, "product_id": "P001", "in_stock": False}
    ]


def test_unwrap_tool_result_payload_supported_envelopes_without_mutation():
    payload = {"content": [{"type": "text", "text": json.dumps({"product_id": "P001", "quantity": 2})}]}
    original = json.loads(json.dumps(payload))

    cases = [
        {"product_id": "P001"},
        json.dumps({"product_id": "P001"}),
        json.dumps([{"product_id": "P001"}]),
        {"type": "text", "text": json.dumps({"product_id": "P001"})},
        [{"type": "text", "text": json.dumps({"product_id": "P001"})}],
        {"content": [{"type": "text", "text": json.dumps({"product_id": "P001"})}]},
        {"structuredContent": {"product_id": "P001"}},
        {"structured_content": {"product_id": "P001"}},
        {"result": {"product_id": "P001"}},
        {"data": {"product_id": "P001"}},
        payload,
    ]

    for case in cases:
        assert unwrap_tool_result_payload(content=case)

    assert payload == original
    assert unwrap_tool_result_payload(content="{bad json") == []
    assert unwrap_tool_result_payload(content=object()) == []


def test_unwrap_tool_result_payload_artifact_and_depth_limit():
    class ToolMessage:
        def __init__(self):
            self.content = ""
            self.artifact = {"product_id": "P001"}

    nested = {"content": {"content": {"content": {"content": {"content": {"content": {"content": {"content": {"content": {"product_id": "too-deep"}}}}}}}}}}

    assert unwrap_tool_result_payload(content="", artifact={"product_id": "P001"}) == [{"product_id": "P001"}]
    assert {"product_id": "P001"} in unwrap_tool_result_payload(content=ToolMessage())
    assert all(payload.get("product_id") != "too-deep" for payload in unwrap_tool_result_payload(content=nested) if isinstance(payload, dict))


def test_store_evidence_retains_product_store_association_without_pickup_inference():
    clear_evidence_context()
    start_evidence_context("sub_store")
    record_tool_call(
        tool_name="stores",
        validated_args={"product_id": "P001", "store_name": "Maple Grove"},
        success=True,
        result={
            "product_id": "P001",
            "found": True,
            "stores": [{"store_id": "S01", "store_name": "Maple Grove", "quantity": 2, "in_stock": True}],
        },
        agent_name="inventory_agent",
    )

    evidence = get_evidence_entries()
    clear_evidence_context()

    assert evidence[0].normalized_facts["stores"] == [
        {"store_id": "S01", "store_name": "Maple Grove", "quantity": 2, "in_stock": True, "product_id": "P001"}
    ]
    assert "pickup_available" not in evidence[0].normalized_facts["stores"][0]


def test_unique_search_result_links_to_subsequent_stock_evidence():
    clear_evidence_context()
    start_evidence_context("sub_link")
    record_tool_call(
        tool_name="search",
        validated_args={"query": "black midi dress"},
        success=True,
        result=[{"product_id": "P001", "name": "Black Midi Dress"}],
        agent_name="inventory_agent",
    )
    record_tool_call(
        tool_name="stock",
        validated_args={"size": "M"},
        success=True,
        result={"in_stock": False, "variants": []},
        agent_name="inventory_agent",
    )

    evidence = get_evidence_entries()
    clear_evidence_context()

    stock = evidence[1].normalized_facts
    assert stock["product_id"] == "P001"
    assert stock["product_name"] == "Black Midi Dress"
    assert stock["items"] == [{"in_stock": False, "size": "M", "product_id": "P001", "product_name": "Black Midi Dress", "name": "Black Midi Dress"}]


def test_unique_search_result_links_to_subsequent_stores_evidence():
    clear_evidence_context()
    start_evidence_context("sub_store_link")
    record_tool_call(
        tool_name="search",
        validated_args={"query": "black midi dress"},
        success=True,
        result=[{"product_id": "P001", "name": "Black Midi Dress"}],
        agent_name="inventory_agent",
    )
    record_tool_call(
        tool_name="stores",
        validated_args={"store_name": "Maple Grove"},
        success=True,
        result={"stores": [{"store_id": "S01", "store_name": "Maple Grove", "quantity": 2, "in_stock": True}]},
        agent_name="inventory_agent",
    )

    evidence = get_evidence_entries()
    clear_evidence_context()

    store = evidence[1].normalized_facts["stores"][0]
    assert store["product_id"] == "P001"
    assert store["product_name"] == "Black Midi Dress"
    assert store["store_name"] == "Maple Grove"
    assert store["quantity"] == 2
    assert store["in_stock"] is True


def test_product_size_and_store_validated_args_are_retained_without_search_link():
    clear_evidence_context()
    start_evidence_context("sub_args")
    record_tool_call(
        tool_name="stock",
        validated_args={"product_id": "P123", "size": "L", "color": "black"},
        success=True,
        result={"in_stock": True, "total_quantity": 4},
        agent_name="inventory_agent",
    )
    record_tool_call(
        tool_name="stores",
        validated_args={"product_id": "P123", "store_id": "S10", "store_name": "Plymouth"},
        success=True,
        result={"stores": [{"quantity": 0, "in_stock": False}]},
        agent_name="inventory_agent",
    )

    evidence = get_evidence_entries()
    clear_evidence_context()

    assert evidence[0].normalized_facts["items"][0] == {
        "product_id": "P123",
        "size": "L",
        "color": "black",
        "in_stock": True,
        "quantity": 4,
    }
    assert evidence[1].normalized_facts["stores"][0] == {
        "quantity": 0,
        "in_stock": False,
        "product_id": "P123",
        "store_id": "S10",
        "store_name": "Plymouth",
    }


def test_wrapped_stock_rows_select_requested_size_and_availability():
    clear_evidence_context()
    start_evidence_context("sub_wrapped_stock")
    record_tool_call(
        tool_name="stock",
        validated_args={"product_id": "P001", "size": "M"},
        success=True,
        result={"content": [{"type": "text", "text": json.dumps({"variants": [{"size": "L", "quantity": 5}, {"size": "M", "quantity": 0}]})}]},
        agent_name="inventory_agent",
    )

    evidence = get_evidence_entries()
    clear_evidence_context()

    assert evidence[0].normalized_facts["items"] == [
        {"size": "M", "quantity": 0, "in_stock": False, "product_id": "P001"}
    ]


def test_wrapped_stock_boolean_availability_without_quantity_remains_valid():
    clear_evidence_context()
    start_evidence_context("sub_bool_stock")
    record_tool_call(
        tool_name="stock",
        validated_args={"product_id": "P001", "size": "M"},
        success=True,
        result=json.dumps({"size": "M", "available": True}),
        agent_name="inventory_agent",
    )

    evidence = get_evidence_entries()
    clear_evidence_context()

    assert evidence[0].normalized_facts["items"] == [
        {"in_stock": True, "product_id": "P001", "size": "M"}
    ]


def test_no_matching_size_produces_no_stock_claim_facts():
    clear_evidence_context()
    start_evidence_context("sub_no_size")
    record_tool_call(
        tool_name="stock",
        validated_args={"product_id": "P001", "size": "M"},
        success=True,
        result={"variants": [{"size": "L", "quantity": 5}]},
        agent_name="inventory_agent",
    )

    evidence = get_evidence_entries()
    clear_evidence_context()

    assert evidence[0].normalized_facts["items"] == []


def test_wrapped_store_rows_select_exact_requested_store():
    clear_evidence_context()
    start_evidence_context("sub_wrapped_store")
    record_tool_call(
        tool_name="stores",
        validated_args={"product_id": "P001", "store_name": "Maple Grove"},
        success=True,
        result={
            "structuredContent": {
                "stores": [
                    {"store_id": "S02", "store_name": "Plymouth", "quantity": 7, "in_stock": True},
                    {"store_id": "S01", "store_name": "Maple Grove", "quantity": 2},
                ]
            }
        },
        agent_name="inventory_agent",
    )

    evidence = get_evidence_entries()
    clear_evidence_context()

    assert evidence[0].normalized_facts["stores"] == [
        {"store_id": "S01", "store_name": "Maple Grove", "quantity": 2, "product_id": "P001", "in_stock": True}
    ]


def test_requested_store_name_echo_alone_is_insufficient():
    clear_evidence_context()
    start_evidence_context("sub_echo_store")
    record_tool_call(
        tool_name="stores",
        validated_args={"product_id": "P001", "store_name": "Maple Grove"},
        success=True,
        result={"requested_store_name": "Maple Grove"},
        agent_name="inventory_agent",
    )

    evidence = get_evidence_entries()
    clear_evidence_context()

    assert evidence[0].normalized_facts == {"product_id": "P001", "requested_store_name": "Maple Grove"}


def test_multiple_search_results_are_not_linked_by_guessing():
    clear_evidence_context()
    start_evidence_context("sub_ambiguous")
    record_tool_call(
        tool_name="search",
        validated_args={"query": "dress"},
        success=True,
        result=[
            {"product_id": "P001", "name": "Black Midi Dress"},
            {"product_id": "P002", "name": "Other Dress"},
        ],
        agent_name="inventory_agent",
    )
    record_tool_call(
        tool_name="stock",
        validated_args={"size": "M"},
        success=True,
        result={"in_stock": False, "variants": []},
        agent_name="inventory_agent",
    )

    evidence = get_evidence_entries()
    clear_evidence_context()

    assert "product_id" not in evidence[1].normalized_facts
    assert "product_id" not in evidence[1].normalized_facts["items"][0]


def test_linkage_is_isolated_by_sub_intent_attempt_and_context():
    clear_evidence_context()
    start_evidence_context("sub_a", attempt_number=0)
    record_tool_call(
        tool_name="search",
        validated_args={"query": "dress"},
        success=True,
        result=[{"product_id": "P001", "name": "Black Midi Dress"}],
        agent_name="inventory_agent",
    )
    start_evidence_context("sub_b", attempt_number=0)
    record_tool_call(
        tool_name="stock",
        validated_args={"size": "M"},
        success=True,
        result={"in_stock": False, "variants": []},
        agent_name="inventory_agent",
    )
    sub_b_evidence = get_evidence_entries()

    start_evidence_context("sub_c", attempt_number=0)
    record_tool_call(
        tool_name="search",
        validated_args={"query": "dress"},
        success=True,
        result=[{"product_id": "P002", "name": "Wrap Dress"}],
        agent_name="inventory_agent",
    )
    start_evidence_context("sub_c", attempt_number=1)
    record_tool_call(
        tool_name="stock",
        validated_args={"size": "M"},
        success=True,
        result={"in_stock": True, "variants": []},
        agent_name="inventory_agent",
    )
    attempt_one_evidence = get_evidence_entries()
    clear_evidence_context()

    assert "product_id" not in sub_b_evidence[0].normalized_facts
    assert "product_id" not in attempt_one_evidence[1].normalized_facts


def test_concurrent_evidence_contexts_remain_isolated():
    async def collect(sub_intent_id, product_id):
        start_evidence_context(sub_intent_id)
        record_tool_call(
            tool_name="search",
            validated_args={"query": product_id},
            success=True,
            result=[{"product_id": product_id, "name": product_id}],
            agent_name="inventory_agent",
        )
        await asyncio.sleep(0)
        record_tool_call(
            tool_name="stock",
            validated_args={"size": "M"},
            success=True,
            result={"in_stock": True, "variants": []},
            agent_name="inventory_agent",
        )
        evidence = get_evidence_entries()
        clear_evidence_context()
        return evidence[1].normalized_facts["product_id"]

    async def run_both():
        return await asyncio.gather(collect("sub_one", "P001"), collect("sub_two", "P002"))

    first, second = asyncio.run(run_both())

    assert first == "P001"
    assert second == "P002"


def test_attempt_zero_and_one_remain_distinguishable():
    clear_evidence_context()
    start_evidence_context("sub_attempt", attempt_number=0)
    record_tool_call(
        tool_name="search",
        validated_args={"query": "dress"},
        success=True,
        result=[{"product_id": "P001", "price": 79.99}],
        agent_name="recommend_agent",
    )
    start_evidence_context("sub_attempt", attempt_number=1)
    record_tool_call(
        tool_name="search",
        validated_args={"query": "dress"},
        success=True,
        result=[{"product_id": "P001", "price": 79.99}],
        agent_name="recommend_agent",
    )
    records = get_tool_call_records()
    clear_evidence_context()

    assert [record.attempt_number for record in records] == [0, 1]
    assert [record.sequence for record in records] == [0, 1]


def test_successful_guarded_tool_call_records_tool_call_and_linked_evidence():
    def search(query: str):
        return [{"product_id": "P001", "name": "Dress", "price": 79.99}]

    clear_evidence_context()
    start_evidence_context("sub_success")
    reset_guard()
    wrapped = wrap_tool_with_guard(make_tool(search), agent_name="recommend_agent")

    result = asyncio.run(call_tool(wrapped, query="dress"))
    records = get_tool_call_records()
    entries = get_evidence_entries()
    clear_evidence_context()

    assert result == [{"product_id": "P001", "name": "Dress", "price": 79.99}]
    assert len(records) == 1
    assert len(entries) == 1
    assert records[0].success is True
    assert records[0].evidence_id == entries[0].evidence_id
    assert records[0].agent_name == "recommend_agent"
    assert records[0].original_args == {"query": "dress"}
    assert records[0].validated_args == {"query": "dress"}


def test_inventory_product_id_string_argument_remains_unchanged():
    original = {"product_id": "P001", "size": "M"}

    normalized = normalize_tool_arguments(tool_name="stock", arguments=original)

    assert normalized == {"product_id": "P001", "size": "M"}
    assert original == {"product_id": "P001", "size": "M"}


def test_inventory_product_id_single_item_list_becomes_string():
    original = {"product_id": ["P001"], "store_name": "Maple Grove"}

    normalized = normalize_tool_arguments(tool_name="stores", arguments=original)

    assert normalized == {"product_id": "P001", "store_name": "Maple Grove"}
    assert original == {"product_id": ["P001"], "store_name": "Maple Grove"}


@pytest.mark.parametrize(
    "value",
    [
        [],
        ["P001", "P002"],
        [["P001"]],
        {"id": "P001"},
        1001,
    ],
)
def test_inventory_product_id_invalid_values_are_rejected(value):
    with pytest.raises(ValueError):
        normalize_tool_arguments(tool_name="stores", arguments={"product_id": value})


def test_unrelated_tool_arguments_are_not_coerced():
    original = {"product_id": ["P001"], "query": "dress"}

    normalized = normalize_tool_arguments(tool_name="search", arguments=original)

    assert normalized == original
    assert normalized is not original


def test_guard_uses_normalized_arguments_but_preserves_original_argument_evidence():
    def stores(product_id: str):
        assert isinstance(product_id, str)
        return {"product_id": product_id, "stores": []}

    clear_evidence_context()
    start_evidence_context("sub_normalize")
    reset_guard()
    wrapped = wrap_tool_with_guard(make_tool(stores, name="stores"), agent_name="inventory_agent")

    result = asyncio.run(call_tool(wrapped, product_id=["P001"]))
    records = get_tool_call_records()
    entries = get_evidence_entries()
    clear_evidence_context()

    assert result == {"product_id": "P001", "stores": []}
    assert records[0].success is True
    assert records[0].original_args == {"product_id": ["P001"]}
    assert records[0].validated_args == {"product_id": "P001"}
    assert entries[0].original_args == {"product_id": ["P001"]}
    assert entries[0].validated_args == {"product_id": "P001"}


def test_guard_returns_safe_tool_failure_for_invalid_normalized_arguments():
    def stores(product_id: str):
        return {"product_id": product_id, "stores": []}

    clear_evidence_context()
    start_evidence_context("sub_invalid_args")
    reset_guard()
    wrapped = wrap_tool_with_guard(make_tool(stores, name="stores"), agent_name="inventory_agent")

    result = asyncio.run(call_tool(wrapped, product_id=["P001", "P002"]))
    records = get_tool_call_records()
    clear_evidence_context()

    assert result == SAFE_TOOL_ARGUMENT_FAILURE
    assert records[0].success is False
    assert records[0].error_code == ERROR_ARGUMENT_VALIDATION_FAILED
    assert records[0].original_args == {"product_id": ["P001", "P002"]}
    assert records[0].validated_args == {}


def test_guard_returns_safe_tool_failure_for_schema_validation_errors():
    def stores(product_id: str):
        raise ValueError("validation error: Input should be a valid string")

    clear_evidence_context()
    start_evidence_context("sub_schema_invalid")
    reset_guard()
    wrapped = wrap_tool_with_guard(make_tool(stores, name="stores"), agent_name="inventory_agent")

    result = asyncio.run(call_tool(wrapped, product_id="P001"))
    records = get_tool_call_records()
    clear_evidence_context()

    assert result == SAFE_TOOL_ARGUMENT_FAILURE
    assert records[0].success is False
    assert records[0].error_code == ERROR_ARGUMENT_VALIDATION_FAILED
    assert "Input should be" not in records[0].model_dump_json()


def test_content_and_artifact_tuple_prefers_structured_artifact():
    result = (
        [{"type": "text", "text": json.dumps({"product_id": "text_only", "in_stock": True})}],
        {
            "product_id": "P001",
            "variants": [{"size": "M", "color": "black", "quantity": 0, "in_stock": False}],
        },
    )

    facts = normalize_tool_result("stock", result)

    assert facts["product_id"] == "P001"
    assert facts["items"] == [
        {"product_id": "P001", "size": "M", "color": "black", "quantity": 0, "in_stock": False}
    ]


def test_unknown_wrapper_object_is_ignored_safely():
    class UnknownWrapper:
        @property
        def content(self):
            raise AssertionError("unrestricted reflection should not read this")

    assert unwrap_tool_result_payload(content=UnknownWrapper()) == []


def test_stock_argument_enrichment_adds_explicit_size_and_color():
    set_inventory_argument_constraints({"size": "medium", "color": "black"})
    try:
        normalized = normalize_tool_arguments(
            tool_name="stock",
            arguments={"product_id": "P001"},
        )
    finally:
        clear_inventory_argument_constraints()

    assert normalized == {"product_id": "P001", "size": "M", "color": "black"}


def test_stock_argument_enrichment_rejects_explicit_color_conflict():
    set_inventory_argument_constraints({"size": "M", "color": "black"})
    try:
        with pytest.raises(ValueError):
            normalize_tool_arguments(
                tool_name="stock",
                arguments={"product_id": "P001", "size": "M", "color": "floral"},
            )
    finally:
        clear_inventory_argument_constraints()


def test_stock_argument_enrichment_does_not_guess_missing_color():
    set_inventory_argument_constraints({"size": "M"})
    try:
        normalized = normalize_tool_arguments(
            tool_name="stock",
            arguments={"product_id": "P001"},
        )
    finally:
        clear_inventory_argument_constraints()

    assert normalized == {"product_id": "P001", "size": "M"}


def test_actual_wrapped_stock_tool_retains_structured_inventory_fields(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_STRIPE_MCP", False)
    monkeypatch.setenv("PATH", f"{sys.executable.rsplit('/', 1)[0]}:{''}")

    async def run():
        manager = MCPToolManager()
        try:
            await manager.start()
            stock_tool = next(tool for tool in manager.tools if tool.name == "stock")
            clear_evidence_context()
            start_evidence_context("sub_actual_stock")
            reset_guard()
            set_inventory_argument_constraints({"size": "M", "color": "black"})
            wrapped = wrap_tool_with_guard(stock_tool, agent_name="inventory_agent")
            await wrapped.coroutine(product_id="P001", size="M")
            entries = get_evidence_entries()
            clear_evidence_context()
            clear_inventory_argument_constraints()
            return entries
        finally:
            await manager.stop()

    entries = asyncio.run(run())
    facts = entries[0].normalized_facts

    assert entries[0].validated_args == {"product_id": "P001", "size": "M", "color": "black"}
    assert facts["items"] == [
        {"product_id": "P001", "size": "M", "color": "black", "quantity": 0, "in_stock": False}
    ]


def test_actual_wrapped_stores_tool_retains_structured_store_fields(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_STRIPE_MCP", False)
    monkeypatch.setenv("PATH", f"{sys.executable.rsplit('/', 1)[0]}:{''}")

    async def run():
        manager = MCPToolManager()
        try:
            await manager.start()
            stores_tool = next(tool for tool in manager.tools if tool.name == "stores")
            clear_evidence_context()
            start_evidence_context("sub_actual_stores")
            reset_guard()
            set_inventory_argument_constraints({"size": "S", "color": "black"})
            try:
                wrapped = wrap_tool_with_guard(stores_tool, agent_name="inventory_agent")
                await wrapped.coroutine(product_id=["P001"], store_name="Maple Grove")
                entries = get_evidence_entries()
                records = get_tool_call_records()
                clear_evidence_context()
            finally:
                clear_inventory_argument_constraints()
            return records, entries
        finally:
            await manager.stop()

    records, entries = asyncio.run(run())
    facts = entries[0].normalized_facts

    assert records[0].original_args == {"product_id": ["P001"], "store_name": "Maple Grove"}
    assert records[0].validated_args == {"product_id": "P001", "store_name": "Maple Grove", "size": "S", "color": "black"}
    assert facts["stores"][0]["store_id"] == "S01"
    assert facts["stores"][0]["store_name"] == "Maple Grove"
    assert facts["stores"][0]["quantity"] == 2
    assert facts["stores"][0]["in_stock"] is True
    assert facts["stores"][0]["size"] == "S"
    assert facts["stores"][0]["color"] == "black"
    assert "pickup_available" not in facts["stores"][0]


def test_stores_evidence_records_exact_unavailable_variant_without_store_rows():
    clear_evidence_context()
    start_evidence_context("sub_store_variant_unavailable")
    record_tool_call(
        tool_name="stores",
        validated_args={"product_id": "P001", "size": "M", "color": "black"},
        success=True,
        result={
            "product_id": "P001",
            "found": True,
            "requested_size": "M",
            "requested_color": "black",
            "variant_available": False,
            "stores": [],
        },
        agent_name="inventory_agent",
    )
    entries = get_evidence_entries()
    clear_evidence_context()

    assert entries[0].normalized_facts["product_id"] == "P001"
    assert entries[0].normalized_facts["size"] == "M"
    assert entries[0].normalized_facts["color"] == "black"
    assert entries[0].normalized_facts["quantity"] == 0
    assert entries[0].normalized_facts["in_stock"] is False


def test_fulfillment_evidence_records_exact_unavailable_delivery_variant():
    clear_evidence_context()
    start_evidence_context("sub_delivery_variant_unavailable")
    record_tool_call(
        tool_name="fulfillment_options",
        validated_args={"product_id": "P001", "size": "M", "color": "black"},
        success=True,
        result={
            "product_id": "P001",
            "found": True,
            "requested_size": "M",
            "requested_color": "black",
            "delivery": {
                "available": False,
                "quantity": 0,
                "size": "M",
                "color": "black",
            },
        },
        agent_name="inventory_agent",
    )
    entries = get_evidence_entries()
    clear_evidence_context()

    assert entries[0].validated_args == {"product_id": "P001", "size": "M", "color": "black"}
    assert entries[0].normalized_facts["product_id"] == "P001"
    assert entries[0].normalized_facts["size"] == "M"
    assert entries[0].normalized_facts["color"] == "black"
    assert entries[0].normalized_facts["delivery_available"] is False
    assert entries[0].normalized_facts["quantity"] == 0


def test_sensitive_args_are_redacted_and_arbitrary_objects_are_omitted():
    clear_evidence_context()
    start_evidence_context("sub_redact")
    record_tool_call(
        tool_name="search",
        validated_args={
            "query": "dress",
            "api_key": "secret",
            "authorization_header": "Bearer secret",
            "object": object(),
        },
        success=True,
        result=[{"product_id": "P001", "price": 79.99, "raw": object()}],
        agent_name="recommend_agent",
    )
    records = get_tool_call_records()
    entries = get_evidence_entries()
    clear_evidence_context()

    assert records[0].validated_args == {
        "query": "dress",
        "api_key": "[REDACTED]",
        "authorization_header": "[REDACTED]",
    }
    assert entries[0].normalized_facts == {"items": [{"product_id": "P001", "price": 79.99}]}


def test_raw_exception_messages_are_not_recorded_and_exception_behavior_is_unchanged():
    def failing_tool(query: str):
        raise ValueError("secret internal failure")

    clear_evidence_context()
    start_evidence_context("sub_failure")
    reset_guard()
    wrapped = wrap_tool_with_guard(make_tool(failing_tool), agent_name="recommend_agent")

    with pytest.raises(ValueError, match="secret internal failure"):
        asyncio.run(call_tool(wrapped, query="dress"))
    records = get_tool_call_records()
    clear_evidence_context()

    assert len(records) == 1
    assert records[0].success is False
    assert records[0].error_code == ERROR_TOOL_EXECUTION_FAILED
    assert "secret internal failure" not in records[0].model_dump_json()


def test_evidence_recording_failure_does_not_break_successful_tool_call(monkeypatch):
    def search(query: str):
        return [{"product_id": "P001", "price": 79.99}]

    def broken_record_tool_call(**kwargs):
        raise RuntimeError("recording failed")

    clear_evidence_context()
    start_evidence_context("sub_recording_failure")
    reset_guard()
    wrapped = wrap_tool_with_guard(make_tool(search), agent_name="recommend_agent")
    monkeypatch.setattr(tool_guard, "record_tool_call", broken_record_tool_call)

    result = asyncio.run(call_tool(wrapped, query="dress"))
    clear_evidence_context()

    assert result == [{"product_id": "P001", "price": 79.99}]


def test_repeated_call_denial_is_recorded_and_guard_behavior_unchanged():
    def search(query: str):
        return [{"product_id": "P001", "price": 79.99}]

    clear_evidence_context()
    start_evidence_context("sub_repeated")
    reset_guard(max_total_calls=10, max_identical_calls=1)
    wrapped = wrap_tool_with_guard(make_tool(search), agent_name="recommend_agent")

    first = asyncio.run(call_tool(wrapped, query="dress"))
    second = asyncio.run(call_tool(wrapped, query="dress"))
    records = get_tool_call_records()
    clear_evidence_context()

    assert first == [{"product_id": "P001", "price": 79.99}]
    assert "already made this turn" in second
    assert records[-1].success is False
    assert records[-1].error_code == ERROR_REPEATED_TOOL_CALL


def test_total_call_limit_denial_is_recorded_and_guard_behavior_unchanged():
    def search(query: str):
        return [{"product_id": "P001", "price": 79.99}]

    clear_evidence_context()
    start_evidence_context("sub_limit")
    reset_guard(max_total_calls=0, max_identical_calls=1)
    wrapped = wrap_tool_with_guard(make_tool(search), agent_name="recommend_agent")

    result = asyncio.run(call_tool(wrapped, query="dress"))
    records = get_tool_call_records()
    clear_evidence_context()

    assert "Tool call limit reached" in result
    assert records[0].success is False
    assert records[0].error_code == ERROR_TOOL_CALL_LIMIT_EXCEEDED


def test_contextvars_keep_concurrent_evidence_isolated():
    async def run_one(sub_intent_id, product_id):
        start_evidence_context(sub_intent_id)
        record_tool_call(
            tool_name="search",
            validated_args={"query": product_id},
            success=True,
            result=[{"product_id": product_id, "price": 10.0}],
            agent_name="recommend_agent",
        )
        await asyncio.sleep(0)
        records = get_tool_call_records()
        entries = get_evidence_entries()
        clear_evidence_context()
        return records, entries

    async def run_both():
        return await asyncio.gather(
            run_one("sub_one", "P001"),
            run_one("sub_two", "P002"),
        )

    first, second = asyncio.run(run_both())

    assert first[0][0].sub_intent_id == "sub_one"
    assert first[1][0].entity_id == "P001"
    assert second[0][0].sub_intent_id == "sub_two"
    assert second[1][0].entity_id == "P002"


def test_non_streaming_lifecycle_records_attempts_and_does_not_append_evidence_to_history(monkeypatch):
    snapshots = []

    class FakeApp:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, payload, config):
            self.calls += 1
            if self.calls == 1:
                record_tool_call(
                    tool_name="search",
                    validated_args={"query": "dress"},
                    success=True,
                    result=[{"product_id": "P001", "price": 79.99}],
                    agent_name="recommend_agent",
                )
                return {
                    "messages": [
                        *payload["messages"],
                        tool_message("search", json.dumps([{"product_id": "P001", "price": 79.99}])),
                        message("recommend_agent", "This costs $49.99."),
                    ]
                }
            record_tool_call(
                tool_name="search",
                validated_args={"query": "dress"},
                success=True,
                result=[{"product_id": "P001", "price": 79.99}],
                agent_name="recommend_agent",
            )
            return {
                "messages": [
                    *payload["messages"],
                    tool_message("search", json.dumps([{"product_id": "P001", "price": 79.99}])),
                    message("recommend_agent", "This costs $79.99."),
                ]
            }

    def capture_records():
        records = get_tool_call_records()
        if records:
            snapshots.append(records)
        return records

    monkeypatch.setattr("scout.agents.supervisor.get_tool_call_records", capture_records)

    history = []
    reply, products = asyncio.run(_run_single_intent(FakeApp(), history, "dress", debug=False))

    assert reply == "I couldn’t verify a reliable product result for that request."
    assert products == []
    assert [record.attempt_number for record in snapshots[-1]] == [0]
    assert all("evidence_id" not in item for item in history)
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert get_tool_call_records() == []


def test_streaming_lifecycle_records_attempts_and_preserves_sse_shape(monkeypatch):
    snapshots = []

    class FakeStreamingApp:
        def __init__(self):
            self.correction_calls = 0

        async def astream_events(self, payload, version, config):
            record_tool_call(
                tool_name="search",
                validated_args={"query": "dress"},
                success=True,
                result=[{"product_id": "P001", "price": 79.99}],
                agent_name="recommend_agent",
            )
            yield {
                "event": "on_chain_end",
                "name": "LangGraph",
                "metadata": {},
                "data": {
                    "output": {
                        "messages": [
                            *payload["messages"],
                            tool_message("search", json.dumps([{"product_id": "P001", "price": 79.99}])),
                            message("recommend_agent", "This costs $49.99."),
                        ]
                    }
                },
            }

        async def ainvoke(self, payload, config):
            self.correction_calls += 1
            record_tool_call(
                tool_name="search",
                validated_args={"query": "dress"},
                success=True,
                result=[{"product_id": "P001", "price": 79.99}],
                agent_name="recommend_agent",
            )
            return {
                "messages": [
                    *payload["messages"],
                    tool_message("search", json.dumps([{"product_id": "P001", "price": 79.99}])),
                    message("recommend_agent", "This costs $79.99."),
                ]
            }

    def capture_records():
        records = get_tool_call_records()
        if records:
            snapshots.append(records)
        return records

    monkeypatch.setattr("scout.agents.supervisor.get_tool_call_records", capture_records)

    history = []
    events = asyncio.run(
        _collect_async(_run_single_intent_streaming(FakeStreamingApp(), history, "dress"))
    )

    assert events == [
        ("progress", "understanding_request"),
        ("progress", "verifying_claims"),
        ("progress", "preparing_response"),
        ("result", ("I couldn’t verify a reliable product result for that request.", [])),
    ]
    assert [record.attempt_number for record in snapshots[-1]] == [0]
    assert all("evidence_id" not in item for item in history)
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert get_tool_call_records() == []


async def _collect_async(async_iterable):
    return [item async for item in async_iterable]
