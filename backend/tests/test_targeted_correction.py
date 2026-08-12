import asyncio
import json
from types import SimpleNamespace

from scout.agents.claims import ClaimType
from scout.agents.evidence import (
    EvidenceEntry,
    ProposedClaim,
    RejectedClaim,
    VerificationResult,
    get_evidence_entries,
    get_tool_call_records,
    record_tool_call,
)
from scout.agents.supervisor import (
    CORRECTION_FINALIZATION_ALLOWANCE_SECONDS,
    FinalizedResponse,
    _attempt_targeted_correction,
    _build_correction_instruction,
    _run_single_intent,
    _run_single_intent_streaming,
    _should_attempt_targeted_correction,
)
from scout.api.chat import ChatRequest, ChatResponse


def claim(
    claim_type=ClaimType.PRODUCT_PRICE,
    *,
    claim_id="cl_1",
    subject_id="P001",
    field="price",
    value=49.99,
    source_agent="recommend_agent",
):
    return ProposedClaim(
        claim_id=claim_id,
        claim_type=claim_type,
        subject_id=subject_id,
        field=field,
        value=value,
        evidence_ids=["ev_1"],
        source_agent=source_agent,
    )


def finalized_for(agent, *, verified=False, reason="value_mismatch", claim_type=ClaimType.PRODUCT_PRICE):
    proposed = claim(claim_type=claim_type, source_agent=agent)
    rejected = [] if verified else [
        RejectedClaim(
            claim_id=proposed.claim_id,
            reason_code=reason,
            explanation="safe diagnostic",
            responsible_agent=agent,
            missing_evidence=[f"{claim_type.value}:price"],
        )
    ]
    return FinalizedResponse(
        reply="safe fallback",
        products=[],
        proposed_claims=[proposed],
        verification_result=VerificationResult(
            verified=verified,
            approved_claim_ids=[proposed.claim_id] if verified else [],
            rejected_claims=rejected,
            missing_evidence=[],
            correction_agent=None if verified else agent,
        ),
    )


def finalized_product_with_rejection(*, rejected_claim_type=ClaimType.PRODUCT_RATING, rejected_field="rating", rejected_value=4.9):
    identity = claim(
        ClaimType.PRODUCT_IDENTITY,
        claim_id="cl_name",
        subject_id="P001",
        field="name",
        value="Dress",
    )
    price = claim(
        ClaimType.PRODUCT_PRICE,
        claim_id="cl_price",
        subject_id="P001",
        field="price",
        value=79.99,
    )
    rejected_claim = claim(
        rejected_claim_type,
        claim_id="cl_rejected",
        subject_id="P001",
        field=rejected_field,
        value=rejected_value,
    )
    return FinalizedResponse(
        reply="Dress is a Scout option for $79.99.",
        products=[{"product_id": "P001", "name": "Dress", "source": "internal", "price": 79.99}],
        proposed_claims=[identity, price, rejected_claim],
        verification_result=VerificationResult(
            verified=False,
            approved_claim_ids=["cl_name", "cl_price"],
            rejected_claims=[
                RejectedClaim(
                    claim_id="cl_rejected",
                    reason_code="field_not_present",
                    explanation="optional unsupported",
                    responsible_agent="recommend_agent",
                )
            ],
            correction_agent="recommend_agent",
        ),
    )


def finalized_inventory_response(*, subject_id="product:P001:size:M", in_stock=True, store=False, rejected_inventory_subject=None):
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
            subject_id=subject_id,
            field="in_stock",
            value=in_stock,
            source_agent="inventory_agent",
        ),
        claim(
            ClaimType.PRODUCT_RATING,
            claim_id="cl_optional",
            subject_id="P001",
            field="rating",
            value=4.3,
            source_agent="recommend_agent",
        ),
    ]
    approved_ids = ["cl_name", "cl_stock"]
    if store:
        claims.insert(
            1,
            claim(
                ClaimType.STORE_IDENTITY,
                claim_id="cl_store",
                subject_id="S01",
                field="store_name",
                value="Maple Grove",
                source_agent="inventory_agent",
            ),
        )
        approved_ids.insert(1, "cl_store")
    rejected_claim_id = "cl_missing_inventory" if rejected_inventory_subject else "cl_optional"
    if rejected_inventory_subject:
        claims.append(
            claim(
                ClaimType.INVENTORY_AVAILABILITY,
                claim_id="cl_missing_inventory",
                subject_id=rejected_inventory_subject,
                field="in_stock",
                value=True,
                source_agent="inventory_agent",
            )
        )

    return FinalizedResponse(
        reply="Black Midi Dress is available in size M." if not store else "Black Midi Dress is available at the Maple Grove store.",
        products=[],
        proposed_claims=claims,
        verification_result=VerificationResult(
            verified=False,
            approved_claim_ids=approved_ids,
            rejected_claims=[
                RejectedClaim(
                    claim_id=rejected_claim_id,
                    reason_code="field_not_present",
                    explanation="unsupported",
                    responsible_agent="inventory_agent" if rejected_inventory_subject else "recommend_agent",
                )
            ],
            correction_agent="inventory_agent",
        ),
    )


