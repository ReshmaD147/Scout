from __future__ import annotations

import asyncio
import re
import sys
import time

from scout.agents.diagnostics import (
    get_diagnostics,
    record_selected_specialist,
    record_tool_call_timing,
)
from scout.agents.evidence import (
    get_evidence_entries,
    get_tool_call_records,
    record_tool_call,
)
from scout.agents.intent_splitter import StructuredIntent


def _supervisor_override(name: str, current):
    supervisor_module = sys.modules.get("scout.agents.supervisor")
    override = getattr(supervisor_module, name, None) if supervisor_module is not None else None
    if override is not None and override is not current:
        return override
    return None


def _inventory_constraints_from_structured_intent(
    structured_intent: StructuredIntent | None,
) -> dict[str, str]:
    if structured_intent is None:
        return {}
    if structured_intent.request_type not in {"inventory_availability", "store_availability"}:
        return {}
    constraints = {}
    if structured_intent.product_id:
        constraints["product_id"] = structured_intent.product_id
    if structured_intent.size:
        constraints["size"] = structured_intent.size
    if structured_intent.color:
        constraints["color"] = structured_intent.color
    return constraints


async def _run_tool_first_if_possible(
    *,
    structured_intent: StructuredIntent | None,
    sub_intent: str,
    agent_name: str,
) -> bool:
    if structured_intent is None:
        return False
    if (
        agent_name == "inventory_agent"
        and structured_intent.request_type == "store_availability"
        and structured_intent.product_id
        and structured_intent.location
        and (structured_intent.size or structured_intent.color)
    ):
        stock_args = {"product_id": structured_intent.product_id}
        if structured_intent.size:
            stock_args["size"] = structured_intent.size
        if structured_intent.color:
            stock_args["color"] = structured_intent.color
        await _execute_read_only_tool("stock", stock_args, agent_name=agent_name)
        await _execute_read_only_tool(
            "stores",
            {
                key: value
                for key, value in {
                    "product_id": structured_intent.product_id,
                    "store_name": structured_intent.location,
                    "size": structured_intent.size,
                    "color": structured_intent.color,
                }.items()
                if value
            },
            agent_name=agent_name,
        )
        _record_tool_first("stores", agent_name, continued_from="stock")
        return True
    tool_name, args = _tool_first_call(structured_intent, agent_name)
    if not tool_name:
        return False
    result = await _execute_read_only_tool(tool_name, args, agent_name=agent_name)
    if tool_name == "recommend_products" and not result:
        # A clear recommendation with genuinely zero internal matches
        # needs the REAL agent path, not this deterministic shortcut -
        # only the full recommend_agent (with its NEEDS_EXTERNAL_CHECK
        # handoff instruction) knows to hand off to external_offer_agent
        # for real, third-party alternatives. Returning False here lets
        # the caller correctly fall through to that existing, proven
        # path. Confirmed via a real regression found while adding this
        # deterministic path: without this check, a genuinely
        # unmatched internal search silently produced a generic
        # "couldn't verify" message instead of real external offers.
        return False
    _record_tool_first(tool_name, agent_name)
    return True


