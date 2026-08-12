import asyncio
import copy
import json
from types import SimpleNamespace

from scout.agents.claims import ClaimType
from scout.agents.evidence import (
    EvidenceEntry,
    ProposedClaim,
    get_evidence_entries,
    record_tool_call,
    start_evidence_context,
)
from scout.agents.supervisor import _run_single_intent, _run_single_intent_streaming
from scout.agents.verification import RejectionCode, verify_claims


def evidence(
    *,
    evidence_id="ev_1",
    success=True,
    agent_name="recommend_agent",
    tool_name="search",
    entity_type="product",
    entity_id="P001",
    facts=None,
):
    return EvidenceEntry(
        evidence_id=evidence_id,
        sequence=0,
        sub_intent_id="sub_1",
        attempt_number=0,
        agent_name=agent_name,
        tool_name=tool_name,
        validated_args={},
        entity_type=entity_type,
        entity_id=entity_id,
        normalized_facts=facts or {"product_id": entity_id, "name": "Dress", "price": 79.99},
        success=success,
    )


def claim(
    claim_type,
    *,
    claim_id="cl_1",
    subject_id="P001",
    field,
    value,
    evidence_ids=None,
    source_agent="recommend_agent",
):
    return ProposedClaim(
        claim_id=claim_id,
        claim_type=claim_type,
        subject_id=subject_id,
        field=field,
        value=value,
        evidence_ids=evidence_ids if evidence_ids is not None else ["ev_1"],
        source_agent=source_agent,
    )


def reason(result, claim_id="cl_1"):
    return next(rejected.reason_code for rejected in result.rejected_claims if rejected.claim_id == claim_id)


def message(name, content, msg_type="ai"):
    return SimpleNamespace(name=name, content=content, type=msg_type)


def test_color_specific_inventory_verification_rejects_other_variant_evidence():
    entries = [
        evidence(
            facts={
                "product_id": "P001",
                "name": "Black Midi Dress",
                "size": "M",
                "color": "floral",
                "quantity": 3,
                "in_stock": True,
            },
            agent_name="inventory_agent",
            tool_name="stock",
        )
    ]
    requested_black_m = claim(
        ClaimType.INVENTORY_AVAILABILITY,
        subject_id="product:P001:size:M:color:black",
        field="in_stock",
        value=False,
        source_agent="inventory_agent",
    )

    result = verify_claims(
        proposed_claims=[requested_black_m],
        evidence_entries=entries,
        customer_message="Is the black midi dress in a medium?",
    )

    assert not result.verified
    assert reason(result) == RejectionCode.SUBJECT_MISMATCH.value


def test_color_specific_zero_quantity_inventory_verifies_unavailable():
    entries = [
        evidence(
            facts={
                "product_id": "P001",
                "name": "Black Midi Dress",
                "size": "M",
                "color": "black",
                "quantity": 0,
                "in_stock": False,
            },
            agent_name="inventory_agent",
            tool_name="stock",
        )
    ]
    requested_black_m = claim(
        ClaimType.INVENTORY_AVAILABILITY,
        subject_id="product:P001:size:M:color:black",
        field="in_stock",
        value=False,
        source_agent="inventory_agent",
    )

    result = verify_claims(
        proposed_claims=[requested_black_m],
        evidence_entries=entries,
        customer_message="Is the black midi dress in a medium?",
    )

    assert result.verified
    assert result.approved_claim_ids == ["cl_1"]


def tool_message(name, content):
    return message(name, content, msg_type="tool")


def test_every_claim_is_approved_or_rejected_and_order_is_deterministic():
    claims = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", field="name", value="Dress"),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_bad", field="price", value=80.00),
    ]

    result = verify_claims(proposed_claims=claims, evidence_entries=[evidence()], customer_message="")

    assert result.verified is False
    assert result.approved_claim_ids == ["cl_name"]
    assert [rejected.claim_id for rejected in result.rejected_claims] == ["cl_bad"]


