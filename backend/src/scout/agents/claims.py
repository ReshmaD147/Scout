from __future__ import annotations

import re
from copy import deepcopy
from enum import StrEnum
from typing import Any

from scout.agents.evidence import EvidenceEntry, ProposedClaim, is_json_compatible


class ClaimType(StrEnum):
    PRODUCT_IDENTITY = "product_identity"
    PRODUCT_PRICE = "product_price"
    PRODUCT_RATING = "product_rating"
    PROMOTION = "promotion"
    INVENTORY_QUANTITY = "inventory_quantity"
    INVENTORY_AVAILABILITY = "inventory_availability"
    STORE_IDENTITY = "store_identity"
    STORE_DISTANCE = "store_distance"
    PICKUP_AVAILABILITY = "pickup_availability"
    DELIVERY_AVAILABILITY = "delivery_availability"
    FULFILLMENT_ESTIMATE = "fulfillment_estimate"
    ORDER_STATUS = "order_status"
    ORDER_TRACKING = "order_tracking"
    PAYMENT_STATUS = "payment_status"
    RETURN_ELIGIBILITY = "return_eligibility"
    POLICY_STATEMENT = "policy_statement"
    EXTERNAL_OFFER_IDENTITY = "external_offer_identity"
    EXTERNAL_OFFER_PRICE = "external_offer_price"
    EXTERNAL_OFFER_VENDOR = "external_offer_vendor"
    EXTERNAL_OFFER_USE_CASE = "external_offer_use_case"
    EXTERNAL_OFFER_ATTRIBUTE = "external_offer_attribute"
    EXTERNAL_OFFER_AVAILABILITY = "external_offer_availability"
    ACCESS_DENIED = "access_denied"


CANONICAL_AGENT_NAMES = {
    "recommend_agent",
    "inventory_agent",
    "order_agent",
    "external_offer_agent",
    "policy_agent",
    "supervisor",
}

PRICE_PATTERN = re.compile(r"\$(\d+(?:\.\d{1,2})?)")
DISTANCE_PATTERN = re.compile(r"\b(\d+(?:\.\d+)?)\s*miles?\b", re.IGNORECASE)
QUANTITY_PATTERN = re.compile(r"\b(?:qty|quantity|have|has)\s*:?\s*(\d+)\b", re.IGNORECASE)
RETURN_WINDOW_PATTERN = re.compile(r"\b(\d+)\s*days?\b", re.IGNORECASE)
TRACKING_PATTERN = re.compile(r"\b(?:tracking|tracking number)\s*(?:is|#|:)?\s*([A-Z0-9][A-Z0-9-]{5,})\b", re.IGNORECASE)
ORDER_STATUSES = {"pending", "processing", "shipped", "delivered", "cancelled", "canceled", "returned"}


def propose_claims(
    *,
    reply_text: str,
    products: list,
    evidence_entries: list[EvidenceEntry],
    source_agent: str | None = None,
) -> list[ProposedClaim]:
    evidence = [entry.model_copy(deep=True) for entry in evidence_entries]
    product_items = [deepcopy(product) for product in products if isinstance(product, dict)]
    fallback_agent = source_agent if source_agent in CANONICAL_AGENT_NAMES else "supervisor"
    builder = _ClaimBuilder(fallback_agent=fallback_agent)

    for product in product_items:
        _add_product_claims(builder, product, evidence)

    for entry in evidence:
        _add_evidence_claims(builder, entry)

    _add_reply_candidates(builder, reply_text or "", evidence, fallback_agent)

    return builder.claims


class _ClaimBuilder:
    def __init__(self, fallback_agent: str):
        self.fallback_agent = fallback_agent
        self.claims: list[ProposedClaim] = []
        self._by_key: dict[tuple, ProposedClaim] = {}

    def add(
        self,
        *,
        claim_type: ClaimType,
        subject_id: str | None,
        field: str,
        value: Any,
        evidence_ids: list[str] | None = None,
        source_agent: str | None = None,
    ) -> None:
        if not field or value is None or not is_json_compatible(value):
            return
        normalized_evidence_ids = _unique(evidence_ids or [])
        key = (claim_type.value, subject_id, field, _normalized_value_key(value))
        existing = self._by_key.get(key)
        if existing:
            existing.evidence_ids = _unique([*existing.evidence_ids, *normalized_evidence_ids])
            if existing.source_agent == "supervisor" and source_agent in CANONICAL_AGENT_NAMES:
                existing.source_agent = source_agent
            return

        resolved_agent = source_agent if source_agent in CANONICAL_AGENT_NAMES else self.fallback_agent
        claim = ProposedClaim(
            claim_type=claim_type.value,
            subject_id=subject_id,
            field=field,
            value=value,
            evidence_ids=normalized_evidence_ids,
            source_agent=resolved_agent,
        )
        self._by_key[key] = claim
        self.claims.append(claim)


