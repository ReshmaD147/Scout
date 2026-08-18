import asyncio
import json
from types import SimpleNamespace

from scout.agents import rendering
from scout.agents.claims import ClaimType
from scout.agents.evidence import EvidenceEntry, ProposedClaim, VerificationResult, get_evidence_entries, record_tool_call
from scout.agents.intent_splitter import merge_answers
from scout.agents.rendering import DOMAIN_FALLBACKS, final_safety_scan, render_verified_response
from scout.agents.supervisor import _run_single_intent, _run_single_intent_streaming
from scout.agents.verification import verify_claims
from scout.api.chat import ChatResponse


def claim(
    claim_type,
    *,
    claim_id,
    subject_id,
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
        evidence_ids=evidence_ids or ["ev_1"],
        source_agent=source_agent,
    )


def result(approved_ids):
    return VerificationResult(verified=True, approved_claim_ids=approved_ids)


def evidence(
    *,
    evidence_id="ev_1",
    agent_name="recommend_agent",
    entity_id="P001",
    facts=None,
):
    return EvidenceEntry(
        evidence_id=evidence_id,
        sequence=0,
        sub_intent_id="sub_1",
        attempt_number=0,
        agent_name=agent_name,
        tool_name="search",
        validated_args={},
        entity_type="product",
        entity_id=entity_id,
        normalized_facts=facts or {},
        success=True,
    )


def message(name, content, msg_type="ai"):
    return SimpleNamespace(name=name, content=content, type=msg_type)


def tool_message(name, content):
    return message(name, content, msg_type="tool")


def test_color_specific_unavailable_inventory_renders_from_approved_claims():
    claims = [
        claim(
            ClaimType.PRODUCT_IDENTITY,
            claim_id="cl_name",
            subject_id="P001",
            field="name",
            value="Black Midi Dress",
            source_agent="inventory_agent",
        ),
        claim(
            ClaimType.INVENTORY_AVAILABILITY,
            claim_id="cl_stock",
            subject_id="product:P001:size:M:color:black",
            field="in_stock",
            value=False,
            source_agent="inventory_agent",
        ),
    ]
    reply, products = render_verified_response(
        original_reply="Raw prose says something else.",
        products=[],
        proposed_claims=claims,
        verification_result=result(["cl_name", "cl_stock"]),
        customer_message="Is the black midi dress in a medium?",
    )

    assert reply == (
        "The Black Midi Dress is out of stock in black, medium. "
        "I can check nearby stores, check online or delivery availability, or find similar products."
    )
    assert products == []
    assert "P001" not in reply


def test_internal_product_id_retained_in_structured_product_but_hidden_from_prose():
    claims = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", subject_id="P001", field="name", value="Black Midi Dress"),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_price", subject_id="P001", field="price", value=79.99),
    ]

    reply, products = render_verified_response(
        original_reply="P001 is great.",
        products=[{"product_id": "P001", "name": "Black Midi Dress", "price": 79.99}],
        proposed_claims=claims,
        verification_result=result(["cl_name", "cl_price"]),
        customer_message="Recommend a dress under $80",
    )

    assert "P001" not in reply
    assert products[0]["product_id"] == "P001"


def test_variant_unavailable_overrides_general_store_inventory_wording():
    claims = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", subject_id="P001", field="name", value="Black Midi Dress", source_agent="inventory_agent"),
        claim(ClaimType.STORE_IDENTITY, claim_id="cl_store", subject_id="S01", field="store_name", value="Maple Grove", source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_AVAILABILITY, claim_id="cl_variant", subject_id="product:P001:size:M:color:black", field="in_stock", value=False, source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_QUANTITY, claim_id="cl_store_qty", subject_id="product:P001:store:S01", field="quantity", value=2, source_agent="inventory_agent"),
    ]

    reply, products = render_verified_response(
        original_reply="P001 is available at Maple Grove.",
        products=[],
        proposed_claims=claims,
        verification_result=result(["cl_name", "cl_store", "cl_variant", "cl_store_qty"]),
        customer_message="Is it available at Maple Grove?",
    )

    assert reply == (
        "Maple Grove has other inventory for that item, but the black, medium option is currently out of stock. "
        "I can check nearby stores, check online or delivery availability, or find similar products."
    )
    assert products == []
    assert "P001" not in reply


def test_nearby_store_variant_unavailable_renders_not_found_nearby():
    claims = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", subject_id="P001", field="name", value="Black Midi Dress", source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_AVAILABILITY, claim_id="cl_variant", subject_id="product:P001:size:M:color:black", field="in_stock", value=False, source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_QUANTITY, claim_id="cl_qty", subject_id="product:P001:size:M:color:black", field="quantity", value=0, source_agent="inventory_agent"),
    ]

    reply, products = render_verified_response(
        original_reply="Black Midi Dress is in stock.",
        products=[],
        proposed_claims=claims,
        verification_result=result(["cl_name", "cl_variant", "cl_qty"]),
        customer_message="Check nearby stores for Black Midi Dress in black, size M",
    )

    assert reply == (
        "Black Midi Dress in black, medium is not available in nearby store inventory. "
        "I can check nearby stores, check online or delivery availability, or find similar products."
    )
    assert products == []
    assert "P001" not in reply