def test_general_rejection_cases_and_inputs_are_not_mutated():
    ev = evidence()
    claims = [
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_empty", field="price", value=79.99, evidence_ids=[]),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_unknown", field="price", value=79.99, evidence_ids=["ev_missing"]),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_failed", field="price", value=79.99, evidence_ids=["ev_failed"]),
        claim("made_up", claim_id="cl_type", field="price", value=79.99),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_field", field="name", value="Dress"),
    ]
    failed = evidence(evidence_id="ev_failed", success=False)
    original_claims = copy.deepcopy(claims)
    original_evidence = copy.deepcopy([ev, failed])

    result = verify_claims(proposed_claims=claims, evidence_entries=[ev, failed], customer_message="")

    assert reason(result, "cl_empty") == RejectionCode.MISSING_EVIDENCE
    assert reason(result, "cl_unknown") == RejectionCode.UNKNOWN_EVIDENCE_ID
    assert reason(result, "cl_failed") == RejectionCode.UNSUCCESSFUL_EVIDENCE
    assert reason(result, "cl_type") == RejectionCode.UNSUPPORTED_CLAIM_TYPE
    assert reason(result, "cl_field") == RejectionCode.UNSUPPORTED_FIELD
    assert claims == original_claims
    assert [ev, failed] == original_evidence


def test_matching_and_mismatched_product_name_price_and_money_normalization():
    ev = evidence(facts={"product_id": "P001", "name": "Black Dress", "price": "79.990"})
    claims = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", field="name", value=" black dress "),
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_bad_name", field="name", value="Blue Dress"),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_price", field="price", value=79.99),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_bad_price", field="price", value=80.00),
    ]

    result = verify_claims(proposed_claims=claims, evidence_entries=[ev], customer_message="")

    assert result.approved_claim_ids == ["cl_name", "cl_price"]
    assert reason(result, "cl_bad_name") == RejectionCode.VALUE_MISMATCH
    assert reason(result, "cl_bad_price") == RejectionCode.VALUE_MISMATCH


def test_rating_requires_rating_and_score_cannot_verify_rating():
    ev = evidence(facts={"product_id": "P001", "name": "Dress", "score": 0.99})
    rating = claim(ClaimType.PRODUCT_RATING, field="rating", value=4.8)

    result = verify_claims(proposed_claims=[rating], evidence_entries=[ev], customer_message="")

    assert reason(result) == RejectionCode.FIELD_NOT_PRESENT


def test_promotion_name_and_price_require_explicit_promotion_evidence():
    promo_ev = evidence(facts={"product_id": "P001", "promotion": {"name": "Summer Sale", "discounted_price": 68}})
    plain_ev = evidence(evidence_id="ev_plain", facts={"product_id": "P001", "price": 68})
    claims = [
        claim(ClaimType.PROMOTION, claim_id="cl_promo_name", field="promotion_name", value="summer sale"),
        claim(ClaimType.PROMOTION, claim_id="cl_promo_price", field="promotion_price", value=68.00),
        claim(ClaimType.PROMOTION, claim_id="cl_plain_discount", field="promotion_price", value=68.00, evidence_ids=["ev_plain"]),
    ]

    result = verify_claims(proposed_claims=claims, evidence_entries=[promo_ev, plain_ev], customer_message="")

    assert result.approved_claim_ids == ["cl_promo_name", "cl_promo_price"]
    assert reason(result, "cl_plain_discount") == RejectionCode.FIELD_NOT_PRESENT


def test_inventory_quantity_availability_and_store_specific_matching():
    ev = evidence(
        agent_name="inventory_agent",
        tool_name="stock",
        entity_id="P001",
        facts={"product_id": "P001", "store_id": "S001", "quantity": 3},
    )
    zero_ev = evidence(
        evidence_id="ev_zero",
        agent_name="inventory_agent",
        tool_name="stock",
        entity_id="P002",
        facts={"product_id": "P002", "store_id": "S001", "quantity": 0},
    )
    product_ev = evidence(evidence_id="ev_product", facts={"product_id": "P001", "name": "Dress"})
    claims = [
        claim(ClaimType.INVENTORY_QUANTITY, claim_id="cl_qty", field="quantity", value=3, source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_QUANTITY, claim_id="cl_qty_bad", field="quantity", value=4, source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_AVAILABILITY, claim_id="cl_in", field="in_stock", value=True, source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_AVAILABILITY, claim_id="cl_out", subject_id="P002", field="in_stock", value=False, evidence_ids=["ev_zero"], source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_AVAILABILITY, claim_id="cl_presence", field="in_stock", value=True, evidence_ids=["ev_product"], source_agent="recommend_agent"),
        claim(ClaimType.INVENTORY_QUANTITY, claim_id="cl_wrong_store", subject_id="S002", field="quantity", value=3, source_agent="inventory_agent"),
    ]

    result = verify_claims(proposed_claims=claims, evidence_entries=[ev, zero_ev, product_ev], customer_message="")

    assert result.approved_claim_ids == ["cl_qty", "cl_in", "cl_out"]
    assert reason(result, "cl_qty_bad") == RejectionCode.VALUE_MISMATCH
    assert reason(result, "cl_presence") == RejectionCode.WRONG_AGENT_DOMAIN
    assert reason(result, "cl_wrong_store") == RejectionCode.SUBJECT_MISMATCH


