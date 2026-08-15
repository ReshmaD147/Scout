import asyncio

from scout.mcp_server.server import mcp


EXPECTED_TOOL_NAMES = {
    "search",
    "search_external_offers",
    "recommend_products",
    "stock",
    "alternatives",
    "fulfillment_options",
    "stores",
    "order_history",
    "return_eligibility",
    "orders",
    "retrieve_policy_chunks",
    "shipment_status",
}

FORBIDDEN_TOOL_NAMES = {
    "checkout",
    "payment",
    "pay",
    "refund",
    "cancel",
    "cancel_order",
    "create_order",
    "update_order",
    "delete_order",
    "mutate_order",
    "update_inventory",
    "set_inventory",
    "adjust_inventory",
    "sql",
    "query_sql",
    "shell",
    "exec",
    "http",
    "request",
    "fetch",
}


def _registered_tool_names() -> set[str]:
    return {tool.name for tool in asyncio.run(mcp.list_tools())}


def test_registered_mcp_tool_names_match_expected_registry():
    assert _registered_tool_names() == EXPECTED_TOOL_NAMES


def test_mcp_registry_excludes_mutating_or_unrestricted_tools():
    tool_names = _registered_tool_names()

    assert tool_names.isdisjoint(FORBIDDEN_TOOL_NAMES)
    assert all("refund" not in name for name in tool_names)
    assert all("cancel" not in name for name in tool_names)
    assert all("checkout" not in name for name in tool_names)
    assert all("payment" not in name for name in tool_names)
    assert all("sql" not in name for name in tool_names)
    assert all("shell" not in name for name in tool_names)
    assert all("http" not in name for name in tool_names)
