from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import StrEnum
from typing import Any

from scout.agents.claims import CANONICAL_AGENT_NAMES, ClaimType
from scout.agents.evidence import EvidenceEntry, ProposedClaim, RejectedClaim, VerificationResult

PRICE_PATTERN = re.compile(r"\$(\d+(?:\.\d{1,2})?)")
BUDGET_PATTERN = re.compile(
    r"\b(?:under|below|maximum|max|no more than)\s*\$?(\d+(?:\.\d{1,2})?)\b",
    re.IGNORECASE,
)
DISTANCE_TOLERANCE_MILES = Decimal("0.05")


class RejectionCode(StrEnum):
    MISSING_EVIDENCE = "missing_evidence"
    UNKNOWN_EVIDENCE_ID = "unknown_evidence_id"
    UNSUCCESSFUL_EVIDENCE = "unsuccessful_evidence"
    UNSUPPORTED_CLAIM_TYPE = "unsupported_claim_type"
    UNSUPPORTED_FIELD = "unsupported_field"
    SUBJECT_MISMATCH = "subject_mismatch"
    FIELD_NOT_PRESENT = "field_not_present"
    VALUE_MISMATCH = "value_mismatch"
    AMBIGUOUS_SUBJECT = "ambiguous_subject"
    BUDGET_EXCEEDED = "budget_exceeded"
    WRONG_AGENT_DOMAIN = "wrong_agent_domain"
    EXTERNAL_OFFER_NOT_LABELED = "external_offer_not_labeled"
    INSUFFICIENT_POLICY_SUPPORT = "insufficient_policy_support"


SUPPORTED_FIELDS = {
    ClaimType.PRODUCT_IDENTITY.value: {"name"},
    ClaimType.PRODUCT_PRICE.value: {"price"},
    ClaimType.PRODUCT_RATING.value: {"rating"},
    ClaimType.PROMOTION.value: {"promotion_name", "promotion_price"},
    ClaimType.INVENTORY_QUANTITY.value: {"quantity"},
    ClaimType.INVENTORY_AVAILABILITY.value: {"in_stock"},
    ClaimType.STORE_IDENTITY.value: {"store_name"},
    ClaimType.STORE_DISTANCE.value: {"distance_miles"},
    ClaimType.PICKUP_AVAILABILITY.value: {"pickup_available"},
    ClaimType.DELIVERY_AVAILABILITY.value: {"delivery_available"},
    ClaimType.FULFILLMENT_ESTIMATE.value: {"pickup_estimate", "delivery_estimate"},
    ClaimType.ORDER_STATUS.value: {"status"},
    ClaimType.ORDER_TRACKING.value: {"tracking_number"},
    ClaimType.PAYMENT_STATUS.value: {"payment_status"},
    ClaimType.RETURN_ELIGIBILITY.value: {"return_eligible"},
    ClaimType.POLICY_STATEMENT.value: {"statement", "policy_name", "policy_version", "source_document", "source_section"},
    ClaimType.EXTERNAL_OFFER_IDENTITY.value: {"product_name", "url_or_offer_id"},
    ClaimType.EXTERNAL_OFFER_PRICE.value: {"price"},
    ClaimType.EXTERNAL_OFFER_VENDOR.value: {"vendor"},
    ClaimType.EXTERNAL_OFFER_USE_CASE.value: {"use_case"},
    ClaimType.EXTERNAL_OFFER_ATTRIBUTE.value: {"attribute", "waterproof"},
    ClaimType.EXTERNAL_OFFER_AVAILABILITY.value: {"availability"},
    ClaimType.ACCESS_DENIED.value: {"message"},
}