def test_size_specific_stock_cannot_verify_other_size_or_general_search():
    stock_m = evidence(
        agent_name="inventory_agent",
        tool_name="stock",
        entity_id="P001",
        facts={"items": [{"product_id": "P001", "size": "M", "quantity": 0, "in_stock": False}]},
    )
    search_only = evidence(evidence_id="ev_search", agent_name="inventory_agent", tool_name="search", facts={"product_id": "P001", "name": "Black Midi Dress"})
    claims = [
        claim(ClaimType.INVENTORY_AVAILABILITY, claim_id="cl_m", subject_id="product:P001:size:M", field="in_stock", value=False, source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_AVAILABILITY, claim_id="cl_l", subject_id="product:P001:size:L", field="in_stock", value=False, source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_AVAILABILITY, claim_id="cl_general", subject_id="P001", field="in_stock", value=True, evidence_ids=["ev_search"], source_agent="inventory_agent"),
    ]

    result = verify_claims(proposed_claims=claims, evidence_entries=[stock_m, search_only], customer_message="")

    assert result.approved_claim_ids == ["cl_m"]
    assert reason(result, "cl_l") == RejectionCode.SUBJECT_MISMATCH
    assert reason(result, "cl_general") == RejectionCode.FIELD_NOT_PRESENT


def test_store_stock_cannot_verify_other_store_or_pickup():
    store_ev = evidence(
        agent_name="inventory_agent",
        tool_name="stores",
        entity_id="P001",
        facts={"stores": [{"product_id": "P001", "store_id": "S01", "store_name": "Maple Grove", "quantity": 2, "in_stock": True}]},
    )
    claims = [
        claim(ClaimType.INVENTORY_QUANTITY, claim_id="cl_maple", subject_id="product:P001:store:S01", field="quantity", value=2, source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_QUANTITY, claim_id="cl_plymouth", subject_id="product:P001:store:S99", field="quantity", value=2, source_agent="inventory_agent"),
        claim(ClaimType.PICKUP_AVAILABILITY, claim_id="cl_pickup", subject_id="product:P001:store:S01", field="pickup_available", value=True, source_agent="inventory_agent"),
    ]

    result = verify_claims(proposed_claims=claims, evidence_entries=[store_ev], customer_message="")

    assert result.approved_claim_ids == ["cl_maple"]
    assert reason(result, "cl_plymouth") == RejectionCode.SUBJECT_MISMATCH
    assert reason(result, "cl_pickup") == RejectionCode.FIELD_NOT_PRESENT


