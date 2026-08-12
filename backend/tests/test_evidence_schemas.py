from datetime import datetime

import pytest
from pydantic import ValidationError

from scout.agents.evidence import (
    EvidenceEntry,
    ProposedClaim,
    RejectedClaim,
    ToolCallRecord,
    VerificationResult,
)
from scout.api.chat import ChatRequest, ChatResponse


def make_evidence_entry(**overrides):
    data = {
        "sequence": 0,
        "sub_intent_id": "sub_1",
        "attempt_number": 0,
        "agent_name": "recommend_agent",
        "tool_name": "search",
        "validated_args": {"query": "dress", "max_price": 100},
        "entity_type": "product",
        "entity_id": "P001",
        "normalized_facts": {"price": 79.99, "tags": ["dress", "black"]},
        "success": True,
    }
    data.update(overrides)
    return EvidenceEntry(**data)


def make_tool_call_record(**overrides):
    data = {
        "sequence": 0,
        "sub_intent_id": "sub_1",
        "attempt_number": 0,
        "agent_name": "recommend_agent",
        "tool_name": "search",
        "validated_args": {"query": "dress"},
        "success": True,
    }
    data.update(overrides)
    return ToolCallRecord(**data)


def test_valid_evidence_entry_creation():
    entry = make_evidence_entry()

    assert entry.evidence_id.startswith("ev_")
    assert entry.sequence == 0
    assert entry.original_args == {}
    assert entry.normalized_facts["price"] == 79.99


def test_valid_tool_call_record_creation():
    record = make_tool_call_record()

    assert record.call_id.startswith("tc_")
    assert record.evidence_id is None
    assert record.original_args == {}
    assert record.success is True


def test_valid_proposed_claim_creation():
    claim = ProposedClaim(
        claim_type="product_price",
        subject_id="P001",
        field="price",
        value=79.99,
        evidence_ids=["ev_1"],
        source_agent="recommend_agent",
    )

    assert claim.claim_id.startswith("cl_")
    assert claim.evidence_ids == ["ev_1"]


def test_valid_verification_result_creation():
    rejected = RejectedClaim(
        claim_id="cl_bad",
        reason_code="missing_evidence",
        explanation="No stock evidence was recorded.",
        responsible_agent="inventory_agent",
        missing_evidence=["stock"],
    )

    result = VerificationResult(
        verified=False,
        approved_claim_ids=["cl_good"],
        rejected_claims=[rejected],
        missing_evidence=["stock"],
        correction_agent="inventory_agent",
    )

    assert not result.verified
    assert result.rejected_claims[0].claim_id == "cl_bad"


def test_unique_default_ids_where_defaults_are_supplied():
    assert make_evidence_entry().evidence_id != make_evidence_entry().evidence_id
    assert make_tool_call_record().call_id != make_tool_call_record().call_id
    assert (
        ProposedClaim(
            claim_type="product_name",
            field="name",
            value="Black Midi Dress",
            source_agent="recommend_agent",
        ).claim_id
        != ProposedClaim(
            claim_type="product_name",
            field="name",
            value="Wrap Dress",
            source_agent="recommend_agent",
        ).claim_id
    )


def test_timezone_aware_timestamps():
    entry = make_evidence_entry()
    record = make_tool_call_record()

    assert entry.timestamp.tzinfo is not None
    assert entry.timestamp.utcoffset() is not None
    assert record.timestamp.tzinfo is not None
    assert record.timestamp.utcoffset() is not None


def test_mutable_defaults_are_not_shared():
    first = VerificationResult(verified=True)
    second = VerificationResult(verified=True)

    first.approved_claim_ids.append("cl_1")
    first.rejected_claims.append(
        RejectedClaim(
            claim_id="cl_bad",
            reason_code="missing",
            explanation="Missing evidence.",
        )
    )
    first.missing_evidence.append("stock")

    assert second.approved_claim_ids == []
    assert second.rejected_claims == []
    assert second.missing_evidence == []


def test_json_serialization_and_reconstruction():
    entry = make_evidence_entry()
    payload = entry.model_dump_json()

    reconstructed = EvidenceEntry.model_validate_json(payload)

    assert reconstructed == entry
    assert isinstance(reconstructed.timestamp, datetime)


def test_nested_json_compatible_normalized_facts():
    entry = make_evidence_entry(
        normalized_facts={
            "product": {
                "id": "P001",
                "price": 79.99,
                "available": True,
                "sizes": ["S", "M"],
                "promotion": None,
            }
        }
    )

    assert entry.normalized_facts["product"]["sizes"] == ["S", "M"]


def test_rejects_negative_sequence():
    with pytest.raises(ValidationError):
        make_evidence_entry(sequence=-1)


def test_rejects_negative_attempt_number():
    with pytest.raises(ValidationError):
        make_tool_call_record(attempt_number=-1)


@pytest.mark.parametrize(
    "model_factory,field_name",
    [
        (lambda: make_evidence_entry(evidence_id=" "), "evidence_id"),
        (lambda: make_evidence_entry(tool_name=""), "tool_name"),
        (
            lambda: ProposedClaim(
                claim_type=" ",
                field="price",
                value=79.99,
                source_agent="recommend_agent",
            ),
            "claim_type",
        ),
        (
            lambda: ProposedClaim(
                claim_type="product_price",
                field=" ",
                value=79.99,
                source_agent="recommend_agent",
            ),
            "field",
        ),
    ],
)
def test_rejects_blank_required_identifiers(model_factory, field_name):
    with pytest.raises(ValidationError) as exc_info:
        model_factory()

    assert field_name in str(exc_info.value)


@pytest.mark.parametrize(
    "bad_value",
    [
        object(),
        ValueError("boom"),
        lambda: None,
        b"bytes",
        {"nested": object()},
    ],
)
def test_rejects_non_json_compatible_normalized_facts(bad_value):
    with pytest.raises(ValidationError):
        make_evidence_entry(normalized_facts={"bad": bad_value})


def test_rejects_malformed_nested_rejected_claim_input():
    with pytest.raises(ValidationError):
        VerificationResult(
            verified=False,
            rejected_claims=[
                {
                    "claim_id": "",
                    "reason_code": "missing_evidence",
                    "explanation": "Missing evidence.",
                }
            ],
        )


def test_existing_chat_request_and_response_schemas_remain_unchanged():
    request_fields = ChatRequest.model_fields
    response_fields = ChatResponse.model_fields

    assert set(request_fields) == {"message", "session_id"}
    assert request_fields["message"].is_required()
    assert request_fields["session_id"].default is None
    assert set(response_fields) == {"session_id", "reply", "products"}
    assert response_fields["session_id"].is_required()
    assert response_fields["reply"].is_required()
    assert response_fields["products"].default == []
