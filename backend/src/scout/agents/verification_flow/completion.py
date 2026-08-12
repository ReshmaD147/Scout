from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from scout.agents.intent_splitter import StructuredIntent


@dataclass
class EvidenceCompletionDecision:
    complete: bool
    responsible_domain: str = ""
    supporting_evidence_ids: list[str] | None = None
    completion_reason: str = ""


def _completion_decimal_money(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def can_finalize_from_evidence(
    *,
    intent: str,
    structured_intent: StructuredIntent | None,
    evidence_entries: list,
    tool_history: list | None = None,
) -> EvidenceCompletionDecision:
    successful = [entry for entry in evidence_entries if getattr(entry, "success", False)]
    if not successful:
        return EvidenceCompletionDecision(False)
    request_type = structured_intent.request_type if structured_intent else intent

    if request_type == "product_recommendation":
        products = [
            facts
            for entry in successful
            if entry.tool_name in {"recommend_products", "search"}
            for facts in _iter_completion_facts(entry.normalized_facts)
            if facts.get("product_id") and facts.get("name") and facts.get("price") is not None
        ]
        max_budget = Decimal(str(structured_intent.budget_max)) if structured_intent and structured_intent.budget_max is not None else None
        if products and all(max_budget is None or _completion_decimal_money(product.get("price")) <= max_budget for product in products if _completion_decimal_money(product.get("price")) is not None):
            return EvidenceCompletionDecision(True, "product", [entry.evidence_id for entry in successful if entry.tool_name in {"recommend_products", "search"}], "product_evidence_complete")

    if request_type == "external_offer":
        offers = [
            facts
            for entry in successful
            if entry.tool_name == "search_external_offers"
            for facts in _iter_completion_facts(entry.normalized_facts)
            if facts.get("external_product_id") and facts.get("name") and facts.get("vendor_name") and facts.get("price") is not None and facts.get("click_url")
        ]
        if offers:
            return EvidenceCompletionDecision(True, "external", [entry.evidence_id for entry in successful if entry.tool_name == "search_external_offers"], "external_offer_evidence_complete")

    if request_type == "inventory_availability":
        requested_size = structured_intent.size if structured_intent else None
        requested_color = structured_intent.color.lower() if structured_intent and structured_intent.color else None
        for entry in successful:
            if entry.tool_name != "stock":
                continue
            for facts in _iter_completion_facts(entry.normalized_facts):
                if not facts.get("product_id"):
                    continue
                if requested_size and str(facts.get("size", "")).upper() != requested_size:
                    continue
                if requested_color and str(facts.get("color", "")).lower() != requested_color:
                    continue
                if facts.get("quantity") is not None or facts.get("in_stock") is not None:
                    return EvidenceCompletionDecision(True, "inventory", [entry.evidence_id], "inventory_evidence_complete")

    if request_type == "store_availability":
        requested_store = (structured_intent.location or "").strip().lower() if structured_intent else ""
        for entry in successful:
            if entry.tool_name != "stores":
                continue
            for facts in _iter_completion_facts(entry.normalized_facts):
                if not facts.get("product_id"):
                    continue
                store_name = str(facts.get("store_name", "")).strip().lower()
                if requested_store and store_name != requested_store:
                    continue
                if facts.get("store_id") and (facts.get("quantity") is not None or facts.get("in_stock") is not None):
                    return EvidenceCompletionDecision(True, "store", [entry.evidence_id], "store_evidence_complete")

    if request_type == "order_status":
        requested_order = structured_intent.order_id if structured_intent else None
        for entry in successful:
            if entry.tool_name != "orders":
                continue
            for facts in _iter_completion_facts(entry.normalized_facts):
                if facts.get("order_id") == requested_order and facts.get("status"):
                    return EvidenceCompletionDecision(True, "order", [entry.evidence_id], "order_status_evidence_complete")

    if request_type == "policy_question":
        policy_entries = [
            entry
            for entry in successful
            if entry.tool_name == "retrieve_policy_chunks"
            and any(facts.get("statement") for facts in _iter_completion_facts(entry.normalized_facts))
        ]
        if policy_entries:
            return EvidenceCompletionDecision(True, "policy", [entry.evidence_id for entry in policy_entries], "policy_evidence_complete")

    return EvidenceCompletionDecision(False)


def _iter_completion_facts(facts: dict):
    if isinstance(facts, dict):
        yield facts
        for key in ("items", "stores"):
            value = facts.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        yield item