def test_online_delivery_variant_unavailable_renders_delivery_specific_reply():
    claims = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", subject_id="P001", field="name", value="Black Midi Dress", source_agent="inventory_agent"),
        claim(ClaimType.DELIVERY_AVAILABILITY, claim_id="cl_delivery", subject_id="product:P001:size:M:color:black", field="delivery_available", value=False, source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_QUANTITY, claim_id="cl_qty", subject_id="product:P001:size:M:color:black", field="quantity", value=0, source_agent="inventory_agent"),
    ]

    reply, products = render_verified_response(
        original_reply="Black Midi Dress is available online.",
        products=[],
        proposed_claims=claims,
        verification_result=result(["cl_name", "cl_delivery", "cl_qty"]),
        customer_message="Check online or delivery availability for Black Midi Dress in black, size M",
    )

    assert reply == (
        "The Black Midi Dress in black, medium is unavailable for online or delivery fulfillment. "
        "I can find similar products."
    )
    assert products == []


def test_online_delivery_variant_available_renders_delivery_specific_reply():
    claims = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", subject_id="P001", field="name", value="Black Midi Dress", source_agent="inventory_agent"),
        claim(ClaimType.DELIVERY_AVAILABILITY, claim_id="cl_delivery", subject_id="product:P001:size:S:color:black", field="delivery_available", value=True, source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_QUANTITY, claim_id="cl_qty", subject_id="product:P001:size:S:color:black", field="quantity", value=3, source_agent="inventory_agent"),
    ]

    reply, _products = render_verified_response(
        original_reply="Black Midi Dress is available online.",
        products=[],
        proposed_claims=claims,
        verification_result=result(["cl_name", "cl_delivery", "cl_qty"]),
        customer_message="Check online or delivery availability for Black Midi Dress in black, size S",
    )

    assert reply == "The Black Midi Dress in black, small is available for delivery, with 3 units available."


def test_exact_store_variant_available_renders_narrow_store_variant_fact():
    claims = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", subject_id="P001", field="name", value="Black Midi Dress", source_agent="inventory_agent"),
        claim(ClaimType.STORE_IDENTITY, claim_id="cl_store", subject_id="S01", field="store_name", value="Maple Grove", source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_QUANTITY, claim_id="cl_qty", subject_id="product:P001:size:M:color:black:store:S01", field="quantity", value=2, source_agent="inventory_agent"),
    ]

    reply, _ = render_verified_response(
        original_reply="",
        products=[],
        proposed_claims=claims,
        verification_result=result(["cl_name", "cl_store", "cl_qty"]),
        customer_message="Is it available at Maple Grove?",
    )

    assert reply == "The Black Midi Dress is available at Maple Grove in black, medium, with 2 units remaining."


def test_products_render_only_approved_identity_price_and_optional_fields_without_mutation():
    products = [
        {"product_id": "P001", "name": "Dress", "price": 79.99, "rating": 4.8, "promotion": {"name": "Sale", "discounted_price": 68.0}},
        {"product_id": "P002", "name": "Shoes", "price": 50.0},
    ]
    original = json.loads(json.dumps(products))
    claims = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", subject_id="P001", field="name", value="Dress"),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_price", subject_id="P001", field="price", value=79.99),
        claim(ClaimType.PRODUCT_RATING, claim_id="cl_rating", subject_id="P001", field="rating", value=4.8),
        claim(ClaimType.PROMOTION, claim_id="cl_promo_name", subject_id="P001", field="promotion_name", value="Sale"),
        claim(ClaimType.PROMOTION, claim_id="cl_promo_price", subject_id="P001", field="promotion_price", value=68.0),
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_p2_name", subject_id="P002", field="name", value="Shoes"),
    ]

    reply, rendered = render_verified_response(
        original_reply="Dress is $79.99. Shoes are $50.",
        products=products,
        proposed_claims=claims,
        verification_result=result(["cl_name", "cl_price", "cl_rating", "cl_promo_name", "cl_promo_price", "cl_p2_name"]),
        customer_message="show products",
    )

    assert rendered == [{"product_id": "P001", "name": "Dress", "source": "internal", "price": 79.99, "rating": 4.8, "promotion": {"name": "Sale", "discounted_price": 68.0}}]
    assert "Shoes" not in reply
    assert "$50" not in reply
    assert "placeholder" not in reply.lower()
    assert products == original


def test_product_recommendation_wording_is_customer_friendly_and_approved_only():
    products = [
        {"product_id": "P003", "name": "Wrap Dress", "price": 68.0, "rating": 4.4, "promotion": {"discounted_price": 57.8}},
    ]
    claims = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", subject_id="P003", field="name", value="Wrap Dress"),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_price", subject_id="P003", field="price", value=68.0),
        claim(ClaimType.PRODUCT_RATING, claim_id="cl_rating", subject_id="P003", field="rating", value=4.4),
        claim(ClaimType.PROMOTION, claim_id="cl_promo_price", subject_id="P003", field="promotion_price", value=57.8),
    ]

    reply, rendered = render_verified_response(
        original_reply="Wrap Dress is $12 and has secret same-day delivery.",
        products=products,
        proposed_claims=claims,
        verification_result=result(["cl_name", "cl_price", "cl_rating", "cl_promo_price"]),
        customer_message="Recommend a dress under $80",
    )

    assert reply == "We have the Wrap Dress for $68.00, on sale for $57.80, rated 4.4."
    assert "$12" not in reply
    assert "same-day" not in reply
    assert rendered == [
        {
            "product_id": "P003",
            "name": "Wrap Dress",
            "source": "internal",
            "price": 68.0,
            "rating": 4.4,
            "promotion": {"discounted_price": 57.8},
            "image_url": "/static/products/P003.jpg",
        }
    ]