def _add_product_claims(builder: _ClaimBuilder, product: dict[str, Any], evidence: list[EvidenceEntry]) -> None:
    subject_id = _subject_id_for_product(product)
    if not subject_id:
        return
    matching = _matching_evidence(evidence, subject_id=subject_id, name=product.get("name"))
    evidence_ids = [entry.evidence_id for entry in matching]
    source_agent = _source_agent(matching, builder.fallback_agent)

    if product.get("name") is not None:
        claim_type = (
            ClaimType.EXTERNAL_OFFER_IDENTITY
            if product.get("external_product_id") or product.get("source") == "external"
            else ClaimType.PRODUCT_IDENTITY
        )
        field = "product_name" if claim_type == ClaimType.EXTERNAL_OFFER_IDENTITY else "name"
        builder.add(
            claim_type=claim_type,
            subject_id=subject_id,
            field=field,
            value=product.get("name"),
            evidence_ids=evidence_ids,
            source_agent=source_agent,
        )

    if product.get("price") is not None:
        builder.add(
            claim_type=ClaimType.EXTERNAL_OFFER_PRICE if product.get("external_product_id") else ClaimType.PRODUCT_PRICE,
            subject_id=subject_id,
            field="price",
            value=product.get("price"),
            evidence_ids=evidence_ids,
            source_agent=source_agent,
        )

    if product.get("rating") is not None and not product.get("external_product_id"):
        builder.add(
            claim_type=ClaimType.PRODUCT_RATING,
            subject_id=subject_id,
            field="rating",
            value=product.get("rating"),
            evidence_ids=evidence_ids,
            source_agent=source_agent,
        )

    promotion = product.get("promotion")
    if isinstance(promotion, dict):
        if promotion.get("name") is not None:
            builder.add(
                claim_type=ClaimType.PROMOTION,
                subject_id=subject_id,
                field="promotion_name",
                value=promotion.get("name"),
                evidence_ids=evidence_ids,
                source_agent=source_agent,
            )
        if promotion.get("discounted_price") is not None:
            builder.add(
                claim_type=ClaimType.PROMOTION,
                subject_id=subject_id,
                field="promotion_price",
                value=promotion.get("discounted_price"),
                evidence_ids=evidence_ids,
                source_agent=source_agent,
            )

    if product.get("vendor_name") is not None:
        builder.add(
            claim_type=ClaimType.EXTERNAL_OFFER_VENDOR,
            subject_id=subject_id,
            field="vendor",
            value=product.get("vendor_name"),
            evidence_ids=evidence_ids,
            source_agent=source_agent,
        )

    if product.get("click_url") is not None:
        builder.add(
            claim_type=ClaimType.EXTERNAL_OFFER_IDENTITY,
            subject_id=subject_id,
            field="url_or_offer_id",
            value=product.get("click_url"),
            evidence_ids=evidence_ids,
            source_agent=source_agent,
        )
    if product.get("source") == "external":
        if product.get("waterproof") is not None:
            builder.add(claim_type=ClaimType.EXTERNAL_OFFER_ATTRIBUTE, subject_id=subject_id, field="waterproof", value=bool(product["waterproof"]), evidence_ids=evidence_ids, source_agent=source_agent)
        for use_case in product.get("use_cases", []) if isinstance(product.get("use_cases"), list) else []:
            builder.add(claim_type=ClaimType.EXTERNAL_OFFER_USE_CASE, subject_id=subject_id, field="use_case", value=use_case, evidence_ids=evidence_ids, source_agent=source_agent)
        for attribute in product.get("attributes", []) if isinstance(product.get("attributes"), list) else []:
            builder.add(claim_type=ClaimType.EXTERNAL_OFFER_ATTRIBUTE, subject_id=subject_id, field="attribute", value=attribute, evidence_ids=evidence_ids, source_agent=source_agent)
        if product.get("availability") is not None:
            builder.add(claim_type=ClaimType.EXTERNAL_OFFER_AVAILABILITY, subject_id=subject_id, field="availability", value=product["availability"], evidence_ids=evidence_ids, source_agent=source_agent)


