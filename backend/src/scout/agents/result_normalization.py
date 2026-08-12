from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from scout.agents.diagnostics import get_diagnostics
from scout.agents.evidence import EvidenceEntry
from scout.agents.orchestration.constants import HANDOFF_MARKERS, SPECIALIST_NAMES


PRODUCT_TOOL_NAMES = {"search", "alternatives", "search_external_offers", "recommend_products"}
PRODUCT_FIELDS = {
    "product_id",
    "external_product_id",
    "name",
    "price",
    "rating",
    "promotion",
    "vendor_name",
    "click_url",
    "source",
    "category",
    "subcategory",
    "use_cases",
    "attributes",
    "waterproof",
    "availability",
    "product_url",
    "last_verified_at",
    "satisfied_constraints",
}


@dataclass
class NormalizedAgentResult:
    messages: list = field(default_factory=list)
    final_text: str = ""
    tool_result_candidates: list[dict] = field(default_factory=list)
    routing_marker_present: bool = False


def normalize_agent_result(
    *,
    result: dict,
    input_message_count: int,
    execution_mode: str,
) -> NormalizedAgentResult:
    all_messages = _extract_messages_from_result(result)
    if execution_mode == "supervisor":
        messages = all_messages[input_message_count:] if len(all_messages) >= input_message_count else all_messages
    else:
        messages = all_messages

    tool_candidates = extract_tool_result_candidates(messages)
    final_text = final_assistant_text(messages)
    routing_marker_present = _routing_marker_present(messages)
    _record_shape_diagnostics(
        execution_mode=execution_mode,
        all_messages=all_messages,
        messages=messages,
        result=result,
        tool_candidates=tool_candidates,
        routing_marker_present=routing_marker_present,
    )
    return NormalizedAgentResult(
        messages=messages,
        final_text=final_text,
        tool_result_candidates=tool_candidates,
        routing_marker_present=routing_marker_present,
    )


def _extract_messages_from_result(result: Any) -> list:
    if isinstance(result, dict):
        messages = result.get("messages")
        if isinstance(messages, list):
            return list(messages)
        for value in result.values():
            nested = _extract_messages_from_result(value)
            if nested:
                return nested
        return []
    messages = getattr(result, "messages", None)
    if isinstance(messages, list):
        return list(messages)
    return []


def final_assistant_text(messages: list) -> str:
    for msg in reversed(messages):
        if _message_type(msg) == "tool":
            continue
        text = _text_from_content(getattr(msg, "content", ""))
        stripped = text.strip()
        if stripped and stripped not in HANDOFF_MARKERS:
            return text
    return ""


def _routing_marker_present(messages: list) -> bool:
    for msg in messages:
        if _message_type(msg) == "tool":
            continue
        text = _text_from_content(getattr(msg, "content", "")).strip()
        if text.startswith("NEEDS_EXTERNAL_CHECK"):
            return True
    return False


def extract_tool_result_candidates(messages: list) -> list[dict]:
    products: list[dict] = []
    for msg in messages:
        if _message_type(msg) != "tool":
            continue
        if _message_name(msg) not in PRODUCT_TOOL_NAMES:
            continue
        _extract_product_dicts(getattr(msg, "content", ""), products)
        if hasattr(msg, "artifact"):
            _extract_product_dicts(getattr(msg, "artifact"), products)
    return _dedupe_products(products)


def extract_product_candidates(
    *,
    normalized_messages: list,
    evidence_entries: list[EvidenceEntry],
) -> list[dict]:
    evidence_products = _products_from_evidence(evidence_entries)
    message_products = extract_tool_result_candidates(normalized_messages)
    return _dedupe_products([*evidence_products, *message_products])


def evidence_entries_from_tool_messages(
    *,
    normalized_messages: list,
    sub_intent_id: str,
    attempt_number: int,
    start_sequence: int = 0,
) -> list[EvidenceEntry]:
    entries: list[EvidenceEntry] = []
    for msg in normalized_messages:
        if _message_type(msg) != "tool" or _message_name(msg) not in PRODUCT_TOOL_NAMES:
            continue
        products = extract_tool_result_candidates([msg])
        if not products:
            continue
        first = products[0]
        entity_id = first.get("product_id") or first.get("external_product_id")
        entries.append(
            EvidenceEntry(
                sequence=start_sequence + len(entries),
                sub_intent_id=sub_intent_id,
                attempt_number=attempt_number,
                agent_name=_agent_for_tool(_message_name(msg)),
                tool_name=_message_name(msg),
                validated_args={},
                entity_type="external_product" if first.get("external_product_id") else "product",
                entity_id=entity_id,
                normalized_facts={"items": products},
                success=True,
            )
        )
    return entries