DOMAIN_AGENTS = {
    ClaimType.PRODUCT_IDENTITY.value: {"recommend_agent", "inventory_agent"},
    ClaimType.PRODUCT_PRICE.value: {"recommend_agent"},
    ClaimType.PRODUCT_RATING.value: {"recommend_agent"},
    ClaimType.PROMOTION.value: {"recommend_agent"},
    ClaimType.INVENTORY_QUANTITY.value: {"inventory_agent"},
    ClaimType.INVENTORY_AVAILABILITY.value: {"inventory_agent"},
    ClaimType.STORE_IDENTITY.value: {"inventory_agent"},
    ClaimType.STORE_DISTANCE.value: {"inventory_agent"},
    ClaimType.PICKUP_AVAILABILITY.value: {"inventory_agent"},
    ClaimType.DELIVERY_AVAILABILITY.value: {"inventory_agent"},
    ClaimType.FULFILLMENT_ESTIMATE.value: {"inventory_agent"},
    ClaimType.ORDER_STATUS.value: {"order_agent"},
    ClaimType.ORDER_TRACKING.value: {"order_agent"},
    ClaimType.PAYMENT_STATUS.value: {"order_agent"},
    ClaimType.RETURN_ELIGIBILITY.value: {"order_agent"},
    ClaimType.POLICY_STATEMENT.value: {"policy_agent"},
    ClaimType.EXTERNAL_OFFER_IDENTITY.value: {"external_offer_agent"},
    ClaimType.EXTERNAL_OFFER_PRICE.value: {"external_offer_agent"},
    ClaimType.EXTERNAL_OFFER_VENDOR.value: {"external_offer_agent"},
    ClaimType.EXTERNAL_OFFER_USE_CASE.value: {"external_offer_agent"},
    ClaimType.EXTERNAL_OFFER_ATTRIBUTE.value: {"external_offer_agent"},
    ClaimType.EXTERNAL_OFFER_AVAILABILITY.value: {"external_offer_agent"},
    ClaimType.ACCESS_DENIED.value: {"order_agent", "inventory_agent", "recommend_agent", "policy_agent", "external_offer_agent"},
}

STATUS_CANONICAL = {
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "delivered": "delivered",
    "pending": "pending",
    "processing": "processing",
    "returned": "returned",
    "shipped": "shipped",
}

# Real, fixed policy constants that are legitimate to mention in a reply
# even though they don't come from a product/promotion record this turn —
# shipping fees and thresholds, grounded in the actual shipping.md policy
# (see services/store_service.py, which uses these same values). Treating
# these as "unverified" was a real false-positive bug: the fulfillment
# tool correctly returned these numbers, but verification only knew about
# product prices, not policy constants.
KNOWN_POLICY_PRICES = {"0.00", "5.99", "14.99", "75.00"}


def _collect_known_prices(products: list[dict]) -> set[str]:
    known = set()
    for p in products:
        if "price" in p and p["price"] is not None:
            known.add(f"{float(p['price']):.2f}")
        promo = p.get("promotion")
        if promo and "discounted_price" in promo:
            known.add(f"{float(promo['discounted_price']):.2f}")
    return known


def verify_price_grounding(
    reply: str, products: list[dict], customer_message: str = ""
) -> tuple[bool, str]:
    """Checks that every dollar amount mentioned in the reply matches a
    real price from this turn's actual tool results, the customer's own
    stated budget, OR a known fixed policy constant (shipping fees/
    thresholds) — the last of these because those numbers are real and
    correct even though they don't come from a product record. If NO
    products were returned this turn, there's no product-price ground
    truth to check — treated as unverifiable-but-passing to avoid false
    positives on non-product answers."""
    if not products:
        return True, "no product data this turn — price check not applicable"

    known_prices = _collect_known_prices(products)
    customer_stated_prices = {f"{float(m):.2f}" for m in PRICE_PATTERN.findall(customer_message)}
    mentioned_prices = {f"{float(m):.2f}" for m in PRICE_PATTERN.findall(reply)}

    allowed_prices = known_prices | customer_stated_prices | KNOWN_POLICY_PRICES
    unverified = mentioned_prices - allowed_prices

    if unverified:
        return False, (
            f"reply mentions price(s) {sorted(unverified)} that do not match "
            f"any real price/promotion from this turn's tool results "
            f"({sorted(known_prices)}), the customer's own stated budget "
            f"({sorted(customer_stated_prices)}), or a known policy constant"
        )

    return True, "all mentioned prices verified against tool results, customer budget, or policy"