def _add_evidence_claims(builder: _ClaimBuilder, entry: EvidenceEntry) -> None:
    for facts in _iter_fact_items(entry.normalized_facts):
        subject_id = _subject_id_from_facts(facts) or entry.entity_id
        inventory_subject_id = _inventory_subject_id_from_facts(facts) or subject_id
        evidence_ids = [entry.evidence_id]
        agent = entry.agent_name

        product_name = facts.get("name") if facts.get("name") is not None else facts.get("product_name")
        if facts.get("product_id") and product_name is not None:
            builder.add(claim_type=ClaimType.PRODUCT_IDENTITY, subject_id=subject_id, field="name", value=product_name, evidence_ids=evidence_ids, source_agent=agent)
        if facts.get("external_product_id") and facts.get("name") is not None:
            builder.add(claim_type=ClaimType.EXTERNAL_OFFER_IDENTITY, subject_id=subject_id, field="product_name", value=facts["name"], evidence_ids=evidence_ids, source_agent=agent)
        if facts.get("price") is not None:
            builder.add(claim_type=ClaimType.EXTERNAL_OFFER_PRICE if facts.get("external_product_id") else ClaimType.PRODUCT_PRICE, subject_id=subject_id, field="price", value=facts["price"], evidence_ids=evidence_ids, source_agent=agent)
        if facts.get("rating") is not None and not facts.get("external_product_id"):
            builder.add(claim_type=ClaimType.PRODUCT_RATING, subject_id=subject_id, field="rating", value=facts["rating"], evidence_ids=evidence_ids, source_agent=agent)
        promotion = facts.get("promotion")
        if isinstance(promotion, dict):
            if promotion.get("name") is not None:
                builder.add(claim_type=ClaimType.PROMOTION, subject_id=subject_id, field="promotion_name", value=promotion["name"], evidence_ids=evidence_ids, source_agent=agent)
            if promotion.get("discounted_price") is not None:
                builder.add(claim_type=ClaimType.PROMOTION, subject_id=subject_id, field="promotion_price", value=promotion["discounted_price"], evidence_ids=evidence_ids, source_agent=agent)
        if facts.get("discounted_price") is not None:
            builder.add(claim_type=ClaimType.PROMOTION, subject_id=subject_id, field="promotion_price", value=facts["discounted_price"], evidence_ids=evidence_ids, source_agent=agent)
        if facts.get("vendor_name") is not None:
            builder.add(claim_type=ClaimType.EXTERNAL_OFFER_VENDOR, subject_id=subject_id, field="vendor", value=facts["vendor_name"], evidence_ids=evidence_ids, source_agent=agent)
        if facts.get("click_url") is not None:
            builder.add(claim_type=ClaimType.EXTERNAL_OFFER_IDENTITY, subject_id=subject_id, field="url_or_offer_id", value=facts["click_url"], evidence_ids=evidence_ids, source_agent=agent)
        if facts.get("external_product_id"):
            if facts.get("waterproof") is not None:
                builder.add(claim_type=ClaimType.EXTERNAL_OFFER_ATTRIBUTE, subject_id=subject_id, field="waterproof", value=bool(facts["waterproof"]), evidence_ids=evidence_ids, source_agent=agent)
            for use_case in facts.get("use_cases", []) if isinstance(facts.get("use_cases"), list) else []:
                builder.add(claim_type=ClaimType.EXTERNAL_OFFER_USE_CASE, subject_id=subject_id, field="use_case", value=use_case, evidence_ids=evidence_ids, source_agent=agent)
            for attribute in facts.get("attributes", []) if isinstance(facts.get("attributes"), list) else []:
                builder.add(claim_type=ClaimType.EXTERNAL_OFFER_ATTRIBUTE, subject_id=subject_id, field="attribute", value=attribute, evidence_ids=evidence_ids, source_agent=agent)
            if facts.get("availability") is not None:
                builder.add(claim_type=ClaimType.EXTERNAL_OFFER_AVAILABILITY, subject_id=subject_id, field="availability", value=facts["availability"], evidence_ids=evidence_ids, source_agent=agent)

        quantity = facts.get("quantity")
        if quantity is not None:
            builder.add(claim_type=ClaimType.INVENTORY_QUANTITY, subject_id=inventory_subject_id, field="quantity", value=quantity, evidence_ids=evidence_ids, source_agent=agent)
            if isinstance(quantity, int | float):
                builder.add(claim_type=ClaimType.INVENTORY_AVAILABILITY, subject_id=inventory_subject_id, field="in_stock", value=quantity > 0, evidence_ids=evidence_ids, source_agent=agent)
        for availability_key in ("in_stock", "available"):
            if facts.get(availability_key) is not None:
                builder.add(claim_type=ClaimType.INVENTORY_AVAILABILITY, subject_id=inventory_subject_id, field="in_stock", value=bool(facts[availability_key]), evidence_ids=evidence_ids, source_agent=agent)

        if facts.get("store_id") and facts.get("store_name") is not None:
            builder.add(claim_type=ClaimType.STORE_IDENTITY, subject_id=facts["store_id"], field="store_name", value=facts["store_name"], evidence_ids=evidence_ids, source_agent=agent)
        elif facts.get("requested_store_name") and not facts.get("store_id"):
            # No real store row exists (the requested store had zero
            # matching inventory) — but the customer's own requested
            # store name is real, verified data (it came from their own
            # tool call args, not invented), so it's safe to claim and
            # show in the reply. Uses inventory_subject_id so this
            # claim groups with the same record's quantity/in_stock
            # claims in rendering.py, producing one coherent, store-
            # specific sentence instead of a generic fallback that
            # never mentions which store was actually checked.
            builder.add(claim_type=ClaimType.STORE_IDENTITY, subject_id=inventory_subject_id, field="store_name", value=facts["requested_store_name"], evidence_ids=evidence_ids, source_agent=agent)
        if facts.get("distance_miles") is not None:
            builder.add(claim_type=ClaimType.STORE_DISTANCE, subject_id=facts.get("store_id") or subject_id, field="distance_miles", value=facts["distance_miles"], evidence_ids=evidence_ids, source_agent=agent)
        if facts.get("pickup_available") is not None:
            builder.add(claim_type=ClaimType.PICKUP_AVAILABILITY, subject_id=subject_id, field="pickup_available", value=bool(facts["pickup_available"]), evidence_ids=evidence_ids, source_agent=agent)
        if facts.get("delivery_available") is not None:
            builder.add(claim_type=ClaimType.DELIVERY_AVAILABILITY, subject_id=inventory_subject_id or subject_id, field="delivery_available", value=bool(facts["delivery_available"]), evidence_ids=evidence_ids, source_agent=agent)
        for estimate_key in ("pickup_estimate", "delivery_estimate"):
            if facts.get(estimate_key) is not None:
                builder.add(claim_type=ClaimType.FULFILLMENT_ESTIMATE, subject_id=inventory_subject_id or subject_id, field=estimate_key, value=facts[estimate_key], evidence_ids=evidence_ids, source_agent=agent)

        if facts.get("order_id") and facts.get("status") is not None:
            builder.add(claim_type=ClaimType.ORDER_STATUS, subject_id=facts["order_id"], field="status", value=facts["status"], evidence_ids=evidence_ids, source_agent=agent)
        elif facts.get("found") is False and facts.get("message") is not None:
            # Genuine access-denial case (e.g. orders() returning
            # found=False, authorized=False, with a safe, tool-provided
            # message like "Sign in to view order details.") — this is
            # NOT the customer's real data, it's a deliberate denial
            # message the tool itself already decided is safe to show.
            # Dedicated ACCESS_DENIED claim type (not ORDER_STATUS or
            # POLICY_STATEMENT, both of which had real, distinct
            # matching-logic mismatches when reused for this shape
            # during earlier debugging) — verified via exact-string
            # match only, domain-authorized per specialist, and never
            # reveals any of the customer's actual data.
            builder.add(claim_type=ClaimType.ACCESS_DENIED, subject_id=subject_id or facts.get("order_id"), field="message", value=facts["message"], evidence_ids=evidence_ids, source_agent=agent)
        if facts.get("tracking_number") is not None:
            builder.add(claim_type=ClaimType.ORDER_TRACKING, subject_id=subject_id, field="tracking_number", value=facts["tracking_number"], evidence_ids=evidence_ids, source_agent=agent)
        if facts.get("payment_status") is not None:
            builder.add(claim_type=ClaimType.PAYMENT_STATUS, subject_id=subject_id, field="payment_status", value=facts["payment_status"], evidence_ids=evidence_ids, source_agent=agent)
        for return_key in ("return_eligible", "likely_eligible", "eligible"):
            if facts.get(return_key) is not None:
                builder.add(claim_type=ClaimType.RETURN_ELIGIBILITY, subject_id=subject_id, field="return_eligible", value=bool(facts[return_key]), evidence_ids=evidence_ids, source_agent=agent)

        policy_subject_id = (
            subject_id
            or facts.get("source_document")
            or facts.get("policy_name")
            or "policy"
        )
        for policy_field in ("statement", "policy_name", "policy_version", "source_document", "source_section"):
            if facts.get(policy_field) is not None:
                builder.add(claim_type=ClaimType.POLICY_STATEMENT, subject_id=policy_subject_id, field=policy_field, value=facts[policy_field], evidence_ids=evidence_ids, source_agent=agent)


