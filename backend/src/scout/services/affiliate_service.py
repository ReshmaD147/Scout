from typing import Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator
from sqlalchemy.orm import Session

from scout.db.models import ExternalProduct
from scout.repositories.external_product_repository import ExternalProductRepository
from scout.repositories.affiliate_click_repository import AffiliateClickRepository


# ─────────────────────────────────────────────────────────
# REQUEST / RESPONSE SCHEMAS
# Pydantic models validate and normalize search filters and offer data
# at the boundary, rather than passing loose dicts through the pipeline.
# ─────────────────────────────────────────────────────────

class ExternalOfferSearchFilters(BaseModel):
    query: str = ""
    category: str = ""
    subcategory: str | None = None
    use_case: str | None = None
    required_attributes: list[str] = Field(default_factory=list)
    waterproof: bool | None = None
    color: str | None = None
    budget_max: float | None = None
    availability_required: bool = True

    @field_validator("budget_max")
    @classmethod
    def _valid_budget(cls, value):
        if value is not None and value < 0:
            raise ValueError("budget_max must be non-negative")
        return value

    @field_validator("required_attributes", mode="before")
    @classmethod
    def _normalize_attributes(cls, value):
        if value in (None, ""):
            return []
        if isinstance(value, str):
            return [item.strip().lower() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item).strip().lower() for item in value if str(item).strip()]
        raise ValueError("required_attributes must be a list or comma-separated string")


class StructuredExternalOffer(BaseModel):
    external_product_id: str
    name: str
    vendor_name: str
    category: str
    subcategory: str | None = None
    use_cases: list[str] = Field(default_factory=list)
    attributes: list[str] = Field(default_factory=list)
    waterproof: bool | None = None
    color: str | None = None
    price: float
    original_price: float | None = None
    currency: str = "USD"
    availability: str = "in_stock"
    product_url: HttpUrl
    click_url: str
    source: str = "external"
    last_verified_at: str
    rating: float | None = None
    image_url: str | None = None

    @field_validator("price", "original_price")
    @classmethod
    def _valid_price(cls, value):
        if value is not None and value < 0:
            raise ValueError("price must be non-negative")
        return value


# ─────────────────────────────────────────────────────────
# DEMO CATALOG SUPPLEMENT
# A small, fixed set of hand-authored offers layered on top of the real
# database-backed ExternalProduct catalog — used to guarantee specific
# demo scenarios (e.g. a real waterproof hiking shoe match) always exist
# regardless of what's currently seeded in the database. Not a pattern
# intended for production data.
# ─────────────────────────────────────────────────────────

DEMO_EXTERNAL_OFFERS = {
    "EX010": {
        "external_product_id": "EX010",
        "name": "TrailGuard Waterproof Hiking Shoe",
        "vendor_name": "Outdoor Demo Retailer",
        "category": "shoes",
        "subcategory": "hiking shoes",
        "use_cases": ["hiking"],
        "attributes": ["waterproof", "trail"],
        "waterproof": True,
        "color": "black",
        "price": 64.99,
        "original_price": 79.99,
        "currency": "USD",
        "availability": "in_stock",
        "product_url": "https://example.com/outdoor-demo/trailguard-waterproof-hiking-shoe",
        "click_url": "/affiliate/click/EX010",
        "source": "external",
        "last_verified_at": "2026-07-30T00:00:00+00:00",
        "rating": 4.4,
    }
}


# ─────────────────────────────────────────────────────────
# TAG ENCODING HELPERS
# ExternalProduct stores flexible attributes as comma-separated
# "prefix:value" tags (e.g. "waterproof:true", "use:hiking") rather than
# dedicated columns, so new attribute types don't require a migration.
# ─────────────────────────────────────────────────────────

def _tag_values(tags: list[str], prefix: str) -> list[str]:
    """Return every value for a given tag prefix (e.g. all "use:X" tags)."""
    marker = f"{prefix}:"
    return [tag[len(marker):].strip().lower() for tag in tags if tag.startswith(marker) and tag[len(marker):].strip()]


def _tag_value(tags: list[str], prefix: str) -> str | None:
    """Return the first value for a given tag prefix, or None."""
    values = _tag_values(tags, prefix)
    return values[0] if values else None


def _tag_bool(tags: list[str], prefix: str) -> bool | None:
    """Parse a tag value as a boolean (true/yes/1 vs false/no/0), or
    None if the tag isn't present or isn't a recognized boolean string.
    """
    value = _tag_value(tags, prefix)
    if value in {"true", "yes", "1"}:
        return True
    if value in {"false", "no", "0"}:
        return False
    return None