def _tool_first_call(structured_intent: StructuredIntent, agent_name: str) -> tuple[str | None, dict]:
    if agent_name == "order_agent":
        # shipment_status vs order_status is decided ONCE, at the point
        # the StructuredIntent is created (see
        # _resolve_order_follow_up_intent and the deterministic intent
        # classifier) - request_type itself already carries the correct
        # meaning here, so this is a simple, direct mapping with no
        # keyword-guessing needed.
        if structured_intent.request_type == "shipment_status" and structured_intent.order_id:
            return "shipment_status", {"order_id": structured_intent.order_id}
        if structured_intent.request_type == "order_status" and structured_intent.order_id:
            return "orders", {"order_id": structured_intent.order_id}
        if structured_intent.request_type == "return_eligibility" and structured_intent.order_id:
            return "return_eligibility", {"order_id": structured_intent.order_id}
    if agent_name == "recommend_agent" and structured_intent.request_type == "product_recommendation" and structured_intent.product_type:
        # Deterministic tool-first path for a genuinely clear, unambiguous
        # recommendation request - we already know the product type (and
        # optionally a budget) from deterministic classification, so
        # there's no real ambiguity requiring the model's judgment.
        # Confirmed via a real bug, found through repeated evaluation
        # runs: without this, EVERY recommendation request - even
        # unambiguous ones like "do you have any shoes?" - went through
        # the full AI model, which non-deterministically sometimes
        # decided not to call any tool at all, producing a genuine,
        # intermittent "couldn't verify" failure roughly 1 in 3 times.
        # This uses deterministic state (the already-classified product
        # type) instead of relying on the model to reliably decide to
        # search, per the principle of preferring deterministic handling
        # for information Scout already has.
        args = {"query": structured_intent.text}
        if structured_intent.budget_max is not None:
            args["max_price"] = structured_intent.budget_max
        return "recommend_products", args
    if agent_name == "recommend_agent" and structured_intent.request_type == "similar_products" and structured_intent.product_id:
        args = {"product_id": structured_intent.product_id, "limit": 3}
        if structured_intent.size:
            args["size"] = structured_intent.size
        if structured_intent.color:
            args["color"] = structured_intent.color
        if structured_intent.budget_max is not None:
            args["max_price"] = structured_intent.budget_max
        if structured_intent.product_type:
            args["category"] = structured_intent.product_type
        return "alternatives", args
    if agent_name == "inventory_agent":
        if structured_intent.fulfillment_preference == "delivery" and structured_intent.product_id:
            args = {"product_id": structured_intent.product_id}
            if structured_intent.size:
                args["size"] = structured_intent.size
            if structured_intent.color:
                args["color"] = structured_intent.color
            return "fulfillment_options", args
        if structured_intent.request_type == "inventory_availability" and structured_intent.product_id and structured_intent.size:
            args = {"product_id": structured_intent.product_id, "size": structured_intent.size}
            if structured_intent.color:
                args["color"] = structured_intent.color
            return "stock", args
        if structured_intent.request_type == "store_availability" and structured_intent.product_id and structured_intent.location:
            return "stores", {
                key: value
                for key, value in {
                    "product_id": structured_intent.product_id,
                    "store_name": structured_intent.location,
                    "size": structured_intent.size,
                    "color": structured_intent.color,
                }.items()
                if value
            }
    if agent_name == "external_offer_agent" and structured_intent.request_type == "external_offer":
        category = structured_intent.product_type or _category_from_text(structured_intent.text)
        if category:
            args = _external_offer_args(structured_intent.text, category=category, budget_max=structured_intent.budget_max)
            if structured_intent.budget_max is not None:
                args["max_price"] = structured_intent.budget_max
            return "search_external_offers", args
    return None, {}


async def _execute_read_only_tool(tool_name: str, args: dict, *, agent_name: str) -> object:
    override = _supervisor_override("_execute_read_only_tool", _execute_read_only_tool)
    if override is not None:
        return await override(tool_name, args, agent_name=agent_name)

    allowed = {
        "inventory_agent": {"stock", "stores", "fulfillment_options"},
        "external_offer_agent": {"search_external_offers"},
        "recommend_agent": {"recommend_products", "alternatives"},
        "order_agent": {"orders", "return_eligibility", "shipment_status"},
        "policy_agent": {"retrieve_policy_chunks"},
    }
    if tool_name not in allowed.get(agent_name, set()):
        raise ValueError("deterministic tool-first path attempted a disallowed tool")
    started = time.perf_counter()
    try:
        from scout.mcp_server import server as local_tools

        result = await asyncio.to_thread(getattr(local_tools, tool_name), **args)
    except Exception:
        record_tool_call(
            tool_name=tool_name,
            validated_args=args,
            success=False,
            error_code="tool_execution_failed",
            agent_name=agent_name,
        )
        record_tool_call_timing(
            tool_name=tool_name,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            agent_name=agent_name,
        )
        raise
    record_tool_call(
        tool_name=tool_name,
        validated_args=args,
        success=True,
        result=result,
        agent_name=agent_name,
    )
    record_tool_call_timing(
        tool_name=tool_name,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        agent_name=agent_name,
    )
    return result


def _record_tool_first(tool_name: str, agent_name: str, *, continued_from: str | None = None) -> None:
    record_selected_specialist(agent_name)
    diagnostics = get_diagnostics()
    if diagnostics is not None:
        diagnostics.record(
            "deterministic_tool_first",
            0,
            deterministic_tool_first_used=True,
            tool_name=tool_name,
            selected_specialist=agent_name,
            model_calls_avoided=1,
            continued_from=continued_from,
        )


def _category_from_text(text: str) -> str | None:
    lowered = (text or "").lower()
    for category in ("dresses", "dress", "shoes", "shoe", "tops", "shirts", "pants", "jeans", "jackets", "coats", "skirts", "bags"):
        if re.search(rf"\b{re.escape(category)}\b", lowered):
            return "dresses" if category == "dress" else "shoes" if category == "shoe" else category
    return None