def test_multi_product_recommendation_uses_natural_summary_without_repetition():
    products = [
        {"product_id": "P003", "name": "Wrap Dress", "price": 68.0, "promotion": {"discounted_price": 57.8}},
        {"product_id": "P004", "name": "Slip Dress", "price": 62.5, "promotion": {"discounted_price": 53.12}},
        {"product_id": "P001", "name": "Black Midi Dress", "price": 79.99, "promotion": {"discounted_price": 67.99}},
    ]
    claims = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_wrap_name", subject_id="P003", field="name", value="Wrap Dress"),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_wrap_price", subject_id="P003", field="price", value=68.0),
        claim(ClaimType.PROMOTION, claim_id="cl_wrap_promo", subject_id="P003", field="promotion_price", value=57.8),
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_slip_name", subject_id="P004", field="name", value="Slip Dress"),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_slip_price", subject_id="P004", field="price", value=62.5),
        claim(ClaimType.PROMOTION, claim_id="cl_slip_promo", subject_id="P004", field="promotion_price", value=53.12),
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_black_name", subject_id="P001", field="name", value="Black Midi Dress"),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_black_price", subject_id="P001", field="price", value=79.99),
        claim(ClaimType.PROMOTION, claim_id="cl_black_promo", subject_id="P001", field="promotion_price", value=67.99),
    ]

    reply, rendered = render_verified_response(
        original_reply="P001 is cheap and ships by drone.",
        products=products,
        proposed_claims=claims,
        verification_result=result([approved.claim_id for approved in claims]),
        customer_message="Recommend a dress under $80",
    )

    assert reply == (
        "I found 3 dresses in our catalog: Wrap Dress for $68.00, Slip Dress for $62.50, "
        "and Black Midi Dress for $79.99. Sale prices are shown on the cards."
    )
    assert "Scout option" not in reply
    assert "P001" not in reply
    assert "drone" not in reply
    assert [product["product_id"] for product in rendered] == ["P003", "P004", "P001"]


def test_verified_internal_product_uses_local_seeded_image_when_available():
    claims = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", subject_id="P003", field="name", value="Wrap Dress"),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_price", subject_id="P003", field="price", value=68.0),
    ]

    _reply, rendered = render_verified_response(
        original_reply="Wrap Dress is available.",
        products=[{"product_id": "P003", "name": "Wrap Dress", "price": 68.0, "image_url": "https://picsum.photos/seed/P003/400/500"}],
        proposed_claims=claims,
        verification_result=result(["cl_name", "cl_price"]),
        customer_message="Recommend a dress under $80",
    )

    assert rendered == [
        {
            "product_id": "P003",
            "name": "Wrap Dress",
            "source": "internal",
            "price": 68.0,
            "image_url": "/static/products/P003.jpg",
        }
    ]


def test_rejected_price_rating_and_promotion_never_render():
    claims = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", subject_id="P001", field="name", value="Dress"),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_price", subject_id="P001", field="price", value=79.99),
        claim(ClaimType.PRODUCT_RATING, claim_id="cl_rating", subject_id="P001", field="rating", value=4.8),
        claim(ClaimType.PROMOTION, claim_id="cl_promo", subject_id="P001", field="promotion_name", value="Sale"),
    ]

    reply, rendered = render_verified_response(
        original_reply="Dress is $79.99, rated 4.8, with a Sale promotion.",
        products=[{"product_id": "P001", "name": "Dress", "price": 79.99, "rating": 4.8, "promotion": {"name": "Sale"}}],
        proposed_claims=claims,
        verification_result=result(["cl_name", "cl_price"]),
        customer_message="dress",
    )

    assert rendered == [{"product_id": "P001", "name": "Dress", "source": "internal", "price": 79.99}]
    assert "4.8" not in reply
    assert "Sale" not in reply


def test_inventory_and_fulfillment_render_independent_approved_facts_and_omit_conflicts():
    claims = [
        claim(ClaimType.INVENTORY_QUANTITY, claim_id="cl_qty", subject_id="P001", field="quantity", value=3, source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_AVAILABILITY, claim_id="cl_stock", subject_id="P001", field="in_stock", value=True, source_agent="inventory_agent"),
        claim(ClaimType.PICKUP_AVAILABILITY, claim_id="cl_pickup", subject_id="P001", field="pickup_available", value=True, source_agent="inventory_agent"),
        claim(ClaimType.DELIVERY_AVAILABILITY, claim_id="cl_delivery", subject_id="P001", field="delivery_available", value=False, source_agent="inventory_agent"),
        claim(ClaimType.STORE_DISTANCE, claim_id="cl_distance", subject_id="S001", field="distance_miles", value=4.2, source_agent="inventory_agent"),
        claim(ClaimType.FULFILLMENT_ESTIMATE, claim_id="cl_today", subject_id="P001", field="pickup_estimate", value="today", source_agent="inventory_agent"),
        claim(ClaimType.PICKUP_AVAILABILITY, claim_id="cl_conflict_a", subject_id="P002", field="pickup_available", value=True, source_agent="inventory_agent"),
        claim(ClaimType.PICKUP_AVAILABILITY, claim_id="cl_conflict_b", subject_id="P002", field="pickup_available", value=False, source_agent="inventory_agent"),
    ]

    reply, _ = render_verified_response(
        original_reply="Pickup is available today and delivery is available.",
        products=[],
        proposed_claims=claims,
        verification_result=result(["cl_qty", "cl_stock", "cl_pickup", "cl_delivery", "cl_distance", "cl_conflict_a", "cl_conflict_b"]),
        customer_message="is it available",
    )

    assert "We have 3 in stock for the requested product." in reply
    assert "That item is currently in stock." in reply
    assert "Pickup is available for the requested product." in reply
    assert "Delivery is not available for the requested product." in reply
    assert "That store is about 4.2 miles away." in reply
    assert "today" not in reply
    assert "P002" not in reply