def _add_reply_candidates(
    builder: _ClaimBuilder,
    reply_text: str,
    evidence: list[EvidenceEntry],
    fallback_agent: str,
) -> None:
    linked_prices = _value_evidence_index(evidence, fields={"price", "discounted_price"})
    for match in PRICE_PATTERN.finditer(reply_text):
        value = float(match.group(1))
        matched = linked_prices.get(_normalized_value_key(value), [])
        subject_id = _single_subject_id(matched)
        builder.add(
            claim_type=ClaimType.PRODUCT_PRICE,
            subject_id=subject_id,
            field="price",
            value=value,
            evidence_ids=[entry.evidence_id for entry in matched],
            source_agent=_source_agent(matched, fallback_agent),
        )

    linked_distances = _value_evidence_index(evidence, fields={"distance_miles"})
    for match in DISTANCE_PATTERN.finditer(reply_text):
        value = float(match.group(1))
        matched = linked_distances.get(_normalized_value_key(value), [])
        builder.add(
            claim_type=ClaimType.STORE_DISTANCE,
            subject_id=_single_subject_id(matched),
            field="distance_miles",
            value=value,
            evidence_ids=[entry.evidence_id for entry in matched],
            source_agent=_source_agent(matched, fallback_agent),
        )

    linked_quantities = _value_evidence_index(evidence, fields={"quantity"})
    for match in QUANTITY_PATTERN.finditer(reply_text):
        value = int(match.group(1))
        matched = linked_quantities.get(_normalized_value_key(value), [])
        builder.add(
            claim_type=ClaimType.INVENTORY_QUANTITY,
            subject_id=_single_subject_id(matched),
            field="quantity",
            value=value,
            evidence_ids=[entry.evidence_id for entry in matched],
            source_agent=_source_agent(matched, fallback_agent),
        )

    lower_reply = reply_text.lower()
    for status in sorted(ORDER_STATUSES):
        if re.search(rf"\b{re.escape(status)}\b", lower_reply):
            matched = _entries_with_value(evidence, "status", status)
            if matched:
                builder.add(
                    claim_type=ClaimType.ORDER_STATUS,
                    subject_id=_single_subject_id(matched),
                    field="status",
                    value=status,
                    evidence_ids=[entry.evidence_id for entry in matched],
                    source_agent=_source_agent(matched, fallback_agent),
                )

    for match in TRACKING_PATTERN.finditer(reply_text):
        value = match.group(1)
        matched = _entries_with_value(evidence, "tracking_number", value)
        builder.add(
            claim_type=ClaimType.ORDER_TRACKING,
            subject_id=_single_subject_id(matched),
            field="tracking_number",
            value=value,
            evidence_ids=[entry.evidence_id for entry in matched],
            source_agent=_source_agent(matched, fallback_agent),
        )

    for match in RETURN_WINDOW_PATTERN.finditer(reply_text):
        value = int(match.group(1))
        if "return" not in reply_text[max(0, match.start() - 40): match.end() + 40].lower():
            continue
        matched = _entries_with_value(evidence, "return_window_days", value)
        builder.add(
            claim_type=ClaimType.POLICY_STATEMENT,
            subject_id=_single_subject_id(matched),
            field="statement",
            value=f"{value} days",
            evidence_ids=[entry.evidence_id for entry in matched],
            source_agent=_source_agent(matched, fallback_agent),
        )


