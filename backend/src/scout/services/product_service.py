import re
from typing import Optional

from sqlalchemy.orm import Session

from scout.db.models import Product
from scout.repositories.product_repository import ProductRepository
from scout.repositories.stock_repository import StockRepository
from scout.repositories.promotion_repository import PromotionRepository
from scout.services.cache import ttl_cache

PRICE_PHRASE_PATTERN = re.compile(
    r"(?:under|below|less than)\s*\$?(\d+(?:\.\d{1,2})?)", re.IGNORECASE
)
STOPWORDS = {
    "a", "an", "the", "for", "in", "on", "at", "to", "of", "and", "or",
    "under", "below", "less", "than", "some", "any", "me", "my", "is",
    "are", "do", "you", "have", "with",
}

# Maps common singular/plural phrasing to the actual category values in
# the catalog. If a query token matches one of these, it's treated as a
# CATEGORY FILTER (must match), not a soft OR-keyword — this is what
# stops "comfortable shoes" from also matching unrelated products that
# merely contain the word "comfortable".
CATEGORY_WORD_MAP = {
    "shoe": "shoes", "shoes": "shoes", "sneaker": "shoes", "sneakers": "shoes",
    "boot": "shoes", "boots": "shoes",
    "dress": "dresses", "dresses": "dresses",
    "jacket": "outerwear", "jackets": "outerwear", "coat": "outerwear", "coats": "outerwear",
    "top": "tops", "tops": "tops", "shirt": "tops", "shirts": "tops",
    "blouse": "tops", "sweater": "tops",
    "pant": "bottoms", "pants": "bottoms", "jean": "bottoms", "jeans": "bottoms",
    "short": "bottoms", "shorts": "bottoms", "skirt": "bottoms", "trouser": "bottoms",
    "accessory": "accessories", "accessories": "accessories", "bag": "accessories",
    "scarf": "accessories", "belt": "accessories",
}

# Color words — if a query mentions one of these, ONLY products explicitly
# tagged with that color should match, rather than any product that
# happens to share an unrelated word (e.g. "party") from the same query.
KNOWN_COLORS = {
    "black", "white", "red", "blue", "green", "yellow", "pink", "purple",
    "brown", "gray", "grey", "navy", "beige", "tan", "khaki", "cream",
    "gold", "silver", "orange", "denim",
}

SIZE_ALIASES = {
    "extra small": "XS",
    "x-small": "XS",
    "xsmall": "XS",
    "small": "S",
    "medium": "M",
    "large": "L",
    "extra large": "XL",
    "x-large": "XL",
    "xlarge": "XL",
}


def _normalize_size(size: Optional[str]) -> Optional[str]:
    if not size:
        return None
    stripped = size.strip()
    return SIZE_ALIASES.get(stripped.lower(), stripped)


def _extract_price_constraint(query: str) -> tuple[str, Optional[float]]:
    """Deterministically pulls a price phrase like 'under $100' out of a
    free-text query, returning (query_with_phrase_removed, extracted_max_price).
    No LLM involved — plain regex, consistent with this endpoint's
    deterministic-only design."""
    match = PRICE_PHRASE_PATTERN.search(query)
    if not match:
        return query, None
    max_price = float(match.group(1))
    cleaned_query = query[: match.start()] + query[match.end():]
    return cleaned_query.strip(), max_price


def _tokenize(query: str) -> list[str]:
    """Splits a free-text query into meaningful words, dropping common
    stopwords so a phrase like 'comfortable shoes' matches products
    containing EITHER word, not the whole phrase verbatim."""
    words = re.findall(r"[a-zA-Z0-9]+", query.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 1]


def _get_active_promotion_dict(session: Session, product: Product) -> Optional[dict]:
    """Same promotion-lookup logic used by the AI's recommendation
    pipeline (product-specific promotions take priority over category-
    wide ones) — reused here so plain browsing shows the SAME sale price
    a customer would get by asking Scout directly."""
    promo_repo = PromotionRepository(session)
    promo = promo_repo.get_for_product(product.product_id)
    if not promo:
        promo = promo_repo.get_for_category(product.category)
    if not promo:
        return None
    return {
        "label": promo.label,
        "discount_percent": promo.discount_percent,
        "discounted_price": round(product.price * (1 - promo.discount_percent / 100), 2),
    }


def _product_to_dict(product: Product, session: Optional[Session] = None) -> dict:
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
    }
    if session is not None:
        promotion = _get_active_promotion_dict(session, product)
        if promotion:
            result["promotion"] = promotion
    return result


