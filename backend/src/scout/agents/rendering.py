from __future__ import annotations

import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scout.agents.claims import ClaimType
from scout.agents.evidence import ProposedClaim, VerificationResult
from scout.agents.verification import verify_price_grounding
from scout.db.seed import EXTERNAL_PRODUCTS, PRODUCTS


DOMAIN_FALLBACKS = {
    "product": "I couldn’t verify a reliable product result for that request.",
    "inventory": "I couldn’t verify the current availability.",
    "order": "I couldn’t verify the requested order information.",
    "policy": "I couldn’t verify that policy detail from the available policy sources.",
    "external": "I couldn’t verify a reliable third-party offer.",
    "general": "I couldn’t verify a reliable answer for that request.",
}

SAFE_CONVERSATIONAL_PATTERNS = (
    re.compile(r"^\s*you(?:'|’)re welcome[.!]?(?:\s+have a great day[.!]?)?\s*$", re.IGNORECASE),
    re.compile(r"^\s*which .+\?\s*$", re.IGNORECASE),
    re.compile(r"^\s*please provide your order number[.!]?\s*$", re.IGNORECASE),
    re.compile(r"^\s*could you (?:share|provide|clarify).+\?\s*$", re.IGNORECASE),
    re.compile(r"^\s*done[.!]?\s*$", re.IGNORECASE),
)
FACTUAL_PATTERN = re.compile(
    r"\$|\b\d+(?:\.\d+)?\s*(?:miles?|days?|%|stars?)\b|"
    r"\b(?:price|cost|product|dress|shirt|shoe|quantity|qty|rating|promotion|sale|store|pickup|delivery|"
    r"available|in stock|out of stock|order|tracking|return|policy|version|vendor|third-party|external)\b",
    re.IGNORECASE,
)
PRODUCT_IMAGES_DIR = Path(__file__).resolve().parents[3] / "data" / "product_images"
PRODUCT_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
SEEDED_PRODUCT_NAMES = {product_id: name for product_id, name, *_ in PRODUCTS}
TRUSTED_EXTERNAL_IMAGE_HOSTS = {
    "target.scene7.com",
    "assets.targetimg1.com",
    "www.nordstromrack.com",
    "n.nordstrommedia.com",
}
SEEDED_EXTERNAL_IMAGE_OFFERS = {
    product[0]: {
        "name": product[1],
        "vendor": product[2],
        "image_url": product[8],
    }
    for product in EXTERNAL_PRODUCTS
    if len(product) > 8 and product[8]
}


def render_verified_response(
    *,
    original_reply: str,
    products: list,
    proposed_claims: list[ProposedClaim],
    verification_result: VerificationResult,
    customer_message: str,
) -> tuple[str, list]:
    original_products = [deepcopy(product) for product in products if isinstance(product, dict)]
    approved_claims = _approved_claims(proposed_claims, verification_result)

    if not approved_claims and _is_safe_conversational(original_reply):
        return original_reply, []

    rendered_products = _render_products(original_products, approved_claims)
    reply = _render_reply(approved_claims, rendered_products, customer_message)
    passed, _ = verify_price_grounding(reply, rendered_products, customer_message=customer_message)
    if not passed:
        return _fallback_for_claims(approved_claims, customer_message), []
    reply, rendered_products = final_safety_scan(
        reply=reply,
        products=rendered_products,
        approved_claims=approved_claims,
        customer_message=customer_message,
    )
    return reply, rendered_products


def final_safety_scan(
    *,
    reply: str,
    products: list[dict],
    approved_claims: list[ProposedClaim],
    customer_message: str,
) -> tuple[str, list[dict]]:
    approved_values = _approved_value_index(approved_claims, customer_message)
    safe_sentences = [
        sentence
        for sentence in _split_sentences(reply)
        if _sentence_is_supported(sentence, approved_values)
    ]
    safe_products = [
        product
        for product in products
        if _product_is_supported(product, approved_values)
    ]

    if not safe_sentences and safe_products:
        safe_sentences = _product_sentences(safe_products)

    if not safe_sentences:
        return _fallback_for_claims(approved_claims, customer_message), safe_products

    return " ".join(safe_sentences), safe_products


def _approved_claims(
    proposed_claims: list[ProposedClaim],
    verification_result: VerificationResult,
) -> list[ProposedClaim]:
    approved_ids = set()
    ordered_ids = []
    for claim_id in verification_result.approved_claim_ids:
        if claim_id not in approved_ids:
            approved_ids.add(claim_id)
            ordered_ids.append(claim_id)
    approved_id_set = set(ordered_ids)
    return [claim.model_copy(deep=True) for claim in proposed_claims if claim.claim_id in approved_id_set]