def evidence_entry():
    return EvidenceEntry(
        evidence_id="ev_1",
        sequence=0,
        sub_intent_id="sub_1",
        attempt_number=0,
        agent_name="recommend_agent",
        tool_name="search",
        validated_args={},
        entity_type="product",
        entity_id="P001",
        normalized_facts={"product_id": "P001", "name": "Dress", "price": 79.99},
        success=True,
    )


def message(name, content, msg_type="ai"):
    return SimpleNamespace(name=name, content=content, type=msg_type)


def tool_message(name, content):
    return message(name, content, msg_type="tool")


def test_correction_eligibility_selects_each_single_specialist():
    specialists = {
        "recommend_agent": object(),
        "inventory_agent": object(),
        "order_agent": object(),
        "external_offer_agent": object(),
        "policy_agent": object(),
    }

    assert _should_attempt_targeted_correction(finalized_for("recommend_agent"), correction_attempted=False, specialists=specialists)
    assert _should_attempt_targeted_correction(finalized_for("inventory_agent", claim_type=ClaimType.INVENTORY_QUANTITY), correction_attempted=False, specialists=specialists)
    assert _should_attempt_targeted_correction(finalized_for("order_agent", claim_type=ClaimType.ORDER_STATUS), correction_attempted=False, specialists=specialists)
    assert _should_attempt_targeted_correction(finalized_for("policy_agent", claim_type=ClaimType.POLICY_STATEMENT), correction_attempted=False, specialists=specialists)
    assert _should_attempt_targeted_correction(finalized_for("external_offer_agent", claim_type=ClaimType.EXTERNAL_OFFER_PRICE), correction_attempted=False, specialists=specialists)


def test_correction_ineligible_for_mixed_unknown_conversational_or_verified():
    specialists = {"recommend_agent": object(), "inventory_agent": object()}
    mixed = finalized_for("recommend_agent")
    mixed.verification_result.rejected_claims.append(
        RejectedClaim(
            claim_id="cl_2",
            reason_code="value_mismatch",
            explanation="safe",
            responsible_agent="inventory_agent",
        )
    )
    mixed.verification_result.correction_agent = None

    unknown = finalized_for("unknown_agent")
    conversational = FinalizedResponse(
        reply="You're welcome.",
        products=[],
        proposed_claims=[],
        verification_result=VerificationResult(verified=True),
    )

    assert not _should_attempt_targeted_correction(mixed, correction_attempted=False, specialists=specialists)
    assert not _should_attempt_targeted_correction(unknown, correction_attempted=False, specialists=specialists)
    assert not _should_attempt_targeted_correction(conversational, correction_attempted=False, specialists=specialists)
    assert not _should_attempt_targeted_correction(finalized_for("recommend_agent", verified=True), correction_attempted=False, specialists=specialists)
    assert not _should_attempt_targeted_correction(finalized_for("recommend_agent"), correction_attempted=True, specialists=specialists)
    assert not _should_attempt_targeted_correction(finalized_for("recommend_agent", reason="unsupported_claim_type"), correction_attempted=False, specialists=specialists)


def test_sufficient_product_with_rejected_optional_rating_skips_correction():
    specialists = {"recommend_agent": object()}

    assert not _should_attempt_targeted_correction(
        finalized_product_with_rejection(rejected_claim_type=ClaimType.PRODUCT_RATING, rejected_field="rating", rejected_value=4.9),
        correction_attempted=False,
        specialists=specialists,
        customer_message="Recommend a dress under $80.",
    )