def _products_from_evidence(evidence_entries: list[EvidenceEntry]) -> list[dict]:
    products: list[dict] = []
    for entry in evidence_entries:
        if not entry.success or entry.tool_name not in PRODUCT_TOOL_NAMES:
            continue
        for facts in _iter_fact_items(entry.normalized_facts):
            product = _product_from_facts(facts)
            if product:
                products.append(product)
    return products


def _iter_fact_items(facts: dict[str, Any]):
    items = facts.get("items") if isinstance(facts, dict) else None
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                yield item
        return
    if isinstance(facts, dict):
        yield facts


def _product_from_facts(facts: dict[str, Any]) -> dict | None:
    product_id = facts.get("product_id") or facts.get("external_product_id")
    name = facts.get("name")
    if not isinstance(product_id, str) or not product_id.strip() or not isinstance(name, str) or not name.strip():
        return None

    product: dict[str, Any] = {}
    id_key = "external_product_id" if facts.get("external_product_id") else "product_id"
    product[id_key] = product_id
    product["name"] = name
    if facts.get("external_product_id"):
        product["source"] = "external"
    elif facts.get("source") is not None:
        product["source"] = facts["source"]
    else:
        product["source"] = "internal"

    for key in (
        "price",
        "rating",
        "vendor_name",
        "click_url",
        "category",
        "subcategory",
        "use_cases",
        "attributes",
        "waterproof",
        "availability",
        "product_url",
        "last_verified_at",
        "satisfied_constraints",
    ):
        if facts.get(key) is not None:
            product[key] = facts[key]
    promotion = facts.get("promotion")
    if isinstance(promotion, dict):
        product["promotion"] = {
            key: value
            for key, value in promotion.items()
            if key in {"name", "discounted_price"} and value is not None
        }
    return product


def _extract_product_dicts(value: Any, results: list[dict]) -> None:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return
        _extract_product_dicts(parsed, results)
        return

    if isinstance(value, list):
        for item in value:
            _extract_product_dicts(item, results)
        return

    if isinstance(value, dict):
        if _looks_like_product(value):
            product = {
                key: value[key]
                for key in PRODUCT_FIELDS
                if key in value and value[key] is not None
            }
            if product.get("product_id") and not product.get("source"):
                product["source"] = "internal"
            results.append(product)
            return
        for key in ("text", "content", "artifact", "items", "matches"):
            if key in value:
                _extract_product_dicts(value[key], results)


def _looks_like_product(value: dict[str, Any]) -> bool:
    return bool(value.get("product_id") or value.get("external_product_id")) and isinstance(value.get("name"), str)


def _dedupe_products(products: list[dict]) -> list[dict]:
    by_id = {}
    for product in products:
        if not isinstance(product, dict):
            continue
        key = product.get("product_id") or product.get("external_product_id")
        if not key:
            continue
        if key not in by_id:
            by_id[key] = dict(product)
            continue
        for field, value in product.items():
            if field not in by_id[key] and value is not None:
                by_id[key][field] = value
    return list(by_id.values())


def _agent_for_tool(tool_name: str | None) -> str:
    if tool_name == "search_external_offers":
        return "external_offer_agent"
    return "recommend_agent"


def _message_type(msg) -> str | None:
    return getattr(msg, "type", None) or getattr(msg, "role", None)


def _message_name(msg) -> str | None:
    return getattr(msg, "name", None)


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)


def _record_shape_diagnostics(
    *,
    execution_mode: str,
    all_messages: list,
    messages: list,
    result: Any,
    tool_candidates: list[dict],
    routing_marker_present: bool,
) -> None:
    diagnostics = get_diagnostics()
    if diagnostics is None:
        return
    tool_names = sorted({
        _message_name(msg)
        for msg in messages
        if _message_type(msg) == "tool" and _message_name(msg)
    })
    diagnostics.record(
        "result_normalization",
        0,
        execution_mode=execution_mode,
        result_type=type(result).__name__,
        top_level_keys=",".join(sorted(result.keys())) if isinstance(result, dict) else "",
        top_level_message_count=len(all_messages),
        normalized_message_count=len(messages),
        tool_candidate_count=len(tool_candidates),
        routing_marker_present=routing_marker_present,
        tool_names=",".join(tool_names),
    )