def verify_claims(
    *,
    proposed_claims: list[ProposedClaim],
    evidence_entries: list[EvidenceEntry],
    customer_message: str,
) -> VerificationResult:
    """Deterministically verifies ProposedClaim objects against current
    sub-intent EvidenceEntry objects only. It does not consult model
    messages, response text, session history, databases, networks, or LLMs."""
    claims = [claim.model_copy(deep=True) for claim in proposed_claims]
    evidence = [entry.model_copy(deep=True) for entry in evidence_entries]
    evidence_by_id = {entry.evidence_id: entry for entry in evidence}
    max_budget = _parse_max_budget(customer_message or "")

    approved_claim_ids: list[str] = []
    rejected_claims: list[RejectedClaim] = []
    missing_evidence: list[str] = []

    for claim in claims:
        rejection = _verify_one_claim(claim, evidence_by_id, max_budget)
        if rejection is None:
            if claim.claim_id not in approved_claim_ids:
                approved_claim_ids.append(claim.claim_id)
        else:
            rejected_claims.append(rejection)
            missing_evidence.extend(rejection.missing_evidence)

    return VerificationResult(
        verified=not rejected_claims,
        approved_claim_ids=approved_claim_ids,
        rejected_claims=rejected_claims,
        missing_evidence=_unique(missing_evidence),
        correction_agent=_select_correction_agent(rejected_claims),
    )


def _verify_one_claim(
    claim: ProposedClaim,
    evidence_by_id: dict[str, EvidenceEntry],
    max_budget: Decimal | None,
) -> RejectedClaim | None:
    if claim.claim_type not in SUPPORTED_FIELDS:
        return _reject(claim, RejectionCode.UNSUPPORTED_CLAIM_TYPE, "Claim type is not supported.")

    if claim.field not in SUPPORTED_FIELDS[claim.claim_type]:
        return _reject(claim, RejectionCode.UNSUPPORTED_FIELD, "Claim field is not supported for this claim type.")

    if not claim.evidence_ids:
        return _reject(
            claim,
            RejectionCode.MISSING_EVIDENCE,
            "Claim has no linked tool evidence.",
            [f"{claim.claim_type}:{claim.field}"],
        )

    linked_entries = []
    for evidence_id in claim.evidence_ids:
        entry = evidence_by_id.get(evidence_id)
        if entry is None:
            return _reject(
                claim,
                RejectionCode.UNKNOWN_EVIDENCE_ID,
                "Claim references evidence outside the current evidence set.",
                [evidence_id],
            )
        if not entry.success:
            return _reject(claim, RejectionCode.UNSUCCESSFUL_EVIDENCE, "Claim references unsuccessful tool evidence.")
        linked_entries.append(entry)

    wrong_domain = _domain_rejection(claim, linked_entries)
    if wrong_domain is not None:
        return wrong_domain

    matches_subject = []
    matches_field = []
    for entry in linked_entries:
        for facts in _iter_fact_items(entry.normalized_facts):
            if _subject_matches(claim, entry, facts):
                matches_subject.append((entry, facts))
                if _claim_value_matches(claim, facts):
                    matches_field.append((entry, facts))

    if not matches_subject:
        if claim.subject_id is None:
            return _reject(claim, RejectionCode.AMBIGUOUS_SUBJECT, "Claim subject cannot be resolved from evidence.")
        return _reject(claim, RejectionCode.SUBJECT_MISMATCH, "Claim subject does not match linked evidence.")

    if not any(_field_present_for_claim(claim, facts) for _, facts in matches_subject):
        reason = (
            RejectionCode.INSUFFICIENT_POLICY_SUPPORT
            if claim.claim_type == ClaimType.POLICY_STATEMENT.value
            else RejectionCode.FIELD_NOT_PRESENT
        )
        return _reject(claim, reason, "Required fact is not present in linked evidence.", [f"{claim.claim_type}:{claim.field}"])

    if not matches_field:
        reason = (
            RejectionCode.INSUFFICIENT_POLICY_SUPPORT
            if claim.claim_type == ClaimType.POLICY_STATEMENT.value
            else RejectionCode.VALUE_MISMATCH
        )
        return _reject(claim, reason, "Claim value does not match linked evidence.")

    if claim.claim_type == ClaimType.PRODUCT_PRICE.value and max_budget is not None:
        price = _money(claim.value)
        if price is not None and price > max_budget:
            return _reject(claim, RejectionCode.BUDGET_EXCEEDED, "Verified product price exceeds the stated maximum budget.")

    return None