def test_sufficient_product_with_rejected_optional_promotion_skips_correction():
    specialists = {"recommend_agent": object()}

    assert not _should_attempt_targeted_correction(
        finalized_product_with_rejection(
            rejected_claim_type=ClaimType.PROMOTION,
            rejected_field="promotion_name",
            rejected_value="Flash Sale",
        ),
        correction_attempted=False,
        specialists=specialists,
        customer_message="Recommend a dress under $80.",
    )


def test_one_verified_under_budget_product_is_sufficient():
    specialists = {"recommend_agent": object()}
    finalized = finalized_product_with_rejection()

    assert not _should_attempt_targeted_correction(
        finalized,
        correction_attempted=False,
        specialists=specialists,
        customer_message="Find a dress no more than $80.",
    )


def test_product_identity_and_price_do_not_satisfy_inventory_request():
    specialists = {"recommend_agent": object(), "inventory_agent": object()}
    finalized = finalized_product_with_rejection()
    missing = claim(
        ClaimType.INVENTORY_AVAILABILITY,
        claim_id="cl_missing_inventory",
        subject_id="product:P001:size:M",
        field="in_stock",
        value=True,
        source_agent="inventory_agent",
    )
    finalized.proposed_claims.append(missing)
    finalized.verification_result.rejected_claims = [
        RejectedClaim(
            claim_id="cl_missing_inventory",
            reason_code="field_not_present",
            explanation="missing availability",
            responsible_agent="inventory_agent",
        )
    ]
    finalized.verification_result.correction_agent = "inventory_agent"

    assert _should_attempt_targeted_correction(
        finalized,
        correction_attempted=False,
        specialists=specialists,
        customer_message="Is the black midi dress in a medium?",
    )


def test_matching_size_availability_satisfies_inventory_request():
    specialists = {"inventory_agent": object()}

    assert not _should_attempt_targeted_correction(
        finalized_inventory_response(subject_id="product:P001:size:M"),
        correction_attempted=False,
        specialists=specialists,
        customer_message="Is the black midi dress in a medium?",
    )


def test_wrong_size_is_insufficient_and_unavailable_answer_is_useful():
    specialists = {"inventory_agent": object()}

    assert _should_attempt_targeted_correction(
        finalized_inventory_response(subject_id="product:P001:size:L", rejected_inventory_subject="product:P001:size:M"),
        correction_attempted=False,
        specialists=specialists,
        customer_message="Is the black midi dress in a medium?",
    )
    assert not _should_attempt_targeted_correction(
        finalized_inventory_response(subject_id="product:P001:size:M", in_stock=False),
        correction_attempted=False,
        specialists=specialists,
        customer_message="Is the black midi dress in a medium?",
    )


def test_matching_store_availability_satisfies_store_request_and_wrong_store_does_not():
    specialists = {"inventory_agent": object()}

    assert not _should_attempt_targeted_correction(
        finalized_inventory_response(subject_id="product:P001:store:S01", store=True),
        correction_attempted=False,
        specialists=specialists,
        customer_message="Is the black midi dress available at Maple Grove?",
    )
    assert _should_attempt_targeted_correction(
        finalized_inventory_response(subject_id="product:P001:store:S02", store=True, rejected_inventory_subject="product:P001:store:S01"),
        correction_attempted=False,
        specialists=specialists,
        customer_message="Is the black midi dress available at Maple Grove?",
    )


def test_no_approved_product_still_permits_correction():
    specialists = {"recommend_agent": object()}

    assert _should_attempt_targeted_correction(
        finalized_for("recommend_agent"),
        correction_attempted=False,
        specialists=specialists,
        customer_message="Recommend a dress under $80.",
    )


def test_rejected_required_price_still_permits_correction():
    specialists = {"recommend_agent": object()}
    finalized = finalized_product_with_rejection(
        rejected_claim_type=ClaimType.PRODUCT_PRICE,
        rejected_field="price",
        rejected_value=79.99,
    )
    finalized.verification_result.approved_claim_ids = ["cl_name"]
    finalized.products = [{"product_id": "P001", "name": "Dress", "source": "internal"}]

    assert _should_attempt_targeted_correction(
        finalized,
        correction_attempted=False,
        specialists=specialists,
        customer_message="Recommend a dress under $80.",
    )


def test_insufficient_remaining_time_prevents_correction(monkeypatch):
    specialists = {"recommend_agent": object()}
    monkeypatch.setattr("scout.agents.supervisor.settings.MODEL_INVOCATION_TIMEOUT_SECONDS", 45)

    assert not _should_attempt_targeted_correction(
        finalized_for("recommend_agent"),
        correction_attempted=False,
        specialists=specialists,
        customer_message="Recommend a dress under $80.",
        correction_deadline=100.0,
        now=100.0 + CORRECTION_FINALIZATION_ALLOWANCE_SECONDS,
    )