@ttl_cache
def search_products(
    session: Session,
    query: Optional[str] = None,
    category: Optional[str] = None,
    department: Optional[str] = None,
    brand: Optional[str] = None,
    max_price: Optional[float] = None,
    min_rating: Optional[float] = None,
    in_stock_only: bool = True,
    limit: int = 10,
) -> list[dict]:
    """Business logic lives here: deciding whether/how to apply in-stock
    filtering, extracting price phrases from free text, detecting
    category/color words as hard filters, and tokenizing multi-word
    queries so a phrase like 'comfortable shoes under $100' matches real
    products instead of requiring that exact phrase verbatim. Data access
    is delegated to the repositories."""
    product_repo = ProductRepository(session)
    stock_repo = StockRepository(session)

    if query and max_price is None:
        query, extracted_price = _extract_price_constraint(query)
        if extracted_price is not None:
            max_price = extracted_price

    product_id_filter = None
    if in_stock_only:
        product_id_filter = stock_repo.get_in_stock_product_ids()

    tokens = _tokenize(query) if query else []

    # Detect a hard category filter (e.g. "shoes") among the tokens.
    detected_category = category
    remaining_tokens = list(tokens)
    if detected_category is None:
        for token in tokens:
            if token in CATEGORY_WORD_MAP:
                detected_category = CATEGORY_WORD_MAP[token]
                remaining_tokens = [t for t in remaining_tokens if t != token]
                break

    # Detect a hard color filter (e.g. "red") among the remaining tokens.
    # Applied as a real tag check afterward — never treated as a soft
    # keyword, so "red party dress" cannot match a black dress just
    # because it shares the word "party".
    detected_color = None
    for token in remaining_tokens:
        if token in KNOWN_COLORS:
            detected_color = token
            remaining_tokens = [t for t in remaining_tokens if t != token]
            break

    keyword_tokens = remaining_tokens

    if not keyword_tokens:
        # Query was only a category and/or color word — one plain
        # filtered lookup, then apply the color check on top.
        results = product_repo.find(
            query=None,
            category=detected_category,
            department=department,
            brand=brand,
            max_price=max_price,
            min_rating=min_rating,
            product_id_filter=product_id_filter,
            limit=limit * 3 if detected_color else limit,
        )
    else:
        # Multi-word query: match products containing ANY remaining
        # keyword token, always within the detected category (if any).
        seen_ids = set()
        combined = []
        for token in keyword_tokens:
            token_results = product_repo.find(
                query=token,
                category=detected_category,
                department=department,
                brand=brand,
                max_price=max_price,
                min_rating=min_rating,
                product_id_filter=product_id_filter,
                limit=limit * 3 if detected_color else limit,
            )
            for p in token_results:
                if p.product_id not in seen_ids:
                    seen_ids.add(p.product_id)
                    combined.append(p)
        results = combined

    # Apply the color filter LAST, against the product's actual tags —
    # this is what makes color a genuine hard filter rather than a word
    # that merely needs to appear somewhere in the description.
    if detected_color:
        results = [p for p in results if detected_color in (p.tags or "").lower()]

    return [_product_to_dict(p, session) for p in results[:limit]]


def get_product(session: Session, product_id: str) -> Optional[dict]:
    product_repo = ProductRepository(session)
    product = product_repo.get_by_id(product_id)
    return _product_to_dict(product, session) if product else None


def get_product_variant_options(session: Session, product_id: str) -> list[dict]:
    stock_repo = StockRepository(session)
    return [
        {
            "size": variant.size,
            "color": variant.color,
            "quantity": variant.quantity,
            "in_stock": variant.quantity > 0,
        }
        for variant in stock_repo.get_variants(product_id)
    ]


def check_stock(
    session: Session,
    product_id: str,
    size: Optional[str] = None,
    color: Optional[str] = None,
) -> dict:
    requested_size = _normalize_size(size)
    requested_color = color.strip() if isinstance(color, str) and color.strip() else None
    product_repo = ProductRepository(session)
    stock_repo = StockRepository(session)
    product = product_repo.get_by_id(product_id)
    if not product:
        return {
            "product_id": product_id,
            "found": False,
            "requested_size": requested_size,
            "requested_color": requested_color,
            "in_stock": False,
            "total_quantity": 0,
            "variants": [],
        }

    variants = stock_repo.get_variants(
        product_id,
        size=requested_size,
        color=requested_color,
    )

    if not variants:
        return {
            "product_id": product_id,
            "found": True,
            "requested_size": requested_size,
            "requested_color": requested_color,
            "in_stock": False,
            "total_quantity": 0,
            "variants": [],
        }

    variant_list = [
        {
            "size": v.size,
            "color": v.color,
            "quantity": v.quantity,
            "in_stock": v.quantity > 0,
        }
        for v in variants
    ]
    total_qty = sum(v["quantity"] for v in variant_list)

    return {
        "product_id": product_id,
        "found": True,
        "requested_size": requested_size,
        "requested_color": requested_color,
        "in_stock": total_qty > 0,
        "total_quantity": total_qty,
        "variants": variant_list,
    }