def _domain_rejection(claim: ProposedClaim, entries: list[EvidenceEntry]) -> RejectedClaim | None:
    expected_agents = DOMAIN_AGENTS.get(claim.claim_type, set())
    if any(entry.agent_name not in expected_agents for entry in entries):
        return _reject(claim, RejectionCode.WRONG_AGENT_DOMAIN, "Linked evidence comes from the wrong specialist domain.")

    has_external_facts = any(_entry_has_external_facts(entry) for entry in entries)
    if claim.claim_type in {
        ClaimType.PRODUCT_IDENTITY.value,
        ClaimType.PRODUCT_PRICE.value,
        ClaimType.PRODUCT_RATING.value,
        ClaimType.PROMOTION.value,
        ClaimType.INVENTORY_QUANTITY.value,
        ClaimType.INVENTORY_AVAILABILITY.value,
        ClaimType.PICKUP_AVAILABILITY.value,
        ClaimType.DELIVERY_AVAILABILITY.value,
        ClaimType.FULFILLMENT_ESTIMATE.value,
    } and has_external_facts:
        return _reject(claim, RejectionCode.EXTERNAL_OFFER_NOT_LABELED, "External offer evidence cannot support Scout-owned inventory claims.")

    if claim.claim_type in {
        ClaimType.EXTERNAL_OFFER_IDENTITY.value,
        ClaimType.EXTERNAL_OFFER_PRICE.value,
        ClaimType.EXTERNAL_OFFER_VENDOR.value,
    }:
        if claim.source_agent != "external_offer_agent" or not has_external_facts:
            return _reject(claim, RejectionCode.EXTERNAL_OFFER_NOT_LABELED, "External offer claim is not clearly labeled with external-offer evidence.")

    return None


def _claim_value_matches(claim: ProposedClaim, facts: dict[str, Any]) -> bool:
    for value in _candidate_values(claim, facts):
        if _values_match(claim, value):
            return True
    return False


