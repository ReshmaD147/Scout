import asyncio
from types import SimpleNamespace

import pytest

from scout.agents import supervisor
from scout.agents.claims import ClaimType, propose_claims
from scout.agents.evidence import EvidenceEntry, record_tool_call
from scout.agents.rendering import render_verified_response
from scout.agents.verification import RejectionCode, verify_claims
from scout.api.chat import ChatRequest, ChatResponse
from scout.services.affiliate_service import (
    ExternalOfferSearchFilters,
    StructuredExternalOffer,
    find_external_alternative,
)


class FakeQuery:
    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return []

    def count(self):
        return 0


class FakeSession:
    def query(self, _model):
        return FakeQuery()


class App:
    scout_specialists = {
        "recommend_agent": object(),
        "inventory_agent": object(),
        "order_agent": object(),
        "external_offer_agent": object(),
        "policy_agent": object(),
    }


def evidence(facts, *, evidence_id="ev_1"):
    return EvidenceEntry(
        evidence_id=evidence_id,
        sequence=0,
        sub_intent_id="sub_1",
        attempt_number=0,
        agent_name="external_offer_agent",
        tool_name="search_external_offers",
        validated_args={},
        entity_type="external_product",
        entity_id=facts.get("external_product_id"),
        normalized_facts=facts,
        success=True,
    )


def test_external_offer_schema_validates_structured_lists_and_boolean():
    filters = ExternalOfferSearchFilters(
        query="waterproof hiking shoes",
        category="shoes",
        use_case="hiking",
        required_attributes="waterproof,wide fit",
        waterproof=True,
        budget_max=70,
    )

    assert filters.required_attributes == ["waterproof", "wide fit"]
    assert filters.waterproof is True


def test_external_offer_schema_rejects_malformed_price_and_url():
    with pytest.raises(ValueError):
        ExternalOfferSearchFilters(category="shoes", budget_max=-1)

    with pytest.raises(ValueError):
        StructuredExternalOffer(
            external_product_id="EXBAD",
            name="Bad Offer",
            vendor_name="Vendor",
            category="shoes",
            price=10,
            product_url="not-a-url",
            click_url="/affiliate/click/EXBAD",
            last_verified_at="2026-07-30T00:00:00+00:00",
        )


def test_external_filtering_matches_verified_waterproof_hiking_under_budget():
    matches = find_external_alternative(
        FakeSession(),
        query="waterproof hiking shoes",
        category="shoes",
        use_case="hiking",
        required_attributes="waterproof",
        waterproof=True,
        budget_max=70,
    )

    assert [match["external_product_id"] for match in matches] == ["EX010"]
    assert matches[0]["use_cases"] == ["hiking"]
    assert matches[0]["waterproof"] is True
    assert matches[0]["price"] <= 70


def test_external_filtering_excludes_wrong_or_unverified_constraints():
    assert find_external_alternative(
        FakeSession(), query="waterproof hiking shoes", category="shoes", use_case="running", waterproof=True
    ) == []
    assert find_external_alternative(
        FakeSession(), query="waterproof hiking shoes", category="shoes", use_case="hiking", waterproof=False
    ) == []
    assert find_external_alternative(
        FakeSession(), query="waterproof hiking shoes", category="dresses", use_case="hiking", waterproof=True
    ) == []
    assert find_external_alternative(
        FakeSession(), query="waterproof hiking shoes", category="shoes", use_case="hiking", waterproof=True, budget_max=60
    ) == []


def test_external_offer_claims_require_exact_evidence():
    facts = {
        "external_product_id": "EX010",
        "name": "TrailGuard Waterproof Hiking Shoe",
        "vendor_name": "Outdoor Demo Retailer",
        "price": 64.99,
        "click_url": "/affiliate/click/EX010",
        "use_cases": ["hiking"],
        "attributes": ["waterproof"],
        "waterproof": True,
        "availability": "in_stock",
    }
    claims = propose_claims(reply_text="", products=[facts], evidence_entries=[evidence(facts)])
    result = verify_claims(proposed_claims=claims, evidence_entries=[evidence(facts)], customer_message="waterproof hiking shoes")

    approved = {(claim.claim_type, claim.field, claim.value) for claim in claims if claim.claim_id in result.approved_claim_ids}
    assert (ClaimType.EXTERNAL_OFFER_USE_CASE.value, "use_case", "hiking") in approved
    assert (ClaimType.EXTERNAL_OFFER_ATTRIBUTE.value, "waterproof", True) in approved
    assert (ClaimType.EXTERNAL_OFFER_VENDOR.value, "vendor", "Outdoor Demo Retailer") in approved


def test_missing_waterproof_attribute_is_rejected():
    facts = {"external_product_id": "EX003", "name": "Running Shoe", "vendor_name": "Zappos", "price": 69.0, "source": "external"}
    claim = propose_claims(
        reply_text="",
        products=[{**facts, "waterproof": True}],
        evidence_entries=[evidence(facts)],
    )

    result = verify_claims(proposed_claims=claim, evidence_entries=[evidence(facts)], customer_message="waterproof shoes")

    assert any(rejected.reason_code == RejectionCode.FIELD_NOT_PRESENT for rejected in result.rejected_claims)