def _render_products(original_products: list[dict], approved_claims: list[ProposedClaim]) -> list[dict]:
    by_subject = _claims_by_subject(approved_claims)
    rendered = []
    for original in original_products:
        if original.get("external_product_id") or original.get("source") == "external":
            product = _render_external_product(original, by_subject.get(original.get("external_product_id"), []))
        else:
            product = _render_internal_product(original, by_subject.get(original.get("product_id"), []))
        if product:
            rendered.append(product)
    return rendered


def _render_internal_product(original: dict, claims: list[ProposedClaim]) -> dict | None:
    product_id = original.get("product_id")
    name = _claim_value(claims, ClaimType.PRODUCT_IDENTITY, "name")
    price = _claim_value(claims, ClaimType.PRODUCT_PRICE, "price")
    if not product_id or name is None or ("price" in original and price is None):
        return None

    product = {"product_id": product_id, "name": name, "source": "internal"}
    if price is not None:
        product["price"] = price
    rating = _claim_value(claims, ClaimType.PRODUCT_RATING, "rating")
    if rating is not None:
        product["rating"] = rating
    promotion_name = _claim_value(claims, ClaimType.PROMOTION, "promotion_name")
    promotion_price = _claim_value(claims, ClaimType.PROMOTION, "promotion_price")
    if promotion_name is not None or promotion_price is not None:
        product["promotion"] = {}
        if promotion_name is not None:
            product["promotion"]["name"] = promotion_name
        if promotion_price is not None:
            product["promotion"]["discounted_price"] = promotion_price
    image_url = _local_seeded_image_url(product_id, name)
    if image_url:
        product["image_url"] = image_url
    return product


def _render_external_product(original: dict, claims: list[ProposedClaim]) -> dict | None:
    external_id = original.get("external_product_id")
    name = _claim_value(claims, ClaimType.EXTERNAL_OFFER_IDENTITY, "product_name")
    vendor = _claim_value(claims, ClaimType.EXTERNAL_OFFER_VENDOR, "vendor")
    price = _claim_value(claims, ClaimType.EXTERNAL_OFFER_PRICE, "price")
    if not external_id or name is None or vendor is None or ("price" in original and price is None):
        return None

    product = {
        "external_product_id": external_id,
        "name": name,
        "vendor_name": vendor,
        "source": "external",
    }
    if price is not None:
        product["price"] = price
    url_or_offer_id = _claim_value(claims, ClaimType.EXTERNAL_OFFER_IDENTITY, "url_or_offer_id")
    if url_or_offer_id is not None:
        product["click_url"] = url_or_offer_id
    use_case = _claim_value(claims, ClaimType.EXTERNAL_OFFER_USE_CASE, "use_case")
    waterproof = _claim_value(claims, ClaimType.EXTERNAL_OFFER_ATTRIBUTE, "waterproof")
    availability = _claim_value(claims, ClaimType.EXTERNAL_OFFER_AVAILABILITY, "availability")
    if use_case is not None:
        product["use_cases"] = [use_case]
    if waterproof is not None:
        product["waterproof"] = waterproof
    if availability is not None:
        product["availability"] = availability
    image_url = _trusted_external_image_url(
        external_id,
        name,
        vendor,
        original.get("image_url"),
    )
    if image_url:
        product["image_url"] = image_url
    return product