def _candidate_values(claim: ProposedClaim, facts: dict[str, Any]) -> list[Any]:
    if claim.claim_type == ClaimType.PRODUCT_IDENTITY.value and claim.field == "name":
        return [facts["name"]] if "name" in facts and not facts.get("external_product_id") else []
    if claim.claim_type == ClaimType.PRODUCT_PRICE.value and claim.field == "price":
        return [facts["price"]] if "price" in facts and not facts.get("external_product_id") else []
    if claim.claim_type == ClaimType.PRODUCT_RATING.value and claim.field == "rating":
        return [facts["rating"]] if "rating" in facts else []
    if claim.claim_type == ClaimType.PROMOTION.value:
        promotion = facts.get("promotion")
        values = []
        if isinstance(promotion, dict):
            if claim.field == "promotion_name" and "name" in promotion:
                values.append(promotion["name"])
            if claim.field == "promotion_price" and "discounted_price" in promotion:
                values.append(promotion["discounted_price"])
        if claim.field == "promotion_price" and "discounted_price" in facts:
            values.append(facts["discounted_price"])
        return values
    if claim.claim_type == ClaimType.INVENTORY_QUANTITY.value:
        return [facts["quantity"]] if "quantity" in facts else []
    if claim.claim_type == ClaimType.INVENTORY_AVAILABILITY.value:
        values = []
        for field in ("in_stock", "available"):
            if field in facts:
                values.append(bool(facts[field]))
        quantity = facts.get("quantity")
        if isinstance(quantity, int | float) and not isinstance(quantity, bool):
            values.append(quantity > 0)
        return values
    if claim.claim_type == ClaimType.STORE_IDENTITY.value:
        # A real store row uses "store_name"; the "requested store had
        # no matching inventory" case (see claims.py's STORE_IDENTITY
        # fallback branch) has no real store row at all, only the
        # customer's own requested_store_name at the top level of
        # facts — both are legitimate, verified sources for this
        # claim type, so both are accepted as candidate values.
        values = []
        if "store_name" in facts:
            values.append(facts["store_name"])
        if "requested_store_name" in facts:
            values.append(facts["requested_store_name"])
        return values
    if claim.claim_type == ClaimType.STORE_DISTANCE.value:
        return [facts["distance_miles"]] if "distance_miles" in facts else []
    if claim.claim_type == ClaimType.PICKUP_AVAILABILITY.value:
        return [bool(facts["pickup_available"])] if "pickup_available" in facts else []
    if claim.claim_type == ClaimType.DELIVERY_AVAILABILITY.value:
        return [bool(facts["delivery_available"])] if "delivery_available" in facts else []
    if claim.claim_type == ClaimType.FULFILLMENT_ESTIMATE.value:
        return [facts[claim.field]] if claim.field in facts else []
    if claim.claim_type == ClaimType.ORDER_STATUS.value:
        return [facts["status"]] if "status" in facts and facts.get("order_id") else []
    if claim.claim_type == ClaimType.ORDER_TRACKING.value:
        value = facts.get("tracking_number")
        return [value] if value else []
    if claim.claim_type == ClaimType.PAYMENT_STATUS.value:
        return [facts["payment_status"]] if "payment_status" in facts else []
    if claim.claim_type == ClaimType.RETURN_ELIGIBILITY.value:
        for field in ("return_eligible", "likely_eligible", "eligible"):
            if field in facts:
                return [bool(facts[field])]
        return []
    if claim.claim_type == ClaimType.POLICY_STATEMENT.value:
        return [facts[claim.field]] if claim.field in facts else []
    if claim.claim_type == ClaimType.EXTERNAL_OFFER_IDENTITY.value:
        if claim.field == "product_name" and facts.get("external_product_id") and "name" in facts:
            return [facts["name"]]
        if claim.field == "url_or_offer_id" and facts.get("external_product_id"):
            return [facts.get("click_url"), facts.get("external_product_id")]
        return []
    if claim.claim_type == ClaimType.EXTERNAL_OFFER_PRICE.value:
        return [facts["price"]] if facts.get("external_product_id") and "price" in facts else []
    if claim.claim_type == ClaimType.EXTERNAL_OFFER_VENDOR.value:
        return [facts["vendor_name"]] if facts.get("external_product_id") and "vendor_name" in facts else []
    if claim.claim_type == ClaimType.EXTERNAL_OFFER_USE_CASE.value:
        return facts.get("use_cases", []) if facts.get("external_product_id") and isinstance(facts.get("use_cases"), list) else []
    if claim.claim_type == ClaimType.EXTERNAL_OFFER_ATTRIBUTE.value:
        if not facts.get("external_product_id"):
            return []
        if claim.field == "waterproof":
            return [facts["waterproof"]] if "waterproof" in facts else []
        return facts.get("attributes", []) if isinstance(facts.get("attributes"), list) else []
    if claim.claim_type == ClaimType.EXTERNAL_OFFER_AVAILABILITY.value:
        return [facts["availability"]] if facts.get("external_product_id") and "availability" in facts else []
    if claim.claim_type == ClaimType.ACCESS_DENIED.value:
        return [facts["message"]] if facts.get("found") is False and "message" in facts else []
    return []


def _field_present_for_claim(claim: ProposedClaim, facts: dict[str, Any]) -> bool:
    return bool(_candidate_values_without_value_filter(claim, facts))


def _candidate_values_without_value_filter(claim: ProposedClaim, facts: dict[str, Any]) -> list[Any]:
    values = _candidate_values(claim, facts)
    if values:
        return values
    if claim.claim_type == ClaimType.PRODUCT_RATING.value and "score" in facts:
        return []
    return []