def _external_product_to_dict(product: ExternalProduct) -> dict:
    """Convert an ExternalProduct row (with its tag-encoded attributes)
    into the same structured offer shape as DEMO_EXTERNAL_OFFERS, so
    both sources can be filtered and ranked identically downstream.
    """
    tags = [t.strip() for t in product.tags.split(",") if t.strip()]
    offer = {
        "external_product_id": product.external_product_id,
        "name": product.name,
        "vendor_name": product.vendor_name,
        "category": product.category,
        "subcategory": _tag_value(tags, "subcategory"),
        "use_cases": _tag_values(tags, "use"),
        "attributes": _tag_values(tags, "attr"),
        "waterproof": _tag_bool(tags, "waterproof"),
        "color": _tag_value(tags, "color"),
        "price": product.price,
        "original_price": None,
        "currency": "USD",
        "availability": _tag_value(tags, "availability") or "in_stock",
        "product_url": product.affiliate_link_template,
        "click_url": f"/affiliate/click/{product.external_product_id}",
        "source": "external",
        "last_verified_at": "2026-07-30T00:00:00+00:00",
        "rating": product.rating,
        "image_url": product.image_url,
        "tags": tags,
    }
    return _validated_offer_dict(offer)


# ─────────────────────────────────────────────────────────
# CATEGORY / QUERY NORMALIZATION
# ─────────────────────────────────────────────────────────

# Maps loose customer/model phrasing to the actual category values
# stored in our (small, fixed) mock catalog.
CATEGORY_SYNONYMS = {
    "cocktail dress": "dresses", "party dress": "dresses",
    "evening dress": "dresses", "gown": "dresses",
    "sneaker": "shoes", "sneakers": "shoes", "trainer": "shoes",
    "jacket": "outerwear", "coat": "outerwear",
    "shirt": "tops", "t-shirt": "tops", "blouse": "tops",
    "jeans": "bottoms", "pants": "bottoms", "trousers": "bottoms",
    "footwear": "shoes", "hiking shoe": "shoes", "hiking shoes": "shoes",
}


def _normalize_category(category: str) -> str:
    """Map loose phrasing to a real catalog category. Uses a crude
    singularize (strip trailing 's') rather than a real NLP lemmatizer —
    good enough for this catalog's small, known vocabulary.
    """
    key = category.strip().lower().rstrip("s")  # crude singularize
    for phrase, real_category in CATEGORY_SYNONYMS.items():
        phrase_singular = phrase.rstrip("s")
        if phrase_singular in key or key in phrase_singular:
            return real_category
    return category


def _category_from_query(query: str) -> str:
    """Infer a category from free-text if none was given explicitly."""
    lowered = (query or "").lower()
    if "shoe" in lowered or "footwear" in lowered:
        return "shoes"
    if "dress" in lowered:
        return "dresses"
    return ""


def _use_case_from_query(query: str) -> str | None:
    """Infer a use-case (hiking, running, etc.) from free-text."""
    lowered = (query or "").lower()
    for use_case in ("hiking", "running", "formal", "casual", "athletic"):
        if use_case in lowered:
            return use_case
    return None


# ─────────────────────────────────────────────────────────
# FILTER BUILDING
# ─────────────────────────────────────────────────────────

def _build_filters(**kwargs) -> ExternalOfferSearchFilters:
    """Assemble a validated ExternalOfferSearchFilters from loose
    keyword args, inferring category/use_case/waterproof from free text
    when not explicitly provided.
    """
    query = kwargs.get("query") or ""
    category = kwargs.get("category") or _category_from_query(query)
    required = kwargs.get("required_attributes")
    use_case = kwargs.get("use_case") or _use_case_from_query(query)
    waterproof = kwargs.get("waterproof")
    query_lower = query.lower()
    if waterproof is None and "waterproof" in query_lower:
        waterproof = True
    if required in (None, "") and "waterproof" in query_lower:
        required = ["waterproof"]
    return ExternalOfferSearchFilters(
        query=query,
        category=category or "",
        subcategory=kwargs.get("subcategory"),
        use_case=use_case,
        required_attributes=required or [],
        waterproof=waterproof,
        color=kwargs.get("color"),
        budget_max=kwargs.get("budget_max") if kwargs.get("budget_max") is not None else kwargs.get("max_price"),
    )


# ─────────────────────────────────────────────────────────
# MATCHING & ANNOTATION
# ─────────────────────────────────────────────────────────

def _offer_matches(offer: dict, filters: ExternalOfferSearchFilters) -> bool:
    """True if an offer satisfies every hard constraint in filters."""
    if filters.category and _normalize_category(offer.get("category", "")) != _normalize_category(filters.category):
        return False
    if filters.subcategory and filters.subcategory.lower() not in str(offer.get("subcategory", "")).lower():
        return False
    if filters.use_case and filters.use_case.lower() not in offer.get("use_cases", []):
        return False
    if filters.waterproof is not None and offer.get("waterproof") is not filters.waterproof:
        return False
    attributes = set(offer.get("attributes") or [])
    for attribute in filters.required_attributes:
        if attribute not in attributes:
            return False
    if filters.color and offer.get("color") and offer["color"].lower() != filters.color.lower():
        return False
    if filters.budget_max is not None and offer.get("price", 0) > filters.budget_max:
        return False
    if filters.availability_required and offer.get("availability") != "in_stock":
        return False
    return True


