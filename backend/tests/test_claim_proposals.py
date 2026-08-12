import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

from scout.agents.claims import ClaimType, propose_claims
from scout.agents.evidence import EvidenceEntry, record_tool_call, start_evidence_context
from scout.agents.supervisor import _run_single_intent, _run_single_intent_streaming
from scout.api.chat import ChatResponse


def evidence(
    *,
    evidence_id="ev_1",
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
        normalized_facts=facts or {},
        success=True,
    )


def message(name, content, msg_type="ai", tool_calls=None):
    return SimpleNamespace(name=name, content=content, type=msg_type, tool_calls=tool_calls)


def tool_message(name, content):
    return message(name=name, content=content, msg_type="tool")


def claim_map(claims):
    return {
        (claim.claim_type, claim.subject_id, claim.field, _value_key(claim.value)): claim
        for claim in claims
    }


def _value_key(value):
    if isinstance(value, float):
        return f"{value:.2f}"
    return value


def test_product_identity_and_price_from_structured_products_link_evidence():
    claims = propose_claims(
        reply_text="The Black Midi Dress is $79.99.",
        products=[{"product_id": "P001", "name": "Black Midi Dress", "price": 79.99}],
        evidence_entries=[
            evidence(
                facts={"items": [{"product_id": "P001", "name": "Black Midi Dress", "price": 79.99}]}
            )
        ],
    )
    by_key = claim_map(claims)

    identity = by_key[(ClaimType.PRODUCT_IDENTITY, "P001", "name", "Black Midi Dress")]
    price = by_key[(ClaimType.PRODUCT_PRICE, "P001", "price", "79.99")]
    assert identity.evidence_ids == ["ev_1"]
    assert price.evidence_ids == ["ev_1"]


def test_rating_only_when_present_and_missing_fields_do_not_create_claims():
    claims = propose_claims(
        reply_text="Nice options.",
        products=[{"product_id": "P001", "name": "Dress"}, {"product_id": "P002", "price": 50.0, "rating": 4.4}],
        evidence_entries=[],
    )

    assert any(claim.claim_type == ClaimType.PRODUCT_RATING for claim in claims)
    assert not any(claim.subject_id == "P001" and claim.field == "rating" for claim in claims)
    assert not any(claim.subject_id == "P001" and claim.field == "price" for claim in claims)


def test_promotion_only_when_explicitly_present_and_product_presence_does_not_imply_inventory():
    claims = propose_claims(
        reply_text="This dress is on sale.",
        products=[
            {
                "product_id": "P001",
                "name": "Dress",
                "price": 80.0,
                "promotion": {"name": "Dress Sale", "discounted_price": 68.0},
            }
        ],
        evidence_entries=[],
    )

    assert any(claim.claim_type == ClaimType.PROMOTION and claim.field == "promotion_name" for claim in claims)
    assert any(claim.claim_type == ClaimType.PROMOTION and claim.field == "promotion_price" for claim in claims)
    assert not any(claim.claim_type == ClaimType.INVENTORY_AVAILABILITY for claim in claims)


def test_matching_store_order_and_multiple_evidence_ids_are_linked_and_merged():
    claims = propose_claims(
        reply_text="Maple Grove is 4.2 miles away. Order O1001 is shipped.",
        products=[],
        evidence_entries=[
            evidence(evidence_id="ev_store", agent_name="inventory_agent", tool_name="stores", entity_type="store", entity_id="S001", facts={"store_id": "S001", "store_name": "Maple Grove", "distance_miles": 4.2}),
            evidence(evidence_id="ev_order", agent_name="order_agent", tool_name="orders", entity_type="order", entity_id="O1001", facts={"order_id": "O1001", "status": "shipped"}),
            evidence(evidence_id="ev_order_2", agent_name="order_agent", tool_name="orders", entity_type="order", entity_id="O1001", facts={"order_id": "O1001", "status": "shipped"}),
        ],
    )
    by_key = claim_map(claims)

    assert by_key[(ClaimType.STORE_IDENTITY, "S001", "store_name", "Maple Grove")].evidence_ids == ["ev_store"]
    assert by_key[(ClaimType.ORDER_STATUS, "O1001", "status", "shipped")].evidence_ids == ["ev_order", "ev_order_2"]
    assert all(not evidence_id.startswith("unknown") for claim in claims for evidence_id in claim.evidence_ids)