def test_size_availability_renders_request_specific_available_and_unavailable():
    claims = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", subject_id="P001", field="name", value="Black Midi Dress", source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_AVAILABILITY, claim_id="cl_stock", subject_id="product:P001:size:M", field="in_stock", value=True, source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_QUANTITY, claim_id="cl_qty", subject_id="product:P001:size:M", field="quantity", value=2, source_agent="inventory_agent"),
    ]

    reply, products = render_verified_response(
        original_reply="Raw says Black Midi Dress is $79.99 and available.",
        products=[{"product_id": "P001", "name": "Black Midi Dress", "price": 79.99, "rating": 4.3}],
        proposed_claims=claims,
        verification_result=result(["cl_name", "cl_stock", "cl_qty"]),
        customer_message="Is the black midi dress in a medium?",
    )

    assert reply == "The Black Midi Dress has 2 units available in medium."
    assert products == []

    unavailable = claims[:1] + [
        claim(ClaimType.INVENTORY_AVAILABILITY, claim_id="cl_out", subject_id="product:P001:size:M", field="in_stock", value=False, source_agent="inventory_agent"),
    ]
    reply, _ = render_verified_response(
        original_reply="Raw says it is unavailable.",
        products=[],
        proposed_claims=unavailable,
        verification_result=result(["cl_name", "cl_out"]),
        customer_message="Is the black midi dress in a medium?",
    )

    assert reply == (
        "The Black Midi Dress is out of stock in medium. "
        "I can check nearby stores, check online or delivery availability, or find similar products."
    )


def test_size_availability_combines_multiple_sizes_naturally():
    claims = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", subject_id="P001", field="name", value="Black Midi Dress", source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_QUANTITY, claim_id="cl_qty_s", subject_id="product:P001:size:S:color:black", field="quantity", value=3, source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_AVAILABILITY, claim_id="cl_stock_m", subject_id="product:P001:size:M:color:black", field="in_stock", value=False, source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_QUANTITY, claim_id="cl_qty_l", subject_id="product:P001:size:L:color:black", field="quantity", value=3, source_agent="inventory_agent"),
    ]

    reply, products = render_verified_response(
        original_reply="Raw says small and large are available but medium is out.",
        products=[],
        proposed_claims=claims,
        verification_result=result(["cl_name", "cl_qty_s", "cl_stock_m", "cl_qty_l"]),
        customer_message="Check size availability for Black Midi Dress",
    )

    assert reply == (
        "The Black Midi Dress in black: small has 3 units and large has 3 units available, "
        "but medium is out of stock. Want me to check nearby stores or find a similar option?"
    )
    assert products == []


def test_store_availability_renders_request_specific_answer_without_price_replacement():
    claims = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", subject_id="P001", field="name", value="Black Midi Dress", source_agent="inventory_agent"),
        claim(ClaimType.STORE_IDENTITY, claim_id="cl_store", subject_id="S01", field="store_name", value="Maple Grove", source_agent="inventory_agent"),
        claim(ClaimType.INVENTORY_QUANTITY, claim_id="cl_qty", subject_id="product:P001:store:S01", field="quantity", value=2, source_agent="inventory_agent"),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_price", subject_id="P001", field="price", value=79.99, source_agent="recommend_agent"),
        claim(ClaimType.PRODUCT_RATING, claim_id="cl_rating", subject_id="P001", field="rating", value=4.3, source_agent="recommend_agent"),
    ]

    reply, products = render_verified_response(
        original_reply="Raw says product facts.",
        products=[{"product_id": "P001", "name": "Black Midi Dress", "price": 79.99, "rating": 4.3}],
        proposed_claims=claims,
        verification_result=result(["cl_name", "cl_store", "cl_qty", "cl_price", "cl_rating"]),
        customer_message="Is the black midi dress available at Maple Grove?",
    )

    assert reply == "The Black Midi Dress is in stock at Maple Grove — 2 available."
    assert products == [
        {
            "product_id": "P001",
            "name": "Black Midi Dress",
            "source": "internal",
            "price": 79.99,
            "rating": 4.3,
            "image_url": "/static/products/P001.jpg",
        }
    ]
    assert "$79.99" not in reply