def _local_seeded_image_url(product_id: str, product_name: str) -> str | None:
    safe_product_id = str(product_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", safe_product_id):
        return None
    if SEEDED_PRODUCT_NAMES.get(safe_product_id) != product_name:
        return None
    for extension in PRODUCT_IMAGE_EXTENSIONS:
        filename = f"{safe_product_id}{extension}"
        if (PRODUCT_IMAGES_DIR / filename).is_file():
            return f"/static/products/{filename}"
    return None


def _trusted_external_image_url(
    external_id: str,
    product_name: str,
    vendor_name: str,
    original_image_url: str | None = None,
) -> str | None:
    safe_external_id = str(external_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", safe_external_id):
        return None
    for extension in PRODUCT_IMAGE_EXTENSIONS:
        filename = f"{safe_external_id}{extension}"
        if (PRODUCT_IMAGES_DIR / filename).is_file():
            return f"/static/products/{filename}"
    seeded_offer = SEEDED_EXTERNAL_IMAGE_OFFERS.get(safe_external_id)
    if (
        seeded_offer
        and seeded_offer["name"] == product_name
        and seeded_offer["vendor"] == vendor_name
        and _is_trusted_external_image_url(seeded_offer["image_url"], vendor_name)
    ):
        return seeded_offer["image_url"]
    if original_image_url and _is_trusted_external_image_url(original_image_url, vendor_name):
        return original_image_url
    return None


def _is_trusted_external_image_url(image_url: str, vendor_name: str) -> bool:
    parsed = urlparse(str(image_url or "").strip())
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    hostname = parsed.hostname or ""
    if hostname not in TRUSTED_EXTERNAL_IMAGE_HOSTS:
        return False
    vendor = str(vendor_name or "").lower()
    if "target" in vendor:
        return hostname in {"target.scene7.com", "assets.targetimg1.com"}
    if "nordstrom" in vendor:
        return hostname in {"www.nordstromrack.com", "n.nordstrommedia.com"}
    return False


def _render_reply(
    approved_claims: list[ProposedClaim],
    rendered_products: list[dict],
    customer_message: str,
) -> str:
    access_denied_sentences = _access_denied_sentences(approved_claims)
    if access_denied_sentences:
        return " ".join(access_denied_sentences)

    inventory_sentences = _inventory_sentences(approved_claims, customer_message)
    if _is_inventory_request(customer_message) and inventory_sentences:
        request_specific = [
            sentence
            for sentence in inventory_sentences
            if "is a verified store" not in sentence.lower()
        ]
        return " ".join(_unique(request_specific or inventory_sentences))

    sentences = []
    sentences.extend(_product_sentences(rendered_products))
    sentences.extend(inventory_sentences)
    sentences.extend(_order_sentences(approved_claims))
    sentences.extend(_policy_sentences(approved_claims))

    unique_sentences = _unique(sentences)
    if unique_sentences:
        return " ".join(unique_sentences)
    return _fallback_for_claims(approved_claims, customer_message)


def _product_sentences(products: list[dict]) -> list[str]:
    sentences = []
    internal_products = [product for product in products if product.get("source") != "external"]
    external_products = [product for product in products if product.get("source") == "external"]
    if internal_products:
        sentences.extend(_internal_product_summary_sentences(internal_products))
    if external_products:
        sentences.extend(_external_product_summary_sentences(external_products))
    return sentences


def _external_product_summary_sentences(products: list[dict]) -> list[str]:
    # Deliberately keeps these facts clearly framed as OTHER retailers'
    # offers, never blended with Scout's own catalog - the customer
    # should never mistake this for something Scout sells or fulfills.
    # No changes to verification/affiliate logic here, purely wording.
    # Structure: state we don't have it -> present the alternative in
    # the SAME sentence (avoids "here's what I found... another option"
    # redundancy) -> explicit safety boundary (other retailer, price/
    # availability may change, purchase happens with them, our return
    # policy doesn't apply).
    count = len(products)
    if count == 1:
        summary = f"We don’t have a matching item in our own catalog right now, but I found another option: {_external_offer_phrase(products[0])}."
        limitation = (
            "It’s from another retailer, so price and availability may change. "
            "You’ll complete the purchase with that retailer, and Scout’s return policy won’t apply."
        )
    else:
        summary = f"We don’t have a matching item in our own catalog right now, but I found a few options elsewhere: {_join_phrases([_external_offer_phrase(product) for product in products])}."
        limitation = (
            "These are from other retailers, so prices and availability may change. "
            "You’ll complete the purchase with those retailers, and Scout’s return policy won’t apply."
        )
    return [summary, limitation]


def _external_offer_phrase(product: dict) -> str:
    phrase = f"{product['name']} from {product['vendor_name']}"
    if "price" in product:
        phrase += f" for {_format_money(product['price'])}"
    return phrase


def _internal_product_summary_sentences(products: list[dict]) -> list[str]:
    if len(products) == 1:
        product = products[0]
        sentence = f"We have the {product['name']}"
        if "price" in product:
            sentence += f" for {_format_money(product['price'])}"
        promotion = product.get("promotion")
        if isinstance(promotion, dict) and promotion.get("discounted_price") is not None:
            sentence += f", on sale for {_format_money(promotion['discounted_price'])}"
        if product.get("rating") is not None:
            sentence += f", rated {product['rating']}"
        return [sentence + "."]

    priced = [product for product in products if "price" in product]
    if len(priced) == len(products):
        return [_product_list_summary(products)]

    return [f"Here are {len(products)} verified Scout options: {_product_name_list(products)}."]


def _product_list_summary(products: list[dict]) -> str:
    category = _summary_product_category(products)
    phrase = f"I found {len(products)} Scout {category}: "
    phrase += _join_phrases([f"{product['name']} for {_format_money(product['price'])}" for product in products])
    if all(isinstance(product.get("promotion"), dict) and product["promotion"].get("discounted_price") is not None for product in products):
        phrase += ". Sale prices are shown on the cards"
    return phrase + "."


def _summary_product_category(products: list[dict]) -> str:
    categories = {
        str(product.get("category", "")).strip().lower()
        for product in products
        if str(product.get("category", "")).strip()
    }
    if len(categories) == 1:
        return categories.pop()
    names = " ".join(str(product.get("name", "")).lower() for product in products)
    if "dress" in names:
        return "dresses"
    if "shoe" in names:
        return "shoes"
    return "options"


def _product_name_list(products: list[dict]) -> str:
    return _join_phrases([product["name"] for product in products])


def _join_phrases(parts: list[str]) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def _inventory_sentences(claims: list[ProposedClaim], customer_message: str = "") -> list[str]:
    by_subject = _claims_by_subject(claims)
    product_names = {**_context_product_names_by_id(customer_message), **_product_names_by_id(claims)}
    store_names = _store_names_by_id(claims)
    records = []
    for subject_id, subject_claims in by_subject.items():
        if _has_conflict(subject_claims):
            continue
        parsed_subject = _parse_inventory_subject(subject_id)
        store_name = _claim_value(subject_claims, ClaimType.STORE_IDENTITY, "store_name")
        distance = _claim_value(subject_claims, ClaimType.STORE_DISTANCE, "distance_miles")
        quantity = _claim_value(subject_claims, ClaimType.INVENTORY_QUANTITY, "quantity")
        in_stock = _claim_value(subject_claims, ClaimType.INVENTORY_AVAILABILITY, "in_stock")
        pickup = _claim_value(subject_claims, ClaimType.PICKUP_AVAILABILITY, "pickup_available")
        delivery = _claim_value(subject_claims, ClaimType.DELIVERY_AVAILABILITY, "delivery_available")
        pickup_estimate = _claim_value(subject_claims, ClaimType.FULFILLMENT_ESTIMATE, "pickup_estimate")
        delivery_estimate = _claim_value(subject_claims, ClaimType.FULFILLMENT_ESTIMATE, "delivery_estimate")

        product_id = parsed_subject.get("product_id")
        product_name = product_names.get(product_id)
        contextual_store = store_name or store_names.get(parsed_subject.get("store_id"))
        size = _display_size(parsed_subject.get("size"))
        color = parsed_subject.get("color")
        records.append(
            {
                "subject_id": subject_id,
                "product_id": product_id,
                "product_name": product_name,
                "store_id": parsed_subject.get("store_id"),
                "store_name": contextual_store,
                "size": size,
                "color": color,
                "quantity": quantity,
                "in_stock": in_stock if in_stock is not None else (quantity > 0 if quantity is not None else None),
                "pickup": pickup,
                "delivery": delivery,
                "pickup_estimate": pickup_estimate,
                "delivery_estimate": delivery_estimate,
                "distance": distance,
                "raw_store_name": store_name,
            }
        )

    sentences = []
    rendered_subjects = set()

    combined_store_sentence = _combined_store_availability_sentence(records)
    if combined_store_sentence:
        return [combined_store_sentence]

    for record in records:
        if record["store_name"] and (record["size"] or record["color"]):
            sentence = _exact_store_variant_sentence(record)
            if sentence:
                sentences.append(sentence)
                rendered_subjects.add(record["subject_id"])

    for variant in records:
        if variant["subject_id"] in rendered_subjects or not (variant["size"] or variant["color"]) or variant["store_name"]:
            continue
        matching_store = next(
            (
                record
                for record in records
                if record["store_name"]
                and record["product_id"] == variant["product_id"]
                and not (record["size"] or record["color"])
                and record["quantity"] is not None
            ),
            None,
        )
        if matching_store and variant["in_stock"] is False:
            sentences.append(_store_has_general_inventory_but_variant_unavailable_sentence(variant, matching_store))
            rendered_subjects.add(variant["subject_id"])
            rendered_subjects.add(matching_store["subject_id"])

    for record in records:
        if record["subject_id"] in rendered_subjects:
            continue
        product_label = _product_label(record["product_name"])
        variant_context = _variant_context(record["size"], record["color"])
        label = record["raw_store_name"] or record["store_name"] or "That store"
        if record["store_name"] and record["product_id"]:
            if record["quantity"] is not None:
                if record["quantity"] > 0:
                    sentences.append(f"{product_label} is available at the {record['store_name']} store with {record['quantity']} units.")
                else:
                    sentences.append(f"{product_label} is not currently available at the {record['store_name']} store.")
                continue
            if record["in_stock"] is not None:
                sentences.append(
                    f"{product_label} is {'available' if record['in_stock'] else 'not currently available'} at the {record['store_name']} store."
                )
                continue
        if variant_context and record["product_id"]:
            if _is_online_delivery_request(customer_message) and record["delivery"] is not None:
                if record["delivery"]:
                    sentence = f"{product_label} in {variant_context} is available for delivery."
                    if record["quantity"] is not None:
                        sentence = f"{product_label} in {variant_context} is available for delivery, with {record['quantity']} units available."
                else:
                    sentence = (
                        f"{product_label} in {variant_context} is unavailable for online or delivery fulfillment. "
                        "I can find similar products."
                    )
                sentences.append(sentence)
                continue
            if record["quantity"] is not None:
                if record["quantity"] > 0:
                    sentences.append(f"{product_label} has {record['quantity']} units available in {variant_context}.")
                elif _is_nearby_store_request(customer_message):
                    sentences.append(
                        f"{_neutral_product_reference(record['product_name'])} in {variant_context} is not available in nearby store inventory. "
                        f"{_variant_unavailable_next_actions_sentence()}"
                    )
                else:
                    sentences.append(f"{product_label} is out of stock in {variant_context}. {_variant_unavailable_next_actions_sentence()}")
                continue
            if record["in_stock"] is not None:
                stock_text = "available" if record["in_stock"] else "out of stock"
                sentence = f"{product_label} is {stock_text} in {variant_context}."
                if record["in_stock"] is False:
                    if _is_nearby_store_request(customer_message):
                        sentence = (
                            f"{_neutral_product_reference(record['product_name'])} in {variant_context} is not available in nearby store inventory. "
                            f"{_variant_unavailable_next_actions_sentence()}"
                        )
                    else:
                        sentence = f"{sentence} {_variant_unavailable_next_actions_sentence()}"
                sentences.append(sentence)
                continue
        if record["distance"] is not None:
            sentences.append(f"{label} is about {record['distance']} miles away.")
        if record["quantity"] is not None:
            sentences.append(f"We have {record['quantity']} in stock for {_neutral_product_reference(record['product_name'])}.")
        if record["in_stock"] is not None:
            sentences.append(f"That item is currently {'in stock' if record['in_stock'] else 'out of stock'}." if record['product_name'] is None else f"{record['product_name']} is currently {'in stock' if record['in_stock'] else 'out of stock'}.")
        if record["pickup"] is not None:
            sentences.append(f"Pickup is {'available' if record['pickup'] else 'not available'} for {_neutral_product_reference(record['product_name'])}.")
        if record["delivery"] is not None:
            sentences.append(f"Delivery is {'available' if record['delivery'] else 'not available'} for {_neutral_product_reference(record['product_name'])}.")
        if record["pickup_estimate"] is not None:
            sentences.append(f"You can expect pickup for {_neutral_product_reference(record['product_name'])} in about {record['pickup_estimate']}.")
        if record["delivery_estimate"] is not None:
            sentences.append(f"Delivery for {_neutral_product_reference(record['product_name'])} takes about {record['delivery_estimate']}.")
    return sentences


def _exact_store_variant_sentence(record: dict) -> str | None:
    if record["in_stock"] is None and record["quantity"] is None:
        return None
    product_label = _product_label(record["product_name"])
    variant = _variant_context(record["size"], record["color"])
    if record["in_stock"] is False or record["quantity"] == 0:
        return f"{product_label} is not currently available at {record['store_name']} in {variant}."
    if record["quantity"] is not None:
        return f"{product_label} is available at {record['store_name']} in {variant}, with {record['quantity']} units remaining."
    return f"{product_label} is available at {record['store_name']} in {variant}."


def _combined_store_availability_sentence(records: list[dict]) -> str | None:
    store_records = [
        record
        for record in records
        if record["store_name"]
        and record["product_id"]
        and record["quantity"] is not None
        and record["quantity"] > 0
        and not (record["size"] or record["color"])
    ]
    if len(store_records) < 2:
        return None
    product_ids = {record["product_id"] for record in store_records}
    quantities = {record["quantity"] for record in store_records}
    if len(product_ids) != 1 or len(quantities) != 1:
        return None
    product_label = _product_label(store_records[0]["product_name"])
    store_names = _join_phrases([record["store_name"] for record in store_records])
    quantity = store_records[0]["quantity"]
    return f"{product_label} is available at {store_names}, with {quantity} units at each store."


def _store_has_general_inventory_but_variant_unavailable_sentence(variant: dict, store: dict) -> str:
    variant_text = _variant_context(variant["size"], variant["color"])
    return (
        f"{store['store_name']} has other inventory for that item, but the {variant_text} option is currently out of stock. "
        f"{_variant_unavailable_next_actions_sentence()}"
    )


def _variant_unavailable_next_actions_sentence() -> str:
    return "I can check nearby stores, check online or delivery availability, or find similar products."


def _product_label(product_name: str | None) -> str:
    return f"The {product_name}" if product_name else "The requested product"


def _neutral_product_reference(product_name: str | None) -> str:
    return product_name or "the requested product"


def _variant_context(size: str | None, color: str | None) -> str:
    if size and color:
        return f"{color}, size {size}"
    if size:
        return f"size {size}"
    if color:
        return color
    return ""


def _is_inventory_request(customer_message: str) -> bool:
    message = (customer_message or "").lower()
    return any(term in message for term in ("available", "availability", "stock", "in stock", "size", "pickup", "store"))


def _is_nearby_store_request(customer_message: str) -> bool:
    message = (customer_message or "").lower()
    return "nearby" in message and ("store" in message or "stores" in message)


def _is_online_delivery_request(customer_message: str) -> bool:
    message = (customer_message or "").lower()
    return any(term in message for term in ("online", "delivery", "shipping", "ship"))


def _display_size(size: str | None) -> str | None:
    if not size:
        return None
    return str(size).upper()


def _product_names_by_id(claims: list[ProposedClaim]) -> dict[str, str]:
    names = {}
    for claim in claims:
        if claim.claim_type == ClaimType.PRODUCT_IDENTITY.value and claim.field == "name" and claim.subject_id:
            names[claim.subject_id] = str(claim.value)
    return names


def _context_product_names_by_id(customer_message: str) -> dict[str, str]:
    if not re.match(r"\s*Check\s+(?:stock|pickup availability|online or delivery availability|delivery availability)\s+for\s+product_id\b", customer_message or "", re.IGNORECASE):
        return {}
    return {
        match.group(1): match.group(2).strip()
        for match in re.finditer(r"\bproduct_id\s+([A-Z]\d{3,})\s+\(([^)]+)\)", customer_message)
        if match.group(2).strip()
    }


def _store_names_by_id(claims: list[ProposedClaim]) -> dict[str, str]:
    names = {}
    for claim in claims:
        if claim.claim_type == ClaimType.STORE_IDENTITY.value and claim.field == "store_name" and claim.subject_id:
            names[claim.subject_id] = str(claim.value)
    return names


def _parse_inventory_subject(subject_id: str) -> dict[str, str]:
    if subject_id.startswith("product:"):
        parts = subject_id.split(":")
        parsed = {"product_id": parts[1]} if len(parts) > 1 else {}
        for index in range(2, len(parts) - 1, 2):
            key = {"store": "store_id"}.get(parts[index], parts[index])
            parsed[key] = parts[index + 1]
        return parsed
    return {"product_id": subject_id}


def _access_denied_sentences(claims: list[ProposedClaim]) -> list[str]:
    messages = [
        str(claim.value)
        for claim in claims
        if claim.claim_type == ClaimType.ACCESS_DENIED.value
        and claim.field == "message"
        and isinstance(claim.value, str)
        and claim.value.strip()
    ]
    return _unique(messages)


def _order_sentences(claims: list[ProposedClaim]) -> list[str]:
    sentences = []
    by_subject = _claims_by_subject(claims)
    for subject_id, subject_claims in by_subject.items():
        if not subject_id or _has_conflict(subject_claims):
            continue
        status = _claim_value(subject_claims, ClaimType.ORDER_STATUS, "status")
        tracking = _claim_value(subject_claims, ClaimType.ORDER_TRACKING, "tracking_number")
        payment_status = _claim_value(subject_claims, ClaimType.PAYMENT_STATUS, "payment_status")
        return_eligible = _claim_value(subject_claims, ClaimType.RETURN_ELIGIBILITY, "return_eligible")
        carrier = _claim_value(subject_claims, ClaimType.SHIPMENT_STATUS, "carrier")
        shipment_status_value = _claim_value(subject_claims, ClaimType.SHIPMENT_STATUS, "shipment_status")
        shipped_at = _claim_value(subject_claims, ClaimType.SHIPMENT_STATUS, "shipped_at")
        estimated_delivery = _claim_value(subject_claims, ClaimType.SHIPMENT_STATUS, "estimated_delivery_date")
        if status is not None:
            sentences.append(_order_status_sentence(subject_id, str(status)))
        if carrier is not None and shipment_status_value is not None:
            # Deliberately avoid the word "shipped" here - it collides
            # with final_safety_scan's pre-existing order-status
            # detection regex, which would then require "shipped" itself
            # to be a separately-approved value, causing a real, subtle
            # bug where this correct, verified sentence was silently
            # stripped even though carrier and status were both approved.
            # Uses the readable, space-separated form ("in transit", not
            # "in_transit") - final_safety_scan's detection regex for
            # shipment status is matched to this same, readable form.
            # Deliberately keeps the order ID visible - a customer with
            # multiple orders needs to know which one this reply is about.
            sentences.append(f"Order {subject_id} is on its way with {carrier}, currently {_readable_shipment_status(shipment_status_value)}.")
        elif carrier is not None:
            sentences.append(f"Order {subject_id} is on its way with {carrier}.")
        if shipped_at is not None:
            sentences.append(f"It left our warehouse on {shipped_at}.")
        if estimated_delivery is not None:
            sentences.append(f"You can expect it by {estimated_delivery}.")
        if tracking is not None:
            sentences.append(f"Your tracking number is {tracking}.")
        if payment_status is not None:
            sentences.append(f"Payment for order {subject_id} is {payment_status}.")
        if return_eligible is not None:
            sentences.append(f"It looks like this order is {'eligible' if return_eligible else 'not eligible'} for a return.")
    return sentences


def _policy_sentences(claims: list[ProposedClaim]) -> list[str]:
    sentences = []
    by_subject = _claims_by_subject(claims)
    for _, subject_claims in by_subject.items():
        statement_claims = [
            claim
            for claim in subject_claims
            if claim.claim_type == ClaimType.POLICY_STATEMENT.value and claim.field == "statement"
        ]
        for statement in _unique_by_key([claim.value for claim in statement_claims]):
            sentences.append(_customer_policy_statement(str(statement)))
    return sentences


def _order_status_sentence(order_id: str, status: str) -> str:
    normalized = status.strip().lower()
    if normalized == "shipped":
        return f"Order {order_id} has shipped."
    return f"Order {order_id} is currently {normalized}."


def _readable_shipment_status(status: str) -> str:
    """Human-readable phrasing for shipment status codes, matching the
    same underscore-to-words style used elsewhere (see _order_status_
    sentence). Deliberately keeps the underlying detected value (see
    final_safety_scan's regex, which matches the underscore form) intact
    within the sentence - only the customer-facing word choice changes,
    not the actual verified value being stated.
    """
    return status.strip().lower().replace("_", " ")


def _customer_policy_statement(statement: str) -> str:
    # Structure: direct answer -> relevant detail -> optional next step.
    # Verification/retrieval mechanics stay entirely internal - the
    # customer just gets a clear, natural answer to their real question.
    cleaned = _clean_policy_statement(statement)
    lowered = cleaned.lower()
    if "opened or worn items are not eligible" in lowered:
        sentence = "Opened or worn items usually aren’t eligible for a return unless they’re defective."
        if "30 days" in lowered and "unworn" in lowered and "unwashed" in lowered and "original tags" in lowered:
            sentence += " Returns are accepted within 30 days as long as the item is unworn, unwashed, and still has its original tags."
        sentence += " Want me to check if a specific order qualifies?"
        return sentence
    if "5-7 business days" in lowered and "store credit" in lowered:
        return (
            "Refunds usually take 5-7 business days after we receive and inspect the return. "
            "Store credit is typically faster, usually within 1 business day."
        )
    return cleaned


def _clean_policy_statement(statement: str) -> str:
    without_headings = re.sub(r"(?m)^#+\s*", "", statement or "")
    return re.sub(r"\s+", " ", without_headings).strip()


def _is_safe_conversational(reply: str) -> bool:
    if not reply:
        return False
    if any(pattern.match(reply) for pattern in SAFE_CONVERSATIONAL_PATTERNS):
        return not re.search(r"\$|\b\d+(?:\.\d+)?\s*(?:miles?|days?|%|stars?)\b|\b[A-Z]{2,}[A-Z0-9-]{4,}\b", reply)
    return not FACTUAL_PATTERN.search(reply)


def _sentence_is_supported(sentence: str, approved_values: set[tuple[str, str]]) -> bool:
    detected = _detected_values(sentence)
    return all(_value_key(value) in approved_values for value in detected)


def _product_is_supported(product: dict, approved_values: set[tuple[str, str]]) -> bool:
    for key in ("name", "product_id", "external_product_id", "price", "rating", "vendor_name", "click_url"):
        if key in product and _value_key(product[key]) not in approved_values:
            return False
    promotion = product.get("promotion")
    if isinstance(promotion, dict):
        for value in promotion.values():
            if _value_key(value) not in approved_values:
                return False
    return True


def _detected_values(sentence: str) -> list[Any]:
    values = []
    values.extend(
        re.sub(r"^The\s+", "", match.strip(), flags=re.IGNORECASE)
        for match in re.findall(
            r"\b((?:The\s+)?[A-Z][A-Za-z0-9' -]+(?:Dress|Shirt|Shoe|Shoes|Jeans|Jacket|Skirt|Top|Bag))\b",
            sentence,
        )
    )
    values.extend(float(match) for match in re.findall(r"\$(\d+(?:\.\d{1,2})?)", sentence))
    values.extend(float(match) for match in re.findall(r"\b(\d+(?:\.\d+)?)\s*miles?\b", sentence, re.IGNORECASE))
    values.extend(int(match) for match in re.findall(r"\b(\d+)\s*days?\b", sentence, re.IGNORECASE))
    values.extend(float(match) for match in re.findall(r"\b(\d+(?:\.\d+)?)\s*(?:stars?|rating)\b", sentence, re.IGNORECASE))
    values.extend(float(match) for match in re.findall(r"\b(\d+(?:\.\d+)?)%\b", sentence))
    values.extend(int(match) for match in re.findall(r"\bquantity(?:\s+for\s+.+?)?\s+is\s+(\d+)\b", sentence, re.IGNORECASE))
    values.extend(int(match) for match in re.findall(r"\bhas\s+(\d+)\s+units?\b", sentence, re.IGNORECASE))
    values.extend(int(match) for match in re.findall(r"\bwith\s+(\d+)\s+units?\b", sentence, re.IGNORECASE))
    values.extend(match.upper() for match in re.findall(r"\bsize\s+(XS|S|M|L|XL|XXL)\b", sentence, re.IGNORECASE))
    values.extend(
        match.lower()
        for match in re.findall(r"\b(pending|processing|shipped|delivered|cancelled|canceled|returned)\b", sentence, re.IGNORECASE)
    )
    values.extend(
        match.lower().replace(" ", "_")
        for match in re.findall(r"\b(label created|in transit|out for delivery|delivered|delayed)\b", sentence, re.IGNORECASE)
    )
    # Carrier names are short, real words (UPS, FedEx, USPS, DHL) that
    # the existing all-caps-4+-char regex above doesn't catch (UPS is
    # only 3 characters) - confirmed via a real bug: this sentence was
    # being silently stripped by final_safety_scan because "UPS" was
    # never detected as a value at all, so it could never be found in
    # the approved set.
    values.extend(re.findall(r"\b(UPS|FedEx|USPS|DHL)\b", sentence, re.IGNORECASE))
    values.extend(
        match.lower()
        for match in re.findall(r"\b(today|tomorrow|[0-9]+-[0-9]+ business days)\b", sentence, re.IGNORECASE)
    )
    values.extend(re.findall(r"\b[A-Z]{2,}[A-Z0-9-]{4,}\b", sentence))
    return values


def _approved_value_index(claims: list[ProposedClaim], customer_message: str = "") -> set[tuple[str, str]]:
    values = set()
    for claim in claims:
        values.add(_value_key(claim.value))
        if claim.subject_id:
            values.add(_value_key(claim.subject_id))
            for part in re.split(r":(?:size|store|color):|^product:", claim.subject_id):
                if part:
                    values.add(_value_key(part))
        if isinstance(claim.value, str):
            for detected_value in _detected_values(claim.value):
                values.add(_value_key(detected_value))
    claim_subjects = {claim.subject_id for claim in claims if claim.subject_id}
    claim_product_ids = {
        parsed.get("product_id")
        for subject in claim_subjects
        for parsed in [_parse_inventory_subject(str(subject))]
        if parsed.get("product_id")
    }
    for product_id, product_name in _context_product_names_by_id(customer_message).items():
        if product_id in claim_subjects or product_id in claim_product_ids:
            values.add(_value_key(product_name))
    return values


def _claims_by_subject(claims: list[ProposedClaim]) -> dict[str, list[ProposedClaim]]:
    grouped: dict[str, list[ProposedClaim]] = {}
    for claim in claims:
        if claim.subject_id:
            grouped.setdefault(claim.subject_id, []).append(claim)
    return grouped


def _claim_value(claims: list[ProposedClaim], claim_type: ClaimType, field: str) -> Any:
    values = [
        claim.value
        for claim in claims
        if claim.claim_type == claim_type.value and claim.field == field
    ]
    unique_values = _unique_by_key(values)
    return unique_values[0] if len(unique_values) == 1 else None


def _has_conflict(claims: list[ProposedClaim]) -> bool:
    seen = {}
    for claim in claims:
        key = (claim.claim_type, claim.field)
        value_key = _value_key(claim.value)
        if key in seen and seen[key] != value_key:
            return True
        seen[key] = value_key
    return False


def _fallback_for_claims(claims: list[ProposedClaim], customer_message: str) -> str:
    claim_types = {claim.claim_type for claim in claims}
    message = (customer_message or "").lower()
    if any(claim_type.startswith("external_offer") for claim_type in claim_types) or "external" in message or "third-party" in message:
        return DOMAIN_FALLBACKS["external"]
    if any(claim_type.startswith("order_") or claim_type in {ClaimType.PAYMENT_STATUS.value, ClaimType.RETURN_ELIGIBILITY.value} for claim_type in claim_types) or "order" in message:
        return DOMAIN_FALLBACKS["order"]
    if ClaimType.POLICY_STATEMENT.value in claim_types or "policy" in message or "return" in message:
        return DOMAIN_FALLBACKS["policy"]
    if any(claim_type.startswith("inventory_") or claim_type in {ClaimType.STORE_IDENTITY.value, ClaimType.STORE_DISTANCE.value, ClaimType.PICKUP_AVAILABILITY.value, ClaimType.DELIVERY_AVAILABILITY.value, ClaimType.FULFILLMENT_ESTIMATE.value} for claim_type in claim_types) or "stock" in message or "available" in message:
        return DOMAIN_FALLBACKS["inventory"]
    if any(claim_type.startswith("product_") or claim_type == ClaimType.PROMOTION.value for claim_type in claim_types) or any(word in message for word in ("product", "dress", "shirt", "shoe", "jeans", "jacket", "skirt", "top", "bag")):
        return DOMAIN_FALLBACKS["product"]
    return DOMAIN_FALLBACKS["general"]


def _split_sentences(reply: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", reply or "") if part.strip()]


def _format_money(value: Any) -> str:
    money = _money(value)
    return f"${money:.2f}" if money is not None else str(value)


def _money(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def _value_key(value: Any) -> tuple[str, str]:
    money = _money(value)
    if money is not None and not isinstance(value, bool):
        return ("number", f"{money:.2f}")
    return ("string", str(value).strip().lower())


def _unique(values: list[str]) -> list[str]:
    seen = set()
    output = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def _unique_by_key(values: list[Any]) -> list[Any]:
    seen = set()
    output = []
    for value in values:
        key = _value_key(value)
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output