def _values_match(claim: ProposedClaim, evidence_value: Any) -> bool:
    if evidence_value is None or claim.value is None:
        return False
    if claim.claim_type in {
        ClaimType.PRODUCT_PRICE.value,
        ClaimType.PROMOTION.value,
        ClaimType.EXTERNAL_OFFER_PRICE.value,
    } and claim.field in {"price", "promotion_price"}:
        return _money(claim.value) is not None and _money(claim.value) == _money(evidence_value)
    if claim.claim_type == ClaimType.STORE_DISTANCE.value:
        claimed = _decimal(claim.value)
        actual = _decimal(evidence_value)
        return claimed is not None and actual is not None and abs(claimed - actual) <= DISTANCE_TOLERANCE_MILES
    if claim.claim_type in {
        ClaimType.INVENTORY_QUANTITY.value,
    }:
        return isinstance(claim.value, int) and not isinstance(claim.value, bool) and claim.value == evidence_value
    if claim.claim_type in {
        ClaimType.INVENTORY_AVAILABILITY.value,
        ClaimType.PICKUP_AVAILABILITY.value,
        ClaimType.DELIVERY_AVAILABILITY.value,
        ClaimType.RETURN_ELIGIBILITY.value,
    }:
        return isinstance(claim.value, bool) and claim.value is evidence_value
    if claim.claim_type == ClaimType.ORDER_STATUS.value:
        return _canonical_status(claim.value) is not None and _canonical_status(claim.value) == _canonical_status(evidence_value)
    if claim.claim_type == ClaimType.ACCESS_DENIED.value:
        # Exact-string match only, deliberately — this is a real,
        # tool-provided denial message (e.g. "Sign in to view order
        # details."), never interpreted or categorized, only passed
        # through verbatim against its own source evidence. No
        # canonical vocabulary involved, unlike ORDER_STATUS above —
        # this claim type exists specifically because reusing
        # ORDER_STATUS/POLICY_STATEMENT for this shape caused real,
        # distinct matching-logic failures during live debugging.
        return _normalize_string(claim.value) == _normalize_string(evidence_value)
    return _normalize_string(claim.value) == _normalize_string(evidence_value)


def _subject_matches(claim: ProposedClaim, entry: EvidenceEntry, facts: dict[str, Any]) -> bool:
    subject_ids = _subject_ids(entry, facts)
    if claim.subject_id is None:
        return len(subject_ids) == 1
    return claim.subject_id in subject_ids


def _subject_ids(entry: EvidenceEntry, facts: dict[str, Any]) -> set[str]:
    subject_ids = set()
    for key in ("product_id", "external_product_id", "order_id", "store_id", "customer_id", "source_document", "policy_name"):
        value = facts.get(key)
        if isinstance(value, str) and value.strip():
            subject_ids.add(value)
            if key in {"source_document", "policy_name"}:
                subject_ids.add(value.rsplit(".", 1)[0].strip().lower())
    if subject_ids:
        product_id = facts.get("product_id")
        store_id = facts.get("store_id")
        size = facts.get("size")
        if isinstance(product_id, str) and product_id.strip():
            if isinstance(store_id, str) and store_id.strip():
                subject_ids.add(f"product:{product_id}:store:{store_id}")
            if isinstance(size, str) and size.strip():
                subject = f"product:{product_id}:size:{size.upper()}"
                color = facts.get("color")
                if isinstance(color, str) and color.strip():
                    subject_ids.add(f"{subject}:color:{color.strip().lower()}")
                subject_ids.add(subject)
        return subject_ids
    return {entry.entity_id} if entry.entity_id else set()


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


def _entry_has_external_facts(entry: EvidenceEntry) -> bool:
    return entry.agent_name == "external_offer_agent" or any(
        bool(facts.get("external_product_id"))
        for facts in _iter_fact_items(entry.normalized_facts)
    )


def _parse_max_budget(customer_message: str) -> Decimal | None:
    matches = BUDGET_PATTERN.findall(customer_message)
    if len(matches) != 1:
        return None
    return _money(matches[0])


def _money(value: Any) -> Decimal | None:
    decimal_value = _decimal(value)
    if decimal_value is None:
        return None
    return decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _normalize_string(value: Any) -> str:
    return str(value).strip().lower()


def _canonical_status(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return STATUS_CANONICAL.get(value.strip().lower())


def _reject(
    claim: ProposedClaim,
    reason_code: RejectionCode,
    explanation: str,
    missing_evidence: list[str] | None = None,
) -> RejectedClaim:
    responsible_agent = claim.source_agent if claim.source_agent in CANONICAL_AGENT_NAMES else None
    return RejectedClaim(
        claim_id=claim.claim_id,
        reason_code=reason_code.value,
        explanation=explanation,
        responsible_agent=responsible_agent,
        missing_evidence=missing_evidence or [],
    )


def _select_correction_agent(rejected_claims: list[RejectedClaim]) -> str | None:
    agents = _unique([
        rejected.responsible_agent
        for rejected in rejected_claims
        if rejected.responsible_agent in CANONICAL_AGENT_NAMES and rejected.responsible_agent != "supervisor"
    ])
    return agents[0] if len(agents) == 1 else None


def _unique(values: list[Any]) -> list[Any]:
    seen = set()
    output = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output