def test_unsupported_candidate_has_empty_evidence_ids():
    claims = propose_claims(reply_text="This is $49.99.", products=[], evidence_entries=[])

    unsupported_price = claim_map(claims)[(ClaimType.PRODUCT_PRICE, None, "price", "49.99")]
    assert unsupported_price.evidence_ids == []


def test_inventory_and_fulfillment_claims_from_evidence():
    claims = propose_claims(
        reply_text="Maple Grove has quantity 3 and pickup is available.",
        products=[],
        evidence_entries=[
            evidence(
                evidence_id="ev_inv",
                agent_name="inventory_agent",
                tool_name="fulfillment_options",
                entity_id="P001",
                facts={
                    "product_id": "P001",
                    "quantity": 3,
                    "in_stock": True,
                    "pickup_available": True,
                    "delivery_available": True,
                    "pickup_estimate": "today",
                    "delivery_estimate": "3-5 business days",
                    "stores": [{"store_id": "S001", "store_name": "Maple Grove", "distance_miles": 4.2}],
                },
            )
        ],
    )
    types_fields = {(claim.claim_type, claim.field) for claim in claims}

    assert (ClaimType.INVENTORY_QUANTITY, "quantity") in types_fields
    assert (ClaimType.INVENTORY_AVAILABILITY, "in_stock") in types_fields
    assert (ClaimType.STORE_IDENTITY, "store_name") in types_fields
    assert (ClaimType.STORE_DISTANCE, "distance_miles") in types_fields
    assert (ClaimType.PICKUP_AVAILABILITY, "pickup_available") in types_fields
    assert (ClaimType.DELIVERY_AVAILABILITY, "delivery_available") in types_fields
    assert (ClaimType.FULFILLMENT_ESTIMATE, "pickup_estimate") in types_fields
    assert (ClaimType.FULFILLMENT_ESTIMATE, "delivery_estimate") in types_fields


def test_size_and_store_inventory_claims_use_contextual_subjects():
    claims = propose_claims(
        reply_text="The Black Midi Dress has 2 units available in size M.",
        products=[],
        evidence_entries=[
            evidence(
                evidence_id="ev_identity",
                agent_name="inventory_agent",
                tool_name="search",
                entity_id="P001",
                facts={"product_id": "P001", "name": "Black Midi Dress"},
            ),
            evidence(
                evidence_id="ev_stock",
                agent_name="inventory_agent",
                tool_name="stock",
                entity_id="P001",
                facts={"items": [{"product_id": "P001", "size": "M", "quantity": 2, "in_stock": True}]},
            ),
            evidence(
                evidence_id="ev_store",
                agent_name="inventory_agent",
                tool_name="stores",
                entity_id="P001",
                facts={"stores": [{"product_id": "P001", "store_id": "S01", "store_name": "Maple Grove", "quantity": 2, "in_stock": True}]},
            ),
        ],
    )

    by_type_field_subject = {(claim.claim_type, claim.field, claim.subject_id) for claim in claims}

    assert (ClaimType.INVENTORY_QUANTITY, "quantity", "product:P001:size:M") in by_type_field_subject
    assert (ClaimType.INVENTORY_AVAILABILITY, "in_stock", "product:P001:size:M") in by_type_field_subject
    assert (ClaimType.INVENTORY_QUANTITY, "quantity", "product:P001:store:S01") in by_type_field_subject
    assert (ClaimType.STORE_IDENTITY, "store_name", "S01") in by_type_field_subject