def test_orders_render_specific_approved_claims_without_payment_method_details():
    claims = [
        claim(ClaimType.ORDER_STATUS, claim_id="cl_status", subject_id="O1001", field="status", value="shipped", source_agent="order_agent"),
        claim(ClaimType.ORDER_TRACKING, claim_id="cl_tracking", subject_id="O1001", field="tracking_number", value="TRK123456", source_agent="order_agent"),
        claim(ClaimType.PAYMENT_STATUS, claim_id="cl_payment", subject_id="O1001", field="payment_status", value="paid", source_agent="order_agent"),
        claim(ClaimType.RETURN_ELIGIBILITY, claim_id="cl_return", subject_id="O1001", field="return_eligible", value=True, source_agent="order_agent"),
        claim(ClaimType.ORDER_STATUS, claim_id="cl_wrong", subject_id="O2002", field="status", value="delivered", source_agent="order_agent"),
    ]

    reply, _ = render_verified_response(
        original_reply="Order O1001 shipped with card ending 4242. Order O2002 delivered.",
        products=[],
        proposed_claims=claims,
        verification_result=result(["cl_status", "cl_tracking", "cl_payment", "cl_return"]),
        customer_message="order O1001",
    )

    assert "Order O1001 has shipped." in reply
    assert "TRK123456" in reply
    assert "Payment for order O1001 is paid." in reply
    assert "eligible for a return" in reply
    assert "4242" not in reply
    assert "O2002" not in reply


def test_policy_rendering_requires_approved_statement_and_hides_source_metadata():
    claims = [
        claim(ClaimType.POLICY_STATEMENT, claim_id="cl_statement", subject_id="returns", field="statement", value="Returns accepted within 30 days.", source_agent="policy_agent"),
        claim(ClaimType.POLICY_STATEMENT, claim_id="cl_doc", subject_id="returns", field="source_document", value="returns.md", source_agent="policy_agent"),
        claim(ClaimType.POLICY_STATEMENT, claim_id="cl_version", subject_id="returns", field="policy_version", value="2026-01", source_agent="policy_agent"),
        claim(ClaimType.POLICY_STATEMENT, claim_id="cl_section", subject_id="returns", field="source_section", value="window", source_agent="policy_agent"),
        claim(ClaimType.RETURN_ELIGIBILITY, claim_id="cl_order_return", subject_id="O1001", field="return_eligible", value=True, source_agent="order_agent"),
    ]

    reply, _ = render_verified_response(
        original_reply="You can return anything whenever. Order O1001 is returnable.",
        products=[],
        proposed_claims=claims,
        verification_result=result(["cl_statement", "cl_doc", "cl_version", "cl_section"]),
        customer_message="return policy",
    )

    assert "Returns accepted within 30 days." in reply
    assert "returns.md" not in reply
    assert "2026-01" not in reply
    assert "section window" not in reply
    assert "anything whenever" not in reply
    assert "O1001" not in reply


def test_return_policy_is_summarized_for_customer_without_source_dump():
    statement = (
        "# Return Policy\n\n"
        "Items may be returned within 30 days of delivery for a full refund, provided "
        "they are unworn, unwashed, and have original tags attached. Opened or worn "
        "items are not eligible for return unless defective. Sale items marked \"Final Sale\" cannot be returned or exchanged."
    )
    claims = [
        claim(ClaimType.POLICY_STATEMENT, claim_id="cl_statement", subject_id="returns", field="statement", value=statement, source_agent="policy_agent"),
        claim(ClaimType.POLICY_STATEMENT, claim_id="cl_doc", subject_id="returns", field="source_document", value="returns.md", source_agent="policy_agent"),
    ]

    reply, _ = render_verified_response(
        original_reply=statement,
        products=[],
        proposed_claims=claims,
        verification_result=result(["cl_statement", "cl_doc"]),
        customer_message="Can I return an opened item?",
    )

    assert reply == (
        "Opened or worn items usually aren’t eligible for a return unless they’re defective. "
        "Returns are accepted within 30 days as long as the item is unworn, unwashed, and still has its original tags. "
        "Want me to check if a specific order qualifies?"
    )
    assert "Policy source" not in reply
    assert "returns.md" not in reply


def test_refund_policy_is_summarized_for_customer_without_source_dump():
    statement = (
        "# Refund Policy\n\n"
        "Refunds are issued to the original payment method within 5-7 business days "
        "after we receive and inspect the returned item. Store credit refunds are "
        "processed within 1 business day instead. Shipping fees are non-refundable unless the return is due to our error."
    )
    claims = [
        claim(ClaimType.POLICY_STATEMENT, claim_id="cl_statement", subject_id="refunds", field="statement", value=statement, source_agent="policy_agent"),
        claim(ClaimType.POLICY_STATEMENT, claim_id="cl_doc", subject_id="refunds", field="source_document", value="refunds.md", source_agent="policy_agent"),
    ]

    reply, _ = render_verified_response(
        original_reply=statement,
        products=[],
        proposed_claims=claims,
        verification_result=result(["cl_statement", "cl_doc"]),
        customer_message="How long does a refund take?",
    )

    assert reply == (
        "Refunds usually take 5-7 business days after we receive and inspect the return. "
        "Store credit is typically faster, usually within 1 business day."
    )
    assert "Policy source" not in reply
    assert "refunds.md" not in reply