def test_store_and_fulfillment_rules():
    ev = evidence(
        agent_name="inventory_agent",
        tool_name="fulfillment_options",
        entity_type="store",
        entity_id="S001",
        facts={
            "store_id": "S001",
            "store_name": "Maple Grove",
            "distance_miles": 4.24,
            "pickup_available": True,
            "delivery_available": False,
            "pickup_estimate": "tomorrow",
        },
    )
    stock_only = evidence(
        evidence_id="ev_stock_only",
        agent_name="inventory_agent",
        tool_name="stock",
        entity_type="product",
        entity_id="P001",
        facts={"product_id": "P001", "quantity": 5},
    )
    claims = [
        claim(ClaimType.STORE_IDENTITY, claim_id="cl_store", subject_id="S001", field="store_name", value="maple grove", source_agent="inventory_agent"),
        claim(ClaimType.STORE_IDENTITY, claim_id="cl_store_bad", subject_id="S001", field="store_name", value="Oakdale", source_agent="inventory_agent"),
        claim(ClaimType.STORE_DISTANCE, claim_id="cl_distance", subject_id="S001", field="distance_miles", value=4.2, source_agent="inventory_agent"),
        claim(ClaimType.STORE_DISTANCE, claim_id="cl_distance_bad", subject_id="S001", field="distance_miles", value=4.4, source_agent="inventory_agent"),
        claim(ClaimType.PICKUP_AVAILABILITY, claim_id="cl_pickup", subject_id="S001", field="pickup_available", value=True, source_agent="inventory_agent"),
        claim(ClaimType.DELIVERY_AVAILABILITY, claim_id="cl_delivery", subject_id="S001", field="delivery_available", value=False, source_agent="inventory_agent"),
        claim(ClaimType.DELIVERY_AVAILABILITY, claim_id="cl_pickup_not_delivery", subject_id="S001", field="delivery_available", value=True, source_agent="inventory_agent"),
        claim(ClaimType.FULFILLMENT_ESTIMATE, claim_id="cl_estimate", subject_id="S001", field="pickup_estimate", value="tomorrow", source_agent="inventory_agent"),
        claim(ClaimType.FULFILLMENT_ESTIMATE, claim_id="cl_today", subject_id="S001", field="delivery_estimate", value="today", source_agent="inventory_agent"),
        claim(ClaimType.PICKUP_AVAILABILITY, claim_id="cl_stock_not_pickup", subject_id="P001", field="pickup_available", value=True, evidence_ids=["ev_stock_only"], source_agent="inventory_agent"),
    ]

    result = verify_claims(proposed_claims=claims, evidence_entries=[ev, stock_only], customer_message="")

    assert result.approved_claim_ids == ["cl_store", "cl_distance", "cl_pickup", "cl_delivery", "cl_estimate"]
    assert reason(result, "cl_store_bad") == RejectionCode.VALUE_MISMATCH
    assert reason(result, "cl_distance_bad") == RejectionCode.VALUE_MISMATCH
    assert reason(result, "cl_pickup_not_delivery") == RejectionCode.VALUE_MISMATCH
    assert reason(result, "cl_today") == RejectionCode.FIELD_NOT_PRESENT
    assert reason(result, "cl_stock_not_pickup") == RejectionCode.FIELD_NOT_PRESENT


def test_order_tracking_payment_and_generic_history_rules():
    ev = evidence(agent_name="order_agent", tool_name="orders", entity_type="order", entity_id="O1001", facts={"order_id": "O1001", "status": "shipped", "tracking_number": "TRK123456", "payment_status": "paid"})
    generic = evidence(evidence_id="ev_generic", agent_name="order_agent", tool_name="order_history", entity_type="order", entity_id=None, facts={"status": "shipped"})
    no_tracking = evidence(evidence_id="ev_no_tracking", agent_name="order_agent", tool_name="orders", entity_type="order", entity_id="O1001", facts={"order_id": "O1001", "status": "shipped"})
    claims = [
        claim(ClaimType.ORDER_STATUS, claim_id="cl_status", subject_id="O1001", field="status", value="shipped", source_agent="order_agent"),
        claim(ClaimType.ORDER_STATUS, claim_id="cl_wrong_order", subject_id="O2002", field="status", value="shipped", source_agent="order_agent"),
        claim(ClaimType.ORDER_STATUS, claim_id="cl_status_bad", subject_id="O1001", field="status", value="delivered", source_agent="order_agent"),
        claim(ClaimType.ORDER_TRACKING, claim_id="cl_tracking", subject_id="O1001", field="tracking_number", value="trk123456", source_agent="order_agent"),
        claim(ClaimType.ORDER_TRACKING, claim_id="cl_tracking_missing", subject_id="O1001", field="tracking_number", value="TRK999999", evidence_ids=["ev_no_tracking"], source_agent="order_agent"),
        claim(ClaimType.PAYMENT_STATUS, claim_id="cl_payment", subject_id="O1001", field="payment_status", value="paid", source_agent="order_agent"),
        claim(ClaimType.ORDER_STATUS, claim_id="cl_generic", subject_id=None, field="status", value="shipped", evidence_ids=["ev_generic"], source_agent="order_agent"),
    ]

    result = verify_claims(proposed_claims=claims, evidence_entries=[ev, generic, no_tracking], customer_message="")

    assert result.approved_claim_ids == ["cl_status", "cl_tracking", "cl_payment"]
    assert reason(result, "cl_wrong_order") == RejectionCode.SUBJECT_MISMATCH
    assert reason(result, "cl_status_bad") == RejectionCode.VALUE_MISMATCH
    assert reason(result, "cl_tracking_missing") == RejectionCode.FIELD_NOT_PRESENT
    assert reason(result, "cl_generic") == RejectionCode.AMBIGUOUS_SUBJECT