def test_api_and_sse_schemas_remain_unchanged_for_gate():
    request = ChatRequest(message="dress", session_id="s1")
    response = ChatResponse(session_id="s1", reply="ok", products=[])

    assert request.message == "dress"
    assert request.session_id == "s1"
    assert set(ChatRequest.model_fields) == {"message", "session_id"}
    assert set(ChatResponse.model_fields) == {"session_id", "reply", "products"}
    assert response == ChatResponse(session_id="s1", reply="ok", products=[])


def test_one_correction_maximum_remains_unchanged():
    specialists = {"recommend_agent": object()}

    assert not _should_attempt_targeted_correction(
        finalized_for("recommend_agent"),
        correction_attempted=True,
        specialists=specialists,
        customer_message="Recommend a dress under $80.",
    )


def test_correction_instruction_is_structured_and_safe():
    instruction = _build_correction_instruction(
        original_request="dress under $80",
        finalized=finalized_for("recommend_agent"),
    )

    assert "dress under $80" in instruction
    assert "product_price" in instruction
    assert "value_mismatch" in instruction
    assert "approved tools" in instruction
    assert "unsupported facts" in instruction
    assert "api_key" not in instruction
    assert "traceback" not in instruction.lower()


def test_direct_targeted_correction_invokes_only_selected_specialist_and_sanitizes_output():
    class Specialist:
        def __init__(self):
            self.calls = 0
            self.payloads = []
            self.tools = [SimpleNamespace(name="search")]

        async def ainvoke(self, payload, config):
            self.calls += 1
            self.payloads.append(payload)
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
                    message("inventory_agent", "Wrong transfer says $1."),
                    message("recommend_agent", "Dress is $79.99 and includes unsupported magic."),
                ]
            }

    specialist = Specialist()
    other = Specialist()
    app = SimpleNamespace(scout_specialists={"recommend_agent": specialist, "inventory_agent": other})

    async def run():
        from scout.agents.evidence import start_evidence_context, clear_evidence_context

        start_evidence_context("sub_test", attempt_number=0)
        try:
            record_tool_call(
                tool_name="search",
                validated_args={"query": "dress"},
                success=True,
                result=[{"product_id": "P001", "name": "Dress", "price": 79.99}],
                agent_name="recommend_agent",
            )
            return await _attempt_targeted_correction(
                app,
                initial=finalized_for("recommend_agent"),
                customer_message="dress under $80",
                correction_attempted=False,
                sub_intent_id="sub_test",
            ), get_tool_call_records(), get_evidence_entries()
        finally:
            clear_evidence_context()

    corrected, records, evidence = asyncio.run(run())

    assert specialist.calls == 1
    assert other.calls == 0
    assert "recommend_agent" in app.scout_specialists
    assert [tool.name for tool in specialist.tools] == ["search"]
    assert corrected.reply == "Dress is a Scout option for $79.99."
    assert corrected.products == [{"product_id": "P001", "name": "Dress", "source": "internal", "price": 79.99}]
    assert "magic" not in corrected.reply
    assert [record.attempt_number for record in records] == [0, 1]
    assert [entry.attempt_number for entry in evidence] == [0, 1]
    assert [record.sequence for record in records] == [0, 1]


def test_no_recursive_correction_and_failed_correction_keep_initial_safe_result():
    class FailingSpecialist:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, payload, config):
            self.calls += 1
            raise RuntimeError("boom")

    app = SimpleNamespace(scout_specialists={"recommend_agent": FailingSpecialist()})
    initial = finalized_for("recommend_agent")

    async def run():
        from scout.agents.evidence import start_evidence_context, clear_evidence_context

        start_evidence_context("sub_test", attempt_number=0)
        try:
            return await _attempt_targeted_correction(
                app,
                initial=initial,
                customer_message="dress",
                correction_attempted=False,
                sub_intent_id="sub_test",
            )
        finally:
            clear_evidence_context()

    corrected = asyncio.run(run())

    assert corrected is initial
    assert app.scout_specialists["recommend_agent"].calls == 1
    assert asyncio.run(_attempt_without_context(app, initial)) is initial


async def _attempt_without_context(app, initial):
    return await _attempt_targeted_correction(
        app,
        initial=initial,
        customer_message="dress",
        correction_attempted=True,
        sub_intent_id="sub_test",
    )