def test_external_offer_is_labeled_and_never_returned_as_scout_inventory():
    products = [
        {
            "external_product_id": "EX001",
            "name": "Market Dress",
            "price": 70.0,
            "vendor_name": "Partner",
            "click_url": "/affiliate/click/EX001",
            "source": "external",
            "image_url": "https://example.com/untrusted.jpg",
        }
    ]
    claims = [
        claim(ClaimType.EXTERNAL_OFFER_IDENTITY, claim_id="cl_name", subject_id="EX001", field="product_name", value="Market Dress", source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_VENDOR, claim_id="cl_vendor", subject_id="EX001", field="vendor", value="Partner", source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_PRICE, claim_id="cl_price", subject_id="EX001", field="price", value=70.0, source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_IDENTITY, claim_id="cl_url", subject_id="EX001", field="url_or_offer_id", value="/affiliate/click/EX001", source_agent="external_offer_agent"),
    ]

    reply, rendered = render_verified_response(
        original_reply="Add Market Dress to your Scout cart for $70 from Partner.",
        products=products,
        proposed_claims=claims,
        verification_result=result(["cl_name", "cl_vendor", "cl_price", "cl_url"]),
        customer_message="external offer",
    )

    assert reply == (
        "We don’t have a matching item in our own catalog right now, but I found another option: "
        "Market Dress from Partner for $70.00. "
        "It’s from another retailer, so price and availability may change. "
        "You’ll complete the purchase with that retailer, and our return policy won’t apply."
    )
    assert rendered == [{key: value for key, value in products[0].items() if key != "image_url"}]
    assert rendered[0]["source"] == "external"


def test_multiple_external_offers_are_summarized_once_and_match_rendered_cards(monkeypatch, tmp_path):
    (tmp_path / "EX001.jpg").write_bytes(b"fake image")
    monkeypatch.setattr(rendering, "PRODUCT_IMAGES_DIR", tmp_path)
    products = [
        {"external_product_id": "EX001", "name": "Ruched Midi Dress", "price": 29.96, "vendor_name": "Nordstrom Rack", "click_url": "/affiliate/click/EX001", "source": "external"},
        {
            "external_product_id": "EX011",
            "name": "Enid Satin Body-Con Evening Dress",
            "price": 35.98,
            "vendor_name": "Nordstrom Rack",
            "click_url": "/affiliate/click/EX011",
            "source": "external",
            "image_url": "https://n.nordstrommedia.com/it/e6ad6561-d8a8-4c3f-82c5-d10f5bf0c02d.jpeg?h=368&w=240&dpr=2",
        },
        {"external_product_id": "EX999", "name": "Unapproved Dress", "price": 45.0, "vendor_name": "Target", "click_url": "/affiliate/click/EX999", "source": "external"},
    ]
    claims = [
        claim(ClaimType.EXTERNAL_OFFER_IDENTITY, claim_id="cl_1_name", subject_id="EX001", field="product_name", value="Ruched Midi Dress", source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_VENDOR, claim_id="cl_1_vendor", subject_id="EX001", field="vendor", value="Nordstrom Rack", source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_PRICE, claim_id="cl_1_price", subject_id="EX001", field="price", value=29.96, source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_IDENTITY, claim_id="cl_1_url", subject_id="EX001", field="url_or_offer_id", value="/affiliate/click/EX001", source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_IDENTITY, claim_id="cl_2_name", subject_id="EX011", field="product_name", value="Enid Satin Body-Con Evening Dress", source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_VENDOR, claim_id="cl_2_vendor", subject_id="EX011", field="vendor", value="Nordstrom Rack", source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_PRICE, claim_id="cl_2_price", subject_id="EX011", field="price", value=35.98, source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_IDENTITY, claim_id="cl_2_url", subject_id="EX011", field="url_or_offer_id", value="/affiliate/click/EX011", source_agent="external_offer_agent"),
    ]

    reply, rendered = render_verified_response(
        original_reply="Here are three external offers.",
        products=products,
        proposed_claims=claims,
        verification_result=result([approved.claim_id for approved in claims]),
        customer_message="red cocktail dresses under $50",
    )

    assert reply == (
        "We don’t have a matching item in our own catalog right now, but I found a few options elsewhere: "
        "Ruched Midi Dress from Nordstrom Rack for $29.96 "
        "and Enid Satin Body-Con Evening Dress from Nordstrom Rack for $35.98. "
        "These are from other retailers, so prices and availability may change. "
        "You’ll complete the purchase with those retailers, and our return policy won’t apply."
    )
    assert reply.count("other retailers") == 1
    assert reply.count("return policy") == 1
    assert "Unapproved Dress" not in reply
    assert [product["external_product_id"] for product in rendered] == ["EX001", "EX011"]
    assert rendered[0]["image_url"] == "/static/products/EX001.jpg"
    assert rendered[1]["image_url"] == "https://n.nordstrommedia.com/it/e6ad6561-d8a8-4c3f-82c5-d10f5bf0c02d.jpeg?h=368&w=240&dpr=2"