def _annotate_constraints(offer: dict, filters: ExternalOfferSearchFilters) -> dict:
    """Attach a human-readable list of which requested constraints this
    offer actually satisfies — lets the agent explain WHY a match is
    good, not just present it as a bare result.
    """
    annotated = dict(offer)
    satisfied = []
    if filters.use_case and filters.use_case in annotated.get("use_cases", []):
        satisfied.append(filters.use_case)
    if filters.waterproof is True and annotated.get("waterproof") is True:
        satisfied.append("waterproof")
    for attribute in filters.required_attributes:
        if attribute in annotated.get("attributes", []):
            satisfied.append(attribute)
    if filters.budget_max is not None and annotated.get("price", 0) <= filters.budget_max:
        satisfied.append(f"under ${filters.budget_max:g}")
    annotated["satisfied_constraints"] = sorted(set(satisfied))
    return annotated


def _requested_constraints(filters: ExternalOfferSearchFilters) -> list[str]:
    """Human-readable list of every constraint that was actually
    requested, regardless of whether any offer satisfies it — used for
    an honest "here's what I was looking for" explanation when nothing
    matches.
    """
    constraints = []
    if filters.category:
        constraints.append(filters.category)
    if filters.use_case:
        constraints.append(filters.use_case)
    if filters.waterproof is True:
        constraints.append("waterproof")
    constraints.extend(filters.required_attributes)
    if filters.budget_max is not None:
        constraints.append(f"under ${filters.budget_max:g}")
    return sorted(set(constraints))


def _validated_offer_dict(offer: dict) -> dict:
    """Run a raw offer dict through StructuredExternalOffer for
    validation, returning a JSON-safe plain dict.
    """
    return StructuredExternalOffer(**dict(offer)).model_dump(mode="json")


# ─────────────────────────────────────────────────────────
# PUBLIC SERVICE FUNCTIONS
# ─────────────────────────────────────────────────────────

def find_external_alternative(
    session: Session,
    category: str,
    max_price: Optional[float] = None,
    limit: int = 3,
    query: str = "",
    subcategory: str | None = None,
    use_case: str | None = None,
    required_attributes: list[str] | str | None = None,
    waterproof: bool | None = None,
    color: str | None = None,
    budget_max: Optional[float] = None,
    availability_required: bool = True,
) -> list[dict]:
    """Find real third-party vendor products matching the given
    constraints, combining the database-backed catalog with the fixed
    demo supplement. Used when the internal catalog has no genuine
    match for a customer's request.
    """
    filters = _build_filters(
        query=query,
        category=category,
        subcategory=subcategory,
        use_case=use_case,
        required_attributes=required_attributes,
        waterproof=waterproof,
        color=color,
        budget_max=budget_max if budget_max is not None else max_price,
        availability_required=availability_required,
    )
    normalized = _normalize_category(filters.category)

    ext_repo = ExternalProductRepository(session)
    db_products = ext_repo.find_by_category(normalized)
    db_offers = [_external_product_to_dict(p) for p in db_products]

    offers_by_id = {offer["external_product_id"]: offer for offer in db_offers}
    for offer_id, offer in DEMO_EXTERNAL_OFFERS.items():
        offers_by_id.setdefault(offer_id, _validated_offer_dict(offer))

    candidates = list(offers_by_id.values())
    filtered = [_annotate_constraints(offer, filters) for offer in candidates]
    matches = [offer for offer in filtered if _offer_matches(offer, filters)]
    matches.sort(key=lambda offer: (offer["price"], offer["external_product_id"]))
    return matches[:limit]


def log_affiliate_click(
    session: Session,
    external_product_id: str,
    session_id: Optional[str] = None,
) -> dict:
    """Record that a customer followed a link to an external vendor,
    and return the vendor's real redirect URL.
    """
    ext_repo = ExternalProductRepository(session)
    click_repo = AffiliateClickRepository(session)

    product = ext_repo.get_by_id(external_product_id)
    if not product:
        return {"success": False, "error": "External product not found"}

    click = click_repo.create(external_product_id=external_product_id, session_id=session_id)
    session.commit()

    return {
        "success": True,
        "click_id": click.click_id,
        "redirect_url": product.affiliate_link_template,
        "vendor_name": product.vendor_name,
        "clicked_at": click.clicked_at.isoformat(),
    }


def get_click_stats(session: Session) -> dict:
    """Return total affiliate clicks grouped by vendor name."""
    click_repo = AffiliateClickRepository(session)
    return {"clicks_by_vendor": click_repo.get_click_counts_by_vendor()}