def _iter_fact_items(facts: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    if facts:
        items.append(facts)
    for key in ("items", "variants", "stores", "pickup", "fulfillment_options", "shipping"):
        value = facts.get(key)
        if isinstance(value, dict):
            items.extend(_iter_fact_items(value))
        elif isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
    return items


def _subject_id_for_product(product: dict[str, Any]) -> str | None:
    return product.get("product_id") or product.get("external_product_id")


def _subject_id_from_facts(facts: dict[str, Any]) -> str | None:
    return (
        facts.get("product_id")
        or facts.get("external_product_id")
        or facts.get("order_id")
        or facts.get("store_id")
        or facts.get("customer_id")
    )


def _inventory_subject_id_from_facts(facts: dict[str, Any]) -> str | None:
    product_id = facts.get("product_id")
    if not isinstance(product_id, str) or not product_id.strip():
        return None
    size = facts.get("size")
    color = facts.get("color")
    store_id = facts.get("store_id")
    if isinstance(size, str) and size.strip():
        subject = f"product:{product_id}:size:{size.upper()}"
        if isinstance(color, str) and color.strip():
            subject += f":color:{color.strip().lower()}"
        if isinstance(store_id, str) and store_id.strip():
            subject += f":store:{store_id}"
        return subject
    if isinstance(color, str) and color.strip():
        subject = f"product:{product_id}:color:{color.strip().lower()}"
        if isinstance(store_id, str) and store_id.strip():
            subject += f":store:{store_id}"
        return subject
    if isinstance(store_id, str) and store_id.strip():
        return f"product:{product_id}:store:{store_id}"
    return product_id


def _matching_evidence(
    evidence: list[EvidenceEntry],
    *,
    subject_id: str | None,
    name: Any = None,
) -> list[EvidenceEntry]:
    matches = []
    normalized_name = _normalize_text(name) if isinstance(name, str) else None
    for entry in evidence:
        if subject_id and _entry_contains_value(entry, subject_id):
            matches.append(entry)
            continue
        if normalized_name and _entry_contains_name(entry, normalized_name):
            matches.append(entry)
    return matches


def _entry_contains_value(entry: EvidenceEntry, value: Any) -> bool:
    value_key = _normalized_value_key(value)
    return any(
        _normalized_value_key(item) == value_key
        for facts in _iter_fact_items(entry.normalized_facts)
        for item in facts.values()
        if not isinstance(item, dict | list)
    ) or entry.entity_id == value


def _entry_contains_name(entry: EvidenceEntry, normalized_name: str) -> bool:
    return any(
        _normalize_text(facts.get("name")) == normalized_name
        for facts in _iter_fact_items(entry.normalized_facts)
        if isinstance(facts.get("name"), str)
    )


def _value_evidence_index(evidence: list[EvidenceEntry], *, fields: set[str]) -> dict[tuple, list[EvidenceEntry]]:
    index: dict[tuple, list[EvidenceEntry]] = {}
    for entry in evidence:
        for facts in _iter_fact_items(entry.normalized_facts):
            for field in fields:
                if field in facts:
                    index.setdefault(_normalized_value_key(facts[field]), []).append(entry)
            promotion = facts.get("promotion")
            if isinstance(promotion, dict):
                for field in fields:
                    if field in promotion:
                        index.setdefault(_normalized_value_key(promotion[field]), []).append(entry)
    return index


def _entries_with_value(evidence: list[EvidenceEntry], field: str, value: Any) -> list[EvidenceEntry]:
    value_key = _normalized_value_key(value)
    return [
        entry
        for entry in evidence
        if any(
            field in facts and _normalized_value_key(facts[field]) == value_key
            for facts in _iter_fact_items(entry.normalized_facts)
        )
    ]


def _single_subject_id(entries: list[EvidenceEntry]) -> str | None:
    subject_ids = _unique([entry.entity_id for entry in entries if entry.entity_id])
    return subject_ids[0] if len(subject_ids) == 1 else None


def _source_agent(entries: list[EvidenceEntry], fallback_agent: str) -> str:
    agents = _unique([entry.agent_name for entry in entries if entry.agent_name in CANONICAL_AGENT_NAMES])
    return agents[0] if agents else fallback_agent


def _unique(values: list[Any]) -> list[Any]:
    seen = set()
    unique_values = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return unique_values


def _normalized_value_key(value: Any) -> tuple:
    if isinstance(value, float):
        return ("number", f"{value:.2f}")
    if isinstance(value, int) and not isinstance(value, bool):
        return ("number", f"{float(value):.2f}")
    if isinstance(value, str):
        return ("string", value.strip().lower())
    if isinstance(value, bool):
        return ("bool", value)
    return ("json", str(value))


def _normalize_text(value: Any) -> str:
    return str(value).strip().lower()