def test_seeded_trusted_external_retailer_image_is_rendered():
    products = [
        {
            "external_product_id": "EX009",
            "name": "Twist-Front Midi Dress",
            "price": 45.0,
            "vendor_name": "Target",
            "click_url": "/affiliate/click/EX009",
            "source": "external",
        }
    ]
    claims = [
        claim(ClaimType.EXTERNAL_OFFER_IDENTITY, claim_id="cl_name", subject_id="EX009", field="product_name", value="Twist-Front Midi Dress", source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_VENDOR, claim_id="cl_vendor", subject_id="EX009", field="vendor", value="Target", source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_PRICE, claim_id="cl_price", subject_id="EX009", field="price", value=45.0, source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_IDENTITY, claim_id="cl_url", subject_id="EX009", field="url_or_offer_id", value="/affiliate/click/EX009", source_agent="external_offer_agent"),
    ]

    _, rendered = render_verified_response(
        original_reply="Target option.",
        products=products,
        proposed_claims=claims,
        verification_result=result([approved.claim_id for approved in claims]),
        customer_message="red cocktail dresses under $50",
    )

    assert rendered[0]["image_url"] == "https://target.scene7.com/is/image/Target/GUEST_ed571594-9abb-45e3-b052-0aee6e49a634?wid=600&hei=600&qlt=80&fmt=pjpeg"


def test_external_vendor_and_price_require_approval_and_internal_external_mix_is_excluded():
    products = [{"external_product_id": "EX001", "name": "Market Dress", "price": 70.0, "vendor_name": "Partner", "source": "external"}]
    claims = [
        claim(ClaimType.EXTERNAL_OFFER_IDENTITY, claim_id="cl_name", subject_id="EX001", field="product_name", value="Market Dress", source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_VENDOR, claim_id="cl_vendor", subject_id="EX001", field="vendor", value="Partner", source_agent="external_offer_agent"),
        claim(ClaimType.EXTERNAL_OFFER_PRICE, claim_id="cl_price", subject_id="EX001", field="price", value=70.0, source_agent="external_offer_agent"),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_internal_mix", subject_id="EX001", field="price", value=70.0),
    ]

    reply, rendered = render_verified_response(
        original_reply="Market Dress from Partner is $70.",
        products=products,
        proposed_claims=claims,
        verification_result=result(["cl_name"]),
        customer_message="external",
    )

    assert rendered == []
    assert "Partner" not in reply
    assert "$70" not in reply


def test_empty_and_partial_results_use_safe_fallbacks_without_internal_diagnostics():
    rejected = VerificationResult(
        verified=False,
        approved_claim_ids=[],
        rejected_claims=[],
        missing_evidence=["ev_secret"],
        correction_agent="recommend_agent",
    )

    reply, products = render_verified_response(
        original_reply="Dress is $49.99. evidence ev_secret from recommend_agent failed.",
        products=[{"product_id": "P001", "name": "Dress", "price": 49.99}],
        proposed_claims=[claim(ClaimType.PRODUCT_PRICE, claim_id="cl_price", subject_id="P001", field="price", value=49.99)],
        verification_result=rejected,
        customer_message="product",
    )

    assert reply == DOMAIN_FALLBACKS["product"]
    assert products == []
    assert "ev_secret" not in reply
    assert "recommend_agent" not in reply


def test_original_reply_safe_conversation_preserved_but_unsupported_facts_are_not_copied():
    for safe_reply in ("You're welcome.", "Which size do you prefer?", "Please provide your order number."):
        reply, products = render_verified_response(
            original_reply=safe_reply,
            products=[],
            proposed_claims=[],
            verification_result=result([]),
            customer_message="thanks",
        )
        assert (reply, products) == (safe_reply, [])

    unsafe_reply, _ = render_verified_response(
        original_reply="This costs $49.99, quantity is 7, and delivery arrives today.",
        products=[],
        proposed_claims=[],
        verification_result=result([]),
        customer_message="product",
    )
    assert unsafe_reply == DOMAIN_FALLBACKS["product"]


def test_sentence_with_approved_and_rejected_facts_is_rebuilt_from_claims():
    claims = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", subject_id="P001", field="name", value="Dress"),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_price", subject_id="P001", field="price", value=79.99),
        claim(ClaimType.FULFILLMENT_ESTIMATE, claim_id="cl_today", subject_id="P001", field="delivery_estimate", value="today", source_agent="inventory_agent"),
    ]

    reply, rendered = render_verified_response(
        original_reply="Dress is $79.99 and delivery arrives today.",
        products=[{"product_id": "P001", "name": "Dress", "price": 79.99}],
        proposed_claims=claims,
        verification_result=result(["cl_name", "cl_price"]),
        customer_message="dress",
    )

    assert reply == "We have the Dress for $79.99."
    assert "today" not in reply
    assert rendered == [{"product_id": "P001", "name": "Dress", "source": "internal", "price": 79.99}]


def test_final_scan_catches_unsupported_dollar_product_and_tracking_values():
    approved = [claim(ClaimType.PRODUCT_PRICE, claim_id="cl_price", subject_id="P001", field="price", value=79.99)]

    reply, products = final_safety_scan(
        reply="Blue Dress is $49.99. Tracking number TRK999999.",
        products=[{"product_id": "P001", "name": "Blue Dress", "price": 49.99}],
        approved_claims=approved,
        customer_message="product",
    )

    assert reply == DOMAIN_FALLBACKS["product"]
    assert products == []


def test_final_scan_accepts_fully_verified_output():
    approved = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", subject_id="P001", field="name", value="Dress"),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_price", subject_id="P001", field="price", value=79.99),
    ]

    reply, products = final_safety_scan(
        reply="Dress is a Scout option for $79.99.",
        products=[{"product_id": "P001", "name": "Dress", "price": 79.99, "source": "internal"}],
        approved_claims=approved,
        customer_message="product",
    )

    assert reply == "Dress is a Scout option for $79.99."
    assert products == [{"product_id": "P001", "name": "Dress", "price": 79.99, "source": "internal"}]