def test_returns_and_policy_rules():
    order_ev = evidence(agent_name="order_agent", tool_name="return_eligibility", entity_type="order", entity_id="O1001", facts={"order_id": "O1001", "return_eligible": True})
    policy_ev = evidence(evidence_id="ev_policy", agent_name="policy_agent", tool_name="retrieve_policy_chunks", entity_type="policy", entity_id="returns", facts={"statement": "Returns accepted within 30 days.", "policy_name": "Returns", "policy_version": "2026-01", "source_document": "returns.md", "source_section": "window"})
    claims = [
        claim(ClaimType.RETURN_ELIGIBILITY, claim_id="cl_return", subject_id="O1001", field="return_eligible", value=True, source_agent="order_agent"),
        claim(ClaimType.RETURN_ELIGIBILITY, claim_id="cl_generic_policy_return", subject_id="O1001", field="return_eligible", value=True, evidence_ids=["ev_policy"], source_agent="policy_agent"),
        claim(ClaimType.POLICY_STATEMENT, claim_id="cl_statement", subject_id="returns", field="statement", value="Returns accepted within 30 days.", evidence_ids=["ev_policy"], source_agent="policy_agent"),
        claim(ClaimType.POLICY_STATEMENT, claim_id="cl_prose", subject_id="returns", field="statement", value="You can return anything whenever.", evidence_ids=["ev_policy"], source_agent="policy_agent"),
        claim(ClaimType.POLICY_STATEMENT, claim_id="cl_doc", subject_id="returns", field="source_document", value="returns.md", evidence_ids=["ev_policy"], source_agent="policy_agent"),
        claim(ClaimType.POLICY_STATEMENT, claim_id="cl_section", subject_id="returns", field="source_section", value="window", evidence_ids=["ev_policy"], source_agent="policy_agent"),
        claim(ClaimType.POLICY_STATEMENT, claim_id="cl_version", subject_id="returns", field="policy_version", value="2026-01", evidence_ids=["ev_policy"], source_agent="policy_agent"),
        claim(ClaimType.POLICY_STATEMENT, claim_id="cl_missing_policy", subject_id="returns", field="policy_name", value="Shipping", evidence_ids=["ev_policy"], source_agent="policy_agent"),
    ]

    result = verify_claims(proposed_claims=claims, evidence_entries=[order_ev, policy_ev], customer_message="")

    assert result.approved_claim_ids == ["cl_return", "cl_statement", "cl_doc", "cl_section", "cl_version"]
    assert reason(result, "cl_generic_policy_return") == RejectionCode.WRONG_AGENT_DOMAIN
    assert reason(result, "cl_prose") == RejectionCode.INSUFFICIENT_POLICY_SUPPORT
    assert reason(result, "cl_missing_policy") == RejectionCode.INSUFFICIENT_POLICY_SUPPORT