async def _continue_after_tool_evidence(
    *,
    structured_intent: StructuredIntent | None,
    agent_name: str,
) -> bool:
    if structured_intent is None:
        return False
    entries = [entry for entry in get_evidence_entries() if getattr(entry, "success", False)]
    records = [record for record in get_tool_call_records() if getattr(record, "success", False)]
    latest = entries[-1] if entries else None
    if agent_name == "inventory_agent" and latest is not None and latest.tool_name == "search":
        candidate = _unique_product_candidate(latest.normalized_facts)
        if candidate is None:
            return False
        product_id = candidate["product_id"]
        if structured_intent.request_type == "inventory_availability" and structured_intent.size:
            args = {"product_id": product_id, "size": structured_intent.size}
            if structured_intent.color:
                args["color"] = structured_intent.color
            await _execute_read_only_tool("stock", args, agent_name="inventory_agent")
            _record_tool_first("stock", "inventory_agent", continued_from="search")
            return True
        if structured_intent.request_type == "store_availability" and structured_intent.location:
            await _execute_read_only_tool(
                "stores",
                {
                    key: value
                    for key, value in {
                        "product_id": product_id,
                        "store_name": structured_intent.location,
                        "size": structured_intent.size,
                        "color": structured_intent.color,
                    }.items()
                    if value
                },
                agent_name="inventory_agent",
            )
            _record_tool_first("stores", "inventory_agent", continued_from="search")
            return True
        if structured_intent.fulfillment_preference == "delivery":
            args = {"product_id": product_id}
            if structured_intent.size:
                args["size"] = structured_intent.size
            if structured_intent.color:
                args["color"] = structured_intent.color
            await _execute_read_only_tool("fulfillment_options", args, agent_name="inventory_agent")
            _record_tool_first("fulfillment_options", "inventory_agent", continued_from="search")
            return True
    latest_record = records[-1] if records else None
    if agent_name == "recommend_agent" and latest_record and latest_record.tool_name in {"recommend_products", "search"}:
        products = [
            facts
            for entry in entries
            if entry.tool_name in {"recommend_products", "search"}
            for facts in _iter_completion_facts(entry.normalized_facts)
            if facts.get("product_id") and facts.get("name")
        ]
        if _internal_products_satisfy_explicit_request(products, structured_intent) or _scout_only_requested(structured_intent.text):
            return False
        category = structured_intent.product_type or _category_from_text(structured_intent.text)
        if not category:
            return False
        args = _external_offer_args(structured_intent.text, category=category, budget_max=structured_intent.budget_max)
        await _execute_read_only_tool("search_external_offers", args, agent_name="external_offer_agent")
        _record_tool_first("search_external_offers", "external_offer_agent", continued_from=latest_record.tool_name)
        return True
    return False


def _unique_product_candidate(facts: dict) -> dict | None:
    candidates = []
    for item in _iter_completion_facts(facts):
        product_id = item.get("product_id")
        if isinstance(product_id, str) and product_id.strip():
            candidates.append({"product_id": product_id, "name": item.get("name") or item.get("product_name")})
    by_id = {}
    for candidate in candidates:
        by_id.setdefault(candidate["product_id"], candidate)
    values = list(by_id.values())
    return values[0] if len(values) == 1 else None


def _scout_only_requested(text: str) -> bool:
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in ("scout only", "only scout", "in your inventory only", "internal only"))


def _internal_products_satisfy_explicit_request(products: list[dict], structured_intent: StructuredIntent) -> bool:
    if not products:
        return False
    lowered_request = (structured_intent.text or "").lower()
    required_terms = []
    if structured_intent.color:
        required_terms.append(structured_intent.color.lower())
    for term in ("cocktail", "hiking", "running", "waterproof", "work"):
        if re.search(rf"\b{term}\b", lowered_request):
            required_terms.append(term)
    if not required_terms:
        return True
    return any(_product_contains_terms(product, required_terms) for product in products)


def _product_contains_terms(product: dict, terms: list[str]) -> bool:
    searchable_values = []
    for key in ("name", "category", "department", "brand", "description"):
        value = product.get(key)
        if isinstance(value, str):
            searchable_values.append(value.lower())
    for key in ("tags", "colors"):
        value = product.get(key)
        if isinstance(value, list):
            searchable_values.extend(str(item).lower() for item in value)
        elif isinstance(value, str):
            searchable_values.append(value.lower())
    haystack = " ".join(searchable_values)
    return all(term in haystack for term in terms)


def _external_offer_args(
    query: str,
    *,
    category: str,
    budget_max: float | None = None,
    color: str | None = None,
) -> dict:
    lowered = (query or "").lower()
    args = {"query": query, "category": category}
    if budget_max is not None:
        args["budget_max"] = budget_max
    if color:
        args["color"] = color
    if "hiking" in lowered:
        args["use_case"] = "hiking"
    if "waterproof" in lowered:
        args["waterproof"] = True
        args["required_attributes"] = "waterproof"
    return args


def _iter_completion_facts(facts: dict):
    if isinstance(facts, dict):
        yield facts
        for key in ("items", "stores"):
            value = facts.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        yield item