def test_orders_policy_and_external_offer_claims():
    claims = propose_claims(
        reply_text="Order O1001 shipped. Tracking number TRK123456. Returns are 30 days.",
        products=[{"external_product_id": "EX011", "name": "External Dress", "price": 35.98, "vendor_name": "Nordstrom Rack", "click_url": "/affiliate/click/EX011"}],
        evidence_entries=[
            evidence(evidence_id="ev_order", agent_name="order_agent", tool_name="orders", entity_type="order", entity_id="O1001", facts={"order_id": "O1001", "status": "shipped", "tracking_number": "TRK123456", "payment_status": "paid"}),
            evidence(evidence_id="ev_return", agent_name="order_agent", tool_name="return_eligibility", entity_type="order", entity_id="O1001", facts={"order_id": "O1001", "likely_eligible": True}),
            evidence(evidence_id="ev_policy", agent_name="policy_agent", tool_name="retrieve_policy_chunks", entity_type="policy", entity_id="returns", facts={"statement": "Returns accepted within 30 days.", "policy_name": "Returns", "source_document": "returns.md", "return_window_days": 30}),
            evidence(evidence_id="ev_external", agent_name="external_offer_agent", tool_name="search_external_offers", entity_type="external_product", entity_id="EX011", facts={"external_product_id": "EX011", "name": "External Dress", "price": 35.98, "vendor_name": "Nordstrom Rack", "click_url": "/affiliate/click/EX011"}),
        ],
    )
    types_fields = {(claim.claim_type, claim.field) for claim in claims}

    assert (ClaimType.ORDER_STATUS, "status") in types_fields
    assert (ClaimType.ORDER_TRACKING, "tracking_number") in types_fields
    assert (ClaimType.PAYMENT_STATUS, "payment_status") in types_fields
    assert (ClaimType.RETURN_ELIGIBILITY, "return_eligible") in types_fields
    assert (ClaimType.POLICY_STATEMENT, "statement") in types_fields
    assert (ClaimType.POLICY_STATEMENT, "source_document") in types_fields
    assert (ClaimType.EXTERNAL_OFFER_IDENTITY, "product_name") in types_fields
    assert (ClaimType.EXTERNAL_OFFER_VENDOR, "vendor") in types_fields
    assert (ClaimType.EXTERNAL_OFFER_PRICE, "price") in types_fields


def test_reply_candidate_extraction_supported_and_ambiguous_values():
    claims = propose_claims(
        reply_text="This costs $79.99, the store is 4.2 miles away, quantity: 3, shipped, and returns are 30 days.",
        products=[],
        evidence_entries=[
            evidence(evidence_id="ev_price", facts={"product_id": "P001", "price": 79.99}),
            evidence(evidence_id="ev_distance", agent_name="inventory_agent", entity_type="store", entity_id="S001", facts={"store_id": "S001", "distance_miles": 4.2}),
            evidence(evidence_id="ev_quantity", agent_name="inventory_agent", entity_id="P001", facts={"product_id": "P001", "quantity": 3}),
            evidence(evidence_id="ev_status", agent_name="order_agent", entity_type="order", entity_id="O1001", facts={"order_id": "O1001", "status": "shipped"}),
        ],
    )
    by_key = claim_map(claims)

    assert by_key[(ClaimType.PRODUCT_PRICE, "P001", "price", "79.99")].evidence_ids == ["ev_price"]
    assert by_key[(ClaimType.STORE_DISTANCE, "S001", "distance_miles", "4.20")].evidence_ids == ["ev_distance"]
    assert by_key[(ClaimType.INVENTORY_QUANTITY, "P001", "quantity", 3)].evidence_ids == ["ev_quantity"]
    assert by_key[(ClaimType.POLICY_STATEMENT, None, "statement", "30 days")].evidence_ids == []