def test_external_offer_rules():
    external_ev = evidence(agent_name="external_offer_agent", tool_name="search_external_offers", entity_type="external_product", entity_id="EX001", facts={"external_product_id": "EX001", "name": "Market Dress", "price": 70, "vendor_name": "Nordstrom Rack", "click_url": "/affiliate/click/EX001"})
    internal_ev = evidence(evidence_id="ev_internal", facts={"product_id": "P001", "name": "Dress", "price": 70})
    claims = [
        claim(ClaimType.EXTERNAL_OFFER_IDENTITY, claim_id="cl_ext_name", subject_id="EX001", field="product_name", value="Market Dress", source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_VENDOR, claim_id="cl_vendor", subject_id="EX001", field="vendor", value="nordstrom rack", source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_PRICE, claim_id="cl_ext_price", subject_id="EX001", field="price", value=70.00, source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_VENDOR, claim_id="cl_internal_vendor", subject_id="P001", field="vendor", value="Scout", evidence_ids=["ev_internal"], source_agent="recommend_agent"),
        claim(ClaimType.INVENTORY_AVAILABILITY, claim_id="cl_external_inventory", subject_id="EX001", field="in_stock", value=True, source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_PRICE, claim_id="cl_unlabeled", subject_id="EX001", field="price", value=70.00, source_agent="recommend_agent"),
    ]

    result = verify_claims(proposed_claims=claims, evidence_entries=[external_ev, internal_ev], customer_message="")

    assert result.approved_claim_ids == ["cl_ext_name", "cl_vendor", "cl_ext_price"]
    assert reason(result, "cl_internal_vendor") == RejectionCode.WRONG_AGENT_DOMAIN
    assert reason(result, "cl_external_inventory") == RejectionCode.WRONG_AGENT_DOMAIN
    assert reason(result, "cl_unlabeled") == RejectionCode.EXTERNAL_OFFER_NOT_LABELED


def test_budget_rules_are_narrow():
    ev = evidence(facts={"product_id": "P001", "price": 79.99})
    under_budget = claim(ClaimType.PRODUCT_PRICE, claim_id="cl_under", field="price", value=79.99)
    over_budget = claim(ClaimType.PRODUCT_PRICE, claim_id="cl_over", field="price", value=79.99)
    no_budget = claim(ClaimType.PRODUCT_PRICE, claim_id="cl_none", field="price", value=79.99)
    ambiguous = claim(ClaimType.PRODUCT_PRICE, claim_id="cl_ambiguous", field="price", value=79.99)
    external = claim(ClaimType.EXTERNAL_OFFER_PRICE, claim_id="cl_external_budget", subject_id="EX001", field="price", value=120, evidence_ids=["ev_external"], source_agent="external_offer_agent")
    ext_ev = evidence(evidence_id="ev_external", agent_name="external_offer_agent", tool_name="search_external_offers", entity_type="external_product", entity_id="EX001", facts={"external_product_id": "EX001", "price": 120})

    assert verify_claims(proposed_claims=[under_budget], evidence_entries=[ev], customer_message="under $100").verified
    assert reason(verify_claims(proposed_claims=[over_budget], evidence_entries=[ev], customer_message="no more than $50"), "cl_over") == RejectionCode.BUDGET_EXCEEDED
    assert verify_claims(proposed_claims=[no_budget], evidence_entries=[ev], customer_message="I saw $50 yesterday").verified
    assert verify_claims(proposed_claims=[ambiguous], evidence_entries=[ev], customer_message="between $50 and $100").verified
    assert verify_claims(proposed_claims=[external], evidence_entries=[ext_ev], customer_message="under $100").verified


def test_agent_attribution_and_correction_agent_selection():
    inventory_claim = claim(ClaimType.INVENTORY_QUANTITY, claim_id="cl_inv", field="quantity", value=9, source_agent="inventory_agent")
    inventory_result = verify_claims(proposed_claims=[inventory_claim], evidence_entries=[evidence(agent_name="inventory_agent", tool_name="stock", facts={"product_id": "P001", "quantity": 3})], customer_message="")

    mixed_claim = claim(ClaimType.PRODUCT_PRICE, claim_id="cl_price", field="price", value=88, source_agent="recommend_agent")
    mixed_result = verify_claims(
        proposed_claims=[inventory_claim, mixed_claim],
        evidence_entries=[evidence(agent_name="inventory_agent", tool_name="stock", facts={"product_id": "P001", "quantity": 3}), evidence()],
        customer_message="",
    )

    assert inventory_result.rejected_claims[0].responsible_agent == "inventory_agent"
    assert inventory_result.correction_agent == "inventory_agent"
    assert mixed_result.correction_agent is None