def find_alternatives(
    session: Session,
    product_id: str,
    limit: int = 3,
    size: Optional[str] = None,
    color: Optional[str] = None,
    max_price: Optional[float] = None,
    category: Optional[str] = None,
) -> list[dict]:
    product_repo = ProductRepository(session)
    product = product_repo.get_by_id(product_id)
    if not product:
        return []

    requested_size = _normalize_size(size)
    requested_color = color.strip().lower() if isinstance(color, str) and color.strip() else None
    target_category = category or product.category
    candidates = product_repo.find_by_category(
        target_category, exclude_product_id=product_id, limit=limit * 6
    )

    matching_color = []
    fallback_color = []
    for c in candidates:
        if max_price is not None and c.price > max_price:
            continue
        stock_info = check_stock(
            session,
            c.product_id,
            size=requested_size,
            color=requested_color,
        )
        if stock_info["in_stock"]:
            product_dict = _product_to_dict(c, session)
            product_dict["variant_stock"] = {
                "size": stock_info.get("requested_size"),
                "color": stock_info.get("requested_color"),
                "quantity": stock_info.get("total_quantity"),
                "in_stock": stock_info.get("in_stock"),
            }
            if requested_color and requested_color in (c.tags or "").lower():
                matching_color.append(product_dict)
            else:
                fallback_color.append(product_dict)
        if len(matching_color) >= limit:
            break

    return [*matching_color, *fallback_color][:limit]


def add_to_cart_service(
    session: Session,
    product_id: str,
    quantity: int = 1,
    size: str | None = None,
    color: str | None = None,
    recommendation_id: str | None = None,
    recommendation_session_id: str | None = None,
) -> dict:
    """Shared, deterministic cart-add validation - the single source of
    truth called by both api/cart.py's HTTP endpoint AND Phase 2's
    proactive cart-offer confirmation (supervisor.py). Re-validates real
    stock and price every time, regardless of caller, since availability
    can genuinely change between when a recommendation was made and when
    the customer actually confirms adding it.
    """
    from scout.agents.attribution import validate_recommendation

    product_repo = ProductRepository(session)
    product = product_repo.get_by_id(product_id)

    if not product:
        return {"success": False, "error": "Product not found"}
    if quantity < 1:
        return {"success": False, "error": "Quantity must be at least 1"}

    stock_info = check_stock(session, product_id, size=size, color=color)
    requested_variant = bool(size or color)
    if not stock_info["in_stock"]:
        error = "Out of stock"
        if requested_variant:
            variant = []
            if stock_info["requested_color"]:
                variant.append(stock_info["requested_color"].title())
            if stock_info["requested_size"]:
                variant.append(f"size {stock_info['requested_size']}")
            error = f"{product.name} in {', '.join(variant)} is currently out of stock." if variant else f"{product.name} is currently out of stock."
        return {"success": False, "error": error, "in_stock": False}

    if quantity > stock_info["total_quantity"]:
        return {
            "success": False,
            "error": f"Only {stock_info['total_quantity']} available for {product.name}.",
            "in_stock": True,
            "available_quantity": stock_info["total_quantity"],
        }

    promotion = _get_active_promotion_dict(session, product)
    unit_price = promotion["discounted_price"] if promotion else product.price
    is_scout_attributed = validate_recommendation(
        recommendation_id,
        product.product_id,
        recommendation_session_id,
    )

    return {
        "success": True,
        "in_stock": True,
        "product_id": product.product_id,
        "name": product.name,
        "brand": product.brand,
        "image_url": product.image_url,
        "unit_price": unit_price,
        "quantity": quantity,
        "line_total": round(unit_price * quantity, 2),
        "promotion": promotion,
        "size": stock_info["requested_size"],
        "color": stock_info["requested_color"],
        "available_quantity": stock_info["total_quantity"],
        "attribution_source": "scout" if is_scout_attributed else None,
        "recommendation_id": recommendation_id if is_scout_attributed else None,
        "recommendation_session_id": recommendation_session_id if is_scout_attributed else None,
    }
