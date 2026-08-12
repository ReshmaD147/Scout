from __future__ import annotations

from dataclasses import dataclass
import sys

from scout.agents.claims import propose_claims as _propose_claims
from scout.agents.diagnostics import timed_stage
from scout.agents.rendering import final_safety_scan, render_verified_response
from scout.agents.verification import verify_claims as _verify_claims
from scout.agents.verification import verify_price_grounding as _verify_price_grounding


@dataclass
class FinalizedResponse:
    reply: str
    products: list
    proposed_claims: list
    verification_result: object


def _supervisor_override(name: str, current):
    supervisor_module = sys.modules.get("scout.agents.supervisor")
    override = getattr(supervisor_module, name, None) if supervisor_module is not None else None
    if override is not None and override is not current:
        return override
    return current


def _finalize_verified_response(
    *,
    original_reply: str,
    products: list,
    evidence_entries: list,
    customer_message: str,
) -> FinalizedResponse:
    with timed_stage("claim_proposal"):
        proposed_claims = _supervisor_override("propose_claims", _propose_claims)(
            reply_text=original_reply,
            products=products,
            evidence_entries=evidence_entries,
        )
    with timed_stage("claim_verification"):
        verification_result = _supervisor_override("verify_claims", _verify_claims)(
            proposed_claims=proposed_claims,
            evidence_entries=evidence_entries,
            customer_message=customer_message,
        )
    with timed_stage("rendering"):
        rendered_reply, rendered_products = render_verified_response(
            original_reply=original_reply,
            products=products,
            proposed_claims=proposed_claims,
            verification_result=verification_result,
            customer_message=customer_message,
        )
    passed, _ = _supervisor_override("verify_price_grounding", _verify_price_grounding)(
        rendered_reply,
        rendered_products,
        customer_message=customer_message,
    )
    if not passed:
        rendered_reply = "I couldn’t verify a reliable product result for that request."
        rendered_products = []
    approved_ids = set(verification_result.approved_claim_ids)
    rendered_reply, rendered_products = final_safety_scan(
        reply=rendered_reply,
        products=rendered_products,
        approved_claims=[claim for claim in proposed_claims if claim.claim_id in approved_ids],
        customer_message=customer_message,
    )
    return FinalizedResponse(
        reply=rendered_reply,
        products=rendered_products,
        proposed_claims=proposed_claims,
        verification_result=verification_result,
    )


def _response_usefulness(result: FinalizedResponse) -> tuple[int, int, int]:
    approved_count = len(result.verification_result.approved_claim_ids)
    rejected_count = len(result.verification_result.rejected_claims)
    product_count = len(result.products)
    return approved_count, -rejected_count, product_count


def _choose_better_response(
    initial: FinalizedResponse,
    corrected: FinalizedResponse,
) -> FinalizedResponse:
    return corrected if _response_usefulness(corrected) > _response_usefulness(initial) else initial