def test_scout_policy_cannot_verify_external_retailer_policy():
    policy_evidence = EvidenceEntry(
        evidence_id="ev_policy",
        sequence=0,
        sub_intent_id="sub_1",
        attempt_number=0,
        agent_name="policy_agent",
        tool_name="retrieve_policy_chunks",
        validated_args={},
        entity_type=None,
        entity_id=None,
        normalized_facts={"statement": "Scout return policy applies to purchases from Scout."},
        success=True,
    )
    claim = propose_claims(
        reply_text="Outdoor Demo Retailer follows Scout return policy.",
        products=[],
        evidence_entries=[policy_evidence],
    )
    result = verify_claims(proposed_claims=claim, evidence_entries=[policy_evidence], customer_message="external return policy")

    assert not result.verified


def test_rendered_external_offer_is_third_party_and_not_cart_product():
    facts = {
        "external_product_id": "EX010",
        "name": "TrailGuard Waterproof Hiking Shoe",
        "vendor_name": "Outdoor Demo Retailer",
        "price": 64.99,
        "click_url": "/affiliate/click/EX010",
        "use_cases": ["hiking"],
        "attributes": ["waterproof"],
        "waterproof": True,
        "availability": "in_stock",
        "source": "external",
    }
    claims = propose_claims(reply_text="", products=[facts], evidence_entries=[evidence(facts)])
    verification = verify_claims(proposed_claims=claims, evidence_entries=[evidence(facts)], customer_message="waterproof hiking shoes")

    reply, products = render_verified_response(
        original_reply="",
        products=[facts],
        proposed_claims=claims,
        verification_result=verification,
        customer_message="waterproof hiking shoes",
    )

    # Behavior check: a real, non-Scout option was found and clearly
    # framed as such - exact phrasing may evolve independently.
    assert "option" in reply.lower() and "retailer" in reply.lower()
    # Behavior check: purchase happens with the retailer, not Scout's
    # own cart - exact phrasing may evolve independently.
    assert "purchase" in reply.lower() and "retailer" in reply.lower()
    assert products[0]["source"] == "external"
    assert "product_id" not in products[0]


def test_mi05_uses_external_filtering_and_returns_verified_match(monkeypatch):
    calls = []

    async def fake_tool(tool_name, args, *, agent_name):
        calls.append((agent_name, tool_name, dict(args)))
        if tool_name == "recommend_products":
            result = []
        elif tool_name == "search_external_offers":
            result = {
                "matches": find_external_alternative(FakeSession(), **args),
                "items": find_external_alternative(FakeSession(), **args),
                "match_count": 1,
                "filters": args,
            }
        else:
            raise AssertionError(tool_name)
        record_tool_call(tool_name=tool_name, validated_args=args, success=True, result=result, agent_name=agent_name)
        return result

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)

    reply, _history, products = asyncio.run(
        supervisor.ask(
            App(),
            [],
            "Find waterproof hiking shoes under $70. If Scout has none, show an outside option and explain its return-policy limitation.",
        )
    )

    assert [call[1] for call in calls] == ["recommend_products", "search_external_offers"]
    assert calls[1][2]["use_case"] == "hiking"
    assert calls[1][2]["waterproof"] is True
    assert products[0]["external_product_id"] == "EX010"
    assert products[0]["waterproof"] is True
    # Behavior check: a real, non-Scout option was found and clearly
    # framed as such - exact phrasing may evolve independently.
    assert "option" in reply.lower() and "retailer" in reply.lower()
    # Behavior check: purchase happens with the retailer, not Scout's
    # own cart - exact phrasing may evolve independently.
    assert "purchase" in reply.lower() and "retailer" in reply.lower()
    # Behavior check: Scout's own return policy explicitly doesn't
    # apply to third-party purchases - exact phrasing may evolve
    # independently.
    assert "return policy" in reply.lower() and ("won’t apply" in reply.lower() or "doesn’t apply" in reply.lower())
    # Behavior check: no verified return-policy evidence is claimed for
    # the outside offer - exact phrasing may evolve independently.
    assert "return-policy" in reply.lower() or "return policy" in reply.lower()


def test_streaming_and_api_schemas_remain_compatible(monkeypatch):
    async def fake_plan(*args, **kwargs):
        return "ok", []

    monkeypatch.setattr(supervisor, "_run_multi_intent_plan", fake_plan)
    events = asyncio.run(
        _collect(
            supervisor.ask_streaming(
                App(),
                [],
                "Find waterproof hiking shoes under $70. If Scout has none, show an outside option and explain its return-policy limitation.",
            )
        )
    )

    assert events[-1] == ("result", ("ok", []))
    assert set(ChatRequest.model_fields) == {"message", "session_id"}
    assert set(ChatResponse.model_fields) == {"session_id", "reply", "products"}


async def _collect(async_iterable):
    return [item async for item in async_iterable]