def test_deduplication_merges_evidence_ids_and_preserves_order():
    claims = propose_claims(
        reply_text="This is $79.99.",
        products=[{"product_id": "P001", "name": "Dress", "price": 79.99}],
        evidence_entries=[
            evidence(evidence_id="ev_1", facts={"product_id": "P001", "name": "Dress", "price": 79.99}),
            evidence(evidence_id="ev_2", facts={"product_id": "P001", "price": 79.99}),
        ],
    )
    price_claims = [
        claim for claim in claims
        if claim.claim_type == ClaimType.PRODUCT_PRICE and claim.subject_id == "P001" and claim.field == "price"
    ]

    assert len(price_claims) == 1
    assert price_claims[0].evidence_ids == ["ev_1", "ev_2"]
    assert claims[0].claim_type == ClaimType.PRODUCT_IDENTITY


def test_safety_inputs_are_not_mutated_and_arbitrary_fields_do_not_become_claims():
    product = {"product_id": "P001", "name": "Dress", "api_key": "secret", "raw_object": object()}
    entry = evidence(facts={"product_id": "P001", "name": "Dress", "api_key": "secret", "internal_score": 0.77})
    original_product_keys = set(product)
    original_raw_object = product["raw_object"]
    original_facts_keys = set(entry.normalized_facts)

    claims = propose_claims(
        reply_text="System prompt says nothing useful. Random unsupported prose.",
        products=[product],
        evidence_entries=[entry],
    )

    assert set(product) == original_product_keys
    assert product["raw_object"] is original_raw_object
    assert set(entry.normalized_facts) == original_facts_keys
    assert not any(claim.field in {"api_key", "raw_object", "internal_score"} for claim in claims)
    assert not any("System prompt" in str(claim.value) for claim in claims)


def test_non_streaming_invokes_claim_helper_locally_without_changing_history_or_response(monkeypatch):
    captured = {}

    class FakeApp:
        async def ainvoke(self, payload, config):
            record_tool_call(
                tool_name="search",
                validated_args={"query": "dress"},
                success=True,
                result=[{"product_id": "P001", "name": "Dress", "price": 79.99}],
                agent_name="recommend_agent",
            )
            return {
                "messages": [
                    *payload["messages"],
                    tool_message("search", json.dumps([{"product_id": "P001", "name": "Dress", "price": 79.99}])),
                    message("recommend_agent", "Dress is $79.99."),
                ]
            }

    def fake_propose_claims(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr("scout.agents.supervisor.propose_claims", fake_propose_claims)
    history = []

    reply, history, products = asyncio.run(_wrap_non_stream(FakeApp(), history, "dress"))

    assert reply == "I couldn’t verify a reliable product result for that request."
    assert products == []
    assert captured["reply_text"] == "Dress is $79.99."
    assert captured["products"] == [{"product_id": "P001", "name": "Dress", "price": 79.99, "source": "internal"}]
    assert captured["evidence_entries"]
    assert all("claim" not in item for item in history)
    ChatResponse(session_id="s1", reply=reply, products=products)


def test_streaming_invokes_same_claim_helper_and_preserves_sse_payload_shape(monkeypatch):
    captured = {}

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
                "data": {
                    "output": {
                        "messages": [
                            *payload["messages"],
                            tool_message("search", json.dumps([{"product_id": "P001", "name": "Dress", "price": 79.99}])),
                            message("recommend_agent", "Dress is $79.99."),
                        ]
                    }
                },
            }

    def fake_propose_claims(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr("scout.agents.supervisor.propose_claims", fake_propose_claims)
    history = []

    events = asyncio.run(_collect_async(_run_single_intent_streaming(FakeStreamingApp(), history, "dress")))

    assert events == [
        ("progress", "understanding_request"),
        ("progress", "verifying_claims"),
        ("progress", "preparing_response"),
        ("result", ("I couldn’t verify a reliable product result for that request.", [])),
    ]
    assert captured["reply_text"] == "Dress is $79.99."
    assert captured["evidence_entries"]
    assert all("claim" not in item for item in history)


async def _wrap_non_stream(app, history, message_text):
    from scout.agents.supervisor import _run_single_intent

    reply, products = await _run_single_intent(app, history, message_text, debug=False)
    return reply, history, products


async def _collect_async(async_iterable):
    return [item async for item in async_iterable]