def test_non_streaming_integration_stores_safe_reply_and_preserves_api_schema():
    class FakeApp:
        async def ainvoke(self, payload, config):
            record_tool_call(
                tool_name="search",
                validated_args={"query": "dress"},
                success=True,
                result=[{"product_id": "P001", "name": "Dress", "price": 79.99}],
                agent_name="recommend_agent",
            )
            return {"messages": [*payload["messages"], tool_message("search", json.dumps([{"product_id": "P001", "name": "Dress", "price": 79.99}])), message("recommend_agent", "Dress is $49.99 but secretly $10.")]}

    history = []
    reply, products = asyncio.run(_run_single_intent(FakeApp(), history, "dress", debug=False))

    assert reply == "We have the Dress for $79.99."
    assert products == [{"product_id": "P001", "name": "Dress", "source": "internal", "price": 79.99}]
    assert history[-1] == {"role": "assistant", "content": reply}
    ChatResponse(session_id="s1", reply=reply, products=products)
    assert get_evidence_entries() == []


def test_streaming_integration_preserves_sse_shape_and_cleanup():
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
                "data": {"output": {"messages": [*payload["messages"], tool_message("search", json.dumps([{"product_id": "P001", "name": "Dress", "price": 79.99}])), message("recommend_agent", "Dress is $79.99 and ships by dragon.")]}},
            }

    history = []
    events = asyncio.run(_collect_async(_run_single_intent_streaming(FakeStreamingApp(), history, "dress")))

    assert [event[0] for event in events] == ["progress", "progress", "progress", "result"]
    assert events[-1] == ("result", ("We have the Dress for $79.99.", [{"product_id": "P001", "name": "Dress", "source": "internal", "price": 79.99}]))
    assert history[-1] == {"role": "assistant", "content": events[-1][1][0]}
    assert get_evidence_entries() == []


def test_existing_correction_output_cannot_bypass_rendering():
    class FakeApp:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, payload, config):
            self.calls += 1
            record_tool_call(
                tool_name="search",
                validated_args={"query": "dress"},
                success=True,
                result=[{"product_id": "P001", "name": "Dress", "price": 79.99}],
                agent_name="recommend_agent",
            )
            if self.calls == 1:
                return {"messages": [*payload["messages"], tool_message("search", json.dumps([{"product_id": "P001", "name": "Dress", "price": 79.99}])), message("recommend_agent", "Dress is $49.99.")]}
            return {"messages": [*payload["messages"], tool_message("search", json.dumps([{"product_id": "P001", "name": "Dress", "price": 79.99}])), message("recommend_agent", "Dress is $79.99 and includes a free airplane.")]}

    reply, products = asyncio.run(_run_single_intent(FakeApp(), [], "dress", debug=False))

    assert reply == "We have the Dress for $79.99."
    assert products == [{"product_id": "P001", "name": "Dress", "source": "internal", "price": 79.99}]


def test_multi_intent_merge_is_deterministic_and_cannot_add_factual_values():
    merged = merge_answers(SimpleNamespace(invoke=lambda _: (_ for _ in ()).throw(AssertionError("no llm"))), "combined", ["Safe one.", "Safe two."])

    assert merged == "Safe one. Safe two."


def test_multi_intent_merge_deduplicates_external_policy_limitation():
    merged = merge_answers(
        SimpleNamespace(invoke=lambda _: (_ for _ in ()).throw(AssertionError("no llm"))),
        "outside option and return limitation",
        [
            "Scout does not currently have a matching internal option I can verify. "
            "I found a third-party option: TrailGuard Waterproof Hiking Shoe from Outdoor Demo Retailer for $64.99. "
            "This outside offer can’t be added to the Scout cart, Scout’s return policy doesn’t apply, "
            "and I don’t have verified return-policy information for that retailer.",
            "I do not have verified third-party retailer return-policy evidence for those outside offers.",
        ],
    )

    assert merged == (
        "Scout does not currently have a matching internal option I can verify. "
        "I found a third-party option: TrailGuard Waterproof Hiking Shoe from Outdoor Demo Retailer for $64.99. "
        "This outside offer can’t be added to the Scout cart, Scout’s return policy doesn’t apply, "
        "and I don’t have verified return-policy information for that retailer."
    )


def test_verify_then_render_omits_rejected_claims():
    ev = evidence(facts={"product_id": "P001", "name": "Dress", "price": 79.99})
    claims = [
        claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", subject_id="P001", field="name", value="Dress"),
        claim(ClaimType.PRODUCT_PRICE, claim_id="cl_price", subject_id="P001", field="price", value=49.99),
    ]
    verification = verify_claims(proposed_claims=claims, evidence_entries=[ev], customer_message="")

    reply, products = render_verified_response(
        original_reply="Dress is $49.99.",
        products=[{"product_id": "P001", "name": "Dress", "price": 49.99}],
        proposed_claims=claims,
        verification_result=verification,
        customer_message="product",
    )

    assert "$49.99" not in reply
    assert products == []


async def _collect_async(async_iterable):
    return [item async for item in async_iterable]
