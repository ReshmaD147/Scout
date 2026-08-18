from typing import Optional

from sqlalchemy.orm import Session

from scout.db.models import Product, Promotion
from scout.repositories.product_repository import ProductRepository
from scout.repositories.promotion_repository import PromotionRepository
from scout.services.product_service import check_stock
from scout.services.cache import ttl_cache
from scout.services.recommendation_feedback_service import feedback_adjustment_for_product

# ─────────────────────────────────────────────────────────
# SCORING WEIGHTS
# Must sum to 1.0. Each individual score below is bounded 0-1 before
# weighting, so the final combined score is also bounded 0-1.
# ─────────────────────────────────────────────────────────

WEIGHTS = {
    "rating": 0.35,
    "price_fit": 0.30,
    "promotion": 0.15,
    "semantic_rank": 0.20,
}


# ─────────────────────────────────────────────────────────
# SCORING HELPERS
# ─────────────────────────────────────────────────────────

def _get_active_promotion(session: Session, product: Product) -> Optional[Promotion]:
    """Product-specific promotions take priority over category-wide ones.

    NOTE: this duplicates the same lookup logic as
    product_service._get_active_promotion_dict(). Both exist because one
    returns the ORM object (needed here for discount_percent access) and
    the other returns a plain dict — worth consolidating into one shared
    helper if this drifts further.
    """
    promo_repo = PromotionRepository(session)
    promo = promo_repo.get_for_product(product.product_id)
    if promo:
        return promo
    return promo_repo.get_for_category(product.category)


def _score_candidate(
    product: Product,
    semantic_position: int,
    total_candidates: int,
    target_price: Optional[float],
    promotion: Optional[Promotion],
) -> float:
    """Weighted score combining rating, price-fit-to-budget, whether a
    promotion is active, and how strong the original semantic search
    match was (earlier position = stronger match).
    """
    rating_score = (product.rating or 0) / 5.0

    if target_price:
        price_diff_ratio = abs(product.price - target_price) / target_price
        price_fit_score = max(0.0, 1.0 - price_diff_ratio)
    else:
        price_fit_score = 0.5

    promotion_score = 1.0 if promotion else 0.0
    semantic_rank_score = 1.0 - (semantic_position / max(total_candidates, 1))

    return (
        WEIGHTS["rating"] * rating_score
        + WEIGHTS["price_fit"] * price_fit_score
        + WEIGHTS["promotion"] * promotion_score
        + WEIGHTS["semantic_rank"] * semantic_rank_score
    )


# ─────────────────────────────────────────────────────────
# PUBLIC SERVICE FUNCTION
# ─────────────────────────────────────────────────────────

@ttl_cache
def rank_products(
    session: Session,
    candidate_product_ids: list[str],
    target_price: Optional[float] = None,
    max_price: Optional[float] = None,
    top_n: int = 3,
    session_id: str | None = None,
    customer_id: str | None = None,
) -> list[dict]:
    """Score and rank a list of semantic-search candidate product IDs,
    filtering out anything not currently in stock, and return the top N
    as plain dicts ready for API/tool responses.
    """
    product_repo = ProductRepository(session)
    scored = []
    total = len(candidate_product_ids)

    for position, product_id in enumerate(candidate_product_ids):
        product = product_repo.get_by_id(product_id)
        if not product:
            continue
        if max_price is not None and product.price > max_price:
            continue

        stock_info = check_stock(session, product_id)
        if not stock_info["in_stock"]:
            continue

        promotion = _get_active_promotion(session, product)
        score = _score_candidate(product, position, total, target_price, promotion)
        score += feedback_adjustment_for_product(
            session,
            product_id=product.product_id,
            session_id=session_id,
            customer_id=customer_id,
        )

        result = {
            "product_id": product.product_id,
            "name": product.name,
            "brand": product.brand,
            "department": product.department,
            "category": product.category,
            "description": product.description,
            "price": product.price,
            "rating": product.rating,
            "image_url": product.image_url,
            "tags": [t.strip() for t in product.tags.split(",") if t.strip()],
            "score": round(score, 4),
        }

        if promotion:
            discounted_price = round(product.price * (1 - promotion.discount_percent / 100), 2)
            result["promotion"] = {
                "label": promotion.label,
                "discount_percent": promotion.discount_percent,
                "discounted_price": discounted_price,
            }

        scored.append(result)

    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:top_n]
