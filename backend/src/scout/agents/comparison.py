from __future__ import annotations

import math
from typing import Any

from scout.agents.verification_flow.finalization import FinalizedResponse


_PRODUCT_COMPARISON_LIMITATION = "I couldn’t compare verified products for that part of the request."


def _finalize_product_comparison(state: Any, subgoal: Any) -> FinalizedResponse:
    state.comparison_candidates = list(state.selected_products)
    state.ranking_criteria = _comparison_ranking_criteria(state)
    if not state.comparison_candidates:
        return FinalizedResponse(
            reply=_PRODUCT_COMPARISON_LIMITATION,
            products=[],
            proposed_claims=[],
            verification_result=type("Verification", (), {"approved_claim_ids": []})(),
        )

    best = max(state.comparison_candidates, key=lambda product: _comparison_score(product, state))
    state.selected_best_product_id = best["product_id"]
    state.selected_products = [best]
    state.selected_product_ids = [best["product_id"]]
    return FinalizedResponse(
        reply=_comparison_reply(best, state),
        products=[best],
        proposed_claims=[],
        verification_result=type("Verification", (), {"approved_claim_ids": []})(),
    )


def _comparison_ranking_criteria(state: Any) -> list[str]:
    criteria = ["verified product match"]
    if state.budget_max is not None:
        criteria.append("budget fit")
    if any(product.get("rating") is not None for product in state.selected_products):
        criteria.append("rating")
    if any(isinstance(product.get("promotion"), dict) for product in state.selected_products):
        criteria.append("sale price")
    criteria.append("lower price")
    return criteria


def _comparison_score(product: dict, state: Any) -> tuple:
    price = _safe_float(product.get("price"))
    sale_price = _safe_float(product.get("promotion", {}).get("discounted_price")) if isinstance(product.get("promotion"), dict) else None
    effective_price = sale_price if sale_price is not None else price
    rating = _safe_float(product.get("rating")) or 0
    budget_limit = state.budget_max
    budget_fit = 1 if budget_limit is None or (effective_price is not None and effective_price <= budget_limit) or (price is not None and price <= budget_limit) else 0
    discount = (price - sale_price) if price is not None and sale_price is not None and sale_price < price else 0
    return (
        budget_fit,
        rating,
        discount,
        -(effective_price if effective_price is not None else 10_000),
        str(product.get("name", "")),
    )


def _comparison_reply(product: dict, state: Any) -> str:
    name = product.get("name") or product["product_id"]
    scope = f"the {len(state.comparison_candidates)}" if len(state.comparison_candidates) > 1 else "the"
    price = _safe_float(product.get("price"))
    promotion = product.get("promotion")
    sale_price = _safe_float(promotion.get("discounted_price")) if isinstance(promotion, dict) else None
    if sale_price is not None:
        sale_phrase = f" and its sale price is ${sale_price:.2f}"
    else:
        sale_phrase = ""
    rating = _safe_float(product.get("rating"))
    rating_phrase = ""
    if rating is not None:
        if _has_highest_rating(product, state.comparison_candidates):
            rating_phrase = f", has the highest rating at {rating:.1f}"
        else:
            rating_phrase = f", has a {rating:.1f} rating"
    if price is not None:
        budget_phrase = f" under ${state.budget_max:.0f}" if state.budget_max is not None and price <= state.budget_max else ""
        return f"Of {scope}, I’d pick {name}. It’s{budget_phrase} at ${price:.2f}{rating_phrase}{sale_phrase}."
    basis = ", ".join(state.ranking_criteria)
    suffix = f" based on {basis}" if basis else ""
    return f"Of {scope}, I’d pick {name}{suffix}."


def _has_highest_rating(product: dict, candidates: list[dict]) -> bool:
    rating = _safe_float(product.get("rating"))
    candidate_ratings = [_safe_float(candidate.get("rating")) for candidate in candidates]
    numeric_ratings = [value for value in candidate_ratings if value is not None]
    return bool(numeric_ratings) and rating is not None and rating >= max(numeric_ratings)


def _safe_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