def test_worse_or_conflicting_correction_does_not_replace_better_initial():
    initial = FinalizedResponse(
        reply="Dress is a Scout option for $79.99.",
        products=[{"product_id": "P001", "name": "Dress", "source": "internal", "price": 79.99}],
        proposed_claims=[
            claim(ClaimType.PRODUCT_IDENTITY, claim_id="cl_name", field="name", value="Dress"),
            claim(ClaimType.PRODUCT_PRICE, claim_id="cl_price", field="price", value=79.99),
        ],
        verification_result=VerificationResult(verified=True, approved_claim_ids=["cl_name", "cl_price"]),
    )
    worse = finalized_for("recommend_agent")

    from scout.agents.supervisor import _choose_better_response

    assert _choose_better_response(initial, worse) is initial


def test_non_streaming_integration_uses_targeted_correction_and_stores_safe_reply():
    class Specialist:
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
            return {"messages": [*payload["messages"], tool_message("search", json.dumps([{"product_id": "P001", "name": "Dress", "price": 79.99}])), message("recommend_agent", "Dress is $79.99 raw extra.")]}

    class App:
        def __init__(self):
            self.specialist = Specialist()
            self.calls = 0
            self.scout_specialists = {"recommend_agent": self.specialist}

        async def ainvoke(self, payload, config):
            self.calls += 1
            record_tool_call(
                tool_name="search",
                validated_args={"query": "dress"},
                success=True,
                result=[{"product_id": "P001", "name": "Dress"}],
                agent_name="recommend_agent",
            )
            return {"messages": [*payload["messages"], tool_message("search", json.dumps([{"product_id": "P001", "name": "Dress", "price": 49.99}])), message("recommend_agent", "Dress is $49.99.")]}

    history = []
    app = App()
    reply, products = asyncio.run(_run_single_intent(app, history, "dress", debug=False))

    assert reply == "Dress is a Scout option for $79.99."
    assert products == [{"product_id": "P001", "name": "Dress", "source": "internal", "price": 79.99}]
    assert history == [{"role": "user", "content": "dress"}, {"role": "assistant", "content": reply}]
    assert app.calls == 1
    assert app.specialist.calls == 1
    ChatResponse(session_id="s1", reply=reply, products=products)
    assert get_evidence_entries() == []


def test_streaming_integration_uses_same_helper_and_preserves_sse_shape():
    class Specialist:
        async def ainvoke(self, payload, config):
            record_tool_call(
                tool_name="search",
                validated_args={"query": "dress"},
                success=True,
                result=[{"product_id": "P001", "name": "Dress", "price": 79.99}],
                agent_name="recommend_agent",
            )
            return {"messages": [*payload["messages"], tool_message("search", json.dumps([{"product_id": "P001", "name": "Dress", "price": 79.99}])), message("recommend_agent", "Dress is $79.99 raw extra.")]}

    class App:
        def __init__(self):
            self.scout_specialists = {"recommend_agent": Specialist()}

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
                "data": {"output": {"messages": [*payload["messages"], tool_message("search", json.dumps([{"product_id": "P001", "name": "Dress", "price": 49.99}])), message("recommend_agent", "Dress is $49.99.")]}},
            }

    history = []
    events = asyncio.run(_collect_async(_run_single_intent_streaming(App(), history, "dress")))

    assert [event[0] for event in events] == ["progress", "progress", "progress", "result"]
    assert events[-1] == ("result", ("Dress is a Scout option for $79.99.", [{"product_id": "P001", "name": "Dress", "source": "internal", "price": 79.99}]))
    assert history[-1] == {"role": "assistant", "content": "Dress is a Scout option for $79.99."}


def test_multi_intent_correction_isolated_and_merge_deterministic(monkeypatch):
    from scout.agents.supervisor import ask

    class App:
        async def ainvoke(self, payload, config):
            return {"messages": [*payload["messages"], message("policy_agent", "Done.")]}

    monkeypatch.setattr("scout.agents.supervisor.split_intents", lambda model, message: ["one", "two"])
    monkeypatch.setattr("scout.agents.supervisor.get_chat_model", lambda: object())

    reply, history, products = asyncio.run(ask(App(), [], "combined"))

    assert reply == "Done."
    assert products == []
    assert history == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "Done."},
    ]


async def _collect_async(async_iterable):
    return [item async for item in async_iterable]