def test_safety_does_not_use_customer_reply_or_other_context_as_proof():
    unsupported = claim(ClaimType.PRODUCT_PRICE, field="price", value=49.99, evidence_ids=[])

    result = verify_claims(
        proposed_claims=[unsupported],
        evidence_entries=[],
        customer_message="I need this under $50 and the reply says $49.99",
    )
    unknown = verify_claims(proposed_claims=[claim(ClaimType.PRODUCT_PRICE, field="price", value=49.99, evidence_ids=["ev_other"])], evidence_entries=[], customer_message="")

    assert reason(result) == RejectionCode.MISSING_EVIDENCE
    assert reason(unknown) == RejectionCode.UNKNOWN_EVIDENCE_ID
    assert all("exception" not in rejected.explanation.lower() for rejected in result.rejected_claims)
    assert all("api_key" not in rejected.explanation.lower() for rejected in result.rejected_claims)


def test_non_streaming_calls_broad_verification_locally_and_preserves_behavior(monkeypatch):
    captured = {}
    price_calls = []

    class FakeApp:
        async def ainvoke(self, payload, config):
            record_tool_call(
                tool_name="search",
                validated_args={"query": "dress"},
                success=True,
                result=[{"product_id": "P001", "name": "Dress", "price": 79.99}],
                agent_name="recommend_agent",
            )
            return {"messages": [*payload["messages"], tool_message("search", json.dumps([{"product_id": "P001", "name": "Dress", "price": 79.99}])), message("recommend_agent", "Dress is $79.99.")]}

    def fake_verify_claims(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(verified=True, approved_claim_ids=[], rejected_claims=[], missing_evidence=[], correction_agent=None)

    def fake_price(*args, **kwargs):
        price_calls.append((args, kwargs))
        return True, "ok"

    monkeypatch.setattr("scout.agents.supervisor.verify_claims", fake_verify_claims)
    monkeypatch.setattr("scout.agents.supervisor.verify_price_grounding", fake_price)
    history = []

    reply, products = asyncio.run(_run_single_intent(FakeApp(), history, "dress", debug=False))

    assert reply == "I couldn’t verify a reliable product result for that request."
    assert products == []
    assert captured["proposed_claims"]
    assert captured["evidence_entries"]
    assert price_calls
    assert history == [{"role": "user", "content": "dress"}, {"role": "assistant", "content": "I couldn’t verify a reliable product result for that request."}]
    assert get_evidence_entries() == []


def test_streaming_calls_same_broad_verification_and_preserves_sse_shape(monkeypatch):
    captured = {}
    price_calls = []

    class FakeStreamingApp:
        async def astream_events(self, payload, version, config):
            record_tool_call(
                tool_name="search",
                validated_args={"query": "dress"},
                success=True,
                result=[{"product_id": "P001", "name": "Dress", "price": 79.99}],
                agent_name="recommend_agent",
            )
            yield {
                "event": "on_chain_end",
                "name": "LangGraph",
                "metadata": {},
                "data": {"output": {"messages": [*payload["messages"], tool_message("search", json.dumps([{"product_id": "P001", "name": "Dress", "price": 79.99}])), message("recommend_agent", "Dress is $79.99.")]}},
            }

    def fake_verify_claims(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(verified=True, approved_claim_ids=[], rejected_claims=[], missing_evidence=[], correction_agent=None)

    def fake_price(*args, **kwargs):
        price_calls.append((args, kwargs))
        return True, "ok"

    monkeypatch.setattr("scout.agents.supervisor.verify_claims", fake_verify_claims)
    monkeypatch.setattr("scout.agents.supervisor.verify_price_grounding", fake_price)
    history = []

    events = asyncio.run(_collect_async(_run_single_intent_streaming(FakeStreamingApp(), history, "dress")))

    assert events == [
        ("progress", "understanding_request"),
        ("progress", "verifying_claims"),
        ("progress", "preparing_response"),
        ("result", ("I couldn’t verify a reliable product result for that request.", [])),
    ]
    assert captured["proposed_claims"]
    assert captured["evidence_entries"]
    assert price_calls
    assert history == [{"role": "user", "content": "dress"}, {"role": "assistant", "content": "I couldn’t verify a reliable product result for that request."}]
    assert get_evidence_entries() == []


async def _collect_async(async_iterable):
    return [item async for item in async_iterable]
